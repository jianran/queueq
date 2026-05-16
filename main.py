"""QueueQ — QR queue manager for restaurants.
No app, no phone number. Just scan, see your place, get notified when it's near your turn.
"""

import os
import uuid
import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Query, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import qrcode
import qrcode.image.svg
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("queueq")

app = FastAPI(title="QueueQ")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

DATA_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Database ---
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
            current_number INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS queue_entries (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT NOT NULL,
            ticket_number INTEGER NOT NULL,
            party_size INTEGER DEFAULT 1,
            status TEXT DEFAULT 'waiting',
            created_at TEXT NOT NULL,
            push_subscription TEXT,
            notified_near INTEGER DEFAULT 0,
            FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
        );

        CREATE INDEX IF NOT EXISTS idx_queue_restaurant ON queue_entries(restaurant_id, status);
    """)
    conn.commit()
    conn.close()

init_db()


# --- Helpers ---
def render_html(template_name: str, **kwargs) -> str:
    path = TEMPLATES_DIR / template_name
    if not path.exists():
        raise HTTPException(404, f"Template {template_name} not found")
    html = path.read_text(encoding="utf-8")
    for key, val in kwargs.items():
        html = html.replace("{{ " + key + " }}", str(val) if val is not None else "")
    return html


def generate_qr_svg(data_url: str) -> str:
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(data_url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    return img.to_string().decode("utf-8")


def get_queue_stats(restaurant_id: str) -> dict:
    conn = get_db()
    waiting = conn.execute(
        "SELECT COUNT(*) as count FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting'",
        (restaurant_id,),
    ).fetchone()["count"]

    seated_today = conn.execute(
        "SELECT COUNT(*) as count FROM queue_entries WHERE restaurant_id = ? AND status = 'seated' AND date(created_at) = date('now')",
        (restaurant_id,),
    ).fetchone()["count"]

    conn.close()
    return {"waiting": waiting, "seated_today": seated_today}


# SSE event manager — simple in-memory
sse_clients = {}  # restaurant_id -> list of (entry_id, queue)


def notify_sse(restaurant_id: str, entry_id: str = None):
    """Push update to SSE clients for this restaurant."""
    if restaurant_id not in sse_clients:
        return
    msg = json.dumps({"type": "update", "entry_id": entry_id})
    dead = []
    for client_entry_id, queue in sse_clients[restaurant_id]:
        if entry_id and client_entry_id != entry_id:
            continue
        try:
            queue.put_nowait(msg)
        except:
            dead.append((client_entry_id, queue))
    for d in dead:
        try:
            sse_clients[restaurant_id].remove(d)
        except:
            pass


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index():
    """Landing page with QR scanner simulation and restaurant creation."""
    html = render_html("index.html", restaurant_id="", qr_svg="")
    return HTMLResponse(html)


@app.post("/api/restaurant/create")
async def create_restaurant(name: str = Form(...), passcode: str = Form(...)):
    """Create a new restaurant."""
    rid = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO restaurants (id, name, passcode, created_at) VALUES (?, ?, ?, ?)",
        (rid, name, passcode, now),
    )
    conn.commit()
    conn.close()

    queue_url = f"/queue/{rid}"
    qr_svg = generate_qr_svg(queue_url)

    return JSONResponse({
        "restaurant_id": rid,
        "queue_url": queue_url,
        "qr_svg": qr_svg,
    })


@app.get("/admin/{restaurant_id}", response_class=HTMLResponse)
async def admin_page(restaurant_id: str, passcode: str = ""):
    """Admin dashboard for managing the queue."""
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest:
        raise HTTPException(404, "Restaurant not found")

    entries = conn.execute(
        "SELECT * FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting' ORDER BY ticket_number ASC",
        (restaurant_id,),
    ).fetchall()

    seated = conn.execute(
        "SELECT * FROM queue_entries WHERE restaurant_id = ? AND status = 'seated' ORDER BY created_at DESC LIMIT 10",
        (restaurant_id,),
    ).fetchall()

    stats = get_queue_stats(restaurant_id)
    conn.close()

    entries_json = json.dumps([dict(e) for e in entries])
    seated_json = json.dumps([dict(s) for s in seated])
    queue_url = f"/queue/{restaurant_id}"

    html = render_html(
        "admin.html",
        restaurant_id=restaurant_id,
        restaurant_name=rest["name"],
        passcode=rest["passcode"],
        entries_json=entries_json,
        seated_json=seated_json,
        stats_json=json.dumps(stats),
        queue_url=queue_url,
    )
    return HTMLResponse(html)


@app.get("/queue/{restaurant_id}", response_class=HTMLResponse)
async def queue_page(restaurant_id: str):
    """Customer-facing queue page — shows current wait + option to join."""
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest:
        raise HTTPException(404, "Restaurant not found")

    stats = get_queue_stats(restaurant_id)
    conn.close()

    html = render_html(
        "queue.html",
        restaurant_id=restaurant_id,
        restaurant_name=rest["name"],
        stats_json=json.dumps(stats),
    )
    return HTMLResponse(html)


@app.get("/status/{entry_id}", response_class=HTMLResponse)
async def status_page(entry_id: str):
    """Live status page for a customer — auto-updates via SSE."""
    conn = get_db()
    entry = conn.execute("SELECT * FROM queue_entries WHERE id = ?", (entry_id,)).fetchone()
    if not entry:
        raise HTTPException(404, "Entry not found")

    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (entry["restaurant_id"],)).fetchone()
    conn.close()

    # Calculate position
    conn = get_db()
    ahead = conn.execute(
        "SELECT COUNT(*) as count FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting' AND ticket_number < ?",
        (entry["restaurant_id"], entry["ticket_number"]),
    ).fetchone()["count"]

    total_waiting = conn.execute(
        "SELECT COUNT(*) as count FROM queue_entries WHERE restaurant_id = ? AND status = 'waiting'",
        (entry["restaurant_id"],),
    ).fetchone()["count"]
    conn.close()

    html = render_html(
        "status.html",
        entry_id=entry_id,
        restaurant_id=entry["restaurant_id"],
        restaurant_name=rest["name"],
        ticket_number=entry["ticket_number"],
        party_size=entry["party_size"],
        status=entry["status"],
        position=ahead + 1,
        total_waiting=total_waiting,
    )
    return HTMLResponse(html)


@app.post("/api/queue/join")
async def join_queue(
    restaurant_id: str = Form(...),
    party_size: int = Form(1),
    push_subscription: str = Form(""),
):
    """Customer joins the queue."""
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest:
        conn.close()
        raise HTTPException(404, "Restaurant not found")

    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Get next ticket number
    max_ticket = conn.execute(
        "SELECT MAX(ticket_number) as max_t FROM queue_entries WHERE restaurant_id = ?",
        (restaurant_id,),
    ).fetchone()["max_t"]
    ticket_number = (max_ticket or 0) + 1

    conn.execute(
        "INSERT INTO queue_entries (id, restaurant_id, ticket_number, party_size, status, created_at, push_subscription) VALUES (?, ?, ?, ?, 'waiting', ?, ?)",
        (entry_id, restaurant_id, ticket_number, party_size, now, push_subscription),
    )
    conn.commit()
    conn.close()

    notify_sse(restaurant_id)

    return JSONResponse({
        "entry_id": entry_id,
        "ticket_number": ticket_number,
        "status_url": f"/status/{entry_id}",
    })


@app.post("/api/queue/seat")
async def seat_customer(restaurant_id: str = Form(...), entry_id: str = Form(...), passcode: str = Form("")):
    """Admin marks a customer as seated."""
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest or rest["passcode"] != passcode:
        conn.close()
        raise HTTPException(403, "Invalid passcode")

    conn.execute(
        "UPDATE queue_entries SET status = 'seated' WHERE id = ? AND restaurant_id = ?",
        (entry_id, restaurant_id),
    )
    conn.commit()
    conn.close()

    notify_sse(restaurant_id)
    return JSONResponse({"status": "ok"})


@app.post("/api/queue/cancel")
async def cancel_customer(restaurant_id: str = Form(...), entry_id: str = Form(...), passcode: str = Form("")):
    """Admin cancels a customer's entry."""
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest or rest["passcode"] != passcode:
        conn.close()
        raise HTTPException(403, "Invalid passcode")

    conn.execute(
        "UPDATE queue_entries SET status = 'cancelled' WHERE id = ? AND restaurant_id = ?",
        (entry_id, restaurant_id),
    )
    conn.commit()
    conn.close()

    notify_sse(restaurant_id)
    return JSONResponse({"status": "ok"})


@app.get("/api/queue/{restaurant_id}/status")
async def get_queue_status(restaurant_id: str):
    """Get current queue status."""
    conn = get_db()
    entries = conn.execute(
        "SELECT id, ticket_number, party_size, status, created_at FROM queue_entries "
        "WHERE restaurant_id = ? AND status = 'waiting' ORDER BY ticket_number ASC",
        (restaurant_id,),
    ).fetchall()

    stats = get_queue_stats(restaurant_id)
    conn.close()

    return JSONResponse({
        "entries": [dict(e) for e in entries],
        "stats": stats,
    })


@app.get("/api/queue/{restaurant_id}/events")
async def queue_events(restaurant_id: str):
    """SSE endpoint for real-time queue updates."""
    import asyncio

    async def event_generator():
        queue = asyncio.Queue()
        if restaurant_id not in sse_clients:
            sse_clients[restaurant_id] = []
        # We'll use the entry_id as None for restaurant-level updates
        sse_clients[restaurant_id].append((None, queue))
        try:
            while True:
                data = await queue.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                sse_clients[restaurant_id].remove((None, queue))
            except:
                pass

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/queue/entry/{entry_id}/events")
async def entry_events(entry_id: str):
    """SSE endpoint for a specific entry — sends updates when this customer's status changes."""
    import asyncio

    async def event_generator():
        queue = asyncio.Queue()
        conn = get_db()
        entry = conn.execute("SELECT * FROM queue_entries WHERE id = ?", (entry_id,)).fetchone()
        conn.close()
        if not entry:
            yield f"data: {json.dumps({'type': 'error', 'msg': 'not found'})}\n\n"
            return

        rid = entry["restaurant_id"]
        if rid not in sse_clients:
            sse_clients[rid] = []
        sse_clients[rid].append((entry_id, queue))

        # Send initial state
        yield f"data: {json.dumps({'type': 'connected', 'entry_id': entry_id})}\n\n"

        try:
            while True:
                data = await queue.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                sse_clients[rid].remove((entry_id, queue))
            except:
                pass

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/vapid/public-key")
async def get_vapid_public_key():
    """Return the VAPID public key for push notification subscription."""
    # For MVP, generate a simple key pair or use a default
    # In production, use pywebpush with proper VAPID keys
    return JSONResponse({
        "public_key": "BIPU7RvX9cZQVOyv5FhYxXqT0W3sHgvGnLqjMmRkSwNt",
        "note": "Demo key — replace with proper VAPID keys for production"
    })


# --- Run ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
