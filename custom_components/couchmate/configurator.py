"""Graphical room, device and entity configurator for CouchMate."""
from __future__ import annotations

from copy import deepcopy

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .const import (
    DOMAIN,
    CONF_AREAS,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_EXCLUDED_ENTITIES,
    CONF_ROOM_TEMPERATURES,
    CONF_ROOM_HUMIDITIES,
    CONF_ROOM_CLIMATES,
    CONF_WEATHER_ENTITY,
    CONF_SELECTION_MODEL,
    SELECTION_MODEL_VERSION,
    CONFIGURATION_MANAGER,
    DEFAULT_PROFILE_ID,
)
from .storage import async_save_entities
from .page_layout import render_page
from .flow import normalize_flow_mode
from .energy_dashboard import energy_enabled
from .security_dashboard import (
    configured_security_dashboard,
    normalize_security_dashboard,
    valid_security_entity,
)
from .washdata import washdata_entry_id, washdata_sensor_entity_ids


def _name(entry, state, fallback):
    return entry.name or entry.original_name or (state.name if state else None) or fallback


def _is_humidity_entity(entity, state) -> bool:
    if entity.entity_id.startswith("sensor."):
        device_class = getattr(entity, "device_class", None) or (state.attributes.get("device_class") if state else None)
        unit = state.attributes.get("unit_of_measurement") if state else None
        return device_class == "humidity" or unit == "%" and "humid" in entity.entity_id.lower()
    return False


def _is_temperature_entity(entity, state) -> bool:
    if entity.entity_id.startswith("sensor."):
        device_class = getattr(entity, "device_class", None) or (state.attributes.get("device_class") if state else None)
        unit = state.attributes.get("unit_of_measurement") if state else None
        return device_class == "temperature" or unit in ("°C", "°F")
    return False


def _entity_area_id(entity, device_registry) -> str | None:
    if entity.area_id:
        return entity.area_id
    if entity.device_id and (device := device_registry.async_get(entity.device_id)):
        return device.area_id
    return None


def _default_profile_hero_orders(hass) -> dict[str, list[str]]:
    manager = hass.data.get(DOMAIN, {}).get(CONFIGURATION_MANAGER)
    if manager is None:
        return {}
    snapshot = manager.snapshot()
    profile = dict(snapshot.get("profiles", {}).get(DEFAULT_PROFILE_ID, {}))
    settings = dict(profile.get("settings", {}))
    companion = dict(settings.get("companion", {}))
    raw_orders = companion.get("hero_entity_order", {})
    if not isinstance(raw_orders, dict):
        return {}
    return {
        str(area_id): [str(entity_id) for entity_id in entity_ids if entity_id]
        for area_id, entity_ids in raw_orders.items()
        if isinstance(entity_ids, list)
    }


def _default_profile_hero_layouts(hass) -> dict[str, dict[str, str]]:
    manager = hass.data.get(DOMAIN, {}).get(CONFIGURATION_MANAGER)
    if manager is None:
        return {}
    snapshot = manager.snapshot()
    profile = dict(snapshot.get("profiles", {}).get(DEFAULT_PROFILE_ID, {}))
    settings = dict(profile.get("settings", {}))
    companion = dict(settings.get("companion", {}))
    raw_layouts = companion.get("hero_layouts", {})
    if not isinstance(raw_layouts, dict):
        return {}
    return {
        str(area_id): dict(layout)
        for area_id, layout in raw_layouts.items()
        if isinstance(layout, dict)
    }


async def _save_default_profile_hero_configuration(
    hass,
    hero_orders: dict[str, list[str]],
    hero_layouts: dict[str, dict[str, str]],
) -> None:
    manager = hass.data.get(DOMAIN, {}).get(CONFIGURATION_MANAGER)
    if manager is None:
        return
    snapshot = manager.snapshot()
    profile = dict(snapshot.get("profiles", {}).get(DEFAULT_PROFILE_ID, {}))
    settings = deepcopy(profile.get("settings", {}))
    companion = dict(settings.get("companion", {}))
    companion["hero_entity_order"] = deepcopy(hero_orders)
    companion["hero_layout_version"] = 1
    companion["hero_layouts"] = deepcopy(hero_layouts)
    settings["companion"] = companion
    await manager.async_update_profile(
        DEFAULT_PROFILE_ID,
        settings=settings,
        expected_revision=snapshot.get("revision"),
    )


def _admin_required(request) -> web.Response | None:
    """Keep the global CouchMate selection restricted to HA administrators."""
    user = request.get("hass_user")
    if user is not None and getattr(user, "is_admin", False):
        return None
    return web.json_response({"error": "admin_required"}, status=403)


class CouchMateConfiguratorView(HomeAssistantView):
    url = "/couchmate/configurator"
    name = "couchmate:configurator"
    requires_auth = False

    async def get(self, request):
        return web.Response(text=HTML, content_type="text/html")


class CouchMateConfiguratorDataView(HomeAssistantView):
    url = "/api/couchmate/configurator/data"
    name = "api:couchmate:configurator:data"
    requires_auth = True

    async def get(self, request):
        denied = _admin_required(request)
        if denied is not None:
            return denied
        hass = request.app["hass"]
        areas = ar.async_get(hass)
        devices = dr.async_get(hass)
        entities = er.async_get(hass)
        current = hass.data.get(DOMAIN, {})
        device_area = {device.id: device.area_id for device in devices.devices.values()}
        room_temperatures = current.get("room_temperatures", {})
        room_humidities = current.get("room_humidities", {})
        room_climates = current.get("room_climates", {})
        weather_entity = current.get("weather_entity")
        hero_orders = _default_profile_hero_orders(hass)
        hero_layouts = _default_profile_hero_layouts(hass)
        selection_model = current.get("selection_model", {})
        model_areas = dict(selection_model.get("areas", {})) if selection_model.get("version") == SELECTION_MODEL_VERSION else {}
        thermostat_card_style = selection_model.get("thermostat_card_style", "full")
        if thermostat_card_style not in ("ring", "full_vertical", "full", "compact", "hidden"):
            thermostat_card_style = "full"
        show_room_name = selection_model.get("show_room_name", True)
        if not isinstance(show_room_name, bool):
            show_room_name = True
        show_room_climate = selection_model.get("show_room_climate", True)
        if not isinstance(show_room_climate, bool):
            show_room_climate = True
        explicit_entities = set(current.get("explicit_entities", []))
        fully_selected_devices = set(current.get("devices", []))
        security_dashboard = configured_security_dashboard(selection_model)
        security_candidates = {"alarm": [], "camera": [], "contact": []}
        for entity in entities.entities.values():
            state = hass.states.get(entity.entity_id)
            for kind in security_candidates:
                if valid_security_entity(entity.entity_id, kind, entity, state):
                    security_candidates[kind].append({
                        "entity_id": entity.entity_id,
                        "name": _name(entity, state, entity.entity_id),
                    })
        for candidates in security_candidates.values():
            candidates.sort(key=lambda item: item["name"].casefold())
        weather_candidates = []
        for entity in entities.entities.values():
            if entity.disabled or not entity.entity_id.startswith("weather."):
                continue
            state = hass.states.get(entity.entity_id)
            weather_candidates.append({
                "entity_id": entity.entity_id,
                "name": _name(entity, state, entity.entity_id),
            })

        result_areas = []
        for area in sorted(areas.areas.values(), key=lambda x: x.name.casefold()):
            area_devices = []
            timer_candidates = []
            washdata_candidates = []
            temperature_candidates = []
            humidity_candidates = []
            climate_candidates = []
            flow_candidates = []
            hero_right_candidates = []

            for entity in entities.entities.values():
                if entity.disabled:
                    continue
                entity_area = entity.area_id or (device_area.get(entity.device_id) if entity.device_id else None)
                if entity_area != area.id:
                    continue
                state = hass.states.get(entity.entity_id)
                if _is_temperature_entity(entity, state):
                    temperature_candidates.append({
                        "entity_id": entity.entity_id,
                        "name": _name(entity, state, entity.entity_id),
                    })
                if _is_humidity_entity(entity, state):
                    humidity_candidates.append({
                        "entity_id": entity.entity_id,
                        "name": _name(entity, state, entity.entity_id),
                    })
                if entity.entity_id.startswith("climate."):
                    climate_candidates.append({
                        "entity_id": entity.entity_id,
                        "name": _name(entity, state, entity.entity_id),
                    })
                domain = entity.entity_id.split(".", 1)[0]
                if domain in {"light", "switch", "cover", "scene", "script"}:
                    hero_right_candidates.append({"entity_id": entity.entity_id, "name": _name(entity, state, entity.entity_id), "domain": domain})
                if domain in {
                    "light", "switch", "cover", "media_player", "scene", "script"
                }:
                    flow_candidates.append({
                        "entity_id": entity.entity_id,
                        "name": _name(entity, state, entity.entity_id),
                        "domain": domain,
                    })

            # Timer helpers often have an area but no device. Helpers without
            # an HA area can be assigned to one CouchMate room below.
            for entity in entities.entities.values():
                if entity.disabled or not entity.entity_id.startswith("timer."):
                    continue
                entity_area = _entity_area_id(entity, devices)
                if entity_area is not None and entity_area != area.id:
                    continue
                state = hass.states.get(entity.entity_id)
                timer_candidates.append({
                    "entity_id": entity.entity_id,
                    "name": _name(entity, state, entity.entity_id),
                    "area_unassigned": entity_area is None,
                })

            for device in devices.devices.values():
                if device.area_id != area.id:
                    continue
                device_entities = []
                for entity in entities.entities.values():
                    if entity.disabled or entity.device_id != device.id:
                        continue
                    state = hass.states.get(entity.entity_id)
                    device_entities.append({
                        "entity_id": entity.entity_id,
                        "name": _name(entity, state, entity.entity_id),
                        "domain": entity.entity_id.split(".", 1)[0],
                        "selected": entity.entity_id in explicit_entities,
                        "excluded": entity.entity_id in current.get("excluded_entities", []),
                    })
                model_device = dict(dict(model_areas.get(area.id, {})).get("devices", {})).get(device.id, {})
                mode = model_device.get("mode") if isinstance(model_device, dict) else None
                if mode not in ("all", "entities"):
                    mode = "entities" if any(item["selected"] for item in device_entities) else ("all" if device.id in fully_selected_devices else "none")
                area_devices.append({
                    "id": device.id,
                    "name": device.name_by_user or device.name or device.id,
                    "manufacturer": device.manufacturer or "",
                    "model": device.model or "",
                    "selection_mode": mode,
                    "selected_count": sum(1 for item in device_entities if item["selected"]),
                    "entities": sorted(device_entities, key=lambda x: x["name"].casefold()),
                })
            # A WashData appliance may have no HA area. Select its display
            # sensors as one group without moving the device in Home Assistant.
            for device in devices.devices.values():
                if washdata_entry_id(device) is None or device.area_id not in (None, area.id):
                    continue
                if not washdata_sensor_entity_ids(device, entities):
                    continue
                washdata_candidates.append({
                    "device_id": device.id,
                    "name": device.name_by_user or device.name or device.id,
                    "area_unassigned": device.area_id is None,
                })
            result_areas.append({
                "id": area.id,
                "name": area.name,
                "selected": bool(model_areas.get(area.id)) or bool(room_temperatures.get(area.id)) or bool(room_humidities.get(area.id)) or bool(room_climates.get(area.id)),
                "temperature_entity": room_temperatures.get(area.id),
                "temperature_candidates": sorted(temperature_candidates, key=lambda x: x["name"].casefold()),
                "humidity_entity": room_humidities.get(area.id),
                "humidity_candidates": sorted(humidity_candidates, key=lambda x: x["name"].casefold()),
                "climate_entity": room_climates.get(area.id),
                "climate_candidates": sorted(climate_candidates, key=lambda x: x["name"].casefold()),
                "thermostat_card_style": (
                    dict(model_areas.get(area.id, {})).get("thermostat_card_style")
                    if dict(model_areas.get(area.id, {})).get("thermostat_card_style")
                    in ("ring", "full_vertical", "full", "compact", "hidden")
                    else None
                ),
                "hero_order": hero_orders.get(area.id, []),
                "hero_right_entities": list(dict(model_areas.get(area.id, {})).get("hero_right_entities", []))[:12],
                "hero_right_candidates": sorted(hero_right_candidates, key=lambda item: item["name"].casefold()),
                "flow_entities": list(
                    dict(model_areas.get(area.id, {})).get("flow_entities", [])
                )[:3],
                "flow_mode": normalize_flow_mode(
                    dict(model_areas.get(area.id, {})).get("flow_mode")
                ),
                "flow_candidates": sorted(
                    flow_candidates,
                    key=lambda x: x["name"].casefold(),
                ),
                "timer_entities": list(
                    dict(model_areas.get(area.id, {})).get("timer_entities", [])
                ),
                "timer_candidates": sorted(
                    timer_candidates,
                    key=lambda x: x["name"].casefold(),
                ),
                "washdata_devices": list(
                    dict(model_areas.get(area.id, {})).get("washdata_devices", [])
                ),
                "washdata_candidates": sorted(
                    washdata_candidates,
                    key=lambda x: x["name"].casefold(),
                ),
                "hero_layout": hero_layouts.get(area.id, {}),
                "devices": sorted(area_devices, key=lambda x: x["name"].casefold()),
            })
        return self.json({
            "areas": result_areas,
            "energy_dashboard": {"enabled": energy_enabled(selection_model)},
            "weather_entity": weather_entity,
            "thermostat_card_style": thermostat_card_style,
            "show_room_name": show_room_name,
            "show_room_climate": show_room_climate,
            "security_dashboard": security_dashboard,
            "security_candidates": security_candidates,
            "weather_candidates": sorted(
                weather_candidates,
                key=lambda item: item["name"].casefold(),
            ),
        })


class CouchMateConfiguratorSaveView(HomeAssistantView):
    url = "/api/couchmate/configurator/save"
    name = "api:couchmate:configurator:save"
    requires_auth = True

    async def post(self, request):
        denied = _admin_required(request)
        if denied is not None:
            return denied
        hass = request.app["hass"]
        payload = await request.json()
        raw_model = dict(payload.get("selection_model", {}))
        raw_areas = dict(raw_model.get("areas", {}))

        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
        area_registry = ar.async_get(hass)

        model_areas: dict[str, dict] = {}
        selected_devices: list[str] = []
        explicit_entities: list[str] = []
        temperatures: dict[str, str] = {}
        humidities: dict[str, str] = {}
        climates: dict[str, str] = {}
        hero_orders = _default_profile_hero_orders(hass)
        hero_layouts = _default_profile_hero_layouts(hass)
        assigned_timers: set[str] = set()
        assigned_washdata_devices: set[str] = set()
        security_dashboard = normalize_security_dashboard(
            raw_model.get("security_dashboard"), entity_registry, getattr(hass, "states", None)
        )

        thermostat_card_style = raw_model.get("thermostat_card_style", "full")
        if thermostat_card_style not in ("ring", "full_vertical", "full", "compact", "hidden"):
            thermostat_card_style = "full"
        show_room_name = raw_model.get("show_room_name", True)
        if not isinstance(show_room_name, bool):
            show_room_name = True
        show_room_climate = raw_model.get("show_room_climate", True)
        if not isinstance(show_room_climate, bool):
            show_room_climate = True

        weather = raw_model.get("weather")
        weather_entry = entity_registry.async_get(str(weather)) if weather else None
        weather_entity = (
            str(weather)
            if weather_entry is not None and str(weather).startswith("weather.")
            else None
        )

        for area_id, raw_area in raw_areas.items():
            area_id = str(area_id)
            if area_registry.async_get_area(area_id) is None or not isinstance(raw_area, dict):
                continue
            area_cfg: dict = {"devices": {}}

            area_cfg["flow_mode"] = normalize_flow_mode(raw_area.get("flow_mode"))

            room_thermostat_card_style = raw_area.get("thermostat_card_style")
            if room_thermostat_card_style in ("ring", "full_vertical", "full", "compact", "hidden"):
                area_cfg["thermostat_card_style"] = room_thermostat_card_style

            temperature = raw_area.get("temperature")
            if temperature and entity_registry.async_get(str(temperature)):
                temperatures[area_id] = str(temperature)
                area_cfg["temperature"] = str(temperature)

            humidity = raw_area.get("humidity")
            if humidity and entity_registry.async_get(str(humidity)):
                humidities[area_id] = str(humidity)
                area_cfg["humidity"] = str(humidity)

            climate = raw_area.get("climate")
            climate_entry = entity_registry.async_get(str(climate)) if climate else None
            if (
                climate_entry is not None
                and str(climate).startswith("climate.")
                and _entity_area_id(climate_entry, device_registry) == area_id
            ):
                climates[area_id] = str(climate)
                area_cfg["climate"] = str(climate)

            for device_id, raw_device in dict(raw_area.get("devices", {})).items():
                device_id = str(device_id)
                device = device_registry.async_get(device_id)
                if device is None or not isinstance(raw_device, dict):
                    continue
                mode = raw_device.get("mode")
                if mode == "all":
                    selected_devices.append(device_id)
                    area_cfg["devices"][device_id] = {"mode": "all", "entities": []}
                    continue
                if mode != "entities":
                    continue
                valid_entities: list[str] = []
                for entity_id in raw_device.get("entities", []):
                    entity_id = str(entity_id)
                    entry = entity_registry.async_get(entity_id)
                    if entry is None or entry.device_id != device_id:
                        continue
                    valid_entities.append(entity_id)
                valid_entities = list(dict.fromkeys(valid_entities))
                if valid_entities:
                    explicit_entities.extend(valid_entities)
                    area_cfg["devices"][device_id] = {"mode": "entities", "entities": valid_entities}

            valid_hero_order: list[str] = []
            for entity_id in raw_area.get("hero_order", []):
                entity_id = str(entity_id)
                entry = entity_registry.async_get(entity_id)
                if entry is None or not entity_id.startswith(("light.", "switch.")):
                    continue
                if _entity_area_id(entry, device_registry) != area_id:
                    continue
                valid_hero_order.append(entity_id)
            valid_hero_order = list(dict.fromkeys(valid_hero_order))
            if valid_hero_order:
                hero_orders[area_id] = valid_hero_order
                area_cfg["hero_order"] = valid_hero_order
            else:
                hero_orders.pop(area_id, None)

            # Explicit, room-local right-column content. It is independent of
            # automatic media/camera placement and of the middle device order.
            raw_right = raw_area.get("hero_right_entities", [])
            valid_right: list[str] = []
            if isinstance(raw_right, list):
                for entity_id in raw_right:
                    if not isinstance(entity_id, str) or entity_id in valid_right:
                        continue
                    entry = entity_registry.async_get(entity_id)
                    if entry is None or entry.disabled or not entity_id.startswith(("light.", "switch.", "cover.", "scene.", "script.")):
                        continue
                    if _entity_area_id(entry, device_registry) != area_id:
                        continue
                    valid_right.append(entity_id)
                    if len(valid_right) == 12:
                        break
            if valid_right:
                area_cfg["hero_right_entities"] = valid_right
                explicit_entities.extend(valid_right)
                valid_hero_order = [entity_id for entity_id in valid_hero_order if entity_id not in valid_right]
                if valid_hero_order:
                    area_cfg["hero_order"] = valid_hero_order
                    hero_orders[area_id] = valid_hero_order
                else:
                    area_cfg.pop("hero_order", None)
                    hero_orders.pop(area_id, None)

            valid_flow_entities: list[str] = []
            for entity_id in raw_area.get("flow_entities", [])[:3]:
                entity_id = str(entity_id)
                entry = entity_registry.async_get(entity_id)
                if entry is None or entry.disabled:
                    continue
                if not entity_id.startswith((
                    "light.", "switch.", "cover.", "media_player.",
                    "scene.", "script."
                )):
                    continue
                if _entity_area_id(entry, device_registry) != area_id:
                    continue
                valid_flow_entities.append(entity_id)
            valid_flow_entities = list(dict.fromkeys(valid_flow_entities))[:3]
            if valid_flow_entities:
                area_cfg["flow_entities"] = valid_flow_entities
                explicit_entities.extend(valid_flow_entities)

            valid_timers: list[str] = []
            raw_timers = raw_area.get("timer_entities", [])
            if isinstance(raw_timers, list):
                for timer_id in raw_timers:
                    if not isinstance(timer_id, str) or not timer_id.startswith("timer."):
                        continue
                    timer_entry = entity_registry.async_get(timer_id)
                    if timer_entry is None or timer_entry.disabled or timer_id in assigned_timers:
                        continue
                    timer_area = _entity_area_id(timer_entry, device_registry)
                    if timer_area is not None and timer_area != area_id:
                        continue
                    valid_timers.append(timer_id)
                    assigned_timers.add(timer_id)
            if valid_timers:
                area_cfg["timer_entities"] = valid_timers
                explicit_entities.extend(valid_timers)

            valid_washdata_devices: list[str] = []
            raw_washdata_devices = raw_area.get("washdata_devices", [])
            if isinstance(raw_washdata_devices, list):
                for raw_device_id in raw_washdata_devices:
                    if not isinstance(raw_device_id, str) or raw_device_id in assigned_washdata_devices:
                        continue
                    device = device_registry.async_get(raw_device_id)
                    if (
                        device is None
                        or device.area_id not in (None, area_id)
                        or washdata_entry_id(device) is None
                    ):
                        continue
                    sensors = washdata_sensor_entity_ids(device, entity_registry)
                    if not sensors:
                        continue
                    valid_washdata_devices.append(raw_device_id)
                    assigned_washdata_devices.add(raw_device_id)
                    explicit_entities.extend(sensors.values())
            if valid_washdata_devices:
                area_cfg["washdata_devices"] = valid_washdata_devices

            raw_hero_layout = raw_area.get("hero_layout", {})
            if isinstance(raw_hero_layout, dict):
                allowed_layout_styles = {
                    "thermostat_card_style": {
                        "ring", "full_vertical", "full", "compact", "hidden"
                    },
                    "device_card_style": {"bubble", "tile", "toggle", "icon"},
                    "camera_card_style": {"large", "compact"},
                    "media_card_style": {"transport", "compact"},
                }
                hero_layout = {
                    key: value
                    for key, allowed in allowed_layout_styles.items()
                    if isinstance((value := raw_hero_layout.get(key)), str)
                    and value in allowed
                }
                if hero_layout:
                    hero_layouts[area_id] = hero_layout

            if area_cfg.get("temperature") or area_cfg.get("humidity") or area_cfg.get("climate") or area_cfg.get("thermostat_card_style") or area_cfg.get("hero_order") or area_cfg.get("flow_entities") or area_cfg.get("hero_right_entities") or area_cfg.get("timer_entities") or area_cfg.get("washdata_devices") or area_cfg["flow_mode"] != "automatic" or area_cfg["devices"]:
                model_areas[area_id] = area_cfg

        selected_devices = list(dict.fromkeys(selected_devices))
        explicit_entities = list(dict.fromkeys(explicit_entities))
        selection_model = {
            "version": SELECTION_MODEL_VERSION,
            "energy_dashboard": {
                "enabled": raw_model.get("energy_dashboard", {}).get("enabled") is True
                if isinstance(raw_model.get("energy_dashboard"), dict) else False,
            },
            "weather": weather_entity,
            "thermostat_card_style": thermostat_card_style,
            "show_room_name": show_room_name,
            "show_room_climate": show_room_climate,
            "areas": model_areas,
            "security_dashboard": security_dashboard,
        }

        # Version 2 is authoritative: no broad area selection is stored. A
        # device is expanded only when its explicit mode is "all"; otherwise
        # only the exact checked entity ids are exposed.
        data = {
            CONF_AREAS: [],
            CONF_DEVICES: selected_devices,
            CONF_ENTITIES: explicit_entities,
            CONF_EXCLUDED_ENTITIES: [],
            CONF_ROOM_TEMPERATURES: temperatures,
            CONF_ROOM_HUMIDITIES: humidities,
            CONF_ROOM_CLIMATES: climates,
            CONF_WEATHER_ENTITY: weather_entity,
            CONF_SELECTION_MODEL: selection_model,
        }
        await async_save_entities(hass, data)
        await _save_default_profile_hero_configuration(
            hass,
            hero_orders,
            hero_layouts,
        )
        from . import _resolve_filter
        resolved = sorted(_resolve_filter(
            hass,
            areas=[],
            devices=selected_devices,
            entities=explicit_entities,
            excluded_entities=[],
        ))
        runtime = hass.data.setdefault(DOMAIN, {})
        runtime.update({
            "areas": [],
            "devices": selected_devices,
            "explicit_entities": explicit_entities,
            "excluded_entities": [],
            "room_temperatures": temperatures,
            "room_humidities": humidities,
            "room_climates": climates,
            "weather_entity": weather_entity,
            "selection_model": selection_model,
            "entities": resolved,
        })
        runtime.pop("energy_dashboard_cache", None)
        runtime["energy_dashboard_generation"] = runtime.get("energy_dashboard_generation", 0) + 1
        entry = runtime.get("entry")
        if entry:
            hass.config_entries.async_update_entry(entry, data=data)
        return self.json({
            "success": True,
            "resolved_count": len(resolved),
            "temperature_count": len(temperatures),
            "humidity_count": len(humidities),
            "climate_count": len(climates),
            "hero_order_count": len(hero_orders),
            "weather_configured": weather_entity is not None,
            "area_count": len(model_areas),
            "device_count": len(selected_devices),
            "entity_count": len(explicit_entities),
        })


async def async_setup_configurator(hass):
    hass.http.register_view(CouchMateConfiguratorView())
    hass.http.register_view(CouchMateConfiguratorDataView())
    hass.http.register_view(CouchMateConfiguratorSaveView())


HTML = r'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CouchMate Core Dev Preview – Geräte &amp; Funktionen</title><style>
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:20px}.tile{border:1px solid var(--line);border-radius:26px;background:var(--card);padding:26px;cursor:pointer;text-align:left;color:inherit;min-height:190px;transition:.16s ease}.tile:hover{border-color:#607078;background:var(--card2);transform:translateY(-1px)}.tile.active{outline:3px solid var(--accent);background:#17312e}.tile.partial{outline:2px solid var(--accent);background:#172523}.tile .state{display:inline-block;margin-top:12px;padding:6px 10px;border-radius:999px;background:#111719;color:var(--muted);font-size:14px}.tile.active .state,.tile.partial .state{color:var(--accent)}.icon{width:48px;height:48px;color:var(--accent);margin-bottom:26px}.icon svg{width:100%;height:100%;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.choice-grid,.entities{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}.choice,.entity{display:flex;gap:16px;align-items:center;border:1px solid var(--line);border-radius:20px;padding:21px 22px;background:var(--card);min-height:104px;cursor:pointer;overflow:hidden}.choice:hover,.entity:hover{background:var(--card2)}.choice input,.entity input{width:22px;height:22px;accent-color:var(--accent);flex:0 0 auto}.choice b,.entity b{font-size:19px}.choice .sub,.entity .sub{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block;max-width:100%}.config-panel{border:1px solid var(--line);border-radius:26px;padding:24px;background:#141a1d;margin:0 0 32px}.config-panel h3{margin:0 0 6px;font-size:21px}.config-panel p{margin:0 0 20px}.card-select{width:100%;margin-top:8px;border:1px solid var(--line);border-radius:12px;background:var(--card2);color:#fff;padding:12px;font:inherit}.order-list{display:grid;gap:10px}.order-row{display:flex;align-items:center;gap:14px;border:1px solid var(--line);border-radius:18px;background:var(--card);padding:14px 16px}.order-row .copy{min-width:0;flex:1}.order-row b,.order-row .sub{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.order-actions{display:flex;gap:8px}.order-actions button{width:42px;height:42px;border:1px solid var(--line);border-radius:12px;background:var(--card2);color:#fff;font-size:20px;cursor:pointer}.order-actions button:disabled{opacity:.3;cursor:default}.hidden{display:none!important}.crumb{color:var(--muted);margin:10px 0 28px;font-size:19px}.back{background:none;border:0;color:var(--accent);font-size:18px;cursor:pointer;padding:0}.toast{position:fixed;right:28px;bottom:28px;max-width:520px;border:1px solid var(--line);border-radius:20px;padding:18px 22px;background:#20282c;box-shadow:0 18px 50px rgba(0,0,0,.35);z-index:20}.toast.ok{border-color:var(--ok)}.toast.error{border-color:var(--danger)}.toast strong{display:block;font-size:18px;margin-bottom:5px}@media(max-width:700px){.grid,.choice-grid{grid-template-columns:1fr}.toast{left:18px;right:18px;bottom:18px}}
</style>
<style>
#thermostatStyleGrid,#roomThermostatStyleGrid{grid-template-columns:repeat(2,minmax(0,1fr))}.thermostat-choice{display:grid;grid-template-columns:24px minmax(0,1fr);align-items:start;min-height:0;padding:20px}.thermostat-choice:has(input:checked){border-color:var(--accent);box-shadow:0 0 0 2px rgba(var(--accent-rgb),.22);background:#172523}.thermostat-choice>input{margin-top:3px}.thermostat-choice .choice-copy{display:block;min-width:0}.thermostat-choice .choice-copy>.sub{white-space:normal;margin-top:4px}.thermostat-preview{margin-top:16px;border:1px solid #526067;border-radius:20px;background:linear-gradient(145deg,#252c30,#181d20);padding:16px;color:#f8fbfb;box-shadow:0 12px 28px rgba(0,0,0,.22);cursor:pointer}.thermo-head{display:flex;align-items:center;gap:9px;font-size:14px;font-weight:750}.thermo-head .power{margin-left:auto;color:var(--accent);font-size:19px}.thermo-icon{width:21px;height:21px;color:var(--accent);flex:0 0 auto}.thermo-icon svg{width:100%;height:100%;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.thermo-body{display:grid;gap:12px;margin-top:16px}.thermo-reading{border-radius:15px;background:rgba(255,255,255,.045);padding:12px}.thermo-label{display:block;color:var(--muted);font-size:10px;font-weight:750;letter-spacing:.09em}.thermo-number{display:block;margin-top:3px;font-size:28px;font-weight:780;letter-spacing:-.04em}.thermo-target{display:flex;align-items:center;justify-content:space-between;gap:8px}.thermo-step{display:grid;place-items:center;width:27px;height:27px;border-radius:9px;background:#313b40;color:var(--accent);font-size:18px;font-weight:650}.thermo-metrics{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.thermo-chip{display:inline-flex;align-items:center;gap:5px;padding:6px 9px;border-radius:999px;background:#101618;color:#cdd5d8;font-size:11px}.thermo-chip.accent{background:rgba(var(--accent-rgb),.14);color:var(--accent)}.thermo-chip svg{width:13px;height:13px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}.thermo-modes{display:flex;gap:7px;margin-top:10px}.thermo-mode{padding:6px 9px;border-radius:9px;background:#313b40;color:#cdd5d8;font-size:10px}.thermo-mode.active{background:var(--accent);color:#071310;font-weight:750}.preview-full_vertical{max-width:300px;min-height:228px}.preview-full_vertical .thermo-body{grid-template-columns:1fr 1fr}.preview-full_vertical .thermo-metrics{grid-column:1/-1}.preview-full{min-height:148px}.preview-full .thermo-body{grid-template-columns:.8fr 1fr 1.15fr;align-items:stretch}.preview-full .thermo-modes{grid-column:1/-1}.preview-compact{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"head chip" "value target";align-items:center;gap:10px 12px;min-height:92px}.preview-compact .thermo-head{grid-area:head;min-width:0}.preview-compact .compact-value{grid-area:value;font-size:24px;font-weight:780;white-space:nowrap}.preview-compact .compact-target{grid-area:target;display:flex;align-items:center;gap:6px}.preview-compact>.thermo-chip{grid-area:chip;justify-self:end}.preview-hidden{display:grid;gap:10px;background:transparent;border-style:dashed;box-shadow:none}.preview-hidden .hidden-card{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:13px}.preview-hidden .room-control{border:1px solid #526067;border-radius:13px;background:#20272a;padding:11px 13px;text-align:center;color:#f4f7f7;font-size:12px;font-weight:750}.preview-inherit{display:flex;align-items:center;gap:12px;min-height:78px;border-style:dashed;background:rgba(255,255,255,.025);box-shadow:none}.preview-inherit .inherit-symbol{display:grid;place-items:center;width:34px;height:34px;border-radius:11px;background:rgba(var(--accent-rgb),.12);color:var(--accent);font-size:20px}.preview-inherit .inherit-copy{font-size:12px;color:var(--muted)}.preview-inherit .inherit-copy strong{display:block;color:#fff;font-size:14px;margin-bottom:2px}@media(max-width:900px){#thermostatStyleGrid,#roomThermostatStyleGrid{grid-template-columns:1fr}}@media(max-width:700px){.thermostat-choice{grid-template-columns:22px minmax(0,1fr);padding:17px}.preview-full .thermo-body{grid-template-columns:1fr 1fr}.preview-full .thermo-metrics,.preview-full .thermo-modes{grid-column:1/-1}.preview-full_vertical{max-width:none}}
.preview-full_vertical{min-height:300px}.preview-full_vertical .thermo-body{grid-template-columns:1fr}.preview-full_vertical .thermo-metrics{grid-column:auto}
.preview-ring{display:grid;justify-items:center;gap:12px;min-height:284px}.preview-ring .thermo-head{width:100%}.thermo-ring-dial{position:relative;display:grid;place-items:center;width:166px;height:166px;border-radius:50%;background:conic-gradient(var(--accent) 0 72%,#354147 72% 100%);box-shadow:0 0 30px rgba(var(--accent-rgb),.12)}.thermo-ring-dial::after{content:"";position:absolute;inset:11px;border-radius:50%;background:#1a2023;box-shadow:inset 0 0 0 1px rgba(255,255,255,.06)}.thermo-ring-copy{position:relative;z-index:1;text-align:center}.thermo-ring-copy .thermo-label{margin-bottom:2px}.thermo-ring-target{font-size:34px;font-weight:780;letter-spacing:-.04em}.thermo-ring-current{display:block;margin-top:3px;color:var(--muted);font-size:11px}.thermo-ring-controls{display:flex;align-items:center;justify-content:center;gap:20px}.thermo-ring-controls .thermo-step{width:34px;height:34px;border-radius:50%}
.preview-ring{width:340px;max-width:100%}
.choice{align-items:flex-start}.choice>span{min-width:0}.choice .sub{white-space:normal;overflow:visible;text-overflow:clip}.entity{align-items:center}.entity .sub{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
</style><style>
.flow-entry,.flow-content-heading{display:flex;align-items:center;justify-content:space-between;gap:20px}.flow-content-heading h2{margin:0}.flow-content-heading{margin-bottom:12px}.flow-entry .button{flex-shrink:0}.button.secondary{background:var(--card2);color:#fff;border:1px solid var(--line)}.flow-room-label{display:block;font-size:19px;font-weight:700}.flow-mode-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.flow-mode-grid .choice:has(input:checked){outline:2px solid var(--accent);background:#172523}.flow-preview{margin-top:24px;padding:20px;border:1px dashed var(--line);border-radius:18px;text-align:center}.flow-preview-hero{padding:20px;border-radius:12px;background:var(--card2);color:var(--muted);font-size:14px}.flow-preview-deck{display:flex;gap:8px;margin-top:10px;padding:8px;border:1px solid var(--line);border-radius:16px}.flow-preview-item{flex:1;min-width:0;padding:12px 8px;border-radius:10px;background:var(--card2);overflow-wrap:anywhere;font-size:14px}.flow-preview .sub{margin-top:12px;font-size:14px}.flow-candidates-heading{margin:26px 0 14px!important}.entity>span{min-width:0}.flow-remove{width:auto!important;padding:0 12px;font-size:14px!important}.flow-unavailable{color:var(--warning)}
@media(max-width:900px){.flow-mode-grid{grid-template-columns:1fr}}@media(max-width:760px){.flow-entry{align-items:stretch;flex-direction:column}.entities{grid-template-columns:minmax(0,1fr)}.flow-preview-deck{flex-direction:column}.flow-selected-row{flex-wrap:wrap}.flow-selected-row .order-actions{width:100%;justify-content:flex-end}}
</style><!--CORE_PAGE_STYLES--></head><body><main class="wrap">
<!--CORE_PAGE_HEADER-->
<section id="areas" class="section"><div id="energyDashboardPanel" class="config-panel"><h3>Energie-Dashboard</h3><p class="sub">Optionales Dashboard mit heutigen Stromsummen und Tagesverlauf aus der bestehenden Home-Assistant-Energy-Konfiguration. Dazu müssen Energiequellen und Recorder-Statistiken in Home Assistant eingerichtet sein.</p><label class="choice"><input id="energyDashboardEnabled" type="checkbox"><span><b>Energie-Dashboard anzeigen</b><span class="sub">Verwendet Netzbezug, Einspeisung, Solar- und Batteriespeicherwerte aus Home Assistant Energy. Es werden keine Steuerbefehle gesendet.</span></span></label></div><div id="securityDashboardPanel" class="config-panel"><h3>Sicherheits-Dashboard</h3><p class="sub">Optionales Dashboard für Alarmanlagen, Kamerabilder und Tür-/Fensterkontakte. Die Ansicht zeigt nur die hier gewählten Quellen. Eine PIN wird nie gespeichert.</p><label class="choice"><input id="securityDashboardEnabled" type="checkbox"><span><b>Sicherheits-Dashboard anzeigen</b><span class="sub">Scharf-/Unscharfschalten ist nur für ausdrücklich gewählte Alarmanlagen möglich.</span></span></label><h4>Alarmanlagen</h4><div id="securityAlarmGrid" class="entities"></div><h4>Kameras und Bilder</h4><div id="securityCameraGrid" class="entities"></div><h4>Tür- und Fensterkontakte</h4><div id="securityContactGrid" class="entities"></div></div><div id="weatherPanel" class="config-panel"><h3>Wettervorhersage</h3><p class="sub">Wähle optional eine globale Wetter-Entität für Uhrzeit und Forecast auf allen Apple TVs.</p><div id="weatherGrid" class="choice-grid"></div></div><div id="thermostatStylePanel" class="config-panel"><h3>Hero-Darstellung</h3><p class="sub">Der Thermostat-Standard gilt für alle Räume und kann im Raum gezielt überschrieben werden.</p><div id="thermostatStyleGrid" class="choice-grid"></div><div class="choice-grid" style="margin-top:16px"><label class="choice"><input id="showRoomName" type="checkbox"><span><b>Raumname anzeigen</b><span class="sub">Blendet nur den Raumnamen oberhalb des Hero ein oder aus.</span></span></label><label class="choice"><input id="showRoomClimate" type="checkbox"><span><b>Raumklima anzeigen</b><span class="sub">Blendet Temperatur und Humidity-Icon unabhängig vom Raumnamen ein oder aus.</span></span></label></div></div><h2>Räume</h2><div id="areaGrid" class="grid"></div></section>
<section id="devices" class="section hidden"><button class="back" id="backAreas">← Räume</button><div class="crumb" id="areaName"></div>
<div class="config-panel flow-entry"><div><h3>Flow-Leiste</h3><div class="sub">Inhalte unter der Hero Card für diesen Raum festlegen.</div></div><button id="openRoomFlow" class="button secondary" type="button">Flow konfigurieren</button></div>
<div id="climatePanel" class="config-panel"><h3>Thermostat</h3><p class="sub">Dieses Thermostat wird direkt im Hero bedienbar. Ohne Auswahl verwendet CouchMate automatisch das einzige freigegebene Thermostat.</p><div id="climateGrid" class="choice-grid"></div></div>
<div id="roomThermostatStylePanel" class="config-panel"><h3>Darstellung im Hero</h3><p class="sub">Optional kann dieser Raum vom globalen Thermostat-Layout abweichen.</p><div id="roomThermostatStyleGrid" class="choice-grid"></div></div>
<div id="heroCardStylePanel" class="config-panel"><h3>Kartenarten im Hero</h3><p class="sub">Links sind 340 pt reserviert, rechts 410 pt. Geräte verwenden maximal zwei Spalten mit je 190 pt. Diese Auswahl wird auch von der Companion gespiegelt.</p><div class="choice-grid"><label class="choice"><span><b>Geräte · 190 pt je Karte</b><select id="deviceCardStyle" class="card-select"><option value="bubble">Bubble</option><option value="tile">Kachel</option><option value="toggle">Schalter</option><option value="icon">Nur Icon</option></select></span></label><label class="choice"><span><b>Kamera · 410 pt</b><select id="cameraCardStyle" class="card-select"><option value="large">Große Vorschau</option><option value="compact">Kompakt</option></select></span></label><label class="choice"><span><b>Media · 410 pt</b><select id="mediaCardStyle" class="card-select"><option value="transport">Mit Steuerung</option><option value="compact">Kompakt</option></select></span></label></div></div>
<div id="temperaturePanel" class="config-panel"><h3>Raumtemperatur</h3><p class="sub">Eine explizite Quelle überschreibt den Istwert des Thermostats.</p><div id="temperatureGrid" class="choice-grid"></div></div>
<div id="humidityPanel" class="config-panel"><h3>Luftfeuchtigkeit</h3><p class="sub">Eine explizite Quelle überschreibt die Luftfeuchte des Thermostats.</p><div id="humidityGrid" class="choice-grid"></div></div>
<div id="timerPanel" class="config-panel"><h3>Timer</h3><p class="sub">Wähle Home-Assistant-Timer für diesen Raum. Timer ohne Home-Assistant-Bereich kannst du hier genau einem Raum zuordnen.</p><div id="timerGrid" class="entities"></div><p id="timerEmpty" class="sub hidden">Keine Timer-Helfer vorhanden. Erstelle einen Timer unter Einstellungen → Geräte &amp; Dienste → Helfer.</p></div>
<div id="washdataPanel" class="config-panel"><h3>WashData-Geräte</h3><p class="sub">Wähle Waschmaschine, Trockner oder Spülmaschine für die Restzeit-Anzeige. Geräte ohne Home-Assistant-Bereich kannst du hier einem CouchMate-Raum zuordnen.</p><div id="washdataGrid" class="entities"></div><p id="washdataEmpty" class="sub hidden">Keine WashData-Geräte vorhanden.</p></div>
<div id="heroOrderPanel" class="config-panel"><h3>Anordnung im Hero</h3><p class="sub">Sortiere die ausgewählten Lichter und Schalter. Dieselbe Reihenfolge kann später auch in der Companion geändert werden.</p><div id="heroOrderGrid" class="order-list"></div></div>

<div id="heroRightPanel" class="config-panel"><h3>Rechter Hero-Bereich</h3><p class="sub">Eigene Lichter, Schalter, Rollläden, Szenen und Skripte unter Kamera und Mediaplayern. Ohne Auswahl bleibt der freie Bereich leer. Geräte erscheinen in zwei Spalten mit je 190 pt Breite. Überschüssige Karten bleiben über die Raumsteuerung erreichbar. XXL ist nur ohne weitere Karten in diesem Bereich verfügbar.</p><div id="heroRightSelection" class="order-list"></div><h3>Funktionen auswählen</h3><div id="heroRightGrid" class="entities"></div></div>
<h2>Geräte</h2><div id="deviceGrid" class="grid"></div></section>
<section id="entities" class="section hidden"><button class="back" id="backDevices">← Geräte</button><div class="crumb" id="deviceName"></div><div class="config-panel"><label class="entity"><input id="selectWholeDevice" type="checkbox"><span><b>Ganzes Gerät verwenden</b><span class="sub">Alle aktuellen und zukünftigen Funktionen dieses Geräts an CouchMate übertragen.</span></span></label></div><h2>Funktionen</h2><div id="entityGrid" class="entities"></div></section>
<section id="flowSection" class="section hidden" aria-label="Flow konfigurieren">
<div class="config-panel"><label class="flow-room-label" for="flowRoomSelect">Raum auswählen</label><select id="flowRoomSelect" class="card-select"></select></div>
<div id="flowNoRooms" class="config-panel hidden"><h2>Noch keine Räume vorhanden</h2><p class="sub">Lege in Home Assistant einen Bereich an und ordne ihm Geräte oder Funktionen zu. Anschließend kannst du hier seine Flow-Leiste konfigurieren.</p></div>
<div id="flowSettings">
<div class="config-panel"><h2>Darstellung der Flow-Leiste</h2><div id="flowModeGrid" class="choice-grid flow-mode-grid">
<label class="choice"><input type="radio" name="flowMode" value="automatic"><span><b>Automatisch</b><span class="sub">Passende Aktionen erscheinen automatisch. Deine Inhalte füllen bis zu drei freie Plätze.</span></span></label>
<label class="choice"><input type="radio" name="flowMode" value="custom"><span><b>Eigene Inhalte</b><span class="sub">Zeigt ausschließlich deine Auswahl in der von dir festgelegten Reihenfolge.</span></span></label>
<label class="choice"><input type="radio" name="flowMode" value="hidden"><span><b>Ausblenden</b><span class="sub">Keine Flow-Leiste in diesem Raum. Deine Inhaltsauswahl bleibt gespeichert.</span></span></label>
</div><div id="flowPreview" class="flow-preview" aria-label="Vorschau der Flow-Leiste"></div></div>
<div id="flowPanel" class="config-panel"><div class="flow-content-heading"><h2>Inhalte</h2><span id="flowCount" class="sub" aria-live="polite"></span></div><p id="flowContentHint" class="sub"></p><div id="flowSelection" class="order-list"></div><h3 class="flow-candidates-heading">Verfügbare Funktionen</h3><p id="flowNoCandidates" class="sub hidden">Diesem Raum sind noch keine geeigneten Funktionen zugeordnet. Ordne ihm in Home Assistant Lichter, Schalter, Rollläden, Mediaplayer, Szenen oder Skripte zu.</p><div id="flowGrid" class="entities"></div></div>
</div></section>
</main><div id="toast" class="toast hidden" role="status" aria-live="polite"></div><script>
let data,area,device,flowArea;
const selected={energyEnabled:false,securityEnabled:false,securityAlarms:[],securityCameras:[],securityContacts:[],devices:new Map(),temperatures:{},humidities:{},climates:{},timers:{},washdata:{},orders:{},right:{},flows:{},flowModes:{},layouts:{},thermostatStyle:'full',thermostatStyles:{},showRoomName:true,showRoomClimate:true,weather:null};
const paths={room:'<path d="M4 10.5 12 4l8 6.5V20H4z"/><path d="M9 20v-6h6v6"/>',light:'<path d="M9 18h6"/><path d="M10 22h4"/><path d="M8.5 14.5A6 6 0 1 1 15.5 14.5c-1 .8-1.5 1.8-1.5 3h-4c0-1.2-.5-2.2-1.5-3Z"/>',switch:'<path d="M7 2v5M17 2v5"/><path d="M5 7h14v7a7 7 0 0 1-14 0Z"/><path d="M9 21h6"/>',media_player:'<rect x="3" y="5" width="18" height="13" rx="2"/><path d="m10 9 5 2.5-5 2.5Z"/><path d="M8 22h8"/>',climate:'<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0Z"/><path d="M12 11v6"/>',cover:'<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M4 8h16M4 13h16M4 18h16"/>',sensor:'<path d="M4 19V5M4 19h16"/><path d="m7 15 3-4 3 2 5-7"/>',default:'<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M9 9h6v6H9z"/>'};
function icon(kind){return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[kind]||paths.default}</svg>`}
function roomKind(name){const n=name.toLowerCase();if(n.includes('wohn'))return 'media_player';if(n.includes('küche')||n.includes('kueche'))return 'switch';if(n.includes('schlaf'))return 'light';if(n.includes('bad')||n.includes('wc'))return 'climate';if(n.includes('garten'))return 'sensor';if(n.includes('garage'))return 'cover';return 'room'}
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const auth=()=>{try{const t=JSON.parse(localStorage.getItem('hassTokens')||'{}');return t.access_token?{'Authorization':'Bearer '+t.access_token}:{} }catch(e){return {}}};
async function api(url,options={}){options.headers={...(options.headers||{}),...auth()};const r=await fetch(url,options);if(r.status===401||r.status===403)throw new Error('AUTH');if(!r.ok)throw new Error('HTTP '+r.status);return r}
function showToast(type,title,text){toast.className='toast '+type;toast.innerHTML=`<strong>${esc(title)}</strong><span>${esc(text)}</span>`;clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>toast.classList.add('hidden'),6000)}
function deviceState(d){return selected.devices.get(d.id)||{mode:'none',entities:new Set()}}
function setDeviceState(id,state){if(state.mode==='none'||(state.mode==='entities'&&!state.entities.size))selected.devices.delete(id);else selected.devices.set(id,state)}
function tile(title,sub,kind,state,fn){const b=document.createElement('button');const cls=state==='all'?' active':state==='partial'?' partial':'';b.className='tile'+cls;const status=state==='all'?'Alle Funktionen':state==='partial'?sub.selection:'';b.innerHTML=`<div class="icon">${icon(kind)}</div><h3>${esc(title)}</h3><div class="sub">${esc(sub.text)}</div>${status?`<span class="state">${esc(status)}</span>`:''}`;b.onclick=fn;return b}
function renderChoices(panel,grid,candidates,current,setValue,group,noneTitle,noneSubtitle){grid.innerHTML='';panel.classList.remove('hidden');const choices=[{entity_id:null,name:noneTitle,subtitle:noneSubtitle},...candidates];for(const choice of choices){const row=document.createElement('label');row.className='choice';row.innerHTML=`<input type="radio" name="${group}"><span><b>${esc(choice.name)}</b><span class="sub">${esc(choice.subtitle??choice.entity_id)}</span></span>`;const input=row.querySelector('input');input.checked=(current??null)===(choice.entity_id??null);input.onchange=()=>setValue(choice.entity_id);grid.append(row)}}
function thermostatPreview(style){
  const climateIcon='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0Z"/><path d="M12 11v6"/></svg>';
  const dropIcon='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3s6 6.2 6 11a6 6 0 0 1-12 0c0-4.8 6-11 6-11Z"/></svg>';
  if(style===null)return `<div class="thermostat-preview preview-inherit"><span class="inherit-symbol">↳</span><span class="inherit-copy"><strong>Globale Karte</strong>Zeigt hier die oben gewählte Darstellung.</span></div>`;
  if(style==='hidden')return `<div class="thermostat-preview preview-hidden"><div class="hidden-card"><span class="thermo-icon">${climateIcon}</span><span>Thermostat wird nicht angezeigt</span></div><div class="room-control">Raumsteuerung öffnen</div></div>`;
  if(style==='ring')return `<div class="thermostat-preview preview-ring"><div class="thermo-head"><span class="thermo-icon">${climateIcon}</span><span>Raumthermostat</span><span class="thermo-chip">${dropIcon}48 %</span></div><div class="thermo-ring-dial"><div class="thermo-ring-copy"><span class="thermo-label">SOLL</span><span class="thermo-ring-target">22°</span><span class="thermo-ring-current">Ist 21,5° · Heizt</span></div></div><div class="thermo-ring-controls"><span class="thermo-step">−</span><span class="thermo-mode active">Auto</span><span class="thermo-step">+</span></div></div>`;
  if(style==='compact')return `<div class="thermostat-preview preview-compact"><div class="thermo-head"><span class="thermo-icon">${climateIcon}</span><span>Raumthermostat</span></div><span class="compact-value">21,5°</span><div class="compact-target"><span class="thermo-step">−</span><strong>22°</strong><span class="thermo-step">+</span></div><span class="thermo-chip">${dropIcon}48 %</span></div>`;
  return `<div class="thermostat-preview preview-${style}"><div class="thermo-head"><span class="thermo-icon">${climateIcon}</span><span>Raumthermostat</span><span class="power">⌁</span></div><div class="thermo-body"><div class="thermo-reading"><span class="thermo-label">IST</span><span class="thermo-number">21,5°</span></div><div class="thermo-reading thermo-target"><span class="thermo-step">−</span><span><span class="thermo-label">SOLL</span><strong>22°</strong></span><span class="thermo-step">+</span></div><div class="thermo-metrics"><span class="thermo-chip">${dropIcon}48 %</span><span class="thermo-chip accent">● Heizt</span></div><div class="thermo-modes"><span class="thermo-mode active">Auto</span><span class="thermo-mode">Heizen</span><span class="thermo-mode">Aus</span></div></div></div>`;
}
function renderThermostatStyles(panel,grid,current,setValue,group,allowInherit){
  grid.innerHTML='';panel.classList.remove('hidden');const choices=[];
  if(allowInherit)choices.push({id:null,name:'Globalen Standard verwenden',subtitle:'Übernimmt die Auswahl auf der Raumübersicht.'});
  choices.push(
    {id:'ring',name:'Thermostat-Ring',subtitle:'Runde Sollwertsteuerung mit Istwert, Humidity-Icon und Heizstatus.'},
    {id:'full_vertical',name:'Vollständig · vertikal',subtitle:'Istwert, Sollwert, Humidity-Icon, Heizstatus und Modi untereinander auf 340 pt Breite.'},
    {id:'full',name:'Vollständig',subtitle:'Istwert, Sollwert, Humidity-Icon, Heizstatus und verfügbare Modi.'},
    {id:'compact',name:'Kompakt',subtitle:'Schmale Temperatursteuerung mit weniger Zusatzinformationen.'},
    {id:'hidden',name:'Ausblenden',subtitle:'Kein Thermostat im Hero; die Raumsteuerung bleibt verfügbar.'}
  );
  for(const choice of choices){const row=document.createElement('label');row.className='choice thermostat-choice';row.innerHTML=`<input type="radio" name="${group}"><span class="choice-copy"><b>${esc(choice.name)}</b><span class="sub">${esc(choice.subtitle)}</span>${thermostatPreview(choice.id)}</span>`;const input=row.querySelector('input');input.checked=(current??null)===(choice.id??null);input.onchange=()=>setValue(choice.id);grid.append(row)}
}
function roomHeroLayout(a){return selected.layouts[a.id]||(selected.layouts[a.id]={device_card_style:'bubble',camera_card_style:'large',media_card_style:'transport'})}
function renderHeroCardStyles(){const layout=roomHeroLayout(area);deviceCardStyle.value=layout.device_card_style||'bubble';cameraCardStyle.value=layout.camera_card_style||'large';mediaCardStyle.value=layout.media_card_style||'transport';deviceCardStyle.onchange=()=>layout.device_card_style=deviceCardStyle.value;cameraCardStyle.onchange=()=>layout.camera_card_style=cameraCardStyle.value;mediaCardStyle.onchange=()=>layout.media_card_style=mediaCardStyle.value}
function areaSelectionState(a){let count=0,all=0;for(const d of a.devices){const st=deviceState(d);if(st.mode==='all')all++;else if(st.mode==='entities')count+=st.entities.size}if(all)return 'partial';if(count||selected.temperatures[a.id]||selected.humidities[a.id]||selected.climates[a.id]||selected.thermostatStyles[a.id]||(selected.timers[a.id]||[]).length||(selected.washdata[a.id]||[]).length||(selected.orders[a.id]||[]).length||(selected.right[a.id]||[]).length||(selected.flows[a.id]||[]).length||['custom','hidden'].includes(selected.flowModes[a.id]))return 'partial';return 'none'}
function deviceSelectionState(d){const st=deviceState(d);return st.mode==='all'?'all':st.mode==='entities'&&st.entities.size?'partial':'none'}
function selectedHeroEntities(a){const result=[];for(const d of a.devices){const st=deviceState(d);for(const e of d.entities){if(!['light','switch'].includes(e.domain)||(selected.right[a.id]||[]).includes(e.entity_id))continue;if(st.mode==='all'||(st.mode==='entities'&&st.entities.has(e.entity_id)))result.push(e)}}const seen=new Set();return result.filter(e=>!seen.has(e.entity_id)&&seen.add(e.entity_id))}
function normalizedHeroOrder(a){const candidates=selectedHeroEntities(a);const byID=new Map(candidates.map(e=>[e.entity_id,e]));const ordered=[];for(const id of selected.orders[a.id]||[]){if(byID.has(id)){ordered.push(byID.get(id));byID.delete(id)}}ordered.push(...[...byID.values()].sort((x,y)=>x.name.localeCompare(y.name)));selected.orders[a.id]=ordered.map(e=>e.entity_id);return ordered}
function renderHeroOrder(){heroOrderGrid.innerHTML='';const ordered=normalizedHeroOrder(area);heroOrderPanel.classList.toggle('hidden',!ordered.length);ordered.forEach((e,index)=>{const row=document.createElement('div');row.className='order-row';row.innerHTML=`<div class="icon" style="width:34px;height:34px;margin:0">${icon(e.domain)}</div><div class="copy"><b>${esc(e.name)}</b><span class="sub">${esc(e.entity_id)}</span></div><div class="order-actions"><button type="button" aria-label="Nach oben" ${index===0?'disabled':''}>↑</button><button type="button" aria-label="Nach unten" ${index===ordered.length-1?'disabled':''}>↓</button></div>`;const buttons=row.querySelectorAll('button');buttons[0].onclick=()=>moveHero(index,-1);buttons[1].onclick=()=>moveHero(index,1);heroOrderGrid.append(row)})}
function renderHeroRight(){
    const candidates=area.hero_right_candidates||[],byID=new Map(candidates.map(e=>[e.entity_id,e]));
    const ids=selected.right[area.id]||[];
    heroRightSelection.innerHTML='';heroRightGrid.innerHTML='';
    if(!ids.length){const hint=document.createElement('p');hint.className='sub';hint.textContent='Keine zusätzlichen Karten ausgewählt.';heroRightSelection.append(hint)}
    ids.forEach((id,index)=>{
        const entity=byID.get(id),row=document.createElement('div');row.className='order-row flow-selected-row';
        row.innerHTML=`<div class="copy"><b>${index+1}. ${esc(entity?.name||id)}</b><span class="sub">${esc(id)}</span></div><div class="order-actions"><button type="button" aria-label="Nach oben" ${index===0?'disabled':''}>↑</button><button type="button" aria-label="Nach unten" ${index===ids.length-1?'disabled':''}>↓</button><button type="button" class="flow-remove">Entfernen</button></div>`;
        const buttons=row.querySelectorAll('button');
        const move=delta=>{const next=[...ids];[next[index],next[index+delta]]=[next[index+delta],next[index]];selected.right[area.id]=next;renderHeroRight()};
        buttons[0].onclick=()=>move(-1);buttons[1].onclick=()=>move(1);buttons[2].onclick=()=>{selected.right[area.id]=ids.filter(value=>value!==id);renderHeroRight();renderHeroOrder()};heroRightSelection.append(row);
    });
    for(const entity of candidates){
        const row=document.createElement('label');row.className='entity';row.innerHTML=`<input type="checkbox"><span><b>${esc(entity.name)}</b><span class="sub">${esc(entity.entity_id)}</span></span>`;
        const input=row.querySelector('input');input.checked=ids.includes(entity.entity_id);input.disabled=!input.checked&&ids.length>=12;
        input.onchange=()=>{selected.right[area.id]=input.checked?[...ids,entity.entity_id]:ids.filter(id=>id!==entity.entity_id);renderHeroRight();renderHeroOrder()};heroRightGrid.append(row);
    }
}
function moveHero(index,delta){const order=selected.orders[area.id]||[];const target=index+delta;if(target<0||target>=order.length)return;[order[index],order[target]]=[order[target],order[index]];selected.orders[area.id]=order;renderHeroOrder()}
function showCoreSection(section){
    const isFlow=section==='flow';
    flowSection.classList.toggle('hidden',!isFlow);
    pageTitle.textContent=isFlow?'Flow-Leiste':'Geräte & Funktionen';
    pageDescription.textContent=isFlow?'Vorschläge und eigene Inhalte unter der Hero Card':'Räume, Quellen und Funktionen';
    document.title='CouchMate Core Dev Preview – '+pageTitle.textContent;
    document.querySelectorAll('.nav a[data-core-section]').forEach(link=>{if(link.dataset.coreSection===section)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current')});
}
function navigateCore(section,roomID){
    const url=new URL(location.href);
    if(section==='flow')url.searchParams.set('section','flow');else url.searchParams.delete('section');
    history.pushState({},'',url);
    if(section==='flow')renderFlowPage(roomID);else renderAreas();
    window.scrollTo({top:0});
}
function renderFlowPage(roomID){
    showCoreSection('flow');
    areas.classList.add('hidden');devices.classList.add('hidden');entities.classList.add('hidden');
    flowArea=data.areas.find(item=>item.id===(roomID||flowArea?.id||area?.id))||data.areas[0];
    flowRoomSelect.innerHTML=data.areas.map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
    flowRoomSelect.disabled=!flowArea;
    flowSettings.classList.toggle('hidden',!flowArea);flowNoRooms.classList.toggle('hidden',!!flowArea);
    if(!flowArea)return;
    flowRoomSelect.value=flowArea.id;
    flowRoomSelect.onchange=()=>renderFlowPage(flowRoomSelect.value);
    renderFlowChoices();
}
function moveFlow(index,delta){
    const ids=selected.flows[flowArea.id]||[],target=index+delta;
    if(target<0||target>=ids.length)return;
    [ids[index],ids[target]]=[ids[target],ids[index]];renderFlowChoices();
}
function renderFlowChoices(){
    if(!flowArea)return;
    const candidates=flowArea.flow_candidates||[],byID=new Map(candidates.map(item=>[item.entity_id,item]));
    const ids=selected.flows[flowArea.id]||[],mode=selected.flowModes[flowArea.id]||'automatic';
    flowModeGrid.querySelectorAll('input').forEach(input=>{input.checked=input.value===mode;input.onchange=()=>{selected.flowModes[flowArea.id]=input.value;renderFlowChoices()}});
    flowCount.textContent=`${ids.length} von 3 gewählt`;
    flowContentHint.textContent=mode==='custom'?'Wähle bis zu drei Funktionen. Mit den Pfeilen legst du ihre Reihenfolge fest. Ohne Auswahl bleibt die Leiste leer.':mode==='hidden'?'Die Leiste ist ausgeblendet. Du kannst deine Inhalte für später weiter bearbeiten.':'Automatische Aktionen haben Vorrang. Deine Auswahl füllt freie Plätze. Wähle „Eigene Inhalte“, wenn ausschließlich diese Funktionen erscheinen sollen.';
    const names=ids.map(id=>byID.get(id)?.name||id);
    const previewItems=mode==='automatic'?['Automatische Aktionen',...(names.length?names.slice(0,2):['Freier Platz','Freier Platz'])]:names;
    flowPreview.innerHTML='<div class="flow-preview-hero">Hero Card</div>'+(mode==='hidden'?'<div class="sub">Flow-Leiste ausgeblendet</div>':previewItems.length?`<div class="flow-preview-deck">${previewItems.map(name=>`<div class="flow-preview-item">${esc(name)}</div>`).join('')}</div>`:'<div class="sub">Wähle unten deine ersten Inhalte.</div>');
    flowSelection.innerHTML='';
    ids.forEach((id,index)=>{
        const entity=byID.get(id),row=document.createElement('div');
        row.className='order-row flow-selected-row';
        row.innerHTML=`<div class="copy"><b>${index+1}. ${esc(entity?.name||id)}</b><span class="sub ${entity?'':'flow-unavailable'}">${entity?esc(id):'Nicht mehr verfügbar – entferne diese Auswahl, um den Platz freizugeben.'}</span></div><div class="order-actions"><button type="button" aria-label="${esc(entity?.name||id)} nach oben" ${index===0?'disabled':''}>↑</button><button type="button" aria-label="${esc(entity?.name||id)} nach unten" ${index===ids.length-1?'disabled':''}>↓</button><button type="button" class="flow-remove" aria-label="${esc(entity?.name||id)} entfernen">Entfernen</button></div>`;
        const buttons=row.querySelectorAll('button');buttons[0].onclick=()=>moveFlow(index,-1);buttons[1].onclick=()=>moveFlow(index,1);buttons[2].onclick=()=>{selected.flows[flowArea.id]=ids.filter(value=>value!==id);renderFlowChoices()};
        flowSelection.append(row);
    });
    flowNoCandidates.classList.toggle('hidden',!!candidates.length);
    flowGrid.innerHTML='';
    for(const entity of candidates){
        const row=document.createElement('label');row.className='entity';
        row.innerHTML=`<input type="checkbox"><div class="icon" style="width:34px;height:34px;margin:0">${icon(entity.domain)}</div><span><b>${esc(entity.name)}</b><span class="sub">${esc(entity.entity_id)}</span></span>`;
        const input=row.querySelector('input');input.checked=ids.includes(entity.entity_id);input.disabled=!input.checked&&ids.length>=3;
        input.onchange=()=>{const next=[...(selected.flows[flowArea.id]||[])],index=next.indexOf(entity.entity_id);if(input.checked&&index<0&&next.length<3)next.push(entity.entity_id);if(!input.checked&&index>=0)next.splice(index,1);selected.flows[flowArea.id]=next;renderFlowChoices()};
        flowGrid.append(row);
    }
}

function renderSecurityDashboard(){
    securityDashboardEnabled.checked=selected.securityEnabled;
    securityDashboardEnabled.onchange=()=>selected.securityEnabled=securityDashboardEnabled.checked;
    for(const [kind,grid,field] of [
        ['alarm',securityAlarmGrid,'securityAlarms'],
        ['camera',securityCameraGrid,'securityCameras'],
        ['contact',securityContactGrid,'securityContacts'],
    ]){
        grid.innerHTML='';
        const candidates=data.security_candidates?.[kind]||[];
        if(!candidates.length){const empty=document.createElement('p');empty.className='sub';empty.textContent='Keine passenden Home-Assistant-Entitäten vorhanden.';grid.append(empty);continue}
        for(const entity of candidates){
            const row=document.createElement('label');row.className='entity';
            row.innerHTML=`<input type="checkbox"><span><b>${esc(entity.name)}</b><span class="sub">${esc(entity.entity_id)}</span></span>`;
            const input=row.querySelector('input');input.checked=selected[field].includes(entity.entity_id);
            input.onchange=()=>{selected[field]=input.checked?[...new Set([...selected[field],entity.entity_id])]:selected[field].filter(id=>id!==entity.entity_id)};
            grid.append(row);
        }
    }
}
function renderWeather(){renderChoices(weatherPanel,weatherGrid,data.weather_candidates,selected.weather,value=>selected.weather=value,'globalWeather','Automatisch','Erste verfügbare Wetter-Entität verwenden')}
function renderTimerChoices(){
    const candidates=area.timer_candidates||[];
    timerGrid.innerHTML='';
    timerEmpty.classList.toggle('hidden',!!candidates.length);
    for(const timer of candidates){
        const row=document.createElement('label');row.className='entity';
        const assignedRoom=Object.keys(selected.timers).find(id=>id!==area.id&&(selected.timers[id]||[]).includes(timer.entity_id));
        const assignment=timer.area_unassigned?(assignedRoom?`Bisher ${data.areas.find(item=>item.id===assignedRoom)?.name||'anderem Raum'} zugeordnet`:'Ohne Home-Assistant-Bereich'):'Home-Assistant-Bereich';
        row.innerHTML=`<input type="checkbox"><span><b>${esc(timer.name)}</b><span class="sub">${esc(timer.entity_id)} · ${esc(assignment)}</span></span>`;
        const input=row.querySelector('input');input.checked=(selected.timers[area.id]||[]).includes(timer.entity_id);
        input.onchange=()=>{
            if(input.checked){
                for(const id of Object.keys(selected.timers))selected.timers[id]=(selected.timers[id]||[]).filter(value=>value!==timer.entity_id);
                selected.timers[area.id]=[...(selected.timers[area.id]||[]),timer.entity_id];
            }else selected.timers[area.id]=(selected.timers[area.id]||[]).filter(value=>value!==timer.entity_id);
            renderTimerChoices();
        };
        timerGrid.append(row);
    }
}
function renderWashdataChoices(){
    const candidates=area.washdata_candidates||[];
    washdataGrid.innerHTML='';
    washdataEmpty.classList.toggle('hidden',!!candidates.length);
    for(const appliance of candidates){
        const row=document.createElement('label');row.className='entity';
        const assignedRoom=Object.keys(selected.washdata).find(id=>id!==area.id&&(selected.washdata[id]||[]).includes(appliance.device_id));
        const assignment=appliance.area_unassigned?(assignedRoom?`Bisher ${data.areas.find(item=>item.id===assignedRoom)?.name||'anderem Raum'} zugeordnet`:'Ohne Home-Assistant-Bereich'):'Home-Assistant-Bereich';
        row.innerHTML=`<input type="checkbox"><span><b>${esc(appliance.name)}</b><span class="sub">${esc(assignment)}</span></span>`;
        const input=row.querySelector('input');input.checked=(selected.washdata[area.id]||[]).includes(appliance.device_id);
        input.onchange=()=>{
            if(input.checked){
                for(const id of Object.keys(selected.washdata))selected.washdata[id]=(selected.washdata[id]||[]).filter(value=>value!==appliance.device_id);
                selected.washdata[area.id]=[...(selected.washdata[area.id]||[]),appliance.device_id];
            }else selected.washdata[area.id]=(selected.washdata[area.id]||[]).filter(value=>value!==appliance.device_id);
            renderWashdataChoices();
        };
        washdataGrid.append(row);
    }
}
function renderAreas(){
    showCoreSection('configurator');
    areas.classList.remove('hidden');devices.classList.add('hidden');entities.classList.add('hidden');
    renderWeather();
    renderSecurityDashboard();
    energyDashboardEnabled.checked=selected.energyEnabled;
    energyDashboardEnabled.onchange=()=>selected.energyEnabled=energyDashboardEnabled.checked;
    renderThermostatStyles(thermostatStylePanel,thermostatStyleGrid,selected.thermostatStyle,value=>selected.thermostatStyle=value||'full','globalThermostatStyle',false);
    showRoomName.checked=selected.showRoomName;
    showRoomName.onchange=()=>selected.showRoomName=showRoomName.checked;
    showRoomClimate.checked=selected.showRoomClimate;
    showRoomClimate.onchange=()=>selected.showRoomClimate=showRoomClimate.checked;
    areaGrid.innerHTML='';
    for(const a of data.areas){
        const selectedCount=a.devices.reduce((n,d)=>{
            const st=deviceState(d);
            return n+(st.mode==='all'?d.entities.length:st.mode==='entities'?st.entities.size:0);
        },0)+(selected.timers[a.id]||[]).length+(selected.washdata[a.id]||[]).length;
        const timerCount=(a.timer_candidates||[]).filter(timer=>!timer.area_unassigned||(selected.timers[a.id]||[]).includes(timer.entity_id)).length;
        const washdataCount=(a.washdata_candidates||[]).filter(item=>!item.area_unassigned||(selected.washdata[a.id]||[]).includes(item.device_id)).length;
        const subtitle=`${a.devices.length} Geräte${timerCount?` · ${timerCount} Timer`:''}${washdataCount?` · ${washdataCount} WashData`:''}`;
        areaGrid.append(tile(a.name,{text:subtitle,selection:selectedCount?`${selectedCount} Funktionen gewählt`:''},roomKind(a.name),areaSelectionState(a),()=>{area=a;renderDevices()}));
    }
}
function renderDevices(){showCoreSection('configurator');openRoomFlow.onclick=()=>navigateCore('flow',area.id);areas.classList.add('hidden');devices.classList.remove('hidden');entities.classList.add('hidden');areaName.textContent=area.name;deviceGrid.innerHTML='';renderChoices(climatePanel,climateGrid,area.climate_candidates,selected.climates[area.id],value=>{if(value)selected.climates[area.id]=value;else delete selected.climates[area.id]},`climate-${area.id}`,'Automatisch','Einziges freigegebenes Thermostat verwenden');renderThermostatStyles(roomThermostatStylePanel,roomThermostatStyleGrid,selected.thermostatStyles[area.id]??null,value=>{if(value)selected.thermostatStyles[area.id]=value;else delete selected.thermostatStyles[area.id]},`roomThermostatStyle-${area.id}`,true);renderHeroCardStyles();renderChoices(temperaturePanel,temperatureGrid,area.temperature_candidates,selected.temperatures[area.id],value=>{if(value)selected.temperatures[area.id]=value;else delete selected.temperatures[area.id]},`temperature-${area.id}`,'Thermostat-Fallback','Istwert des Thermostats verwenden');renderChoices(humidityPanel,humidityGrid,area.humidity_candidates,selected.humidities[area.id],value=>{if(value)selected.humidities[area.id]=value;else delete selected.humidities[area.id]},`humidity-${area.id}`,'Thermostat-Fallback','Luftfeuchte des Thermostats verwenden');renderHeroOrder();renderHeroRight();renderTimerChoices();renderWashdataChoices();for(const d of area.devices){const domain=d.entities[0]?.domain||'default';const st=deviceState(d);const subtitle=[d.manufacturer,d.model].filter(Boolean).join(' · ')||`${d.entities.length} Funktionen`;deviceGrid.append(tile(d.name,{text:subtitle,selection:st.mode==='entities'?`${st.entities.size} ${st.entities.size===1?'Funktion':'Funktionen'} gewählt`:''},domain,deviceSelectionState(d),()=>{device=d;renderEntities()}))}}
function renderEntities(){devices.classList.add('hidden');entities.classList.remove('hidden');deviceName.textContent=`${area.name} · ${device.name}`;entityGrid.innerHTML='';let st=deviceState(device);selectWholeDevice.checked=st.mode==='all';selectWholeDevice.onchange=()=>{if(selectWholeDevice.checked)setDeviceState(device.id,{mode:'all',entities:new Set()});else setDeviceState(device.id,{mode:'none',entities:new Set()});renderEntities()};for(const e of device.entities){st=deviceState(device);const row=document.createElement('label');row.className='entity';row.innerHTML=`<input type="checkbox" ${(st.mode==='all'||(st.mode==='entities'&&st.entities.has(e.entity_id)))?'checked':''} ${st.mode==='all'?'disabled':''}><span><b>${esc(e.name)}</b><span class="sub">${esc(e.entity_id)}</span></span>`;const c=row.querySelector('input');c.onchange=()=>{const current=deviceState(device);const ids=current.mode==='entities'?new Set(current.entities):new Set();if(c.checked)ids.add(e.entity_id);else ids.delete(e.entity_id);setDeviceState(device.id,{mode:'entities',entities:ids})};entityGrid.append(row)}}
function buildSelectionModel(){const model={version:2,energy_dashboard:{enabled:selected.energyEnabled},security_dashboard:{enabled:selected.securityEnabled,alarm_entity_ids:[...new Set(selected.securityAlarms)],camera_entity_ids:[...new Set(selected.securityCameras)],contact_entity_ids:[...new Set(selected.securityContacts)]},weather:selected.weather,thermostat_card_style:selected.thermostatStyle,show_room_name:selected.showRoomName,show_room_climate:selected.showRoomClimate,areas:{}};for(const a of data.areas){const cfg={devices:{}};const flowMode=selected.flowModes[a.id]||'automatic';if(flowMode!=='automatic')cfg.flow_mode=flowMode;if(selected.temperatures[a.id])cfg.temperature=selected.temperatures[a.id];if(selected.humidities[a.id])cfg.humidity=selected.humidities[a.id];if(selected.climates[a.id])cfg.climate=selected.climates[a.id];if(selected.thermostatStyles[a.id])cfg.thermostat_card_style=selected.thermostatStyles[a.id];cfg.hero_layout={...roomHeroLayout(a),thermostat_card_style:selected.thermostatStyles[a.id]??selected.thermostatStyle};const order=normalizedHeroOrder(a).map(e=>e.entity_id);if(order.length)cfg.hero_order=order;const right=(selected.right[a.id]||[]).slice(0,12);if(right.length)cfg.hero_right_entities=right;const flow=(selected.flows[a.id]||[]).slice(0,3);if(flow.length)cfg.flow_entities=flow;const timers=[...new Set(selected.timers[a.id]||[])];if(timers.length)cfg.timer_entities=timers;const washdata=[...new Set(selected.washdata[a.id]||[])];if(washdata.length)cfg.washdata_devices=washdata;for(const d of a.devices){const st=deviceState(d);if(st.mode==='all')cfg.devices[d.id]={mode:'all',entities:[]};else if(st.mode==='entities'&&st.entities.size)cfg.devices[d.id]={mode:'entities',entities:[...st.entities]}}if(cfg.temperature||cfg.humidity||cfg.climate||cfg.thermostat_card_style||cfg.hero_order||cfg.hero_right_entities||cfg.flow_entities||cfg.timer_entities||cfg.washdata_devices||cfg.flow_mode||Object.keys(cfg.devices).length)model.areas[a.id]=cfg}return model}
api('/api/couchmate/configurator/data').then(r=>r.json()).then(j=>{data=j;selected.energyEnabled=j.energy_dashboard?.enabled===true;selected.securityEnabled=j.security_dashboard?.enabled===true;selected.securityAlarms=[...new Set(j.security_dashboard?.alarm_entity_ids||[])];selected.securityCameras=[...new Set(j.security_dashboard?.camera_entity_ids||[])];selected.securityContacts=[...new Set(j.security_dashboard?.contact_entity_ids||[])];selected.weather=j.weather_entity??null;selected.thermostatStyle=j.thermostat_card_style??'full';selected.showRoomName=j.show_room_name??true;selected.showRoomClimate=j.show_room_climate??true;for(const a of data.areas){if(a.temperature_entity)selected.temperatures[a.id]=a.temperature_entity;if(a.humidity_entity)selected.humidities[a.id]=a.humidity_entity;if(a.climate_entity)selected.climates[a.id]=a.climate_entity;const heroLayout=a.hero_layout||{};selected.layouts[a.id]={device_card_style:heroLayout.device_card_style||'bubble',camera_card_style:heroLayout.camera_card_style||'large',media_card_style:heroLayout.media_card_style||'transport'};if(heroLayout.thermostat_card_style)selected.thermostatStyles[a.id]=heroLayout.thermostat_card_style;else if(a.thermostat_card_style)selected.thermostatStyles[a.id]=a.thermostat_card_style;selected.orders[a.id]=a.hero_order||[];selected.right[a.id]=[...new Set(a.hero_right_entities||[])].slice(0,12);selected.flows[a.id]=[...new Set(a.flow_entities||[])].slice(0,3);selected.timers[a.id]=[...new Set(a.timer_entities||[])];selected.washdata[a.id]=[...new Set(a.washdata_devices||[])];selected.flowModes[a.id]=['automatic','custom','hidden'].includes(a.flow_mode)?a.flow_mode:'automatic';for(const d of a.devices){const ids=new Set(d.entities.filter(e=>e.selected).map(e=>e.entity_id));if(d.selection_mode==='all')selected.devices.set(d.id,{mode:'all',entities:new Set()});else if(ids.size)selected.devices.set(d.id,{mode:'entities',entities:ids})}}if(new URL(location.href).searchParams.get('section')==='flow')renderFlowPage();else renderAreas()}).catch(e=>showToast('error','Konfigurator konnte nicht geladen werden',e.message==='AUTH'?'Öffne die Seite im selben Browser, in dem du bei Home Assistant angemeldet bist.':e.message));
document.querySelectorAll('.nav a[data-core-section]').forEach(link=>{if(link.dataset.coreSection==='management')return;link.onclick=event=>{if(!data)return;event.preventDefault();navigateCore(link.dataset.coreSection)}});
window.addEventListener('popstate',()=>{if(!data)return;if(new URL(location.href).searchParams.get('section')==='flow')renderFlowPage();else renderAreas()});
backAreas.onclick=renderAreas;backDevices.onclick=renderDevices;save.onclick=async()=>{save.disabled=true;save.textContent='Speichert …';showToast('', 'Auswahl wird gespeichert','Bitte einen Moment warten.');try{const r=await api('/api/couchmate/configurator/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selection_model:buildSelectionModel()})});const j=await r.json();if(!j.success)throw new Error('Unbekannter Speicherfehler');showToast('ok','Auswahl erfolgreich gespeichert',`${j.area_count} Räume · ${j.entity_count} einzelne Funktionen · ${j.climate_count} Thermostate · Wetter ${j.weather_configured?'fest gewählt':'automatisch'}`)}catch(e){showToast('error','Speichern fehlgeschlagen',e.message==='AUTH'?'Die Home-Assistant-Anmeldung ist abgelaufen. Bitte Home Assistant neu laden.':e.message)}finally{save.disabled=false;save.textContent='Auswahl speichern'}};
</script></body></html>'''


HTML = render_page(HTML, "configurator")
