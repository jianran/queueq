"""QueueQ — QR queue for restaurants.
No app, no phone number. Owner prints QR, customer scans, joins queue,
leaves a Google review, gets a free dish. That's it.
"""

import os
import uuid
import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

import io
from fastapi import FastAPI, HTTPException, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import qrcode
import qrcode.image.svg
from PIL import Image, ImageDraw, ImageFont
import uvicorn

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("queueq")


def _get_base_url():
    """Get the base URL for QR codes."""
    env_url = os.getenv("QUEUEQ_URL")
    if env_url:
        return env_url.rstrip("/")
    return "https://1b482b05-e819-4ed8-b659-1d3fa0d5f106-00-rvafaujaazq9.pike.replit.dev"


app = FastAPI(title="QueueQ")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

DATA_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── VAPID Keys ──────────────────────────────────────────────────────────
VAPID_FILE = DATA_DIR / "vapid.json"
VAPID_CLAIMS = None


def _ensure_vapid():
    """Load or generate VAPID keys on first run."""
    global VAPID_CLAIMS
    if VAPID_CLAIMS is not None:
        return VAPID_CLAIMS
    if VAPID_FILE.exists():
        with open(VAPID_FILE) as f:
            data = json.load(f)
        # Migrate old format (PEM private key + X962 public key) to new format
        if "-----BEGIN" in data.get("private_key", ""):
            logger.info("Migrating old VAPID key format")
            from cryptography.hazmat.primitives import serialization
            from base64 import urlsafe_b64encode
            from py_vapid import Vapid
            priv_pem = data["private_key"]
            priv_key = serialization.load_pem_private_key(
                priv_pem.encode(), password=None
            )
            priv_der = priv_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
            priv_der_b64 = urlsafe_b64encode(priv_der).rstrip(b"=").decode()
            spki_bytes = priv_key.public_key().public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )
            spki_b64 = urlsafe_b64encode(spki_bytes).rstrip(b"=").decode()
            data = {
                "public_key": spki_b64,
                "private_key": priv_der_b64,
            }
            VAPID_FILE.write_text(json.dumps(data, indent=2))
    else:
        if webpush is None:
            logger.warning("pywebpush not installed; push notifications disabled")
            VAPID_CLAIMS = None
            return None
        from cryptography.hazmat.primitives import serialization
        from base64 import urlsafe_b64encode
        from py_vapid import Vapid
        v = Vapid()
        v.generate_keys()
        # pywebpush needs DER-encoded private key (base64url for from_string)
        priv_der = v.private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        priv_der_b64 = urlsafe_b64encode(priv_der).rstrip(b"=").decode()
        # Safari/iOS needs SPKI-encoded public key (with algorithm ID)
        spki_bytes = v.public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        spki_b64 = urlsafe_b64encode(spki_bytes).rstrip(b"=").decode()
        data = {
            "public_key": spki_b64,
            "private_key": priv_der_b64,
        }
        VAPID_FILE.write_text(json.dumps(data, indent=2))
        logger.info("Generated new VAPID keys → %s", VAPID_FILE)

    base_url = _get_base_url()
    VAPID_CLAIMS = {
        "vapid_public_key": data["public_key"],
        "vapid_private_key": data["private_key"],
        "subscriber": f"mailto:{base_url}",
    }
    return VAPID_CLAIMS


def send_push(subscription_json: str, title: str, body: str, url: str):
    """Send a Web Push notification. Returns True on success."""
    if webpush is None:
        logger.warning("pywebpush not available, skipping push")
        return False
    info = _ensure_vapid()
    if not info:
        return False
    try:
        sub = json.loads(subscription_json)
        payload = json.dumps({"title": title, "body": body, "url": url})
        logger.info("Sending push to endpoint: %s", sub.get("endpoint", "unknown")[:60])
        response = webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=info["vapid_private_key"],
            vapid_claims={"sub": info["subscriber"]},
        )
        logger.info("Push sent successfully, status: %d", response.status_code)
        return True
    except WebPushException as e:
        # 410 Gone = subscription expired, clean up
        if getattr(e, "response", None) and e.response.status_code in (410, 404):
            logger.info("Push subscription expired (410/404), cleaning up")
            return "expired"
        # Other errors
        err_detail = str(e)
        if getattr(e, "response", None):
            err_detail += f" (status={e.response.status_code})"
        logger.warning("push send failed: %s", err_detail)
        return False
    except Exception as e:
        logger.warning("push send error: %s", e)
        return False


def get_db():
    conn = sqlite3.connect(str(DATA_DIR / "queueq.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS restaurants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            passcode TEXT NOT NULL,
            ticket_counter INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS queue_entries (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT NOT NULL,
            ticket_number INTEGER NOT NULL,
            party_size INTEGER DEFAULT 1,
            status TEXT DEFAULT 'waiting',
            created_at TEXT NOT NULL,
            review_opened INTEGER DEFAULT 0,
            review_confirmed INTEGER DEFAULT 0,
            called_at TEXT,
            seated_at TEXT,
            push_subscription TEXT DEFAULT NULL,
            FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
        );
        CREATE INDEX IF NOT EXISTS idx_queue_rid ON queue_entries(restaurant_id, status);
    """)
    conn.commit()
    conn.close()


init_db()
_ensure_vapid()  # warm up VAPID keys


def render(template: str, **kwargs) -> str:
    path = TEMPLATES_DIR / template
    html = path.read_text(encoding="utf-8")
    for k, v in kwargs.items():
        html = html.replace("{{ " + k + " }}", str(v) if v is not None else "")
    return html


# --- Web pages ---

@app.get("/", response_class=HTMLResponse)
async def landing():
    return HTMLResponse(render("index.html"))


@app.get("/queue/{restaurant_id}", response_class=HTMLResponse)
async def queue_page(restaurant_id: str):
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    conn.close()
    if not rest:
        raise HTTPException(404, "Restaurant not found")
    return HTMLResponse(render("queue.html", restaurant_id=restaurant_id, restaurant_name=rest["name"]))


@app.get("/queue/{restaurant_id}/manifest.json")
async def queue_manifest(restaurant_id: str):
    """Dynamic PWA manifest so Home Screen opens the queue page, not the landing page."""
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    conn.close()
    if not rest:
        raise HTTPException(404)
    base_url = _get_base_url()
    manifest = {
        "name": f"QueueQ — {rest['name']}",
        "short_name": rest['name'],
        "description": f"Check wait time and join the queue at {rest['name']}.",
        "start_url": f"/queue/{restaurant_id}",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#161b22",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/badge-72.png", "sizes": "72x72", "type": "image/png"},
        ]
    }
    return JSONResponse(manifest)


@app.get("/status/{entry_id}", response_class=HTMLResponse)
async def status_page(entry_id: str):
    conn = get_db()
    entry = conn.execute("SELECT * FROM queue_entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    if not entry:
        raise HTTPException(404, "Entry not found")
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (entry["restaurant_id"],)).fetchone()
    waiting = conn.execute(
        "SELECT COUNT(*) as c FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting' AND ticket_number < ?",
        (entry["restaurant_id"], entry["ticket_number"]),
    ).fetchone()["c"]
    conn.close()
    return HTMLResponse(render(
        "status.html",
        entry_id=entry_id,
        restaurant_id=entry["restaurant_id"],
        restaurant_name=rest["name"],
        ticket_number=entry["ticket_number"],
        party_size=entry["party_size"],
        status=entry["status"],
        position=waiting + 1,
        review_opened=entry["review_opened"],
        review_confirmed=entry["review_confirmed"],
    ))


@app.get("/admin/{restaurant_id}", response_class=HTMLResponse)
async def admin_page(restaurant_id: str):
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    conn.close()
    if not rest:
        raise HTTPException(404, "Restaurant not found")
    return HTMLResponse(render(
        "admin.html",
        restaurant_id=restaurant_id,
        restaurant_name=rest["name"],
        passcode=rest["passcode"],
    ))


# --- API ---

@app.post("/api/restaurant/create")
async def create_restaurant(name: str = Form(...), passcode: str = Form(...)):
    rid = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute("INSERT INTO restaurants (id, name, passcode, created_at) VALUES (?, ?, ?, ?)",
                 (rid, name, passcode, now))
    conn.commit()
    conn.close()
    base_url = _get_base_url()
    queue_url = f"{base_url}/queue/{rid}"
    qr_svg = qrcode.make(queue_url, image_factory=qrcode.image.svg.SvgPathImage).to_string().decode()
    return JSONResponse({"restaurant_id": rid, "queue_url": queue_url, "qr_svg": qr_svg})


@app.post("/api/restaurant/reset-counter")
async def reset_counter(restaurant_id: str = Form(...), passcode: str = Form("")):
    """Reset ticket counter back to 0 (new day). Does not delete queue entries."""
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest or rest["passcode"] != passcode:
        conn.close()
        raise HTTPException(403, "Wrong passcode")
    conn.execute("UPDATE restaurants SET ticket_counter = 0 WHERE id = ?", (restaurant_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok", "counter": 0})


@app.post("/api/queue/join")
async def join_queue(restaurant_id: str = Form(...), party_size: int = Form(1), client_token: str = Form("")):
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest:
        conn.close()
        raise HTTPException(404, "Restaurant not found")

    # Dedup: same client_token can't have an active entry
    if client_token:
        dup = conn.execute(
            "SELECT id FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting' AND id LIKE ?",
            (restaurant_id, f"{client_token}%"),
        ).fetchone()
        if dup:
            conn.close()
            return JSONResponse({"error": "already_in_queue", "entry_id": dup["id"]})

    entry_id = f"{client_token or uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE restaurants SET ticket_counter = ticket_counter + 1 WHERE id = ?", (restaurant_id,))
    ticket = conn.execute("SELECT ticket_counter FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO queue_entries (id, restaurant_id, ticket_number, party_size, status, created_at) "
        "VALUES (?, ?, ?, ?, 'waiting', ?)",
        (entry_id, restaurant_id, ticket, party_size, now),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"entry_id": entry_id, "ticket_number": ticket, "status_url": f"/status/{entry_id}"})


@app.post("/api/queue/call")
async def call_next(restaurant_id: str = Form(...), passcode: str = Form(""), party_size: int = Form(0)):
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest or rest["passcode"] != passcode:
        conn.close()
        raise HTTPException(403, "Wrong passcode")

    # Pick longest-waiting entry — filter by table size if specified
    # Priority: exact size match first, then smaller parties
    if party_size > 0:
        # Try exact size match first (longest-waiting)
        entry = conn.execute(
            "SELECT * FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting' AND party_size = ? ORDER BY ticket_number ASC LIMIT 1",
            (restaurant_id, party_size),
        ).fetchone()
        # Fall back to smaller parties that fit
        if not entry:
            entry = conn.execute(
                "SELECT * FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting' AND party_size < ? ORDER BY party_size DESC, ticket_number ASC LIMIT 1",
                (restaurant_id, party_size),
            ).fetchone()
    else:
        entry = conn.execute(
            "SELECT * FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting' ORDER BY ticket_number ASC LIMIT 1",
            (restaurant_id,),
        ).fetchone()

    if not entry:
        conn.close()
        return JSONResponse({"error": "empty_queue"})

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE queue_entries SET status = 'called', called_at = ? WHERE id = ?",
                 (now, entry["id"]))
    conn.commit()

    # ── Send push notification if the customer subscribed ────────────────
    sub_json = entry["push_subscription"]
    if sub_json:
        base_url = _get_base_url()
        status_url = f"{base_url}/status/{entry['id']}"
        rest_name = rest["name"]
        logger.info("Sending push to entry %s, ticket #%d", entry["id"], entry["ticket_number"])
        result = send_push(
            sub_json,
            title="🪑 Table Ready!",
            body=f"Your table at {rest_name} is ready! Party of {entry['party_size']}. Please return.",
            url=status_url,
        )
        if result == "expired":
            conn.execute("UPDATE queue_entries SET push_subscription = NULL WHERE id = ?",
                         (entry["id"],))
            conn.commit()
            logger.info("Cleaned up expired push sub for entry %s", entry["id"])

    conn.close()
    return JSONResponse({
        "entry_id": entry["id"],
        "ticket_number": entry["ticket_number"],
        "party_size": entry["party_size"],
        "review_confirmed": entry["review_confirmed"],
        "review_opened": entry["review_opened"],
    })


@app.post("/api/queue/seat")
async def seat_customer(entry_id: str = Form(...), restaurant_id: str = Form(...), passcode: str = Form("")):
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest or rest["passcode"] != passcode:
        conn.close()
        raise HTTPException(403, "Wrong passcode")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE queue_entries SET status = 'seated', seated_at = ? WHERE id = ? AND restaurant_id = ?",
                 (now, entry_id, restaurant_id))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "seated"})


@app.post("/api/push/subscribe")
async def subscribe_push(entry_id: str = Form(...), subscription: str = Form(...)):
    """Store a Web Push subscription associated with a queue entry."""
    conn = get_db()
    entry = conn.execute("SELECT * FROM queue_entries WHERE id = ?", (entry_id,)).fetchone()
    if not entry:
        conn.close()
        raise HTTPException(404, "Entry not found")
    conn.execute("UPDATE queue_entries SET push_subscription = ? WHERE id = ?",
                 (subscription, entry_id))
    conn.commit()
    conn.close()
    # Log first 50 chars of endpoint for debugging
    try:
        ep = json.loads(subscription).get("endpoint", "?")[:60]
    except:
        ep = "?"
    logger.info("Push subscription stored for entry %s → %s", entry_id[:20], ep)
    return JSONResponse({"status": "ok"})


@app.post("/api/push/unsubscribe")
async def unsubscribe_push(entry_id: str = Form(...)):
    """Remove push subscription from an entry."""
    conn = get_db()
    conn.execute("UPDATE queue_entries SET push_subscription = NULL WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok"})


@app.get("/api/vapid-public-key")
async def get_vapid_public_key():
    info = _ensure_vapid()
    if not info:
        raise HTTPException(500, "VAPID not configured")
    return JSONResponse({"public_key": info["vapid_public_key"]})


@app.post("/api/queue/leave")
async def leave_queue(entry_id: str = Form(...)):
    """Cancel a waiting queue entry — customer leaves the queue."""
    conn = get_db()
    entry = conn.execute("SELECT * FROM queue_entries WHERE id = ?", (entry_id,)).fetchone()
    if not entry:
        conn.close()
        raise HTTPException(404, "Entry not found")
    if entry["status"] not in ("waiting", "called"):
        conn.close()
        return JSONResponse({"error": "cannot_leave", "status": entry["status"]})
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE queue_entries SET status = 'cancelled', called_at = ? WHERE id = ?",
                 (now, entry_id))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "cancelled"})


@app.post("/api/queue/review-opened")
async def mark_review_opened(entry_id: str = Form(...)):
    conn = get_db()
    conn.execute("UPDATE queue_entries SET review_opened = 1 WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok"})


@app.get("/api/queue/{restaurant_id}/status")
async def queue_status(restaurant_id: str):
    conn = get_db()
    waiting = conn.execute(
        "SELECT id, ticket_number, party_size, review_opened, review_confirmed, "
        "strftime('%s','now') - strftime('%s', created_at) as wait_seconds "
        "FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting' ORDER BY ticket_number ASC",
        (restaurant_id,),
    ).fetchall()
    counts = conn.execute(
        "SELECT status, COUNT(*) as c FROM queue_entries WHERE restaurant_id = ? GROUP BY status",
        (restaurant_id,),
    ).fetchall()
    conn.close()
    stats = {r["status"]: r["c"] for r in counts}
    return JSONResponse({"waiting": [dict(r) for r in waiting], "stats": stats})


@app.get("/api/queue/entry/{entry_id}")
async def get_entry(entry_id: str):
    conn = get_db()
    entry = conn.execute("SELECT * FROM queue_entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    if not entry:
        raise HTTPException(404)
    return JSONResponse({
        "entry_id": entry["id"],
        "ticket_number": entry["ticket_number"],
        "party_size": entry["party_size"],
        "status": entry["status"],
        "review_opened": entry["review_opened"],
        "review_confirmed": entry["review_confirmed"],
    })


@app.post("/api/poster/{restaurant_id}")
async def generate_poster(restaurant_id: str, photo: UploadFile = File(...)):
    """Generate poster: store photo + QR + 'Review to get free menu' text."""
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    conn.close()
    if not rest:
        raise HTTPException(404)

    base_url = _get_base_url()
    queue_url = f"{base_url}/queue/{restaurant_id}"

    photo_bytes = await photo.read()
    store_img = Image.open(io.BytesIO(photo_bytes))

    # Square crop + resize to 600x600
    s = min(store_img.width, store_img.height, 600)
    cx, cy = store_img.width // 2, store_img.height // 2
    store_img = store_img.crop((cx - s//2, cy - s//2, cx + s//2, cy + s//2)).resize((600, 600), Image.LANCZOS)

    # Generate QR
    qr = qrcode.QRCode(border=1, box_size=6)
    qr.add_data(queue_url)
    qr.make(fit=True)
    qr_pil = qr.make_image(fill_color="black", back_color="white").convert("RGBA").resize((120, 120), Image.LANCZOS)

    # Composite
    poster = store_img.convert("RGBA")
    draw = ImageDraw.Draw(poster)

    # Subtle vignette — single radial gradient overlay
    vignette = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vignette)
    cx, cy = 300, 300
    for r in range(300, 0, -5):
        alpha = int(35 * (1 - r / 300))
        vdraw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(0, 0, 0, alpha))
    poster = Image.alpha_composite(poster, vignette)
    draw = ImageDraw.Draw(poster)

    # QR at bottom-right
    qx, qy = 600 - 120 - 14, 600 - 120 - 14
    poster.paste(qr_pil, (qx, qy), qr_pil)

    # Text "Review to get free menu!"
    text = "✨ Review to get free menu!"
    sub = "Scan QR to join the queue"

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
    except:
        font = ImageFont.load_default()

    # Main text
    tb = draw.textbbox((0, 0), text, font=font)
    tx = (600 - (tb[2] - tb[0])) / 2
    ty = qy - 40
    draw.text((tx+1, ty+1), text, fill=(0,0,0,180), font=font)
    draw.text((tx, ty), text, fill=(255,255,255,230), font=font)

    # Sub text
    sb = draw.textbbox((0, 0), sub, font=font)
    sx = (600 - (sb[2] - sb[0])) / 2
    sy = ty + 30
    draw.text((sx+1, sy+1), sub, fill=(0,0,0,150), font=font)
    draw.text((sx, sy), sub, fill=(255,255,255,160), font=font)

    buf = io.BytesIO()
    poster.convert("RGB").save(buf, "PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting HTTP on port %d", port)
    uvicorn.run("main:app", host="0.0.0.0", port=port)
