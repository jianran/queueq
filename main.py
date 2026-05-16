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

from fastapi import FastAPI, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse
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
            FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
        );
        CREATE INDEX IF NOT EXISTS idx_queue_rid ON queue_entries(restaurant_id, status);
    """)
    conn.commit()
    conn.close()


init_db()


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
    queue_url = f"/queue/{rid}"
    qr_svg = qrcode.make(queue_url, image_factory=qrcode.image.svg.SvgPathImage).to_string().decode()
    return JSONResponse({"restaurant_id": rid, "queue_url": queue_url, "qr_svg": qr_svg})


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
async def call_next(restaurant_id: str = Form(...), passcode: str = Form("")):
    conn = get_db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
    if not rest or rest["passcode"] != passcode:
        conn.close()
        raise HTTPException(403, "Wrong passcode")

    # Pick longest-waiting entry
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
