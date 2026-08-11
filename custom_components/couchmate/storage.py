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
        return data or {"entities": [], "areas": [], "devices": [], "selection_model": {"version": 2, "areas": {}}}
    except Exception:
        _LOGGER.exception("Error loading CouchMate selections")
        return {"entities": [], "areas": [], "devices": [], "selection_model": {"version": 2, "areas": {}}}

async def async_save_entities(hass: HomeAssistant, data: dict[str, Any]) -> None:
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await store.async_save(data)
