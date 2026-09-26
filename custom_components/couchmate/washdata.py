"""Identify WashData appliance sensors by registry identity, not entity names.

WashData gives each appliance a HA device identifier ``(ha_washdata,
config_entry_id)`` and its sensors unique IDs ``<config_entry_id>_<key>``.
Entity IDs can be renamed by the user, so they are never used to infer roles.
"""
from __future__ import annotations


SENSOR_KEYS = {
    "state": "washer_state",
    "program": "washer_program",
    "time_remaining": "time_remaining",
    "cycle_progress": "cycle_progress",
    "current_phase": "current_phase",
    "total_duration": "total_duration",
}


def washdata_entry_id(device) -> str | None:
    for identifier in getattr(device, "identifiers", ()) or ():
        if (
            isinstance(identifier, (tuple, list))
            and len(identifier) == 2
            and identifier[0] == "ha_washdata"
            and isinstance(identifier[1], str)
            and identifier[1]
        ):
            return identifier[1]
    return None


def washdata_sensor_entity_ids(device, entity_registry) -> dict[str, str]:
    """Return enabled display sensors for a verified WashData HA device."""
    entry_id = washdata_entry_id(device)
    if entry_id is None:
        return {}
    roles_by_unique_id = {
        f"{entry_id}_{key}": role for role, key in SENSOR_KEYS.items()
    }
    result: dict[str, str] = {}
    for entity in entity_registry.entities.values():
        if (
            entity.device_id != device.id
            or getattr(entity, "disabled", False)
            or not entity.entity_id.startswith("sensor.")
            or getattr(entity, "platform", "ha_washdata") != "ha_washdata"
        ):
            continue
        role = roles_by_unique_id.get(getattr(entity, "unique_id", None))
        if role is not None:
            result[role] = entity.entity_id
    return result


def selected_washdata_rooms(selection_model) -> dict[str, str]:
    """Map explicitly chosen WashData devices to exactly one CouchMate room."""
    if not isinstance(selection_model, dict):
        return {}
    areas = selection_model.get("areas", {})
    if not isinstance(areas, dict):
        return {}
    rooms: dict[str, str] = {}
    for area_id, area in areas.items():
        if not isinstance(area, dict) or not isinstance(area.get("washdata_devices"), list):
            continue
        for device_id in area["washdata_devices"]:
            if isinstance(device_id, str) and device_id:
                rooms.setdefault(device_id, str(area_id))
    return rooms
