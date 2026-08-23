"""REST API for CouchMate Core Dev Preview."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

from aiohttp import web
import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.components.camera import (
    async_get_image as async_get_camera_image,
    async_request_stream,
)
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.image import async_get_image as async_get_image_entity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONFIGURATION_MANAGER, DOMAIN, PAIRING_MANAGER
from .pairing import PairingManager, PairingStatus
from .storage import async_save_entities

_LOGGER = logging.getLogger(__name__)


def _manager(hass: HomeAssistant) -> PairingManager:
    return hass.data[DOMAIN][PAIRING_MANAGER]


def _profile_hero_configuration(
    hass: HomeAssistant,
    client_id: str,
) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """Return the normalized v1 Hero layout assigned to this client."""
    configuration = hass.data.get(DOMAIN, {}).get(CONFIGURATION_MANAGER)
    if configuration is None:
        return {}, {}

    profile = configuration.client_snapshot(client_id).get("profile", {})
    settings = profile.get("settings", {})
    companion = settings.get("companion", {}) if isinstance(settings, dict) else {}
    if not isinstance(companion, dict):
        return {}, {}

    raw_orders = companion.get("hero_entity_order", {})
    hero_orders = {
        str(area_id): list(dict.fromkeys(
            str(entity_id)
            for entity_id in entity_ids
            if isinstance(entity_id, str) and entity_id
        ))[:3]
        for area_id, entity_ids in raw_orders.items()
        if isinstance(area_id, str) and isinstance(entity_ids, list)
    } if isinstance(raw_orders, dict) else {}

    allowed_styles = {
        "thermostat_card_style": {"full_vertical", "full", "compact", "hidden"},
        "device_card_style": {"bubble", "tile", "toggle", "icon"},
        "camera_card_style": {"large", "compact"},
        "media_card_style": {"transport", "compact"},
    }
    raw_layouts = companion.get("hero_layouts", {})
    hero_layouts: dict[str, dict[str, str]] = {}
    if isinstance(raw_layouts, dict):
        for area_id, raw_layout in raw_layouts.items():
            if not isinstance(area_id, str) or not isinstance(raw_layout, dict):
                continue
            normalized = {
                key: value
                for key, choices in allowed_styles.items()
                if isinstance((value := raw_layout.get(key)), str)
                and value in choices
            }
            if normalized:
                hero_layouts[area_id] = normalized

    return hero_orders, hero_layouts


def _pairing_response(payload: Mapping[str, Any], status: int = 200) -> web.Response:
    """Return sensitive pairing state without allowing intermediary caching."""
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _json_safe(value: Any) -> Any:
    """Convert Home Assistant values into JSON-safe primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "as_dict"):
        try:
            return _json_safe(value.as_dict())
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def _entity_payload(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """Build one robust client entity payload from current registries and state."""
    state = hass.states.get(entity_id)
    if state is None:
        return None

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    device = dev_reg.async_get(entry.device_id) if entry and entry.device_id else None
    area_id = (entry.area_id if entry else None) or (device.area_id if device else None)
    area = area_reg.async_get_area(area_id) if area_id else None

    name = None
    if entry:
        name = entry.name or entry.original_name
    if not name:
        name = state.attributes.get("friendly_name")

    return {
        "entity_id": entity_id,
        "state": state.state,
        "attributes": _json_safe(dict(state.attributes)),
        "last_changed": state.last_changed.isoformat(),
        "last_updated": state.last_updated.isoformat(),
        "area_id": area_id,
        "area_name": area.name if area else None,
        "device_id": entry.device_id if entry else None,
        "device_name": (device.name_by_user or device.name) if device else None,
        "name": name,
        "icon": (entry.icon or entry.original_icon) if entry else None,
        "device_class": entry.device_class if entry else None,
        "unit_of_measurement": entry.unit_of_measurement if entry else None,
    }


def _effective_client_entity_ids(hass: HomeAssistant) -> list[str]:
    """Return the entities exposed to and controllable by a CouchMate client."""
    domain_data = hass.data.get(DOMAIN, {})
    configured_area_ids = list(domain_data.get("areas", []))
    full_device_ids = set(domain_data.get("devices", []))
    explicit_entity_ids = list(domain_data.get("explicit_entities", []))
    excluded_entity_ids = list(domain_data.get("excluded_entities", []))
    room_temperature_ids = dict(domain_data.get("room_temperatures", {}))
    room_humidity_ids = dict(domain_data.get("room_humidities", {}))
    room_climate_ids = dict(domain_data.get("room_climates", {}))
    weather_entity_id = domain_data.get("weather_entity")
    selection_model = dict(domain_data.get("selection_model", {}))

    # Resolve registry-backed selections for every client snapshot instead of
    # relying on the flattened list created when Core started or the selection
    # was saved. Home Assistant can add an entity to a selected device or add a
    # device to a legacy selected area at any time; polling clients must see
    # those changes without a Core reload.
    from . import _resolve_filter

    selected = sorted(_resolve_filter(
        hass,
        areas=configured_area_ids,
        devices=list(full_device_ids),
        entities=explicit_entity_ids,
        excluded_entities=excluded_entity_ids,
    ))

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    full_device_entity_ids = [
        entry.entity_id
        for entry in entity_registry.entities.values()
        if entry.device_id in full_device_ids
        and hass.states.get(entry.entity_id) is not None
    ]

    selection_model_area_ids = {
        str(area_id)
        for area_id in dict(selection_model.get("areas", {})).keys()
        if area_id
    }
    configured_media_entity_ids: list[str] = []
    for entry in entity_registry.entities.values():
        if entry.disabled or not entry.entity_id.startswith("media_player."):
            continue
        if hass.states.get(entry.entity_id) is None:
            continue
        device = device_registry.async_get(entry.device_id) if entry.device_id else None
        entity_area_id = entry.area_id or (device.area_id if device else None)
        if entity_area_id and entity_area_id in selection_model_area_ids:
            configured_media_entity_ids.append(entry.entity_id)

    return list(dict.fromkeys([
        *selected,
        *full_device_entity_ids,
        *configured_media_entity_ids,
        *room_temperature_ids.values(),
        *room_humidity_ids.values(),
        *room_climate_ids.values(),
        *([weather_entity_id] if weather_entity_id else []),
    ]))


def _resolved_room_climate_ids(
    hass: HomeAssistant,
    effective_entity_ids: list[str],
) -> dict[str, str]:
    """Resolve one controllable thermostat per room without guessing.

    An explicitly configured climate source always wins.  Otherwise a room
    receives a fallback only when exactly one exposed, available climate
    entity belongs to it.
    """
    configured = {
        str(area_id): str(entity_id)
        for area_id, entity_id in dict(
            hass.data.get(DOMAIN, {}).get("room_climates", {})
        ).items()
        if area_id and entity_id and hass.states.get(str(entity_id)) is not None
    }
    candidates: dict[str, list[str]] = {}

    for entity_id in effective_entity_ids:
        if not entity_id.startswith("climate."):
            continue
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            continue
        payload = _entity_payload(hass, entity_id)
        area_id = payload.get("area_id") if payload else None
        if area_id:
            candidates.setdefault(str(area_id), []).append(entity_id)

    for area_id, entity_ids in candidates.items():
        unique_ids = list(dict.fromkeys(entity_ids))
        if area_id not in configured and len(unique_ids) == 1:
            configured[area_id] = unique_ids[0]

    return configured


class CouchMateEntitiesView(HomeAssistantView):
    url = "/api/couchmate/entities"
    name = "api:couchmate:entities"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        if DOMAIN not in hass.data:
            return web.json_response({"error": "CouchMate Core Dev Preview not configured"}, status=400)
        selected = list(hass.data[DOMAIN].get("entities", []))
        detailed_entities: list[dict[str, Any]] = []
        skipped: list[str] = []
        for entity_id in selected:
            try:
                payload = _entity_payload(hass, entity_id)
                if payload is None:
                    skipped.append(entity_id)
                    continue
                detailed_entities.append(payload)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unable to serialize CouchMate entity %s", entity_id)
                skipped.append(f"{entity_id}: {err}")

        return web.json_response(
            {"entities": detailed_entities, "count": len(detailed_entities), "skipped": skipped},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            data = vol.Schema({vol.Required("entities"): [str]})(await request.json())
        except (ValueError, vol.Invalid) as err:
            return web.json_response({"error": f"Invalid data: {err}"}, status=400)
        valid = [entity_id for entity_id in data["entities"] if hass.states.get(entity_id)]
        hass.data[DOMAIN]["entities"] = valid
        await async_save_entities(hass, {"entities": valid})
        return web.json_response({"success": True, "entities": valid, "count": len(valid)})


class CouchMateInfoView(HomeAssistantView):
    url = "/api/couchmate/info"
    name = "api:couchmate:info"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        return web.json_response({
            "integration": "CouchMate Core Dev Preview",
            "version": "1.4.0-beta.6",
            "domain": DOMAIN,
            "filtered_entities_count": len(hass.data.get(DOMAIN, {}).get("entities", [])),
            "pairing": True,
            "status": "active",
        })


class PairingCreateView(HomeAssistantView):
    url = "/api/couchmate/pairing/create"
    name = "api:couchmate:pairing:create"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        if DOMAIN not in hass.data:
            return _pairing_response({"error": "not_configured"}, status=503)
        try:
            data = await request.json()
        except Exception:
            data = {}
        capabilities = data.get("capabilities", [])
        if not isinstance(capabilities, list):
            capabilities = []
        session = _manager(hass).create_session(
            str(data.get("device_name", "Apple TV")),
            capabilities=[str(item) for item in capabilities],
        )
        requested_rights = []
        if "configuration:write" in session.capabilities:
            requested_rights.append("Profile und Dashboard-Einstellungen ändern")
        if "backgrounds:write" in session.capabilities:
            requested_rights.append("Raumbilder hochladen und entfernen")
        rights_notice = (
            " Angefragte Rechte: **" + ", ".join(requested_rights) + "**."
            if requested_rights
            else ""
        )
        persistent_notification.async_create(
            hass,
            f"Ein Apple TV namens **{session.device_name}** möchte sich mit CouchMate verbinden. "
            f"Kopplungscode: **{session.code}**.{rights_notice} Öffne in der Sidebar "
            "**CouchMate Core Dev Preview → Apple TVs & Design**, oder bestätige ihn "
            f"über den Dienst `{DOMAIN}.approve_pairing`.",
            title="CouchMate Core Dev Preview – Kopplungsanfrage",
            notification_id=f"{DOMAIN}_pairing_{session.session_id}",
        )
        return _pairing_response(session.public_dict())


class PairingStatusView(HomeAssistantView):
    url = "/api/couchmate/pairing/status"
    name = "api:couchmate:pairing:status"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        session_id = request.query.get("session_id", "")
        session = _manager(request.app["hass"]).get_by_session_id(session_id)
        if not session:
            return _pairing_response({"error": "session_not_found"}, status=404)
        payload = session.public_dict()
        if session.status == PairingStatus.APPROVED:
            payload["exchange_token"] = session.exchange_token
        return _pairing_response(payload)


class PairingApproveView(HomeAssistantView):
    url = "/api/couchmate/pairing/approve"
    name = "api:couchmate:pairing:approve"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        data = await request.json()
        manager = _manager(request.app["hass"])
        code = str(data.get("code", ""))
        pending = manager.get_by_code(code)
        if not pending:
            return _pairing_response({"error": "code_not_found"}, status=404)
        if pending.capabilities:
            user = request.get("hass_user")
            if user is None or not getattr(user, "is_admin", False):
                return _pairing_response({"error": "admin_required"}, status=403)
        session = manager.approve(code)
        return _pairing_response(session.public_dict())


class PairingExchangeView(HomeAssistantView):
    url = "/api/couchmate/pairing/exchange"
    name = "api:couchmate:pairing:exchange"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        data = await request.json()
        credentials = await _manager(request.app["hass"]).async_exchange(
            str(data.get("session_id", "")), str(data.get("exchange_token", ""))
        )
        if not credentials:
            return _pairing_response({"error": "exchange_denied"}, status=403)
        return _pairing_response(credentials)


async def _client_id_from_request(request: web.Request) -> str | None:
    """Validate a CouchMate client bearer token."""
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return await _manager(request.app["hass"]).async_validate_client_token(token)


class PairingCancelView(HomeAssistantView):
    url = "/api/couchmate/pairing/cancel"
    name = "api:couchmate:pairing:cancel"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            data = {}
        session = _manager(request.app["hass"]).cancel(
            str(data.get("session_id", ""))
        )
        if not session:
            return _pairing_response({"error": "session_not_found"}, status=404)
        persistent_notification.async_dismiss(
            request.app["hass"], f"{DOMAIN}_pairing_{session.session_id}"
        )
        return _pairing_response(session.public_dict())


class CouchMateClientInfoView(HomeAssistantView):
    url = "/api/couchmate/client/info"
    name = "api:couchmate:client:info"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        client_id = await _client_id_from_request(request)
        if client_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        hass = request.app["hass"]
        return web.json_response({
            "client_id": client_id,
            "integration": "CouchMate Core Dev Preview",
            "version": "1.4.0-beta.6",
            "status": "active",
            "entities_count": len(hass.data.get(DOMAIN, {}).get("entities", [])),
        })


class CouchMateClientEntitiesView(HomeAssistantView):
    url = "/api/couchmate/client/entities"
    name = "api:couchmate:client:entities"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        client_id = await _client_id_from_request(request)
        if client_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        hass = request.app["hass"]
        selected = list(hass.data.get(DOMAIN, {}).get("entities", []))
        explicit_entity_ids = list(hass.data.get(DOMAIN, {}).get("explicit_entities", []))
        full_device_ids = list(hass.data.get(DOMAIN, {}).get("devices", []))
        room_temperature_ids = dict(hass.data.get(DOMAIN, {}).get("room_temperatures", {}))
        room_humidity_ids = dict(hass.data.get(DOMAIN, {}).get("room_humidities", {}))
        selection_model = dict(hass.data.get(DOMAIN, {}).get("selection_model", {}))
        thermostat_card_style = selection_model.get("thermostat_card_style", "full")
        if thermostat_card_style not in ("full_vertical", "full", "compact", "hidden"):
            thermostat_card_style = "full"
        show_room_name = selection_model.get("show_room_name", True)
        if not isinstance(show_room_name, bool):
            show_room_name = True
        show_room_climate = selection_model.get("show_room_climate", True)
        if not isinstance(show_room_climate, bool):
            show_room_climate = True
        room_thermostat_card_styles = {
            str(area_id): str(area_cfg["thermostat_card_style"])
            for area_id, area_cfg in dict(selection_model.get("areas", {})).items()
            if isinstance(area_cfg, dict)
            and area_cfg.get("thermostat_card_style")
            in ("full_vertical", "full", "compact", "hidden")
        }
        hero_entity_order = {
            str(area_id): [str(entity_id) for entity_id in area_cfg.get("hero_order", [])]
            for area_id, area_cfg in dict(selection_model.get("areas", {})).items()
            if isinstance(area_cfg, dict) and isinstance(area_cfg.get("hero_order"), list)
        }
        profile_hero_order, hero_layouts = _profile_hero_configuration(hass, client_id)
        if profile_hero_order:
            hero_entity_order = profile_hero_order
        effective_selected = _effective_client_entity_ids(hass)
        room_climate_ids = _resolved_room_climate_ids(hass, effective_selected)
        entities: list[dict[str, Any]] = []
        skipped: list[str] = []

        for entity_id in effective_selected:
            try:
                payload = _entity_payload(hass, entity_id)
                if payload is None:
                    skipped.append(entity_id)
                    continue
                if entity_id.startswith(("camera.", "image.")):
                    # The paired-client boundary has its own snapshot proxy.
                    # Never leak Home Assistant's rotating image access token
                    # or a signed entity_picture URL into client state/logs.
                    payload["attributes"].pop("access_token", None)
                    payload["attributes"].pop("entity_picture", None)
                entities.append(payload)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unable to serialize CouchMate client entity %s", entity_id)
                skipped.append(f"{entity_id}: {err}")

        # Send the exact Home Assistant areas that belong to the exposed
        # entities. This guarantees that an individually selected entity
        # creates its room in CouchMate without exposing every other entity
        # from that area.
        areas_by_id: dict[str, dict[str, str]] = {}
        for entity in entities:
            area_id = entity.get("area_id")
            area_name = entity.get("area_name")
            if area_id and area_name:
                areas_by_id[area_id] = {"id": area_id, "name": area_name}

        room_temperatures: dict[str, dict[str, Any]] = {}
        entities_by_id = {item["entity_id"]: item for item in entities}
        for area_id, entity_id in room_temperature_ids.items():
            payload = entities_by_id.get(entity_id)
            if payload is None:
                continue
            room_temperatures[area_id] = {
                "area_id": area_id,
                "area_name": payload.get("area_name"),
                "entity_id": entity_id,
                "state": payload.get("state"),
                "unit_of_measurement": payload.get("unit_of_measurement")
                    or payload.get("attributes", {}).get("unit_of_measurement"),
                "name": payload.get("name"),
            }

        # A room thermostat is the fallback climate source for each value that
        # has no dedicated sensor selection. The thermostat itself remains in
        # the entity list so the client can render it as a real control.
        for area_id, entity_id in room_climate_ids.items():
            if area_id in room_temperatures:
                continue
            payload = entities_by_id.get(entity_id)
            current_temperature = (
                payload.get("attributes", {}).get("current_temperature")
                if payload is not None
                else None
            )
            if payload is None or current_temperature is None:
                continue
            room_temperatures[area_id] = {
                "area_id": area_id,
                "area_name": payload.get("area_name"),
                "entity_id": entity_id,
                "state": str(current_temperature),
                "unit_of_measurement": payload.get("attributes", {}).get("temperature_unit")
                    or hass.config.units.temperature_unit,
                "name": payload.get("name"),
            }

        room_humidities: dict[str, dict[str, Any]] = {}
        for area_id, entity_id in room_humidity_ids.items():
            payload = entities_by_id.get(entity_id)
            if payload is None:
                continue
            room_humidities[area_id] = {
                "area_id": area_id,
                "area_name": payload.get("area_name"),
                "entity_id": entity_id,
                "state": payload.get("state"),
                "unit_of_measurement": payload.get("unit_of_measurement")
                    or payload.get("attributes", {}).get("unit_of_measurement"),
                "name": payload.get("name"),
            }

        for area_id, entity_id in room_climate_ids.items():
            if area_id in room_humidities:
                continue
            payload = entities_by_id.get(entity_id)
            attributes = payload.get("attributes", {}) if payload is not None else {}
            current_humidity = attributes.get("current_humidity", attributes.get("humidity"))
            if payload is None or current_humidity is None:
                continue
            room_humidities[area_id] = {
                "area_id": area_id,
                "area_name": payload.get("area_name"),
                "entity_id": entity_id,
                "state": str(current_humidity),
                "unit_of_measurement": "%",
                "name": payload.get("name"),
            }

        weather: dict[str, Any] | None = None
        configured_weather_entity = hass.data.get(DOMAIN, {}).get("weather_entity")
        weather_entity_ids = (
            [str(configured_weather_entity)]
            if configured_weather_entity
            else [entity_id for entity_id in selected if entity_id.startswith("weather.")]
        )
        if not weather_entity_ids:
            weather_entity_ids = [state.entity_id for state in hass.states.async_all("weather") if state.state not in ("unknown", "unavailable")]

        if weather_entity_ids:
            weather_entity_id = weather_entity_ids[0]
            weather_state = hass.states.get(weather_entity_id)
            if weather_state is not None:
                weather = {
                    "entity_id": weather_entity_id,
                    "state": weather_state.state,
                    "attributes": dict(weather_state.attributes),
                    "forecast": [],
                }
                try:
                    response = await hass.services.async_call(
                        "weather",
                        "get_forecasts",
                        {"type": "daily"},
                        blocking=True,
                        target={"entity_id": weather_entity_id},
                        return_response=True,
                    )
                    if isinstance(response, dict):
                        entity_response = response.get(weather_entity_id, response)
                        if isinstance(entity_response, dict):
                            forecast = entity_response.get("forecast", [])
                            if isinstance(forecast, list):
                                weather["forecast"] = forecast[:3]
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Unable to load daily weather forecast for %s: %s", weather_entity_id, err)

        return web.json_response(
            {
                "client_id": client_id,
                "weather": weather,
                "entities": entities,
                "areas": sorted(areas_by_id.values(), key=lambda item: item["name"].casefold()),
                "room_temperature_entity_ids": room_temperature_ids,
                "room_temperatures": room_temperatures,
                "room_humidity_entity_ids": room_humidity_ids,
                "room_humidities": room_humidities,
                "room_climate_entity_ids": room_climate_ids,
                "hero_entity_order": hero_entity_order,
                "hero_layout_version": 1,
                "hero_layouts": hero_layouts,
                "thermostat_card_style": thermostat_card_style,
                "room_thermostat_card_styles": room_thermostat_card_styles,
                "show_room_name": show_room_name,
                "show_room_climate": show_room_climate,
                # Selection model v2 metadata. Clients must use this as the
                # authoritative whitelist: exact entity ids are rendered exactly,
                # while sibling entities are allowed only for devices explicitly
                # selected in mode "all".
                "selection_model_version": 2,
                "explicit_entity_ids": explicit_entity_ids,
                "full_device_ids": full_device_ids,
                "count": len(entities),
                "selected_count": len(selected),
                "effective_selected_count": len(effective_selected),
                "skipped": skipped,
            },
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )


class CouchMateClientSnapshotView(HomeAssistantView):
    """Serve one selected Home Assistant camera/image to a paired client."""

    url = "/api/couchmate/client/snapshot/{entity_id}"
    name = "api:couchmate:client:snapshot"
    requires_auth = False

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        client_id = await _client_id_from_request(request)
        if client_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)

        hass = request.app["hass"]
        entity_domain = entity_id.split(".", 1)[0]
        if entity_domain not in {"camera", "image"}:
            return web.json_response({"error": "invalid_image_entity"}, status=400)
        if entity_id not in set(_effective_client_entity_ids(hass)):
            return web.json_response({"error": "entity_not_selected"}, status=403)
        if hass.states.get(entity_id) is None:
            return web.json_response({"error": "entity_not_found"}, status=404)

        try:
            if entity_domain == "camera":
                image = await async_get_camera_image(
                    hass,
                    entity_id,
                    timeout=10,
                    width=1280,
                    height=720,
                )
            else:
                image = await async_get_image_entity(
                    hass,
                    entity_id,
                    timeout=10,
                )
        except (HomeAssistantError, TimeoutError, ValueError, KeyError) as err:
            _LOGGER.warning(
                "Unable to load camera snapshot %s for CouchMate client %s: %s",
                entity_id,
                client_id,
                err,
            )
            return web.json_response(
                {"error": "snapshot_unavailable"},
                status=502,
                headers={"Cache-Control": "no-store"},
            )

        return web.Response(
            body=image.content,
            content_type=image.content_type,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )


class CouchMateClientStreamView(HomeAssistantView):
    """Start one selected camera's tokenized Home Assistant HLS stream."""

    url = "/api/couchmate/client/stream/{entity_id}"
    name = "api:couchmate:client:stream"
    requires_auth = False

    async def post(self, request: web.Request, entity_id: str) -> web.Response:
        client_id = await _client_id_from_request(request)
        if client_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)

        hass = request.app["hass"]
        if not entity_id.startswith("camera."):
            return web.json_response({"error": "invalid_camera"}, status=400)
        if entity_id not in set(_effective_client_entity_ids(hass)):
            return web.json_response({"error": "entity_not_selected"}, status=403)
        if hass.states.get(entity_id) is None:
            return web.json_response({"error": "entity_not_found"}, status=404)

        try:
            stream_url = await async_request_stream(hass, entity_id, "hls")
        except (HomeAssistantError, TimeoutError, ValueError, KeyError) as err:
            _LOGGER.warning(
                "Unable to start camera stream %s for CouchMate client %s: %s",
                entity_id,
                client_id,
                err,
            )
            return web.json_response(
                {"error": "stream_unavailable"},
                status=422,
                headers={"Cache-Control": "no-store"},
            )

        return web.json_response(
            {
                "entity_id": entity_id,
                "url": stream_url,
                "content_type": "application/vnd.apple.mpegurl",
            },
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )


_ALLOWED_SERVICES: dict[str, set[str]] = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "media_player": {
        "media_play_pause",
        "media_play",
        "media_pause",
        "turn_on",
        "turn_off",
        "volume_up",
        "volume_down",
        "volume_set",
        "volume_mute",
    },
    "climate": {"turn_on", "turn_off", "set_temperature", "set_hvac_mode"},
    "cover": {"open_cover", "close_cover", "stop_cover", "set_cover_position"},
    "scene": {"turn_on"},
    "script": {"turn_on"},
}


class CouchMateClientServiceView(HomeAssistantView):
    url = "/api/couchmate/client/service"
    name = "api:couchmate:client:service"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        client_id = await _client_id_from_request(request)
        if client_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)

        hass = request.app["hass"]
        try:
            payload = await request.json()
            domain = str(payload.get("domain", "")).strip()
            service = str(payload.get("service", "")).strip()
            entity_ids = [str(item) for item in payload.get("entity_ids", [])]
            service_data = dict(payload.get("data", {}) or {})
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid_json"}, status=400)

        if service not in _ALLOWED_SERVICES.get(domain, set()):
            return web.json_response({"error": "service_not_allowed"}, status=403)
        if not entity_ids:
            return web.json_response({"error": "missing_entity_ids"}, status=400)

        # Use the same effective boundary as the entities endpoint. Anything
        # displayed as a controllable client entity must also be authorized.
        selected = set(_effective_client_entity_ids(hass))
        denied = [entity_id for entity_id in entity_ids if entity_id not in selected]
        if denied:
            return web.json_response({"error": "entity_not_selected", "entities": denied}, status=403)

        wrong_domain = [entity_id for entity_id in entity_ids if entity_id.split(".", 1)[0] != domain]
        if wrong_domain:
            return web.json_response({"error": "domain_mismatch", "entities": wrong_domain}, status=400)

        existing = [entity_id for entity_id in entity_ids if hass.states.get(entity_id) is not None]
        if len(existing) != len(entity_ids):
            missing = sorted(set(entity_ids) - set(existing))
            return web.json_response({"error": "entity_not_found", "entities": missing}, status=404)

        try:
            await hass.services.async_call(
                domain,
                service,
                service_data,
                blocking=True,
                target={"entity_id": entity_ids},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("CouchMate service call failed for client %s", client_id)
            return web.json_response({"error": "service_call_failed", "message": str(err)}, status=500)

        return web.json_response({
            "success": True,
            "client_id": client_id,
            "domain": domain,
            "service": service,
            "entity_ids": entity_ids,
        })


async def async_setup_api(hass: HomeAssistant) -> None:
    for view in (
        CouchMateEntitiesView(),
        CouchMateInfoView(),
        PairingCreateView(),
        PairingStatusView(),
        PairingApproveView(),
        PairingExchangeView(),
        PairingCancelView(),
        CouchMateClientInfoView(),
        CouchMateClientEntitiesView(),
        CouchMateClientSnapshotView(),
        CouchMateClientStreamView(),
        CouchMateClientServiceView(),
    ):
        hass.http.register_view(view)
    _LOGGER.info("CouchMate Core Dev Preview REST and pairing API endpoints registered")
