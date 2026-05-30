import httpx
import pytest
import pytest_asyncio
import respx

from app import database, poller
from app.database import init_db, get_db
from app.poller import poll_city, OPEN_METEO_URL


def _meteo_response(timestamp):
    return httpx.Response(
        200,
        json={
            "current": {
                "time": timestamp,
                "temperature_2m": 12.3,
                "apparent_temperature": 11.0,
                "precipitation": 0.0,
                "wind_speed_10m": 15.2,
                "weather_code": 3,
            }
        },
    )


@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_watchagent.db"
    # get_db() reads database.DB_PATH at call time, so patching the module
    # global is enough to redirect every connection at the tmp file.
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    await init_db()
    yield str(db_file)


async def _count_readings():
    db = await get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM readings")
        return (await cur.fetchone())[0]
    finally:
        await db.close()


@respx.mock
async def test_duplicate_reading_stored_once(tmp_db):
    route = respx.get(OPEN_METEO_URL).mock(
        return_value=_meteo_response("2026-05-30T05:00")
    )

    coords = poller.CITIES["Ottawa"]
    await poll_city("Ottawa", coords["lat"], coords["lon"])
    await poll_city("Ottawa", coords["lat"], coords["lon"])

    assert route.call_count == 2
    assert await _count_readings() == 1


@respx.mock
async def test_distinct_timestamps_stored_separately(tmp_db):
    respx.get(OPEN_METEO_URL).mock(
        side_effect=[
            _meteo_response("2026-05-30T05:00"),
            _meteo_response("2026-05-30T06:00"),
        ]
    )

    coords = poller.CITIES["Ottawa"]
    await poll_city("Ottawa", coords["lat"], coords["lon"])
    await poll_city("Ottawa", coords["lat"], coords["lon"])

    assert await _count_readings() == 2
