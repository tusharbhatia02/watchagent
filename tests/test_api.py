import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fastapi.testclient import TestClient

from app import database, main
from app.database import init_db, get_db
from app.main import app


async def _populate():
    db = await get_db()
    try:
        readings = [
            ("Ottawa", "2026-05-30T03:00", -2.0, -7.0, 0.0, 12.0, 3, "2026-05-30T03:05Z"),
            ("Ottawa", "2026-05-30T04:00", -1.0, -6.0, 0.0, 14.0, 3, "2026-05-30T04:05Z"),
            ("Ottawa", "2026-05-30T05:00", 0.0, -5.0, 0.2, 20.0, 61, "2026-05-30T05:05Z"),
            ("Toronto", "2026-05-30T04:00", 5.0, 3.0, 0.0, 10.0, 1, "2026-05-30T04:05Z"),
            ("Toronto", "2026-05-30T05:00", 6.0, 4.0, 0.0, 11.0, 2, "2026-05-30T05:05Z"),
        ]
        await db.executemany(
            "INSERT INTO readings "
            "(city, timestamp, temperature_2m, apparent_temperature, precipitation, "
            "wind_speed_10m, weather_code, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            readings,
        )
        events = [
            ("Ottawa", "precipitation_onset", "info", "Ottawa: precipitation onset",
             "2026-05-30T05:00", "2026-05-30T05:05Z", 3, "{}"),
            ("Toronto", "high_wind", "warning", "Toronto: high wind",
             "2026-05-30T05:00", "2026-05-30T05:05Z", 5, "{}"),
        ]
        await db.executemany(
            "INSERT INTO events "
            "(city, event_type, severity, description, timestamp, triggered_at, reading_id, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            events,
        )
        await db.commit()
    finally:
        await db.close()


@pytest_asyncio.fixture
async def seeded_db(tmp_path, monkeypatch):
    db_file = tmp_path / "api_test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    # Keep the scheduler from spinning up if the lifespan ever runs.
    monkeypatch.setattr(
        main, "start_scheduler", lambda: SimpleNamespace(shutdown=lambda **k: None)
    )
    await init_db()
    await _populate()
    yield str(db_file)


@pytest_asyncio.fixture
async def client(seeded_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_returns_ok_with_counts(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["readings_stored"], int)
    assert isinstance(body["events_stored"], int)
    assert body["readings_stored"] == 5
    assert body["events_stored"] == 2


async def test_readings_returns_list(client):
    resp = await client.get("/readings")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 5
    # Most recent first.
    assert body[0]["timestamp"] >= body[-1]["timestamp"]
    assert {"city", "timestamp", "temperature_2m"} <= set(body[0].keys())


async def test_readings_city_filter(client):
    resp = await client.get("/readings", params={"city": "Ottawa"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    assert all(row["city"] == "Ottawa" for row in body)


async def test_readings_limit(client):
    resp = await client.get("/readings", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2


async def test_events_returns_list(client):
    resp = await client.get("/events")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert {"city", "event_type", "severity"} <= set(body[0].keys())


async def test_events_city_filter(client):
    resp = await client.get("/events", params={"city": "Toronto"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["city"] == "Toronto"


def test_health_with_testclient(tmp_path, monkeypatch):
    db_file = tmp_path / "api_testclient.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    monkeypatch.setattr(
        main, "start_scheduler", lambda: SimpleNamespace(shutdown=lambda **k: None)
    )
    asyncio.run(init_db())
    asyncio.run(_populate())

    with TestClient(app) as test_client:
        resp = test_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert isinstance(body["readings_stored"], int)
        assert isinstance(body["events_stored"], int)
