"""Read-only electricity overview based on Home Assistant Energy statistics.

The Energy integration owns source selection. CouchMate only stores an opt-in
and reads Recorder's ``change`` values, never a cumulative meter state as a
daily consumption value. The result is deliberately bounded to the current
local day and electricity; it can be extended without changing room selection.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, time, timedelta, timezone
import logging
import math
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ENERGY_ROLES = (
    "grid_import",
    "grid_export",
    "solar_production",
    "battery_charge",
    "battery_discharge",
)
_CACHE_SECONDS = 300


def energy_enabled(selection_model: Any) -> bool:
    """Only a literal true in the saved global selection enables the page."""
    if not isinstance(selection_model, Mapping):
        return False
    settings = selection_model.get("energy_dashboard")
    return isinstance(settings, Mapping) and settings.get("enabled") is True


def _statistic_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def configured_electricity_sources(
    preferences: Mapping[str, Any],
) -> tuple[dict[str, set[str]], list[dict[str, str]]]:
    """Extract electricity statistic IDs from current and legacy Energy prefs."""
    sources = {role: set() for role in ENERGY_ROLES}
    raw_sources = preferences.get("energy_sources", [])
    if isinstance(raw_sources, list):
        for source in raw_sources:
            if not isinstance(source, Mapping):
                continue
            kind = source.get("type")
            entries: list[tuple[str, Any]] = []
            if kind == "grid":
                entries.extend((
                    ("grid_import", source.get("stat_energy_from")),
                    ("grid_export", source.get("stat_energy_to")),
                ))
                # Older HA installations stored separate grid flow arrays.
                for flow in source.get("flow_from", []) if isinstance(source.get("flow_from"), list) else []:
                    if isinstance(flow, Mapping):
                        entries.append(("grid_import", flow.get("stat_energy_from")))
                for flow in source.get("flow_to", []) if isinstance(source.get("flow_to"), list) else []:
                    if isinstance(flow, Mapping):
                        entries.append(("grid_export", flow.get("stat_energy_to")))
            elif kind == "solar":
                entries.append(("solar_production", source.get("stat_energy_from")))
            elif kind == "battery":
                # HA defines from as discharge, to as charge.
                entries.extend((
                    ("battery_discharge", source.get("stat_energy_from")),
                    ("battery_charge", source.get("stat_energy_to")),
                ))
            for role, value in entries:
                if statistic_id := _statistic_id(value):
                    sources[role].add(statistic_id)

    devices: list[dict[str, str]] = []
    raw_devices = preferences.get("device_consumption", [])
    if isinstance(raw_devices, list):
        seen: set[str] = set()
        for device in raw_devices:
            if not isinstance(device, Mapping):
                continue
            statistic_id = _statistic_id(device.get("stat_consumption"))
            if statistic_id is None or statistic_id in seen:
                continue
            seen.add(statistic_id)
            name = device.get("name")
            devices.append({
                "statistic_id": statistic_id,
                "name": name.strip() if isinstance(name, str) and name.strip() else statistic_id,
            })
    return sources, devices


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _local_hour(timestamp: Any, zone: ZoneInfo | timezone) -> str | None:
    number = _finite_number(timestamp)
    if number is None:
        return None
    try:
        instant = datetime.fromtimestamp(number, timezone.utc).astimezone(zone)
    except (OverflowError, OSError, ValueError):
        return None
    return instant.replace(minute=0, second=0, microsecond=0).isoformat()


def _add_home_consumption(
    values: dict[str, float], required_roles: set[str],
) -> None:
    if not required_roles or not required_roles.issubset(values):
        return
    consumption = (
        values.get("grid_import", 0)
        + values.get("solar_production", 0)
        + values.get("battery_discharge", 0)
        - values.get("grid_export", 0)
        - values.get("battery_charge", 0)
    )
    if consumption >= 0:
        values["home_consumption"] = consumption
    elif consumption > -0.000001:
        values["home_consumption"] = 0.0


def aggregate_energy_statistics(
    sources: dict[str, set[str]],
    devices: list[dict[str, str]],
    stats: Mapping[str, Any],
    zone: ZoneInfo | timezone,
) -> tuple[dict[str, float], list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Combine Recorder changes, preserving missing values as missing."""
    totals: dict[str, float] = {}
    buckets: dict[str, dict[str, float]] = defaultdict(dict)
    total_ids: dict[str, set[str]] = defaultdict(set)
    bucket_ids: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    latest_start: float | None = None
    for role, statistic_ids in sources.items():
        for statistic_id in statistic_ids:
            rows = stats.get(statistic_id, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                amount = _finite_number(row.get("change"))
                hour = _local_hour(row.get("start"), zone)
                if amount is None or hour is None:
                    continue
                totals[role] = totals.get(role, 0) + amount
                total_ids[role].add(statistic_id)
                bucket = buckets[hour]
                bucket[role] = bucket.get(role, 0) + amount
                bucket_ids[hour][role].add(statistic_id)
                started = _finite_number(row.get("start"))
                if started is not None:
                    latest_start = max(latest_start or started, started)

    for role, statistic_ids in sources.items():
        if statistic_ids and not statistic_ids.issubset(total_ids[role]):
            totals.pop(role, None)

    required_roles = {role for role, ids in sources.items() if ids}
    _add_home_consumption(totals, required_roles)
    hourly = []
    for hour in sorted(buckets, key=lambda item: datetime.fromisoformat(item).timestamp()):
        values = buckets[hour]
        for role, statistic_ids in sources.items():
            if statistic_ids and not statistic_ids.issubset(bucket_ids[hour][role]):
                values.pop(role, None)
        _add_home_consumption(values, required_roles)
        if values:
            hourly.append({"start": hour, **values})

    device_values = []
    for device in devices:
        values = []
        for row in stats.get(device["statistic_id"], []):
            if not isinstance(row, Mapping):
                continue
            amount = _finite_number(row.get("change"))
            started = _finite_number(row.get("start"))
            if amount is None or started is None:
                continue
            values.append(amount)
            latest_start = max(latest_start or started, started)
        item: dict[str, Any] = dict(device)
        if values:
            item["total"] = sum(values)
        device_values.append(item)

    latest_data_at = (
        datetime.fromtimestamp(latest_start, timezone.utc).isoformat()
        if latest_start is not None else None
    )
    return totals, hourly, device_values, latest_data_at


async def _async_compute_energy_dashboard_payload(
    hass: Any,
    selection_model: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return today's Energy dashboard snapshot for a paired read-only client."""
    if not energy_enabled(selection_model):
        return {"enabled": False}

    try:
        zone = ZoneInfo(hass.config.time_zone)
    except (AttributeError, ZoneInfoNotFoundError, ValueError, TypeError):
        zone = timezone.utc
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    start = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    end = datetime.combine(local_now.date() + timedelta(days=1), time.min, tzinfo=zone)
    base: dict[str, Any] = {
        "enabled": True,
        "source": "home_assistant",
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "as_of": local_now.isoformat(),
        "unit": "kWh",
        "totals": {},
        "hourly": [],
        "devices": [],
        "latest_data_at": None,
    }

    try:
        # Import lazily: an installation without Energy/Recorder configured
        # must still start CouchMate and return a clear empty state.
        from homeassistant.components.energy.data import async_get_manager

        manager = await async_get_manager(hass)
        preferences = manager.data
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Unable to read Home Assistant Energy preferences")
        return {**base, "status": "unconfigured"}
    if not isinstance(preferences, Mapping):
        return {**base, "status": "unconfigured"}

    sources, devices = configured_electricity_sources(preferences)
    for device in devices:
        if device["name"] == device["statistic_id"]:
            state = hass.states.get(device["statistic_id"])
            if state is not None:
                device["name"] = (
                    state.attributes.get("friendly_name") or state.name or device["name"]
                )
    statistic_ids = set().union(*sources.values(), (item["statistic_id"] for item in devices))
    if not statistic_ids:
        return {**base, "status": "unconfigured"}

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        recorder = get_instance(hass)
        utc_start = start.astimezone(timezone.utc)
        utc_now = local_now.astimezone(timezone.utc)
        current_hour = local_now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        stats: dict[str, list[dict[str, Any]]] = {}
        if current_hour > utc_start:
            stats = await recorder.async_add_executor_job(
                statistics_during_period,
                hass, utc_start, current_hour, statistic_ids, "hour",
                {"energy": "kWh"}, {"change"},
            )
        recent = await recorder.async_add_executor_job(
            statistics_during_period,
            hass, current_hour, utc_now, statistic_ids, "5minute",
            {"energy": "kWh"}, {"change"},
        )
        for statistic_id, rows in recent.items():
            stats.setdefault(statistic_id, []).extend(rows)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Unable to read Home Assistant Energy statistics")
        return {**base, "status": "recorder_unavailable"}

    totals, hourly, device_values, latest_data_at = aggregate_energy_statistics(
        sources, devices, stats, zone,
    )
    value = {
        **base,
        "status": "ok" if totals or any("total" in item for item in device_values) else "no_statistics",
        "totals": totals,
        "hourly": hourly,
        "devices": device_values,
        "latest_data_at": latest_data_at,
    }
    return value


async def async_energy_dashboard_payload(
    hass: Any,
    selection_model: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return cached data immediately; refresh Recorder in the background."""
    if not energy_enabled(selection_model):
        return {"enabled": False}

    try:
        zone = ZoneInfo(hass.config.time_zone)
    except (AttributeError, ZoneInfoNotFoundError, ValueError, TypeError):
        zone = timezone.utc
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    day = local_now.date().isoformat()
    runtime = hass.data.setdefault(DOMAIN, {})
    generation = runtime.get("energy_dashboard_generation", 0)
    cached = runtime.get("energy_dashboard_cache")
    same_day = isinstance(cached, dict) and cached.get("day") == day
    if same_day and cached.get("expires", 0) > monotonic():
        return dict(cached["value"])

    task = runtime.get("energy_dashboard_task")
    if (task is None or task.done() or runtime.get("energy_dashboard_task_day") != day
            or runtime.get("energy_dashboard_task_generation") != generation):
        async def refresh() -> None:
            try:
                value = await _async_compute_energy_dashboard_payload(
                    hass, selection_model, now=local_now,
                )
                # A new day may have superseded this task while Recorder worked.
                if (runtime.get("energy_dashboard_task") is asyncio.current_task()
                        and runtime.get("energy_dashboard_generation", 0) == generation):
                    runtime["energy_dashboard_cache"] = {
                        "day": day,
                        "expires": monotonic() + (
                            _CACHE_SECONDS if value.get("status") == "ok" else 60
                        ),
                        "value": value,
                    }
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unable to refresh CouchMate Energy dashboard")
            finally:
                if runtime.get("energy_dashboard_task") is asyncio.current_task():
                    runtime.pop("energy_dashboard_task", None)
                    runtime.pop("energy_dashboard_task_day", None)
                    runtime.pop("energy_dashboard_task_generation", None)

        task = asyncio.create_task(refresh())
        runtime["energy_dashboard_task"] = task
        runtime["energy_dashboard_task_day"] = day
        runtime["energy_dashboard_task_generation"] = generation

    if same_day:
        return dict(cached["value"])
    start = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    end = datetime.combine(local_now.date() + timedelta(days=1), time.min, tzinfo=zone)
    return {
        "enabled": True,
        "source": "home_assistant",
        "status": "loading",
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "as_of": local_now.isoformat(),
        "unit": "kWh",
        "totals": {},
        "hourly": [],
        "devices": [],
        "latest_data_at": None,
    }
