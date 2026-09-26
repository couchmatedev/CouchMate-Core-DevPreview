"""Validate and describe explicitly selected security dashboard sources.

The dashboard is opt-in. Its source lists are independent of the regular
room/device selection, and no alarm code is stored in this model.
"""
from __future__ import annotations

from collections.abc import Mapping


CONTACT_DEVICE_CLASSES = frozenset({"door", "garage_door", "opening", "window"})
ARM_SERVICES = {
    "alarm_arm_home": 1,
    "alarm_arm_away": 2,
    "alarm_arm_night": 4,
    "alarm_arm_vacation": 32,
}
ALARM_SERVICES = frozenset({"alarm_disarm", *ARM_SERVICES})


def _device_class(entry, state) -> str | None:
    value = getattr(entry, "device_class", None)
    if value is None and state is not None:
        value = state.attributes.get("device_class")
    return str(value) if value is not None else None


def valid_security_entity(entity_id: str, kind: str, entry, state) -> bool:
    """Check the registry identity and category, never a friendly name."""
    if entry is None or getattr(entry, "disabled", False) or state is None:
        return False
    if kind == "alarm":
        return entity_id.startswith("alarm_control_panel.")
    if kind == "camera":
        return entity_id.startswith(("camera.", "image."))
    if kind == "contact":
        return (
            entity_id.startswith("binary_sensor.")
            and _device_class(entry, state) in CONTACT_DEVICE_CLASSES
        )
    return False


def normalize_security_dashboard(raw, entity_registry, states) -> dict:
    """Keep only live, enabled entities of the requested security category."""
    raw = raw if isinstance(raw, Mapping) else {}
    result = {"enabled": raw.get("enabled") is True}
    for kind, field in (
        ("alarm", "alarm_entity_ids"),
        ("camera", "camera_entity_ids"),
        ("contact", "contact_entity_ids"),
    ):
        values = raw.get(field, [])
        selected = []
        if isinstance(values, list):
            for entity_id in values:
                if not isinstance(entity_id, str) or entity_id in selected:
                    continue
                entry = entity_registry.async_get(entity_id)
                state = states.get(entity_id) if states is not None else None
                if valid_security_entity(entity_id, kind, entry, state):
                    selected.append(entity_id)
        result[field] = selected
    return result


def configured_security_dashboard(selection_model) -> dict:
    """Read the already validated selection without enabling missing configs."""
    raw = selection_model.get("security_dashboard", {}) if isinstance(selection_model, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "enabled": raw.get("enabled") is True,
        **{
            field: [item for item in raw.get(field, []) if isinstance(item, str)]
            if isinstance(raw.get(field), list) else []
            for field in ("alarm_entity_ids", "camera_entity_ids", "contact_entity_ids")
        },
    }


def alarm_metadata(entity_payload: Mapping) -> dict:
    """Expose only alarm UI metadata; never include arbitrary attributes."""
    attributes = entity_payload.get("attributes", {})
    attributes = attributes if isinstance(attributes, Mapping) else {}
    raw_features = attributes.get("supported_features", 0)
    features = raw_features if isinstance(raw_features, int) and not isinstance(raw_features, bool) else 0
    code_format = attributes.get("code_format")
    code_format = code_format if code_format in ("number", "text") else None
    arm_requires_code = code_format is not None and attributes.get("code_arm_required", True) is not False
    return {
        "entity_id": entity_payload["entity_id"],
        "name": entity_payload.get("name") or entity_payload["entity_id"],
        "state": entity_payload.get("state"),
        "area_id": entity_payload.get("area_id"),
        "area_name": entity_payload.get("area_name"),
        "supported_features": features,
        "available_arm_modes": [service for service, flag in ARM_SERVICES.items() if features & flag],
        "code_format": code_format,
        "code_arm_required": arm_requires_code,
        "code_required": code_format is not None,
    }


def contact_metadata(entity_payload: Mapping) -> dict:
    return {
        "entity_id": entity_payload["entity_id"],
        "name": entity_payload.get("name") or entity_payload["entity_id"],
        "state": entity_payload.get("state"),
        "device_class": entity_payload.get("device_class")
        or entity_payload.get("attributes", {}).get("device_class"),
        "area_id": entity_payload.get("area_id"),
        "area_name": entity_payload.get("area_name"),
    }


def validate_alarm_action(service: str, data: Mapping, state) -> dict | None:
    """Return a minimal HA service body, or None for an invalid request."""
    if service not in ALARM_SERVICES or not isinstance(data, Mapping) or set(data) - {"code"}:
        return None
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    raw_features = state.attributes.get("supported_features", 0)
    features = raw_features if isinstance(raw_features, int) and not isinstance(raw_features, bool) else 0
    if service in ARM_SERVICES and not features & ARM_SERVICES[service]:
        return None
    code_format = state.attributes.get("code_format")
    code_format = code_format if code_format in ("number", "text") else None
    code = data.get("code")
    if code is None:
        if code_format is not None and (service == "alarm_disarm" or state.attributes.get("code_arm_required", True) is not False):
            return None
        return {}
    if not isinstance(code, str) or not code or len(code) > 128 or any(ord(char) < 32 for char in code):
        return None
    if code_format == "number" and not code.isdecimal():
        return None
    return {"code": code}
