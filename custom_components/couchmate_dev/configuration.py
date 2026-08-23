"""Versioned, shared CouchMate configuration storage.

This module deliberately uses its own Home Assistant ``Store`` key.  It does
not read from or write to the existing entity-selection storage and can be
introduced without changing the behaviour of existing CouchMate clients.

The manager owns four kinds of data:

* shared home settings,
* private background metadata in a reserved top-level namespace,
* named Apple TV profiles, and
* the assignment of paired clients to those profiles.

All values crossing the public boundary are defensive copies.  Mutating
methods use the document revision for optimistic concurrency control.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import logging
import math
import re
import unicodedata
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONFIGURATION_SCHEMA_VERSION,
    CONFIGURATION_STORAGE_KEY,
    CONFIGURATION_STORAGE_VERSION,
    DEFAULT_PROFILE_ID,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PROFILE_NAME = "Standard"

_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_JSON_DEPTH = 20
_MAX_JSON_NODES = 20_000
_MAX_KEY_LENGTH = 256
_MAX_STRING_LENGTH = 256 * 1024
_MAX_PROFILE_NAME_LENGTH = 80
_MAX_IDENTIFIER_LENGTH = 256
_PROFILE_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_BETA1_BACKGROUND_KEYS = {
    "schema_version",
    "kind",
    "updated_at",
    "etag",
    "focal_point",
    "source",
    "variants",
}
_BETA1_VARIANT_DIMENSIONS = {
    "thumbnail": (640, 360),
    "1080p": (1920, 1080),
    "2160p": (3840, 2160),
}


class ConfigurationError(Exception):
    """Base exception for CouchMate configuration operations."""


class ConflictError(ConfigurationError):
    """Raised when an optimistic revision check fails."""

    def __init__(self, expected_revision: int, current_revision: int) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            "Configuration revision conflict: expected "
            f"{expected_revision}, current revision is {current_revision}"
        )


class ValidationError(ConfigurationError, ValueError):
    """Raised when configuration input is not safe JSON or is malformed."""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        prefix = f"{path}: " if path else ""
        super().__init__(f"{prefix}{message}")


class NotFoundError(ConfigurationError, LookupError):
    """Raised when a requested profile does not exist."""

    def __init__(self, resource: str, identifier: str) -> None:
        self.resource = resource
        self.identifier = identifier
        super().__init__(f"{resource} not found: {identifier}")


def _default_profile() -> dict[str, Any]:
    return {
        "id": DEFAULT_PROFILE_ID,
        "name": DEFAULT_PROFILE_NAME,
        "revision": 0,
        "settings": {},
    }


def _default_document() -> dict[str, Any]:
    return {
        "schema_version": CONFIGURATION_SCHEMA_VERSION,
        "revision": 0,
        "home": {"rooms": {}},
        # ``home`` and every room object are user-controlled settings.  Keep
        # private file references outside those free-form namespaces so a
        # setting named ``background`` can never be mistaken for asset
        # metadata (or be removed while an upload is managed).
        "backgrounds": {"areas": {}},
        "profiles": {DEFAULT_PROFILE_ID: _default_profile()},
        "client_assignments": {},
    }


def _require_identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("must be a string", path=field)
    identifier = value.strip()
    if not identifier:
        raise ValidationError("must not be empty", path=field)
    if len(identifier) > _MAX_IDENTIFIER_LENGTH:
        raise ValidationError(
            f"must be at most {_MAX_IDENTIFIER_LENGTH} characters", path=field
        )
    if any(ord(character) < 32 for character in identifier):
        raise ValidationError("must not contain control characters", path=field)
    return identifier


def _require_profile_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("must be a string", path="name")
    name = value.strip()
    if not name:
        raise ValidationError("must not be empty", path="name")
    if len(name) > _MAX_PROFILE_NAME_LENGTH:
        raise ValidationError(
            f"must be at most {_MAX_PROFILE_NAME_LENGTH} characters", path="name"
        )
    if any(ord(character) < 32 for character in name):
        raise ValidationError("must not contain control characters", path="name")
    return name


def _require_revision(value: Any, *, field: str = "expected_revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("must be a non-negative integer", path=field)
    return value


def _json_copy(value: Any, *, path: str) -> Any:
    """Validate a value as bounded JSON and return a defensive copy."""
    node_count = 0

    def visit(item: Any, current_path: str, depth: int) -> Any:
        nonlocal node_count
        node_count += 1
        if node_count > _MAX_JSON_NODES:
            raise ValidationError(
                f"contains more than {_MAX_JSON_NODES} values", path=path
            )
        if depth > _MAX_JSON_DEPTH:
            raise ValidationError(
                f"is nested more than {_MAX_JSON_DEPTH} levels", path=current_path
            )

        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            if len(item) > _MAX_STRING_LENGTH:
                raise ValidationError("string is too long", path=current_path)
            return item
        if isinstance(item, int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValidationError("must be a finite number", path=current_path)
            return item
        if isinstance(item, list):
            return [
                visit(child, f"{current_path}[{index}]", depth + 1)
                for index, child in enumerate(item)
            ]
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValidationError("object keys must be strings", path=current_path)
                if not key or len(key) > _MAX_KEY_LENGTH:
                    raise ValidationError(
                        f"object keys must contain 1 to {_MAX_KEY_LENGTH} characters",
                        path=current_path,
                    )
                result[key] = visit(child, f"{current_path}.{key}", depth + 1)
            return result
        raise ValidationError(
            f"unsupported value type {type(item).__name__}", path=current_path
        )

    copied = visit(value, path, 0)
    try:
        encoded = json.dumps(
            copied, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as err:
        raise ValidationError("cannot be encoded as JSON", path=path) from err
    if len(encoded) > _MAX_DOCUMENT_BYTES:
        raise ValidationError(
            f"exceeds the {_MAX_DOCUMENT_BYTES}-byte limit", path=path
        )
    return copied


def _is_beta1_room_background_metadata(area_id: str, value: Any) -> bool:
    """Identify only metadata emitted by the Beta.1 image processor.

    Beta.1 stored private room-image metadata in the otherwise free-form
    ``home.rooms[area_id].background`` setting.  The migration must be strict:
    a user's null, string, or arbitrary design dictionary with the same field
    name is ordinary settings data and must remain byte-for-byte equivalent.
    """

    if not isinstance(value, dict) or set(value) != _BETA1_BACKGROUND_KEYS:
        return False
    if (
        isinstance(value.get("schema_version"), bool)
        or value.get("schema_version") != 1
        or value.get("kind") != "user_upload"
        or not isinstance(value.get("updated_at"), str)
        or not _is_quoted_sha256(value.get("etag"))
    ):
        return False

    focal_point = value.get("focal_point")
    if not isinstance(focal_point, dict) or set(focal_point) != {"x", "y"}:
        return False
    for coordinate in (focal_point.get("x"), focal_point.get("y")):
        if (
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            or not math.isfinite(coordinate)
            or not 0 <= coordinate <= 1
        ):
            return False

    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {"format", "width", "height"}:
        return False
    if source.get("format") not in {"jpeg", "png"}:
        return False
    for dimension in (source.get("width"), source.get("height")):
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or dimension <= 0
        ):
            return False

    variants = value.get("variants")
    if not isinstance(variants, dict) or set(variants) not in (
        {"thumbnail", "1080p"},
        {"thumbnail", "1080p", "2160p"},
    ):
        return False
    directory_name = hashlib.sha256(area_id.encode("utf-8")).hexdigest()[:32]
    required_variant_keys = {
        "width",
        "height",
        "mime_type",
        "bytes",
        "file",
        "sha256",
        "etag",
    }
    for variant_name, variant in variants.items():
        if not isinstance(variant, dict) or set(variant) != required_variant_keys:
            return False
        if (
            (variant.get("width"), variant.get("height"))
            != _BETA1_VARIANT_DIMENSIONS[variant_name]
            or variant.get("mime_type") != "image/jpeg"
            or isinstance(variant.get("bytes"), bool)
            or not isinstance(variant.get("bytes"), int)
            or variant["bytes"] <= 0
        ):
            return False
        digest = variant.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            return False
        if variant.get("etag") != f'"{digest}"':
            return False
        if variant.get("file") != f"{directory_name}/{variant_name}-{digest}.jpg":
            return False
    return True


def _is_quoted_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 66
        and value.startswith('"')
        and value.endswith('"')
        and _SHA256_RE.fullmatch(value[1:-1]) is not None
    )


def _validate_document(value: Any) -> dict[str, Any]:
    """Validate and normalize a complete stored document."""
    document = _json_copy(value, path="configuration")
    if not isinstance(document, dict):
        raise ValidationError("must be an object", path="configuration")
    if document.get("schema_version") != CONFIGURATION_SCHEMA_VERSION:
        raise ValidationError(
            f"unsupported schema version {document.get('schema_version')!r}",
            path="configuration.schema_version",
        )
    document["revision"] = _require_revision(
        document.get("revision"), field="configuration.revision"
    )

    # Only documents without the reserved namespace can be Beta.1 documents.
    # Once the namespace exists, every ``background`` inside settings is
    # unconditionally user data, even if it resembles old private metadata.
    migrate_beta1_room_backgrounds = "backgrounds" not in document

    home = document.get("home")
    if not isinstance(home, dict):
        raise ValidationError("must be an object", path="configuration.home")
    rooms = home.setdefault("rooms", {})
    if not isinstance(rooms, dict):
        raise ValidationError("must be an object", path="configuration.home.rooms")
    for area_id, room in rooms.items():
        _require_identifier(area_id, field="configuration.home.rooms key")
        if not isinstance(room, dict):
            raise ValidationError(
                "must be an object", path=f"configuration.home.rooms.{area_id}"
            )

    # Beta.1 intentionally treated ``home`` and each room as free-form
    # settings.  Background metadata therefore has its own reserved top-level
    # namespace.  ``setdefault`` is the additive migration for valid Beta.1
    # documents, and deliberately leaves every legacy home/room value intact.
    backgrounds = document.setdefault("backgrounds", {"areas": {}})
    if not isinstance(backgrounds, dict):
        raise ValidationError(
            "must be an object", path="configuration.backgrounds"
        )
    if "home" in backgrounds and not isinstance(backgrounds["home"], dict):
        raise ValidationError(
            "must be an object", path="configuration.backgrounds.home"
        )
    background_areas = backgrounds.setdefault("areas", {})
    if not isinstance(background_areas, dict):
        raise ValidationError(
            "must be an object", path="configuration.backgrounds.areas"
        )
    for area_id, metadata in background_areas.items():
        _require_identifier(area_id, field="configuration.backgrounds.areas key")
        if not isinstance(metadata, dict):
            raise ValidationError(
                "must be an object",
                path=f"configuration.backgrounds.areas.{area_id}",
            )

    if migrate_beta1_room_backgrounds:
        for area_id, room in rooms.items():
            legacy_metadata = room.get("background")
            if _is_beta1_room_background_metadata(area_id, legacy_metadata):
                background_areas.setdefault(area_id, legacy_metadata)
                del room["background"]

    profiles = document.get("profiles")
    if not isinstance(profiles, dict):
        raise ValidationError("must be an object", path="configuration.profiles")
    if DEFAULT_PROFILE_ID not in profiles:
        raise ValidationError(
            "the default profile is required", path="configuration.profiles"
        )
    for profile_id, profile in profiles.items():
        normalized_id = _require_identifier(
            profile_id, field="configuration.profiles key"
        )
        if not isinstance(profile, dict):
            raise ValidationError(
                "must be an object", path=f"configuration.profiles.{profile_id}"
            )
        if profile.get("id") != normalized_id:
            raise ValidationError(
                "id must match its object key",
                path=f"configuration.profiles.{profile_id}.id",
            )
        profile["name"] = _require_profile_name(profile.get("name"))
        profile["revision"] = _require_revision(
            profile.get("revision"),
            field=f"configuration.profiles.{profile_id}.revision",
        )
        if not isinstance(profile.get("settings"), dict):
            raise ValidationError(
                "must be an object",
                path=f"configuration.profiles.{profile_id}.settings",
            )

    assignments = document.get("client_assignments")
    if not isinstance(assignments, dict):
        raise ValidationError(
            "must be an object", path="configuration.client_assignments"
        )
    for client_id, profile_id in assignments.items():
        _require_identifier(client_id, field="configuration.client_assignments key")
        if not isinstance(profile_id, str) or profile_id not in profiles:
            raise ValidationError(
                "must reference an existing profile",
                path=f"configuration.client_assignments.{client_id}",
            )
    return document


class ConfigurationManager:
    """Manage CouchMate Dev Preview's independently persisted shared configuration."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(
            hass, CONFIGURATION_STORAGE_VERSION, CONFIGURATION_STORAGE_KEY
        )
        self._lock = asyncio.Lock()
        self._data = _default_document()
        self._initialized = False

    async def async_initialize(self) -> None:
        """Load the stored document once.

        Invalid data is never partially exposed or overwritten during load.  A
        later valid mutation starts from safe defaults and replaces it through
        Home Assistant's atomic ``Store`` implementation.
        """
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            try:
                stored = await self._store.async_load()
                if stored is not None:
                    self._data = _validate_document(stored)
            except Exception:  # noqa: BLE001 - corrupt storage must not break setup
                _LOGGER.exception(
                    "Unable to load CouchMate shared configuration; using defaults"
                )
                self._data = _default_document()
            self._initialized = True

    async def async_remove(self) -> None:
        """Remove all CouchMate settings with the integration."""
        async with self._lock:
            await self._store.async_remove()
            self._data = _default_document()
            self._initialized = True

    def snapshot(self) -> dict[str, Any]:
        """Return the complete configuration as a defensive copy."""
        return deepcopy(self._data)

    def client_snapshot(self, client_id: str) -> dict[str, Any]:
        """Return the effective shared configuration for one paired client."""
        normalized_client_id = _require_identifier(client_id, field="client_id")
        assigned_profile_id = self._data["client_assignments"].get(
            normalized_client_id, DEFAULT_PROFILE_ID
        )
        explicit = normalized_client_id in self._data["client_assignments"]
        return {
            "home": deepcopy(self._data["home"]),
            "profile": deepcopy(self._data["profiles"][assigned_profile_id]),
            "assignment": {
                "client_id": normalized_client_id,
                "profile_id": assigned_profile_id,
                "explicit": explicit,
            },
            "sync_token": self._data["revision"],
        }

    def list_profiles(self) -> list[dict[str, Any]]:
        """Return profiles, with the non-deletable default profile first."""
        profiles = [deepcopy(profile) for profile in self._data["profiles"].values()]
        return sorted(
            profiles,
            key=lambda profile: (
                profile["id"] != DEFAULT_PROFILE_ID,
                profile["name"].casefold(),
                profile["id"],
            ),
        )

    async def async_set_home(
        self, settings: Mapping[str, Any], expected_revision: int | None
    ) -> dict[str, Any]:
        """Replace shared home settings after an optimistic revision check."""
        await self.async_initialize()
        safe_settings = _json_copy(settings, path="home")
        if not isinstance(safe_settings, dict):
            raise ValidationError("must be an object", path="home")
        async with self._lock:
            self._check_revision(expected_revision)
            if "rooms" not in safe_settings:
                safe_settings["rooms"] = {}
            if not isinstance(safe_settings["rooms"], dict):
                raise ValidationError("must be an object", path="home.rooms")
            for area_id, room in safe_settings["rooms"].items():
                if not isinstance(room, dict):
                    raise ValidationError(
                        "must be an object", path=f"home.rooms.{area_id}"
                    )
            if safe_settings == self._data["home"]:
                return deepcopy(self._data["home"])
            candidate = deepcopy(self._data)
            candidate["home"] = safe_settings
            await self._commit(candidate)
            return deepcopy(self._data["home"])

    async def async_create_profile(
        self,
        name: str,
        copy_from_profile_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Create a named profile, optionally copying another profile's settings."""
        await self.async_initialize()
        normalized_name = _require_profile_name(name)
        if copy_from_profile_id is not None:
            copy_from_profile_id = _require_identifier(
                copy_from_profile_id, field="copy_from_profile_id"
            )
        async with self._lock:
            self._check_revision(expected_revision)
            source_settings: dict[str, Any] = {}
            if copy_from_profile_id is not None:
                source = self._data["profiles"].get(copy_from_profile_id)
                if source is None:
                    raise NotFoundError("profile", copy_from_profile_id)
                source_settings = deepcopy(source["settings"])
            profile_id = self._unique_profile_id(normalized_name)
            profile = {
                "id": profile_id,
                "name": normalized_name,
                "revision": 0,
                "settings": source_settings,
            }
            candidate = deepcopy(self._data)
            candidate["profiles"][profile_id] = profile
            await self._commit(candidate)
            return deepcopy(self._data["profiles"][profile_id])

    async def async_update_profile(
        self,
        profile_id: str,
        name: str | None = None,
        settings: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Update a profile's display name and/or settings."""
        await self.async_initialize()
        normalized_profile_id = _require_identifier(profile_id, field="profile_id")
        normalized_name = _require_profile_name(name) if name is not None else None
        safe_settings = (
            _json_copy(settings, path="settings") if settings is not None else None
        )
        if safe_settings is not None and not isinstance(safe_settings, dict):
            raise ValidationError("must be an object", path="settings")
        async with self._lock:
            self._check_revision(expected_revision)
            existing = self._data["profiles"].get(normalized_profile_id)
            if existing is None:
                raise NotFoundError("profile", normalized_profile_id)
            candidate = deepcopy(self._data)
            profile = candidate["profiles"][normalized_profile_id]
            if normalized_name is not None:
                profile["name"] = normalized_name
            if safe_settings is not None:
                profile["settings"] = safe_settings
            if profile == existing:
                return deepcopy(existing)
            profile["revision"] = existing["revision"] + 1
            await self._commit(candidate)
            return deepcopy(self._data["profiles"][normalized_profile_id])

    async def async_delete_profile(
        self, profile_id: str, expected_revision: int | None
    ) -> dict[str, Any]:
        """Delete a profile and safely move its clients to the default profile."""
        await self.async_initialize()
        normalized_profile_id = _require_identifier(profile_id, field="profile_id")
        if normalized_profile_id == DEFAULT_PROFILE_ID:
            raise ValidationError(
                "the default profile cannot be deleted", path="profile_id"
            )
        async with self._lock:
            self._check_revision(expected_revision)
            if normalized_profile_id not in self._data["profiles"]:
                raise NotFoundError("profile", normalized_profile_id)
            candidate = deepcopy(self._data)
            del candidate["profiles"][normalized_profile_id]
            candidate["client_assignments"] = {
                client_id: (
                    DEFAULT_PROFILE_ID
                    if assigned_profile_id == normalized_profile_id
                    else assigned_profile_id
                )
                for client_id, assigned_profile_id in candidate[
                    "client_assignments"
                ].items()
            }
            await self._commit(candidate)
            return self.snapshot()

    async def async_assign_profile(
        self,
        client_id: str,
        profile_id: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Assign a paired client to a named profile."""
        await self.async_initialize()
        normalized_client_id = _require_identifier(client_id, field="client_id")
        normalized_profile_id = _require_identifier(profile_id, field="profile_id")
        async with self._lock:
            self._check_revision(expected_revision)
            if normalized_profile_id not in self._data["profiles"]:
                raise NotFoundError("profile", normalized_profile_id)
            if (
                self._data["client_assignments"].get(normalized_client_id)
                == normalized_profile_id
            ):
                return self.client_snapshot(normalized_client_id)
            candidate = deepcopy(self._data)
            candidate["client_assignments"][normalized_client_id] = (
                normalized_profile_id
            )
            await self._commit(candidate)
            return self.client_snapshot(normalized_client_id)

    async def async_update_assigned_profile_settings(
        self,
        client_id: str,
        settings: Mapping[str, Any],
        expected_revision: int | None,
    ) -> dict[str, Any]:
        """Replace the settings of the profile effective for a client."""
        await self.async_initialize()
        normalized_client_id = _require_identifier(client_id, field="client_id")
        safe_settings = _json_copy(settings, path="settings")
        if not isinstance(safe_settings, dict):
            raise ValidationError("must be an object", path="settings")
        async with self._lock:
            self._check_revision(expected_revision)
            profile_id = self._data["client_assignments"].get(
                normalized_client_id, DEFAULT_PROFILE_ID
            )
            existing = self._data["profiles"][profile_id]
            if safe_settings == existing["settings"]:
                return deepcopy(existing)
            candidate = deepcopy(self._data)
            candidate_profile = candidate["profiles"][profile_id]
            candidate_profile["settings"] = safe_settings
            candidate_profile["revision"] = existing["revision"] + 1
            await self._commit(candidate)
            return deepcopy(self._data["profiles"][profile_id])

    def home_background(self) -> dict[str, Any] | None:
        """Return the explicit whole-home background metadata, if configured."""
        background = self._data["backgrounds"].get("home")
        return deepcopy(background) if isinstance(background, dict) else None

    def background_override_for_area(self, area_id: str) -> dict[str, Any] | None:
        """Return an area's explicit background override, if configured."""
        normalized_area_id = _require_identifier(area_id, field="area_id")
        background = self._data["backgrounds"]["areas"].get(normalized_area_id)
        return deepcopy(background) if isinstance(background, dict) else None

    def background_for_area(self, area_id: str) -> dict[str, Any] | None:
        """Compatibility alias returning only an area's explicit override."""
        return self.background_override_for_area(area_id)

    def effective_background_for_area(self, area_id: str) -> dict[str, Any] | None:
        """Return an area's override or, when absent, the whole-home background."""
        override = self.background_override_for_area(area_id)
        return override if override is not None else self.home_background()

    def background_source_for_area(self, area_id: str) -> str | None:
        """Return ``area`` or ``home`` for the area's effective background."""
        if self.background_override_for_area(area_id) is not None:
            return "area"
        return "home" if self.home_background() is not None else None

    def background_area_ids(self) -> list[str]:
        """Return area ids that currently own explicit background overrides."""
        return sorted(self._data["backgrounds"]["areas"])

    async def async_set_home_background(
        self,
        metadata: Mapping[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Set whole-home background metadata; image bytes live elsewhere."""
        await self.async_initialize()
        safe_metadata = _json_copy(metadata, path="metadata")
        if not isinstance(safe_metadata, dict):
            raise ValidationError("must be an object", path="metadata")
        async with self._lock:
            self._check_revision(expected_revision)
            if self._data["backgrounds"].get("home") == safe_metadata:
                return deepcopy(safe_metadata)
            candidate = deepcopy(self._data)
            candidate["backgrounds"]["home"] = safe_metadata
            await self._commit(candidate)
            return deepcopy(safe_metadata)

    async def async_remove_home_background(
        self, expected_revision: int | None = None
    ) -> bool:
        """Remove only the whole-home background, preserving area overrides."""
        await self.async_initialize()
        async with self._lock:
            self._check_revision(expected_revision)
            if "home" not in self._data["backgrounds"]:
                return False
            candidate = deepcopy(self._data)
            del candidate["backgrounds"]["home"]
            await self._commit(candidate)
            return True

    async def async_set_background(
        self,
        area_id: str,
        metadata: Mapping[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Set JSON metadata for a room background; image bytes live elsewhere."""
        await self.async_initialize()
        normalized_area_id = _require_identifier(area_id, field="area_id")
        safe_metadata = _json_copy(metadata, path="metadata")
        if not isinstance(safe_metadata, dict):
            raise ValidationError("must be an object", path="metadata")
        async with self._lock:
            self._check_revision(expected_revision)
            current = self._data["backgrounds"]["areas"].get(normalized_area_id)
            if current == safe_metadata:
                return deepcopy(safe_metadata)
            candidate = deepcopy(self._data)
            candidate["backgrounds"]["areas"][normalized_area_id] = safe_metadata
            await self._commit(candidate)
            return deepcopy(safe_metadata)

    async def async_remove_background(
        self, area_id: str, expected_revision: int | None = None
    ) -> bool:
        """Remove one room's background metadata, preserving other room data."""
        await self.async_initialize()
        normalized_area_id = _require_identifier(area_id, field="area_id")
        async with self._lock:
            self._check_revision(expected_revision)
            if normalized_area_id not in self._data["backgrounds"]["areas"]:
                return False
            candidate = deepcopy(self._data)
            del candidate["backgrounds"]["areas"][normalized_area_id]
            await self._commit(candidate)
            return True

    def _check_revision(self, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        normalized_revision = _require_revision(expected_revision)
        current_revision = self._data["revision"]
        if normalized_revision != current_revision:
            raise ConflictError(normalized_revision, current_revision)

    def _unique_profile_id(self, name: str) -> str:
        ascii_name = unicodedata.normalize("NFKD", name).encode(
            "ascii", "ignore"
        ).decode("ascii")
        base = _PROFILE_SLUG_RE.sub("-", ascii_name.casefold()).strip("-")
        if not base or base == DEFAULT_PROFILE_ID:
            base = "profile"
        base = base[:80].rstrip("-") or "profile"
        candidate = base
        suffix = 2
        while candidate in self._data["profiles"]:
            suffix_text = f"-{suffix}"
            candidate = f"{base[: 80 - len(suffix_text)].rstrip('-')}{suffix_text}"
            suffix += 1
        return candidate

    async def _commit(self, candidate: dict[str, Any]) -> None:
        candidate["schema_version"] = CONFIGURATION_SCHEMA_VERSION
        candidate["revision"] = self._data["revision"] + 1
        validated = _validate_document(candidate)
        await self._store.async_save(validated)
        self._data = validated


# Explicit alias for call sites that prefer the product-qualified name.
CouchMateConfigurationManager = ConfigurationManager


__all__ = [
    "CONFIGURATION_SCHEMA_VERSION",
    "CONFIGURATION_STORAGE_KEY",
    "CONFIGURATION_STORAGE_VERSION",
    "DEFAULT_PROFILE_ID",
    "ConfigurationManager",
    "CouchMateConfigurationManager",
    "ConfigurationError",
    "ConflictError",
    "ValidationError",
    "NotFoundError",
]
