"""Additive CouchMate2 configuration and private-background API."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
import re
from typing import Any
from urllib.parse import quote

from aiohttp import web
from homeassistant.components import persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .backgrounds import (
    BackgroundManager,
    BackgroundProcessingUnavailable,
    BackgroundValidationError,
    MAX_UPLOAD_BYTES,
    VARIANT_1080P,
    VARIANT_2160P,
    VARIANT_THUMBNAIL,
)
from .configuration import ConfigurationManager, ConflictError, NotFoundError, ValidationError
from .const import BACKGROUND_MANAGER, CONFIGURATION_MANAGER, DOMAIN, PAIRING_MANAGER
from .pairing import PairingManager, PairingSession, PairingStatus

_VARIANTS = {VARIANT_THUMBNAIL, VARIANT_1080P, VARIANT_2160P}
_REVISION_RE = re.compile(r"^[a-f0-9]{64}$")
_PAIRING_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CLIENT_REPRESENTATION_VERSION = 2
_LOGGER = logging.getLogger(__name__)


def _configuration(hass) -> ConfigurationManager:
    return hass.data[DOMAIN][CONFIGURATION_MANAGER]


def _backgrounds(hass) -> BackgroundManager:
    return hass.data[DOMAIN][BACKGROUND_MANAGER]


def _pairing(hass) -> PairingManager:
    return hass.data[DOMAIN][PAIRING_MANAGER]


def _error(code: str, status: int, message: str | None = None, **extra) -> web.Response:
    payload: dict[str, Any] = {"error": code, **extra}
    if message:
        payload["message"] = message
    return web.json_response(payload, status=status)


def _require_admin(request: web.Request) -> web.Response | None:
    user = request.get("hass_user")
    return None if user is not None and getattr(user, "is_admin", False) else _error("admin_required", 403)


def _no_store(response: web.Response) -> web.Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _admin_pairing_payload(session: PairingSession) -> dict[str, Any]:
    """Serialize a pairing request without exposing its exchange secret."""
    status = session.refresh_status()
    return {
        "session_id": session.session_id,
        "code": session.code,
        "device_name": session.device_name,
        "status": status.value,
        "created_at": session.created_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "expires_in": session.remaining_seconds,
        "capabilities": list(session.capabilities),
    }


def _admin_pairing_for_action(
    request: web.Request,
) -> tuple[PairingSession | None, web.Response | None]:
    """Resolve one pending request and return a precise lifecycle error."""
    session_id = request.match_info.get("session_id", "")
    if not _PAIRING_SESSION_ID_RE.fullmatch(session_id):
        return None, _error("invalid_session_id", 400)
    session = _pairing(request.app["hass"]).get_by_session_id(session_id)
    if session is None:
        return None, _error("pairing_not_found", 404)
    status = session.refresh_status()
    if status != PairingStatus.WAITING:
        return None, _error(
            "pairing_not_pending",
            409,
            pairing_status=status.value,
        )
    return session, None


async def _client_id(request: web.Request) -> str | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return await _pairing(request.app["hass"]).async_validate_client_token(token)


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError) as err:
        raise ValidationError("request body must be valid JSON") from err
    if not isinstance(body, dict):
        raise ValidationError("request body must be an object")
    return body


def _operation_error(error: Exception) -> web.Response:
    if isinstance(error, ConflictError):
        return _error("revision_conflict", 409, str(error), current_revision=error.current_revision)
    if isinstance(error, NotFoundError):
        return _error("not_found", 404, str(error))
    if isinstance(error, ValidationError):
        return _error("invalid_configuration", 400, str(error))
    if isinstance(error, BackgroundProcessingUnavailable):
        return _error("image_processing_unavailable", 503, str(error))
    if isinstance(error, BackgroundValidationError):
        text = str(error)
        lowered = text.lower()
        if "10 mib" in lowered:
            return _error("image_too_large", 413, text)
        if "jpeg" in lowered or "png" in lowered or "content type" in lowered:
            return _error("unsupported_image", 415, text)
        return _error("invalid_image", 422, text)
    _LOGGER.exception("Unexpected CouchMate2 configuration API error")
    return _error("internal_error", 500)


def _allowed_area_ids(hass) -> set[str]:
    """Resolve areas from the established global entity authorization boundary."""
    runtime = hass.data.get(DOMAIN, {})
    selection_model = runtime.get("selection_model")
    model_areas = selection_model.get("areas", {}) if isinstance(selection_model, dict) else {}
    allowed = {str(area_id) for area_id in model_areas if area_id}
    allowed.update(str(area_id) for area_id in runtime.get("areas", []) if area_id)
    allowed.update(map(str, runtime.get("room_temperatures", {})))
    allowed.update(map(str, runtime.get("room_humidities", {})))
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    for entity_id in runtime.get("entities", []):
        entry = entity_registry.async_get(entity_id)
        if entry is None:
            continue
        device = device_registry.async_get(entry.device_id) if entry.device_id else None
        area_id = entry.area_id or (device.area_id if device else None)
        if area_id:
            allowed.add(str(area_id))
    return allowed.intersection(map(str, ar.async_get(hass).areas))


def _area_id(room_key: str) -> str:
    prefix = "core:area:"
    return room_key[len(prefix):] if room_key.startswith(prefix) else room_key


def _revision(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    value = str(metadata.get("etag", "")).strip().strip('"').lower()
    return value if _REVISION_RE.fullmatch(value) else None


def _public_background(
    area_id: str | None,
    metadata: Any,
    *,
    admin: bool,
) -> dict[str, Any] | None:
    revision = _revision(metadata)
    variants = metadata.get("variants") if isinstance(metadata, dict) else None
    if revision is None or not isinstance(variants, dict):
        return None
    if admin:
        preview_base = (
            "/api/couchmate/v2/admin/home-background"
            if area_id is None
            else "/api/couchmate/v2/admin/backgrounds/"
            f"{quote(area_id, safe='')}"
        )
    elif area_id is None:
        return None
    else:
        preview_base = ""
    public_variants = {}
    for name, item in variants.items():
        if name not in _VARIANTS or not isinstance(item, dict):
            continue
        url = (
            f"{preview_base}?variant={quote(name, safe='')}"
            if admin
            else "/api/couchmate/v2/client/backgrounds/"
            f"{quote(area_id, safe='')}/{revision}/{quote(name, safe='')}"
        )
        public_variants[name] = {
            "url": url,
            "width": item.get("width"),
            "height": item.get("height"),
            "content_type": item.get("mime_type", "image/jpeg"),
            "etag": item.get("etag"),
        }
    if not public_variants:
        return None
    result = {
        "revision": revision,
        "updated_at": metadata.get("updated_at"),
        "focal_point": deepcopy(metadata.get("focal_point")),
        "variants": public_variants,
    }
    if admin:
        result["admin_preview_url"] = f"{preview_base}?variant={VARIANT_THUMBNAIL}"
    return result


def _public_home(
    home: dict[str, Any],
    allowed: set[str] | None,
) -> dict[str, Any]:
    """Return free-form home settings without collapsing area-key aliases.

    Older clients may use ``core:area:<id>`` while the background manager uses
    the bare Home Assistant area id.  Both keys are valid free-form settings
    and can coexist.  Keeping every authorized entry is the only lossless
    representation; effective image assets are published separately from this
    document so they cannot overwrite user-defined design fields.
    """
    result = deepcopy(home)
    if allowed is None:
        return result

    raw_rooms = result.get("rooms", {})
    if isinstance(raw_rooms, dict):
        result["rooms"] = {
            key: room
            for key, room in raw_rooms.items()
            if _area_id(str(key)) in allowed
        }
    if isinstance(result.get("room_order"), list):
        result["room_order"] = [
            key
            for key in result["room_order"]
            if _area_id(str(key)) in allowed
        ]
    return result


def _public_client_backgrounds(hass, allowed: set[str]) -> dict[str, Any]:
    """Publish effective room assets outside the free-form home document."""
    background_manager = _backgrounds(hass)
    rooms: dict[str, dict[str, Any]] = {}
    for area_id in sorted(allowed):
        metadata = background_manager.effective_background_for_area(area_id)
        background = _public_background(area_id, metadata, admin=False)
        source = background_manager.background_source_for_area(area_id)
        rooms[area_id] = {
            "source": source if background is not None and source else "generated",
            "background": background,
        }
    return {"rooms": rooms}


def _client_response(request: web.Request, client_id: str) -> web.Response:
    hass = request.app["hass"]
    manager = _configuration(hass)
    document = manager.snapshot()
    effective = manager.client_snapshot(client_id)
    profile = effective["profile"]
    allowed_area_ids = _allowed_area_ids(hass)
    authorization_scope = "\0".join(sorted(allowed_area_ids))
    etag_value = hashlib.sha256(
        (
            f"v{_CLIENT_REPRESENTATION_VERSION}:"
            f"{document['revision']}:{profile['id']}:{profile['revision']}:"
            f"{authorization_scope}"
        ).encode()
    ).hexdigest()
    etag = f'"{etag_value}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization"}
    if _etag_matches(request.headers.get("If-None-Match"), etag):
        return web.Response(status=304, headers=headers)
    client = _pairing(hass).client_info(client_id) or {"client_id": client_id}
    return web.json_response({
        "schema_version": document["schema_version"],
        "revision": document["revision"],
        "sync_token": effective["sync_token"],
        "client": {
            "client_id": client_id,
            "device_name": client.get("device_name", "Apple TV"),
            "created_at": client.get("created_at"),
            "last_seen": client.get("last_seen"),
            "assigned_profile_id": effective["assignment"]["profile_id"],
            "capabilities": client.get("capabilities", []),
        },
        "home": _public_home(effective["home"], allowed_area_ids),
        "backgrounds": _public_client_backgrounds(hass, allowed_area_ids),
        "profile": profile,
        "available_profiles": [
            {"id": item["id"], "name": item["name"], "revision": item["revision"]}
            for item in manager.list_profiles()
        ],
    }, headers=headers)


class V2ClientConfigurationView(HomeAssistantView):
    url = "/api/couchmate/v2/client/configuration"
    name = "api:couchmate:v2:client:configuration"
    requires_auth = False

    async def get(self, request):
        client_id = await _client_id(request)
        return _error("unauthorized", 401) if client_id is None else _client_response(request, client_id)


class V2ClientProfilesView(HomeAssistantView):
    url = "/api/couchmate/v2/client/profiles"
    name = "api:couchmate:v2:client:profiles"
    requires_auth = False

    async def get(self, request):
        if await _client_id(request) is None:
            return _error("unauthorized", 401)
        profiles = _configuration(request.app["hass"]).list_profiles()
        return web.json_response({"profiles": [{"id": p["id"], "name": p["name"], "revision": p["revision"]} for p in profiles]})


class V2ClientProfileView(HomeAssistantView):
    url = "/api/couchmate/v2/client/profile"
    name = "api:couchmate:v2:client:profile"
    requires_auth = False

    async def put(self, request):
        client_id = await _client_id(request)
        if client_id is None:
            return _error("unauthorized", 401)
        hass = request.app["hass"]
        if not _pairing(hass).client_has_capability(client_id, "configuration:write"):
            return _error("capability_required", 403)
        try:
            body = await _json_body(request)
            await _configuration(hass).async_assign_profile(
                client_id, body.get("profile_id"), body.get("expected_revision")
            )
            return _client_response(request, client_id)
        except Exception as err:  # noqa: BLE001
            return _operation_error(err)


class V2ClientSettingsView(HomeAssistantView):
    url = "/api/couchmate/v2/client/settings"
    name = "api:couchmate:v2:client:settings"
    requires_auth = False

    async def put(self, request):
        client_id = await _client_id(request)
        if client_id is None:
            return _error("unauthorized", 401)
        hass = request.app["hass"]
        if not _pairing(hass).client_has_capability(client_id, "configuration:write"):
            return _error("capability_required", 403)
        try:
            body = await _json_body(request)
            await _configuration(hass).async_update_assigned_profile_settings(
                client_id, body.get("settings"), body.get("expected_revision")
            )
            return _client_response(request, client_id)
        except Exception as err:  # noqa: BLE001
            return _operation_error(err)


def _file_response(
    request: web.Request,
    metadata: Any,
    path,
    variant: str,
    *,
    immutable: bool,
) -> web.StreamResponse:
    variants = metadata.get("variants", {}) if isinstance(metadata, dict) else {}
    variant_data = variants.get(variant, {}) if isinstance(variants, dict) else {}
    if path is None or not isinstance(variant_data, dict):
        return _error("not_found", 404)
    etag = variant_data.get("etag", "")
    headers = {
        "Content-Type": variant_data.get("mime_type", "image/jpeg"),
        "ETag": etag,
        "Cache-Control": "private, max-age=31536000, immutable" if immutable else "private, no-cache",
        "Vary": "Authorization",
        "X-Content-Type-Options": "nosniff",
    }
    if _etag_matches(request.headers.get("If-None-Match"), etag):
        return web.Response(status=304, headers=headers)
    return web.FileResponse(path, headers=headers)


def _etag_matches(header: str | None, etag: Any) -> bool:
    """Match an HTTP If-None-Match value without weakening opaque ETags."""
    if not header or not isinstance(etag, str) or not etag:
        return False
    expected = etag.removeprefix("W/")
    return any(
        candidate == "*" or candidate.removeprefix("W/") == expected
        for candidate in (part.strip() for part in header.split(","))
    )


def _effective_area_file_response(
    request: web.Request,
    area_id: str,
    variant: str,
    *,
    immutable: bool,
) -> web.StreamResponse:
    manager = _backgrounds(request.app["hass"])
    return _file_response(
        request,
        manager.effective_background_for_area(area_id),
        manager.effective_variant_path(area_id, variant),
        variant,
        immutable=immutable,
    )


def _home_file_response(
    request: web.Request, variant: str, *, immutable: bool
) -> web.StreamResponse:
    manager = _backgrounds(request.app["hass"])
    return _file_response(
        request,
        manager.home_background(),
        manager.home_variant_path(variant),
        variant,
        immutable=immutable,
    )


async def _background_upload(
    request: web.Request,
) -> tuple[bytes, float, float, bool, str | None] | web.Response:
    if (
        request.content_length is not None
        and request.content_length > MAX_UPLOAD_BYTES
    ):
        return _error("image_too_large", 413)
    try:
        focal_x = float(request.query.get("focal_x", "0.5"))
        focal_y = float(request.query.get("focal_y", "0.5"))
    except ValueError as err:
        raise BackgroundValidationError("Focal coordinates must be numbers") from err
    payload = bytearray()
    while True:
        chunk = await request.content.readany()
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > MAX_UPLOAD_BYTES:
            return _error("image_too_large", 413)
    return (
        bytes(payload),
        focal_x,
        focal_y,
        request.query.get("include_4k", "1").lower()
        not in {"0", "false", "no"},
        request.headers.get("Content-Type"),
    )


async def _store_background(request: web.Request, area_id: str) -> web.Response:
    hass = request.app["hass"]
    if ar.async_get(hass).async_get_area(area_id) is None:
        return _error("not_found", 404)
    try:
        upload = await _background_upload(request)
        if isinstance(upload, web.Response):
            return upload
        payload, focal_x, focal_y, include_4k, content_type = upload
        metadata = await _backgrounds(hass).async_store_background(
            area_id,
            payload,
            focal_x=focal_x,
            focal_y=focal_y,
            include_4k=include_4k,
            content_type=content_type,
        )
        return web.json_response(
            {"background": _public_background(area_id, metadata, admin=True)}
        )
    except Exception as err:  # noqa: BLE001
        return _operation_error(err)


async def _store_home_background(request: web.Request) -> web.Response:
    try:
        upload = await _background_upload(request)
        if isinstance(upload, web.Response):
            return upload
        payload, focal_x, focal_y, include_4k, content_type = upload
        metadata = await _backgrounds(
            request.app["hass"]
        ).async_store_home_background(
            payload,
            focal_x=focal_x,
            focal_y=focal_y,
            include_4k=include_4k,
            content_type=content_type,
        )
        return web.json_response(
            {"home_background": _public_background(None, metadata, admin=True)}
        )
    except Exception as err:  # noqa: BLE001
        return _operation_error(err)


class V2ClientBackgroundView(HomeAssistantView):
    url = "/api/couchmate/v2/client/backgrounds/{area_id}/{revision}/{variant}"
    name = "api:couchmate:v2:client:background"
    requires_auth = False

    async def get(self, request):
        if await _client_id(request) is None:
            return _error("unauthorized", 401)
        hass = request.app["hass"]
        area_id = request.match_info["area_id"]
        variant = request.match_info["variant"]
        metadata = _backgrounds(hass).effective_background_for_area(area_id)
        if (
            area_id not in _allowed_area_ids(hass)
            or variant not in _VARIANTS
            or _revision(metadata) != request.match_info["revision"]
        ):
            return _error("not_found", 404)
        return _effective_area_file_response(
            request, area_id, variant, immutable=True
        )


class V2ClientBackgroundMutationView(HomeAssistantView):
    url = "/api/couchmate/v2/client/backgrounds/{area_id}"
    name = "api:couchmate:v2:client:background:mutation"
    requires_auth = False

    async def put(self, request):
        client_id = await _client_id(request)
        if client_id is None:
            return _error("unauthorized", 401)
        hass = request.app["hass"]
        if not _pairing(hass).client_has_capability(client_id, "backgrounds:write"):
            return _error("capability_required", 403)
        area_id = request.match_info["area_id"]
        if area_id not in _allowed_area_ids(hass):
            return _error("not_found", 404)
        return await _store_background(request, area_id)

    async def delete(self, request):
        client_id = await _client_id(request)
        if client_id is None:
            return _error("unauthorized", 401)
        hass = request.app["hass"]
        if not _pairing(hass).client_has_capability(client_id, "backgrounds:write"):
            return _error("capability_required", 403)
        area_id = request.match_info["area_id"]
        if area_id not in _allowed_area_ids(hass):
            return _error("not_found", 404)
        await _backgrounds(hass).async_remove_background(area_id)
        return web.json_response({"success": True})


def _admin_snapshot(hass) -> dict[str, Any]:
    manager = _configuration(hass)
    background_manager = _backgrounds(hass)
    document = manager.snapshot()
    assignments = document.get("client_assignments", {})
    areas = []
    area_registry = ar.async_get(hass)
    for area in sorted(area_registry.areas.values(), key=lambda item: item.name.casefold()):
        metadata = background_manager.effective_background_for_area(area.id)
        background = _public_background(area.id, metadata, admin=True)
        source = background_manager.background_source_for_area(area.id)
        areas.append({
            "id": area.id,
            "name": area.name,
            "background": background,
            "background_source": (
                source if background is not None and source else "generated"
            ),
        })
    registered_area_ids = {area["id"] for area in areas}
    for area_id in manager.background_area_ids():
        if area_id not in registered_area_ids:
            metadata = background_manager.effective_background_for_area(area_id)
            background = _public_background(area_id, metadata, admin=True)
            source = background_manager.background_source_for_area(area_id)
            areas.append({
                "id": area_id,
                "name": f"Gelöschter Raum ({area_id})",
                "deleted": True,
                "background": background,
                "background_source": (
                    source if background is not None and source else "generated"
                ),
            })
    clients = []
    for client in _pairing(hass).list_clients():
        item = deepcopy(client)
        item["profile_id"] = assignments.get(client["client_id"], "default")
        clients.append(item)
    return {
        "schema_version": document["schema_version"],
        "revision": document["revision"],
        "home_background": _public_background(
            None, background_manager.home_background(), admin=True
        ),
        "home": _public_home(document["home"], None),
        "profiles": manager.list_profiles(),
        "clients": sorted(clients, key=lambda item: str(item.get("device_name", "")).casefold()),
        "areas": areas,
    }


class V2AdminPairingRequestsView(HomeAssistantView):
    """List pairing requests that are still awaiting an administrator."""

    url = "/api/couchmate/v2/admin/pairing-requests"
    name = "api:couchmate:v2:admin:pairing_requests"
    requires_auth = True

    async def get(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return _no_store(denied)
        requests = [
            _admin_pairing_payload(session)
            for session in _pairing(request.app["hass"]).list_pending_sessions()
        ]
        return web.json_response(
            {"pairing_requests": requests, "count": len(requests)},
            headers={"Cache-Control": "no-store"},
        )


class V2AdminPairingApproveView(HomeAssistantView):
    """Approve exactly one still-pending pairing request."""

    url = "/api/couchmate/v2/admin/pairing-requests/{session_id}/approve"
    name = "api:couchmate:v2:admin:pairing_request:approve"
    requires_auth = True

    async def post(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return _no_store(denied)
        session, error = _admin_pairing_for_action(request)
        if error is not None:
            return _no_store(error)
        if session is None:
            return _no_store(_error("pairing_not_found", 404))
        approved = _pairing(request.app["hass"]).approve(session.code)
        if approved is None or approved.status != PairingStatus.APPROVED:
            return _no_store(_error("pairing_not_pending", 409))
        persistent_notification.async_dismiss(
            request.app["hass"], f"{DOMAIN}_pairing_{approved.session_id}"
        )
        return web.json_response(
            {
                "success": True,
                "pairing_request": _admin_pairing_payload(approved),
            },
            headers={"Cache-Control": "no-store"},
        )


class V2AdminPairingRejectView(HomeAssistantView):
    """Reject exactly one still-pending pairing request."""

    url = "/api/couchmate/v2/admin/pairing-requests/{session_id}/reject"
    name = "api:couchmate:v2:admin:pairing_request:reject"
    requires_auth = True

    async def post(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return _no_store(denied)
        session, error = _admin_pairing_for_action(request)
        if error is not None:
            return _no_store(error)
        if session is None:
            return _no_store(_error("pairing_not_found", 404))
        rejected = _pairing(request.app["hass"]).cancel(session.session_id)
        if rejected is None or rejected.status != PairingStatus.CANCELLED:
            return _no_store(_error("pairing_not_pending", 409))
        persistent_notification.async_dismiss(
            request.app["hass"], f"{DOMAIN}_pairing_{rejected.session_id}"
        )
        return web.json_response(
            {
                "success": True,
                "pairing_request": _admin_pairing_payload(rejected),
            },
            headers={"Cache-Control": "no-store"},
        )


class V2AdminConfigurationView(HomeAssistantView):
    url = "/api/couchmate/v2/admin/configuration"
    name = "api:couchmate:v2:admin:configuration"
    requires_auth = True

    async def get(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        return web.json_response(_admin_snapshot(request.app["hass"]))

    async def put(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        try:
            body = await _json_body(request)
            await _configuration(request.app["hass"]).async_set_home(
                body.get("home"), body.get("expected_revision")
            )
            return web.json_response(_admin_snapshot(request.app["hass"]))
        except Exception as err:  # noqa: BLE001
            return _operation_error(err)


class V2AdminProfilesView(HomeAssistantView):
    url = "/api/couchmate/v2/admin/profiles"
    name = "api:couchmate:v2:admin:profiles"
    requires_auth = True

    async def post(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        try:
            body = await _json_body(request)
            profile = await _configuration(request.app["hass"]).async_create_profile(
                body.get("name"), body.get("copy_from_profile_id"), body.get("expected_revision")
            )
            return web.json_response({"profile": profile}, status=201)
        except Exception as err:  # noqa: BLE001
            return _operation_error(err)


class V2AdminProfileView(HomeAssistantView):
    url = "/api/couchmate/v2/admin/profiles/{profile_id}"
    name = "api:couchmate:v2:admin:profile"
    requires_auth = True

    async def patch(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        try:
            body = await _json_body(request)
            profile = await _configuration(request.app["hass"]).async_update_profile(
                request.match_info["profile_id"],
                name=body.get("name"),
                settings=body.get("settings"),
                expected_revision=body.get("expected_revision"),
            )
            return web.json_response({"profile": profile})
        except Exception as err:  # noqa: BLE001
            return _operation_error(err)

    async def delete(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        try:
            body = await _json_body(request)
            await _configuration(request.app["hass"]).async_delete_profile(
                request.match_info["profile_id"], body.get("expected_revision")
            )
            return web.json_response({"success": True})
        except Exception as err:  # noqa: BLE001
            return _operation_error(err)


class V2AdminClientProfileView(HomeAssistantView):
    url = "/api/couchmate/v2/admin/clients/{client_id}/profile"
    name = "api:couchmate:v2:admin:client:profile"
    requires_auth = True

    async def put(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        hass = request.app["hass"]
        client_id = request.match_info["client_id"]
        if _pairing(hass).client_info(client_id) is None:
            return _error("not_found", 404)
        try:
            body = await _json_body(request)
            await _configuration(hass).async_assign_profile(
                client_id, body.get("profile_id"), body.get("expected_revision")
            )
            return web.json_response({"success": True})
        except Exception as err:  # noqa: BLE001
            return _operation_error(err)


class V2AdminHomeBackgroundView(HomeAssistantView):
    """Manage the private background inherited by rooms without an override."""

    url = "/api/couchmate/v2/admin/home-background"
    name = "api:couchmate:v2:admin:home_background"
    requires_auth = True

    async def get(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        variant = request.query.get("variant", VARIANT_THUMBNAIL)
        if variant not in _VARIANTS:
            return _error("not_found", 404)
        return _home_file_response(request, variant, immutable=False)

    async def put(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        return await _store_home_background(request)

    async def delete(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        try:
            await _backgrounds(
                request.app["hass"]
            ).async_remove_home_background()
            return web.json_response({"success": True, "home_background": None})
        except Exception as err:  # noqa: BLE001
            return _operation_error(err)


class V2AdminBackgroundView(HomeAssistantView):
    url = "/api/couchmate/v2/admin/backgrounds/{area_id}"
    name = "api:couchmate:v2:admin:background"
    requires_auth = True

    async def get(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        hass = request.app["hass"]
        area_id = request.match_info["area_id"]
        if (
            ar.async_get(hass).async_get_area(area_id) is None
            and _backgrounds(hass).background_override_for_area(area_id) is None
        ):
            return _error("not_found", 404)
        variant = request.query.get("variant", VARIANT_THUMBNAIL)
        if variant not in _VARIANTS:
            return _error("not_found", 404)
        return _effective_area_file_response(
            request, area_id, variant, immutable=False
        )

    async def put(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        return await _store_background(request, request.match_info["area_id"])

    async def delete(self, request):
        denied = _require_admin(request)
        if denied is not None:
            return denied
        hass = request.app["hass"]
        area_id = request.match_info["area_id"]
        if (
            ar.async_get(hass).async_get_area(area_id) is None
            and _backgrounds(hass).background_for_area(area_id) is None
        ):
            return _error("not_found", 404)
        await _backgrounds(hass).async_remove_background(area_id)
        return web.json_response({"success": True})


async def async_setup_configuration_api(hass) -> None:
    """Register endpoints without altering any existing CouchMate route."""
    for view in (
        V2ClientConfigurationView(),
        V2ClientProfilesView(),
        V2ClientProfileView(),
        V2ClientSettingsView(),
        V2ClientBackgroundView(),
        V2ClientBackgroundMutationView(),
        V2AdminPairingRequestsView(),
        V2AdminPairingApproveView(),
        V2AdminPairingRejectView(),
        V2AdminConfigurationView(),
        V2AdminProfilesView(),
        V2AdminProfileView(),
        V2AdminClientProfileView(),
        V2AdminHomeBackgroundView(),
        V2AdminBackgroundView(),
    ):
        hass.http.register_view(view)
