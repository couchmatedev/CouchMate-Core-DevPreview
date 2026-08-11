"""Private, local home and room-background storage for CouchMate.

Uploaded originals are validated and transformed in memory.  Only metadata-free
JPEG variants are persisted below ``/config/couchmate/backgrounds``; the source
image is never retained.  Pillow is deliberately imported inside the executor
job so importing or starting CouchMate does not depend on Pillow being present.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Protocol, TYPE_CHECKING
import warnings

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_LOGGER = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_EDGE = 8192
MIN_IMAGE_WIDTH = 1280
MIN_IMAGE_HEIGHT = 720

BACKGROUND_SCHEMA_VERSION = 1
BACKGROUND_DIRECTORY = ("couchmate", "backgrounds")
# Global image assets are never represented by a synthetic area id.  The
# fixed directory name cannot collide with an area's 32-character hex digest,
# including when a real Home Assistant area happens to be named ``home``.
_HOME_BACKGROUND_DIRECTORY_NAME = "_home"

VARIANT_THUMBNAIL = "thumbnail"
VARIANT_1080P = "1080p"
VARIANT_2160P = "2160p"

_BASE_VARIANTS: tuple[tuple[str, int, int], ...] = (
    (VARIANT_THUMBNAIL, 640, 360),
    (VARIANT_1080P, 1920, 1080),
)
_OPTIONAL_4K_VARIANT = (VARIANT_2160P, 3840, 2160)

_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/png": "PNG",
}


class BackgroundError(Exception):
    """Base class for background handling errors."""


class BackgroundValidationError(BackgroundError, ValueError):
    """The supplied image or focal point is invalid."""


class BackgroundProcessingUnavailable(BackgroundError, RuntimeError):
    """Image processing is unavailable because Pillow is not installed."""


class BackgroundConfigurationManager(Protocol):
    """Configuration-manager surface used by :class:`BackgroundManager`."""

    async def async_set_background(
        self, area_id: str, metadata: Mapping[str, Any]
    ) -> None:
        """Persist background metadata for an area."""

    async def async_remove_background(self, area_id: str) -> None:
        """Remove background metadata for an area."""

    async def async_set_home_background(self, metadata: Mapping[str, Any]) -> None:
        """Persist explicit whole-home background metadata."""

    async def async_remove_home_background(self) -> None:
        """Remove explicit whole-home background metadata."""

    def home_background(self) -> dict[str, Any] | None:
        """Return a defensive copy of the whole-home background metadata."""

    def background_override_for_area(self, area_id: str) -> dict[str, Any] | None:
        """Return a defensive copy of an area's explicit override."""

    def effective_background_for_area(self, area_id: str) -> dict[str, Any] | None:
        """Return an area's override or the inherited whole-home background."""

    def background_source_for_area(self, area_id: str) -> str | None:
        """Return ``area``, ``home``, or ``None`` for an area's background."""

    def background_for_area(self, area_id: str) -> dict[str, Any] | None:
        """Return a defensive copy of the area's background metadata."""

    def background_area_ids(self) -> list[str]:
        """Return all area ids that currently have a background."""


class BackgroundManager:
    """Validate, transform and privately store home and room backgrounds."""

    def __init__(
        self,
        hass: HomeAssistant,
        configuration_manager: BackgroundConfigurationManager,
    ) -> None:
        self._hass = hass
        self._configuration_manager = configuration_manager
        self._config_root = Path(hass.config.path())
        self._root = self._config_root.joinpath(*BACKGROUND_DIRECTORY)
        # Serialising mutations keeps metadata and immutable variant files in a
        # consistent order, including when two uploads target the same area.
        self._mutation_lock = asyncio.Lock()

    @property
    def storage_root(self) -> Path:
        """Return the private storage root (never a Home Assistant www path)."""

        return self._root

    def background_for_area(self, area_id: str) -> dict[str, Any] | None:
        """Return only ``area_id``'s explicit override for compatibility."""

        return self.background_override_for_area(area_id)

    def home_background(self) -> dict[str, Any] | None:
        """Return the explicit whole-home background as a defensive copy."""

        metadata = self._configuration_manager.home_background()
        return deepcopy(metadata) if metadata is not None else None

    def background_override_for_area(self, area_id: str) -> dict[str, Any] | None:
        """Return ``area_id``'s explicit override as a defensive copy."""

        area_id = _validate_area_id(area_id)
        metadata = self._configuration_manager.background_override_for_area(area_id)
        return deepcopy(metadata) if metadata is not None else None

    def effective_background_for_area(self, area_id: str) -> dict[str, Any] | None:
        """Return ``area_id``'s override or its inherited home background."""

        area_id = _validate_area_id(area_id)
        metadata = self._configuration_manager.effective_background_for_area(area_id)
        return deepcopy(metadata) if metadata is not None else None

    def background_source_for_area(self, area_id: str) -> str | None:
        """Return ``area``, ``home``, or ``None`` for the effective background."""

        area_id = _validate_area_id(area_id)
        return self._configuration_manager.background_source_for_area(area_id)

    def variant_path(self, area_id: str, variant: str) -> Path | None:
        """Resolve an explicit area-override variant to a safe private path.

        ``None`` is returned for an unknown variant, invalid legacy metadata, or
        a missing file.  Metadata can therefore never be used for traversal or
        to read a different area's background.
        """

        area_id = _validate_area_id(area_id)
        metadata = self._configuration_manager.background_override_for_area(area_id)
        return self._variant_path(metadata, _area_directory_name(area_id), variant)

    def home_variant_path(self, variant: str) -> Path | None:
        """Resolve a whole-home variant without using a synthetic area id."""

        return self._variant_path(
            self._configuration_manager.home_background(),
            _HOME_BACKGROUND_DIRECTORY_NAME,
            variant,
        )

    def effective_variant_path(self, area_id: str, variant: str) -> Path | None:
        """Resolve an area's explicit or inherited variant to a private path."""

        area_id = _validate_area_id(area_id)
        source = self._configuration_manager.background_source_for_area(area_id)
        if source == "area":
            return self.variant_path(area_id, variant)
        if source == "home":
            return self.home_variant_path(variant)
        return None

    def _variant_path(
        self,
        metadata: Mapping[str, Any] | None,
        directory_name: str,
        variant: str,
    ) -> Path | None:
        """Resolve a variant constrained to its independently selected scope."""

        if not isinstance(metadata, Mapping):
            return None
        variants = metadata.get("variants")
        variant_metadata = variants.get(variant) if isinstance(variants, Mapping) else None
        if not isinstance(variant_metadata, Mapping):
            return None
        relative_file = variant_metadata.get("file")
        if not isinstance(relative_file, str):
            return None
        try:
            path = self._safe_variant_path(directory_name, relative_file)
        except BackgroundValidationError:
            return None
        return path if path.is_file() else None

    async def async_store_background(
        self,
        area_id: str,
        image_data: bytes | bytearray | memoryview,
        *,
        focal_x: float = 0.5,
        focal_y: float = 0.5,
        include_4k: bool = False,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Validate and store a room background, returning its metadata.

        The source must be a static JPEG or PNG no larger than 10 MiB.  Focal
        coordinates are normalised to the inclusive range 0...1 and determine
        the centre of the bounded 16:9 crop.  The original is never written.
        """

        area_id = _validate_area_id(area_id)
        return await self._async_store_target(
            area_id,
            image_data,
            focal_x=focal_x,
            focal_y=focal_y,
            include_4k=include_4k,
            content_type=content_type,
        )

    async def async_store_home_background(
        self,
        image_data: bytes | bytearray | memoryview,
        *,
        focal_x: float = 0.5,
        focal_y: float = 0.5,
        include_4k: bool = False,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Validate and store the whole-home background.

        The global asset has a dedicated storage scope; it is never passed
        through the area-id validation or area directory namespace.
        """

        return await self._async_store_target(
            None,
            image_data,
            focal_x=focal_x,
            focal_y=focal_y,
            include_4k=include_4k,
            content_type=content_type,
        )

    async def _async_store_target(
        self,
        area_id: str | None,
        image_data: bytes | bytearray | memoryview,
        *,
        focal_x: float,
        focal_y: float,
        include_4k: bool,
        content_type: str | None,
    ) -> dict[str, Any]:
        """Store one image in an explicit home or area scope."""

        payload = _validate_upload(image_data, content_type)
        focal_point = (_validate_focal_value(focal_x), _validate_focal_value(focal_y))
        directory_name = (
            _HOME_BACKGROUND_DIRECTORY_NAME
            if area_id is None
            else _area_directory_name(area_id)
        )
        label = "whole home" if area_id is None else f"area {area_id}"

        async with self._mutation_lock:
            old_metadata = (
                self._configuration_manager.home_background()
                if area_id is None
                else self._configuration_manager.background_override_for_area(area_id)
            )
            old_files = _metadata_files(old_metadata)
            metadata, written_files = await self._hass.async_add_executor_job(
                self._process_and_write,
                directory_name,
                payload,
                focal_point,
                bool(include_4k),
                content_type,
            )

            try:
                if area_id is None:
                    await self._configuration_manager.async_set_home_background(metadata)
                else:
                    await self._configuration_manager.async_set_background(
                        area_id, metadata
                    )
            except Exception:
                # Preserve any files referenced by the previous configuration.
                await self._hass.async_add_executor_job(
                    self._remove_unreferenced_files, written_files, old_files
                )
                raise

            try:
                await self._hass.async_add_executor_job(
                    self._cleanup_target_directory,
                    directory_name,
                    _metadata_files(metadata),
                )
            except Exception:
                # The new immutable files and metadata are already consistent;
                # stale variants can safely be removed by a later cleanup.
                _LOGGER.warning(
                    "Unable to clean stale CouchMate backgrounds for %s",
                    label,
                    exc_info=True,
                )

            return deepcopy(metadata)

    async def async_remove_background(self, area_id: str) -> bool:
        """Remove an area's override; it then inherits the home background."""

        area_id = _validate_area_id(area_id)
        async with self._mutation_lock:
            existed = (
                self._configuration_manager.background_override_for_area(area_id)
                is not None
            )
            await self._configuration_manager.async_remove_background(area_id)
            try:
                await self._hass.async_add_executor_job(
                    self._remove_target_directory, _area_directory_name(area_id)
                )
            except Exception:
                _LOGGER.warning(
                    "Unable to remove CouchMate background files for area %s",
                    area_id,
                    exc_info=True,
                )
            return existed

    async def async_remove_home_background(self) -> bool:
        """Remove the home background while retaining every area override."""

        async with self._mutation_lock:
            existed = self._configuration_manager.home_background() is not None
            await self._configuration_manager.async_remove_home_background()
            try:
                await self._hass.async_add_executor_job(
                    self._remove_target_directory,
                    _HOME_BACKGROUND_DIRECTORY_NAME,
                )
            except Exception:
                _LOGGER.warning(
                    "Unable to remove CouchMate whole-home background files",
                    exc_info=True,
                )
            return existed

    async def async_cleanup(self, active_area_ids: Collection[str]) -> None:
        """Remove orphaned directories and stale files.

        ``active_area_ids`` must be the complete current Home Assistant area-id
        collection.  Areas without override metadata have no files to retain;
        the independently stored whole-home background is always retained.
        """

        areas = {_validate_area_id(area_id) for area_id in active_area_ids}
        async with self._mutation_lock:
            for area_id in self._configuration_manager.background_area_ids():
                if area_id not in areas:
                    await self._configuration_manager.async_remove_background(area_id)

            keep_by_directory: dict[str, set[str]] = {}
            home_metadata = self._configuration_manager.home_background()
            if home_metadata is not None:
                keep_by_directory[_HOME_BACKGROUND_DIRECTORY_NAME] = _metadata_files(
                    home_metadata
                )
            for area_id in areas:
                metadata = self._configuration_manager.background_override_for_area(
                    area_id
                )
                if metadata is not None:
                    keep_by_directory[_area_directory_name(area_id)] = _metadata_files(metadata)
            await self._hass.async_add_executor_job(
                self._cleanup_storage_root, keep_by_directory
            )

    async def async_remove_all(self) -> None:
        """Remove every private image variant during integration removal."""
        async with self._mutation_lock:
            await self._hass.async_add_executor_job(self._remove_storage_root)

    def _process_and_write(
        self,
        directory_name: str,
        payload: bytes,
        focal_point: tuple[float, float],
        include_4k: bool,
        content_type: str | None,
    ) -> tuple[dict[str, Any], set[str]]:
        """Process an upload and atomically write immutable variants."""

        try:
            from PIL import Image, ImageOps, UnidentifiedImageError
        except ImportError as err:
            raise BackgroundProcessingUnavailable(
                "Background uploads require the optional Pillow package"
            ) from err

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as probe:
                    source_format = (probe.format or "").upper()
                    if source_format not in {"JPEG", "PNG"}:
                        raise BackgroundValidationError(
                            "Only JPEG and PNG background images are supported"
                        )
                    if getattr(probe, "n_frames", 1) != 1:
                        raise BackgroundValidationError(
                            "Animated background images are not supported"
                        )
                    _validate_dimension_limits(*probe.size)
                    probe.verify()

                declared_type = (
                    content_type.split(";", 1)[0].strip().lower()
                    if content_type
                    else None
                )
                if (
                    declared_type
                    and _ALLOWED_CONTENT_TYPES.get(declared_type) != source_format
                ):
                    raise BackgroundValidationError(
                        "The declared content type does not match the uploaded image"
                    )

                with Image.open(io.BytesIO(payload)) as source:
                    if (source.format or "").upper() != source_format:
                        raise BackgroundValidationError("Image format could not be verified")
                    source.load()
                    oriented = ImageOps.exif_transpose(source)
                    oriented.load()
                    width, height = oriented.size
                    _validate_dimensions(width, height)

                    flattened = Image.new("RGB", oriented.size, (15, 20, 22))
                    if "A" in oriented.getbands() or "transparency" in oriented.info:
                        rgba = oriented.convert("RGBA")
                        flattened.paste(rgba, mask=rgba.getchannel("A"))
                        rgba.close()
                    else:
                        rgb = oriented.convert("RGB")
                        flattened.paste(rgb)
                        rgb.close()

                    crop_box = _focal_crop_box(width, height, *focal_point)
                    cropped = flattened.crop(crop_box)
                    flattened.close()
                    if oriented is not source:
                        oriented.close()

            requested_variants = list(_BASE_VARIANTS)
            if include_4k and cropped.width >= 3840 and cropped.height >= 2160:
                requested_variants.append(_OPTIONAL_4K_VARIANT)

            self._assert_storage_root()
            target_directory = self._target_directory(directory_name)
            _ensure_private_directory(self._root)
            _ensure_private_directory(target_directory)

            resampling = getattr(Image, "Resampling", Image).LANCZOS
            variant_metadata: dict[str, dict[str, Any]] = {}
            written_files: set[str] = set()
            newly_created_files: set[str] = set()
            try:
                for variant_name, target_width, target_height in requested_variants:
                    resized = cropped.resize(
                        (target_width, target_height), resample=resampling
                    )
                    encoded = io.BytesIO()
                    resized.save(
                        encoded,
                        format="JPEG",
                        quality=88,
                        optimize=True,
                        progressive=True,
                        subsampling=2,
                    )
                    resized.close()
                    variant_bytes = encoded.getvalue()
                    digest = hashlib.sha256(variant_bytes).hexdigest()
                    file_name = f"{variant_name}-{digest}.jpg"
                    relative_file = f"{target_directory.name}/{file_name}"
                    final_path = target_directory / file_name
                    existed = final_path.exists()
                    _atomic_write(final_path, variant_bytes)
                    written_files.add(relative_file)
                    if not existed:
                        newly_created_files.add(relative_file)
                    variant_metadata[variant_name] = {
                        "width": target_width,
                        "height": target_height,
                        "mime_type": "image/jpeg",
                        "bytes": len(variant_bytes),
                        "file": relative_file,
                        "sha256": digest,
                        "etag": _etag(digest),
                    }
            except Exception:
                self._remove_unreferenced_files(newly_created_files, set())
                raise
            finally:
                cropped.close()

        except BackgroundError:
            raise
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            SyntaxError,
            ValueError,
        ) as err:
            raise BackgroundValidationError(
                "The uploaded image is damaged or could not be decoded"
            ) from err

        metadata_etag_source = json.dumps(
            {
                "focal_point": focal_point,
                "variants": {
                    key: value["sha256"] for key, value in variant_metadata.items()
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        metadata_digest = hashlib.sha256(metadata_etag_source).hexdigest()
        metadata: dict[str, Any] = {
            "schema_version": BACKGROUND_SCHEMA_VERSION,
            "kind": "user_upload",
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "etag": _etag(metadata_digest),
            "focal_point": {"x": focal_point[0], "y": focal_point[1]},
            "source": {
                "format": source_format.lower(),
                "width": width,
                "height": height,
            },
            "variants": variant_metadata,
        }
        return metadata, written_files

    def _target_directory(self, directory_name: str) -> Path:
        _validate_target_directory_name(directory_name)
        return self._root / directory_name

    def _safe_variant_path(self, directory_name: str, relative_file: str) -> Path:
        self._assert_storage_root()
        _validate_target_directory_name(directory_name)
        relative = Path(relative_file)
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != directory_name
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise BackgroundValidationError("Unsafe background variant path")
        candidate = self._root / relative
        raw_parent = self._root / directory_name
        if self._root.is_symlink() or raw_parent.is_symlink():
            raise BackgroundValidationError("Unsafe background variant path")
        expected_parent = raw_parent.resolve()
        if candidate.resolve().parent != expected_parent:
            raise BackgroundValidationError("Unsafe background variant path")
        return candidate

    def _cleanup_target_directory(
        self, directory_name: str, keep_files: set[str]
    ) -> None:
        self._assert_storage_root()
        _validate_target_directory_name(directory_name)
        if self._root.is_symlink():
            raise BackgroundValidationError("Background storage must not be a symbolic link")
        directory = self._target_directory(directory_name)
        if not directory.exists():
            return
        if directory.is_symlink():
            directory.unlink()
            return
        keep_names = {
            Path(relative).name
            for relative in keep_files
            if Path(relative).parts[:1] == (directory.name,)
        }
        for child in directory.iterdir():
            if child.name not in keep_names:
                _remove_path(child)
        try:
            directory.rmdir()
        except OSError:
            pass

    def _cleanup_storage_root(self, keep_by_directory: Mapping[str, set[str]]) -> None:
        self._assert_storage_root()
        if not self._root.exists():
            return
        if self._root.is_symlink():
            raise BackgroundValidationError("Background storage must not be a symbolic link")
        for child in self._root.iterdir():
            keep_files = keep_by_directory.get(child.name)
            if keep_files is None or not child.is_dir() or child.is_symlink():
                _remove_path(child)
                continue
            keep_names = {
                Path(relative).name
                for relative in keep_files
                if Path(relative).parts[:1] == (child.name,)
            }
            for stored in child.iterdir():
                if stored.name not in keep_names:
                    _remove_path(stored)
        try:
            self._root.rmdir()
        except OSError:
            pass

    def _remove_target_directory(self, directory_name: str) -> None:
        self._assert_storage_root()
        _validate_target_directory_name(directory_name)
        if self._root.is_symlink():
            raise BackgroundValidationError("Background storage must not be a symbolic link")
        _remove_path(self._target_directory(directory_name))
        try:
            self._root.rmdir()
        except OSError:
            pass

    def _remove_unreferenced_files(
        self, candidate_files: Collection[str], preserve_files: Collection[str]
    ) -> None:
        self._assert_storage_root()
        preserve = set(preserve_files)
        for relative_file in set(candidate_files) - preserve:
            relative = Path(relative_file)
            if (
                relative.is_absolute()
                or len(relative.parts) != 2
                or not _is_target_directory_name(relative.parts[0])
            ):
                continue
            candidate = self._root / relative
            raw_parent = self._root / relative.parts[0]
            if self._root.is_symlink() or raw_parent.is_symlink():
                continue
            expected_parent = raw_parent.resolve()
            if candidate.resolve().parent == expected_parent:
                _remove_path(candidate)

    def _assert_storage_root(self) -> None:
        """Reject symlinks below Home Assistant's trusted config directory."""
        current = self._config_root
        for segment in BACKGROUND_DIRECTORY:
            current = current / segment
            if current.is_symlink():
                raise BackgroundValidationError(
                    "Background storage must not contain symbolic links"
                )
        trusted_root = self._config_root.resolve()
        resolved_root = self._root.resolve()
        if resolved_root != trusted_root and trusted_root not in resolved_root.parents:
            raise BackgroundValidationError(
                "Background storage must remain inside the Home Assistant config directory"
            )

    def _remove_storage_root(self) -> None:
        self._assert_storage_root()
        _remove_path(self._root)


def _validate_area_id(area_id: str) -> str:
    if not isinstance(area_id, str) or not area_id or len(area_id) > 512 or "\x00" in area_id:
        raise BackgroundValidationError("A valid area id is required")
    return area_id


def _validate_upload(
    image_data: bytes | bytearray | memoryview, content_type: str | None
) -> bytes:
    if not isinstance(image_data, (bytes, bytearray, memoryview)):
        raise BackgroundValidationError("Background image data must be bytes")
    payload = bytes(image_data)
    if not payload:
        raise BackgroundValidationError("The background image is empty")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise BackgroundValidationError("Background images must not exceed 10 MiB")
    if not (payload.startswith(_JPEG_SIGNATURE) or payload.startswith(_PNG_SIGNATURE)):
        raise BackgroundValidationError("Only JPEG and PNG background images are supported")
    if content_type:
        declared_type = content_type.split(";", 1)[0].strip().lower()
        if declared_type not in _ALLOWED_CONTENT_TYPES:
            raise BackgroundValidationError("Only JPEG and PNG background images are supported")
    return payload


def _validate_focal_value(value: float) -> float:
    try:
        focal_value = float(value)
    except (TypeError, ValueError) as err:
        raise BackgroundValidationError("Focal coordinates must be numbers") from err
    if not math.isfinite(focal_value) or not 0.0 <= focal_value <= 1.0:
        raise BackgroundValidationError("Focal coordinates must be between 0 and 1")
    return focal_value


def _validate_dimensions(width: int, height: int) -> None:
    _validate_dimension_limits(width, height)
    if width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT:
        raise BackgroundValidationError("Background images must be at least 1280 x 720 pixels")


def _validate_dimension_limits(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise BackgroundValidationError("The background image has invalid dimensions")
    if width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE:
        raise BackgroundValidationError("No image edge may exceed 8192 pixels")
    if width * height > MAX_IMAGE_PIXELS:
        raise BackgroundValidationError("Background images must not exceed 32 megapixels")


def _focal_crop_box(
    width: int, height: int, focal_x: float, focal_y: float
) -> tuple[int, int, int, int]:
    target_ratio = 16 / 9
    if width / height > target_ratio:
        crop_height = height
        crop_width = min(width, round(height * target_ratio))
    else:
        crop_width = width
        crop_height = min(height, round(width / target_ratio))

    left = round(focal_x * width - crop_width / 2)
    top = round(focal_y * height - crop_height / 2)
    left = max(0, min(left, width - crop_width))
    top = max(0, min(top, height - crop_height))
    return left, top, left + crop_width, top + crop_height


def _area_directory_name(area_id: str) -> str:
    # Raw area ids never enter a path.  A fixed-size digest is stable across
    # restarts and safe even if a malformed id contains separators.
    return hashlib.sha256(area_id.encode("utf-8")).hexdigest()[:32]


def _is_target_directory_name(value: str) -> bool:
    """Return whether ``value`` belongs to the home or hashed-area namespace."""

    return value == _HOME_BACKGROUND_DIRECTORY_NAME or (
        len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_target_directory_name(value: str) -> None:
    if not _is_target_directory_name(value):
        raise BackgroundValidationError("Unsafe background storage scope")


def _metadata_files(metadata: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(metadata, Mapping):
        return set()
    variants = metadata.get("variants")
    if not isinstance(variants, Mapping):
        return set()
    return {
        value["file"]
        for value in variants.values()
        if isinstance(value, Mapping) and isinstance(value.get("file"), str)
    }


def _etag(digest: str) -> str:
    return f'"{digest}"'


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise BackgroundValidationError("Background storage must not be a symbolic link")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        _LOGGER.debug("Unable to tighten permissions for %s", path, exc_info=True)


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary.name, 0o600)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


__all__ = [
    "BACKGROUND_SCHEMA_VERSION",
    "BackgroundError",
    "BackgroundManager",
    "BackgroundProcessingUnavailable",
    "BackgroundValidationError",
    "MAX_UPLOAD_BYTES",
    "VARIANT_1080P",
    "VARIANT_2160P",
    "VARIANT_THUMBNAIL",
]
