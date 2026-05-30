import json

import pytest
import pytest_asyncio

from app import database
from app.database import init_db, get_db
from app.events import detect_events, CITY_CONFIG

# Required keys on every event record (see .cursor/rules/event_schema.mdc).
EVENT_KEYS = {
    "city", "event_type", "severity", "description",
    "timestamp", "triggered_at", "reading_id", "details",
}

# Baseline "calm" reading; individual tests override only what they exercise.
DEFAULTS = dict(
    temperature_2m=10.0,
    apparent_temperature=9.0,
    precipitation=0.0,
    wind_speed_10m=10.0,
    weather_code=1,
)


def _reading(**over):
    r = dict(DEFAULTS)
    r.update(over)
    return r


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    db_file = tmp_path / "events.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    await init_db()
    conn = await get_db()
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_reading(conn, city, timestamp, **over):
    r = _reading(**over)
    cur = await conn.execute(
        "INSERT INTO readings "
        "(city, timestamp, temperature_2m, apparent_temperature, precipitation, "
        "wind_speed_10m, weather_code, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (city, timestamp, r["temperature_2m"], r["apparent_temperature"],
         r["precipitation"], r["wind_speed_10m"], r["weather_code"], timestamp),
    )
    await conn.commit()
    return cur.lastrowid


async def _process(conn, city, timestamp, **over):
    """Seed a reading (as the poller would) then run detection on it."""
    reading_id = await _seed_reading(conn, city, timestamp, **over)
    return await detect_events(conn, city, _reading(**over), timestamp, reading_id)


def _types(events):
    return [e["event_type"] for e in events]


async def _count(conn, event_type):
    cur = await conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ?", (event_type,)
    )
    return (await cur.fetchone())[0]


async def test_temperature_spike(db):
    await _seed_reading(db, "Ottawa", "2026-05-30T01:00", temperature_2m=15.0, apparent_temperature=14.0)
    events = await _process(db, "Ottawa", "2026-05-30T02:00", temperature_2m=25.0, apparent_temperature=24.0)

    assert _types(events) == ["temperature_spike"]
    event = events[0]
    assert event["severity"] == "warning"  # 25°C is below Ottawa heat_alert (32°C)
    assert EVENT_KEYS <= set(event)
    assert "Ottawa" in event["description"]
    # details must carry the numeric values used in detection (auditability).
    details = json.loads(event["details"])
    assert details["temperature_2m"] == 25.0
    assert details["previous_temperature_2m"] == 15.0
    assert details["threshold"] == CITY_CONFIG["Ottawa"]["temp_spike"]
    assert await _count(db, "temperature_spike") == 1


async def test_temperature_plunge_critical_below_cold_snap(db):
    await _seed_reading(db, "Ottawa", "2026-05-30T01:00", temperature_2m=0.0, apparent_temperature=-1.0)
    events = await _process(db, "Ottawa", "2026-05-30T02:00", temperature_2m=-26.0, apparent_temperature=-27.0)

    assert _types(events) == ["temperature_plunge"]
    # -26°C is below Ottawa cold_snap (-25°C) → escalated to critical.
    assert events[0]["severity"] == "critical"


async def test_no_temperature_event_below_threshold(db):
    await _seed_reading(db, "Ottawa", "2026-05-30T01:00", temperature_2m=15.0)
    events = await _process(db, "Ottawa", "2026-05-30T02:00", temperature_2m=16.0)

    assert events == []


async def test_precipitation_onset_after_three_dry(db):
    for hour in ("01:00", "02:00", "03:00"):
        await _seed_reading(db, "Ottawa", f"2026-05-30T{hour}", precipitation=0.0)
    events = await _process(db, "Ottawa", "2026-05-30T04:00", precipitation=1.2)

    assert _types(events) == ["precipitation_onset"]
    assert events[0]["severity"] == "info"
    assert await _count(db, "precipitation_onset") == 1


async def test_no_precipitation_onset_without_enough_dry_history(db):
    # Only two prior dry readings — fewer than the required run of three.
    for hour in ("01:00", "02:00"):
        await _seed_reading(db, "Ottawa", f"2026-05-30T{hour}", precipitation=0.0)
    events = await _process(db, "Ottawa", "2026-05-30T03:00", precipitation=1.2)

    assert "precipitation_onset" not in _types(events)


async def test_severe_weather_code(db):
    events = await _process(db, "Ottawa", "2026-05-30T01:00", weather_code=95)

    assert _types(events) == ["severe_weather_code"]
    assert events[0]["severity"] == "critical"  # thunderstorm
    assert json.loads(events[0]["details"])["weather_code"] == 95


async def test_feels_like_divergence(db):
    events = await _process(
        db, "Ottawa", "2026-05-30T01:00", temperature_2m=0.0, apparent_temperature=-12.0
    )

    assert _types(events) == ["feels_like_divergence"]
    assert events[0]["severity"] == "warning"
    details = json.loads(events[0]["details"])
    assert details["temperature_2m"] == 0.0
    assert details["apparent_temperature"] == -12.0


async def test_high_wind_hysteresis(db):
    threshold = CITY_CONFIG["Ottawa"]["wind_warning"]

    # Calm baseline, then the first windy reading fires.
    await _seed_reading(db, "Ottawa", "2026-05-30T01:00", wind_speed_10m=10.0)
    first = await _process(db, "Ottawa", "2026-05-30T02:00", wind_speed_10m=threshold + 5)
    assert "high_wind" in _types(first)

    # Still windy with no intervening calm reading → suppressed by hysteresis.
    second = await _process(db, "Ottawa", "2026-05-30T03:00", wind_speed_10m=threshold + 6)
    assert "high_wind" not in _types(second)

    # A calm reading resets the condition, so the next gust fires again.
    await _seed_reading(db, "Ottawa", "2026-05-30T04:00", wind_speed_10m=10.0)
    third = await _process(db, "Ottawa", "2026-05-30T05:00", wind_speed_10m=threshold + 7)
    assert "high_wind" in _types(third)

    assert await _count(db, "high_wind") == 2
