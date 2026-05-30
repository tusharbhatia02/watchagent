import os
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "/data/watchagent.db")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                city                 TEXT    NOT NULL,
                timestamp            TEXT    NOT NULL,
                temperature_2m       REAL,
                apparent_temperature REAL,
                precipitation        REAL,
                wind_speed_10m       REAL,
                weather_code         INTEGER,
                fetched_at           TEXT    NOT NULL,
                UNIQUE (city, timestamp)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                city         TEXT    NOT NULL,
                event_type   TEXT    NOT NULL,
                severity     TEXT    NOT NULL,
                description  TEXT    NOT NULL,
                timestamp    TEXT    NOT NULL,
                triggered_at TEXT    NOT NULL,
                reading_id   INTEGER REFERENCES readings (id),
                details      TEXT    NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_readings_city_ts ON readings (city, timestamp)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_city_type  ON events  (city, event_type, triggered_at)")
        await db.commit()


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db
