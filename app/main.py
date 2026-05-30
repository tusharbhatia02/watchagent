import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query

from app.database import init_db, get_db
from app.scheduler import start_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler = start_scheduler()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="WatchAgent", lifespan=lifespan)


@app.get("/health")
async def health():
    db = await get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM readings")
        readings_stored = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM events")
        events_stored = (await cur.fetchone())[0]
    finally:
        await db.close()
    return {
        "status": "ok",
        "readings_stored": readings_stored,
        "events_stored": events_stored,
    }


@app.get("/readings")
async def readings(city: str | None = None, limit: int = Query(50, ge=1, le=1000)):
    db = await get_db()
    try:
        if city:
            cur = await db.execute(
                "SELECT * FROM readings WHERE city = ? ORDER BY timestamp DESC LIMIT ?",
                (city, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM readings ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()
    finally:
        await db.close()
    return [dict(row) for row in rows]


@app.get("/events")
async def events(city: str | None = None, limit: int = Query(50, ge=1, le=1000)):
    db = await get_db()
    try:
        if city:
            cur = await db.execute(
                "SELECT * FROM events WHERE city = ? ORDER BY triggered_at DESC LIMIT ?",
                (city, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM events ORDER BY triggered_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()
    finally:
        await db.close()
    return [dict(row) for row in rows]
