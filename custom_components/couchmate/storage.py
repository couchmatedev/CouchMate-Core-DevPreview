"""Storage handling for CouchMate."""
from __future__ import annotations
import logging
from typing import Any
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from .const import STORAGE_KEY, STORAGE_VERSION
_LOGGER = logging.getLogger(__name__)

async def async_load_entities(hass: HomeAssistant) -> dict[str, Any]:
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    try:
        data = await store.async_load()
        return data or {
            "entities": [],
            "areas": [],
            "devices": [],
            "room_temperatures": {},
            "room_humidities": {},
            "room_climates": {},
            "weather_entity": None,
            "selection_model": {"version": 2, "areas": {}},
        }
    except Exception:
        _LOGGER.exception("Error loading CouchMate selections")
        return {
            "entities": [],
            "areas": [],
            "devices": [],
            "room_temperatures": {},
            "room_humidities": {},
            "room_climates": {},
            "weather_entity": None,
            "selection_model": {"version": 2, "areas": {}},
        }

async def async_save_entities(hass: HomeAssistant, data: dict[str, Any]) -> None:
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    # Several established API/service paths update only the flat entity list.
    # Merge those partial updates so newer room sources, the global weather
    # source, and the selection model are not silently erased.
    existing = await store.async_load() or {}
    existing.update(data)
    await store.async_save(existing)
