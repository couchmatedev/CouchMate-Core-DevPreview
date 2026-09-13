"""Shared, backwards-compatible Flow presentation settings."""
from __future__ import annotations

from collections.abc import Mapping

FLOW_MODES = ("automatic", "custom", "hidden")


def normalize_flow_mode(value: object) -> str:
    """Older selections and unknown modes keep automatic suggestions."""
    return value if isinstance(value, str) and value in FLOW_MODES else "automatic"


def flow_modes_for_selection(selection_model: Mapping) -> dict[str, str]:
    """Expose each configured room's presentation mode to paired clients."""
    areas = selection_model.get("areas", {})
    if not isinstance(areas, Mapping):
        return {}
    return {
        str(area_id): normalize_flow_mode(area.get("flow_mode"))
        for area_id, area in areas.items()
        if isinstance(area, Mapping)
    }
