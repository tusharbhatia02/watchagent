import logging
from datetime import datetime, timezone

import httpx

from app.database import get_db
from app.events import detect_events

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

CITIES = {
    "Ottawa": {"lat": 45.4215, "lon": -75.6972},
    "Toronto": {"lat": 43.6532, "lon": -79.3832},
    "Vancouver": {"lat": 49.2827, "lon": -123.1207},
}

CURRENT_FIELDS = "temperature_2m,apparent_temperature,precipitation,wind_speed_10m,weather_code"


async def poll_city(city, lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": CURRENT_FIELDS,
        "wind_speed_unit": "kmh",
        "timezone": "auto",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning("Poll failed city=%s status=%s", city, e.response.status_code)
        return
    except httpx.RequestError as e:
        logger.warning("Poll failed city=%s error=%s", city, type(e).__name__)
        return

    current = payload.get("current", {})
    timestamp = current.get("time")
    fetched_at = datetime.now(timezone.utc).isoformat()

    reading = {
        "city": city,
        "timestamp": timestamp,
        "temperature_2m": current.get("temperature_2m"),
        "apparent_temperature": current.get("apparent_temperature"),
        "precipitation": current.get("precipitation"),
        "wind_speed_10m": current.get("wind_speed_10m"),
        "weather_code": current.get("weather_code"),
        "fetched_at": fetched_at,
    }

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO readings "
            "(city, timestamp, temperature_2m, apparent_temperature, precipitation, "
            "wind_speed_10m, weather_code, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reading["city"],
                reading["timestamp"],
                reading["temperature_2m"],
                reading["apparent_temperature"],
                reading["precipitation"],
                reading["wind_speed_10m"],
                reading["weather_code"],
                reading["fetched_at"],
            ),
        )
        await db.commit()

        if cursor.rowcount == 1:
            reading_id = cursor.lastrowid
            logger.info("New reading stored city=%s timestamp=%s", city, timestamp)
            await detect_events(db, city, reading, timestamp, reading_id)
        else:
            logger.debug("Duplicate reading skipped city=%s timestamp=%s", city, timestamp)
    finally:
        await db.close()


async def poll_all_cities():
    for city, coords in CITIES.items():
        await poll_city(city, coords["lat"], coords["lon"])
