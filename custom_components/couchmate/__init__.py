"""CouchMate Core Dev Preview for Home Assistant."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification, websocket_api
from homeassistant.components.frontend import async_register_built_in_panel, async_remove_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .const import (
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
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    CONFIGURATION_MANAGER,
    BACKGROUND_MANAGER,
    DIAGNOSTICS_MANAGER,
    PAIRING_CLIENT_STORAGE_KEY,
    PAIRING_CLIENT_STORAGE_VERSION,
)
from .storage import async_load_entities, async_save_entities
from .pairing import PairingManager
from .const import PAIRING_MANAGER


def _resolve_filter(
    hass: HomeAssistant,
    *,
    areas: list[str],
    devices: list[str],
    entities: list[str],
    excluded_entities: list[str] | None = None,
) -> set[str]:
    """Resolve area / device / entity selections to a flat entity-id set.

    For each picked area, every entity assigned to that area (directly
    or via its device's area) is included. For each picked device,
    every entity registered to that device is included. Explicit
    entity ids are added as-is. Result is unioned and deduplicated.

    Callers may rebuild the set whenever Home Assistant's registries change.
    The client API does this for every snapshot so newly added rooms, devices
    and entities appear without reloading the integration.
    """
    if not (areas or devices or entities):
        return set()

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    area_set = set(areas or [])
    device_set = set(devices or [])
    resolved: set[str] = set(entities or [])

    if area_set or device_set:
        # Pre-compute the device→area map so we can resolve entities
        # whose `area_id` is unset but whose device sits in a picked
        # area. Otherwise picking "Heimkino" would miss any entity
        # that inherits its area from its device.
        device_area = {dev.id: dev.area_id for dev in dev_reg.devices.values()}

        for entry in ent_reg.entities.values():
            if entry.disabled:
                continue
            # Direct device pick.
            if entry.device_id and entry.device_id in device_set:
                resolved.add(entry.entity_id)
                continue
            # Area pick: entity's own area, or its device's area.
            entity_area = entry.area_id or (
                device_area.get(entry.device_id) if entry.device_id else None
            )
            if entity_area and entity_area in area_set:
                resolved.add(entry.entity_id)

    resolved.difference_update(excluded_entities or [])
    return resolved
from .websocket_api import async_setup_websocket_api
from .api import async_setup_api
from .configurator import async_setup_configurator
from .configuration import ConfigurationManager
from .backgrounds import BackgroundManager
from .diagnostics import DiagnosticsManager
from .configuration_api import async_setup_configuration_api
from .management import async_setup_management

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "couchmate"
PANEL_CONFIGURATOR_URL = "/couchmate/configurator"



async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the CouchMate Core Dev Preview component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CouchMate from a config entry."""
    try:
        hass.data.setdefault(DOMAIN, {})
        pairing_manager = PairingManager(hass)
        await pairing_manager.async_initialize()
        hass.data[DOMAIN][PAIRING_MANAGER] = pairing_manager

        # Versioned CouchMate settings are isolated from the established selection and
        # pairing stores. Existing CouchMate clients never read or mutate this
        # manager and therefore retain their current behaviour.
        configuration_manager = ConfigurationManager(hass)
        await configuration_manager.async_initialize()
        background_manager = BackgroundManager(hass, configuration_manager)
        hass.data[DOMAIN][CONFIGURATION_MANAGER] = configuration_manager
        hass.data[DOMAIN][BACKGROUND_MANAGER] = background_manager
        hass.data[DOMAIN][DIAGNOSTICS_MANAGER] = DiagnosticsManager()
        try:
            await background_manager.async_cleanup(ar.async_get(hass).areas)
        except Exception:
            # Cleanup is best-effort and must never affect the established Core.
            _LOGGER.exception("Error cleaning orphaned CouchMate room backgrounds")

        # Load stored selections (areas, devices, individual entities).
        # Older installs only stored `entities` — `.get(..., [])` keeps
        # them working without a migration step.
        try:
            stored = await async_load_entities(hass)
            stored_areas = list(stored.get(CONF_AREAS, []))
            stored_devices = list(stored.get(CONF_DEVICES, []))
            stored_entities = list(stored.get(CONF_ENTITIES, []))
            stored_excluded_entities = list(stored.get(CONF_EXCLUDED_ENTITIES, []))
            stored_room_temperatures = dict(stored.get(CONF_ROOM_TEMPERATURES, {}))
            stored_room_humidities = dict(stored.get(CONF_ROOM_HUMIDITIES, {}))
            stored_room_climates = dict(stored.get(CONF_ROOM_CLIMATES, {}))
            stored_weather_entity = stored.get(CONF_WEATHER_ENTITY)
            stored_selection_model = dict(stored.get(CONF_SELECTION_MODEL, {}))
            if stored_selection_model.get("version") == SELECTION_MODEL_VERSION:
                model_areas = dict(stored_selection_model.get("areas", {}))
                stored_areas = []
                stored_devices = []
                stored_entities = []
                stored_room_temperatures = {}
                stored_room_humidities = {}
                stored_room_climates = {}
                stored_weather_entity = stored_selection_model.get("weather")
                for area_id, area_cfg in model_areas.items():
                    if not isinstance(area_cfg, dict):
                        continue
                    if area_cfg.get("temperature"):
                        stored_room_temperatures[str(area_id)] = str(area_cfg["temperature"])
                    if area_cfg.get("humidity"):
                        stored_room_humidities[str(area_id)] = str(area_cfg["humidity"])
                    if area_cfg.get("climate"):
                        stored_room_climates[str(area_id)] = str(area_cfg["climate"])
                    stored_entities.extend(
                        str(entity_id)
                        for entity_id in area_cfg.get("flow_entities", [])
                        if entity_id
                    )
                    stored_entities.extend(
                        str(entity_id)
                        for entity_id in area_cfg.get("timer_entities", [])
                        if isinstance(entity_id, str) and entity_id.startswith("timer.")
                    )
                    for device_id, device_cfg in dict(area_cfg.get("devices", {})).items():
                        if not isinstance(device_cfg, dict):
                            continue
                        if device_cfg.get("mode") == "all":
                            stored_devices.append(str(device_id))
                        else:
                            stored_entities.extend(str(entity_id) for entity_id in device_cfg.get("entities", []) if entity_id)
                stored_devices = list(dict.fromkeys(stored_devices))
                stored_entities = list(dict.fromkeys(stored_entities))
        except Exception:
            _LOGGER.exception("Error loading stored selections, using config data")
            stored_areas = list(entry.data.get(CONF_AREAS, []))
            stored_devices = list(entry.data.get(CONF_DEVICES, []))
            stored_entities = list(entry.data.get(CONF_ENTITIES, []))
            stored_excluded_entities = list(entry.data.get(CONF_EXCLUDED_ENTITIES, []))
            stored_room_temperatures = dict(entry.data.get(CONF_ROOM_TEMPERATURES, {}))
            stored_room_humidities = dict(entry.data.get(CONF_ROOM_HUMIDITIES, {}))
            stored_room_climates = dict(entry.data.get(CONF_ROOM_CLIMATES, {}))
            stored_weather_entity = entry.data.get(CONF_WEATHER_ENTITY)
            stored_selection_model = dict(entry.data.get(CONF_SELECTION_MODEL, {}))

        # Resolve area + device picks down to a flat entity-id set,
        # unioned with any explicitly-selected entities. The runtime
        # filter (WebSocket / REST / state-change handlers) only needs
        # the resolved set; areas / devices are kept around so the
        # options flow can re-display the user's actual picks.
        resolved = _resolve_filter(
            hass,
            areas=stored_areas,
            devices=stored_devices,
            entities=stored_entities,
            excluded_entities=stored_excluded_entities,
        )
        hass.data[DOMAIN]["entities"] = list(resolved)
        hass.data[DOMAIN]["areas"] = stored_areas
        hass.data[DOMAIN]["devices"] = stored_devices
        hass.data[DOMAIN]["explicit_entities"] = stored_entities
        hass.data[DOMAIN]["excluded_entities"] = stored_excluded_entities
        hass.data[DOMAIN]["room_temperatures"] = stored_room_temperatures
        hass.data[DOMAIN]["room_humidities"] = stored_room_humidities
        hass.data[DOMAIN]["room_climates"] = stored_room_climates
        hass.data[DOMAIN]["weather_entity"] = stored_weather_entity
        hass.data[DOMAIN]["selection_model"] = stored_selection_model if stored_selection_model.get("version") == SELECTION_MODEL_VERSION else {"version": SELECTION_MODEL_VERSION, "areas": {}}
        hass.data[DOMAIN]["entry"] = entry
        hass.data[DOMAIN]["entry_options"] = dict(entry.options)

        await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])

        # Set up WebSocket API
        try:
            await async_setup_websocket_api(hass)
        except Exception as ex:
            _LOGGER.exception("Error setting up WebSocket API")
            return False

        # Set up REST API
        try:
            await async_setup_api(hass)
        except Exception as ex:
            _LOGGER.exception("Error setting up REST API")
            return False

        # The versioned CouchMate API is additive. A failure in the optional
        # extension is logged but must never take the released CouchMate API
        # offline.
        try:
            await async_setup_configuration_api(hass)
        except Exception:
            _LOGGER.exception("Error setting up CouchMate configuration API")

        # Set up graphical configurator
        try:
            await async_setup_configurator(hass)
        except Exception:
            _LOGGER.exception("Error setting up graphical configurator")
            return False

        try:
            await async_setup_management(hass)
        except Exception:
            _LOGGER.exception("Error setting up CouchMate management page")

        # Expose the graphical configurator as a native Home Assistant
        # sidebar entry. The iframe uses a relative URL, so it works with
        # local IPs, host names, reverse proxies and Home Assistant Cloud.
        try:
            async_register_built_in_panel(
                hass,
                component_name="iframe",
                sidebar_title="CouchMate Core Dev Preview",
                sidebar_icon="mdi:sofa-single",
                sidebar_default_visible=True,
                frontend_url_path=PANEL_URL_PATH,
                config={"url": PANEL_CONFIGURATOR_URL},
                require_admin=True,
                update=True,
                show_in_sidebar=True,
            )
            hass.data[DOMAIN]["panel_registered"] = True
        except Exception:
            _LOGGER.exception("Error registering CouchMate sidebar panel")
            return False

        # Register services
        try:
            await _async_setup_services(hass)
        except Exception as ex:
            _LOGGER.exception("Error setting up services")
            return False

        # Add update listener
        entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

        _LOGGER.info("CouchMate Core Dev Preview setup completed successfully")
        return True

    except Exception as ex:
        _LOGGER.exception("Error setting up CouchMate Core Dev Preview integration")
        return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    try:
        if hass.data.get(DOMAIN, {}).get("panel_registered"):
            async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)

        # Remove services (with error handling)
        try:
            hass.services.async_remove(DOMAIN, "add_entity")
            hass.services.async_remove(DOMAIN, "remove_entity")
            hass.services.async_remove(DOMAIN, "set_entities")
            hass.services.async_remove(DOMAIN, "uninstall")
            hass.services.async_remove(DOMAIN, "approve_pairing")
            hass.services.async_remove(DOMAIN, "reject_pairing")
        except Exception as ex:
            _LOGGER.warning("Error removing services during unload: %s", ex)

        # Pop the domain entirely instead of `clear()` so no empty
        # container is left behind for handlers that test
        # `if DOMAIN in hass.data`. Note that WebSocket commands and
        # REST views registered during setup cannot be unregistered
        # in HA — they're guarded inside their handlers to no-op
        # when the domain is gone, so they degrade cleanly until HA
        # restarts.
        await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])
        hass.data.pop(DOMAIN, None)

        _LOGGER.info("CouchMate Core Dev Preview unloaded successfully")
        return True

    except Exception as ex:
        _LOGGER.exception("Error unloading CouchMate Core Dev Preview integration")
        return False


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete persisted storage when the user removes the integration.

    Without this, the entity-selection list at `.storage/couchmate`
    survives the deletion. The next time the user re-adds Couch
    Core Dev Preview config flow loads that file and pre-populates the
    form with the old entities — which is what made the integration
    feel like it 'kept staying' after the user clicked Delete.
    """
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    try:
        await store.async_remove()
        _LOGGER.info("CouchMate Core Dev Preview storage removed during integration removal")
    except Exception:
        _LOGGER.exception("Error removing CouchMate Core Dev Preview storage")

    # Privacy-sensitive CouchMate settings and image derivatives use their
    # own stores. Remove them independently so a failure cannot prevent the
    # established integration removal path.
    configuration_manager = hass.data.get(DOMAIN, {}).get(CONFIGURATION_MANAGER)
    try:
        configuration_manager = configuration_manager or ConfigurationManager(hass)
        await configuration_manager.async_remove()
    except Exception:
        _LOGGER.exception("Error removing CouchMate configuration storage")

    try:
        background_manager = hass.data.get(DOMAIN, {}).get(BACKGROUND_MANAGER)
        if background_manager is None:
            configuration_manager = configuration_manager or ConfigurationManager(hass)
            background_manager = BackgroundManager(hass, configuration_manager)
        await background_manager.async_remove_all()
    except Exception:
        _LOGGER.exception("Error removing CouchMate room backgrounds")

    try:
        pairing_store = Store(
            hass,
            PAIRING_CLIENT_STORAGE_VERSION,
            PAIRING_CLIENT_STORAGE_KEY,
        )
        await pairing_store.async_remove()
    except Exception:
        _LOGGER.exception("Error removing CouchMate paired-client storage")


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Keep the configurator open when its saved selection is already applied."""
    runtime = hass.data.get(DOMAIN, {})
    applied_selection = {
        CONF_AREAS: runtime.get("areas"),
        CONF_DEVICES: runtime.get("devices"),
        CONF_ENTITIES: runtime.get("explicit_entities"),
        CONF_EXCLUDED_ENTITIES: runtime.get("excluded_entities"),
        CONF_ROOM_TEMPERATURES: runtime.get("room_temperatures"),
        CONF_ROOM_HUMIDITIES: runtime.get("room_humidities"),
        CONF_ROOM_CLIMATES: runtime.get("room_climates"),
        CONF_WEATHER_ENTITY: runtime.get("weather_entity"),
        CONF_SELECTION_MODEL: runtime.get("selection_model"),
    }
    if (
        runtime.get("entry") is entry
        and runtime.get("entry_options") == dict(entry.options)
        and dict(entry.data) == applied_selection
    ):
        # The configurator has already persisted and applied these values.
        # Reloading would briefly remove its sidebar panel and make Home
        # Assistant navigate away from the room currently being edited.
        return
    await async_reload_entry(hass, entry)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def _async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for CouchMate."""

    @callback
    def add_entity(call):
        """Add an entity to the filter list."""
        entity_id = call.data.get("entity_id")
        if entity_id and entity_id not in hass.data[DOMAIN]["entities"]:
            hass.data[DOMAIN]["entities"].append(entity_id)
            hass.async_create_task(
                async_save_entities(hass, {"entities": hass.data[DOMAIN]["entities"]})
            )
            _LOGGER.info("Added %s to CouchMate filter", entity_id)

    @callback
    def remove_entity(call):
        """Remove an entity from the filter list."""
        entity_id = call.data.get("entity_id")
        if entity_id in hass.data[DOMAIN]["entities"]:
            hass.data[DOMAIN]["entities"].remove(entity_id)
            hass.async_create_task(
                async_save_entities(hass, {"entities": hass.data[DOMAIN]["entities"]})
            )
            _LOGGER.info("Removed %s from CouchMate filter", entity_id)

    @callback
    def set_entities(call):
        """Set the complete entity filter list."""
        entities = call.data.get("entities", [])
        hass.data[DOMAIN]["entities"] = entities
        hass.async_create_task(
            async_save_entities(hass, {"entities": entities})
        )
        _LOGGER.info("Updated CouchMate filter with %d entities", len(entities))

    async def uninstall(call):
        """Clean uninstall: remove the config entry while the
        integration code is still loaded.

        Why this exists: when a user removes the integration via HACS,
        HA can no longer execute `async_unload_entry` /
        `async_remove_entry` because the module is gone — leaving an
        orphaned config entry and the persisted storage file behind.
        Running this service first triggers the normal HA removal
        path (which calls our `async_remove_entry`, which deletes
        `.storage/couchmate`), so the subsequent HACS file
        deletion has nothing to mop up.
        """
        entry = hass.data.get(DOMAIN, {}).get("entry")
        if entry is None:
            _LOGGER.warning(
                "CouchMate Core Dev Preview uninstall called but no config entry "
                "was found — already uninstalled?"
            )
            return
        entry_id = entry.entry_id
        _LOGGER.info(
            "CouchMate Core Dev Preview uninstall service called — removing config "
            "entry %s and persisted storage", entry_id
        )
        await hass.config_entries.async_remove(entry_id)
        _LOGGER.info(
            "CouchMate Core Dev Preview config entry removed. You can now delete "
            "the integration from HACS to remove the files."
        )
        # Surface a persistent notification so the user sees it without
        # tailing logs — the typical user calls this from the UI and
        # never sees `_LOGGER.info` output.
        persistent_notification.async_create(
            hass,
            (
                "CouchMate Core Dev Preview has been removed from Home Assistant. "
                "Open HACS → CouchMate Core Dev Preview → Remove to delete the "
                "integration files, then restart Home Assistant."
            ),
            title="CouchMate Core Dev Preview uninstalled",
            notification_id=f"{DOMAIN}_uninstalled",
        )

    async def approve_pairing(call):
        """Approve a pending Apple TV pairing code."""
        code = str(call.data.get("code", ""))
        manager = hass.data[DOMAIN][PAIRING_MANAGER]
        pending = manager.get_by_code(code)
        if pending is None:
            _LOGGER.warning("Pairing code not found: %s", code)
            return
        if pending.capabilities:
            user_id = getattr(getattr(call, "context", None), "user_id", None)
            user = await hass.auth.async_get_user(user_id) if user_id else None
            if user is None or not getattr(user, "is_admin", False):
                _LOGGER.warning(
                    "Denied capability-bearing CouchMate pairing approval by a non-admin"
                )
                persistent_notification.async_create(
                    hass,
                    "Diese CouchMate-Kopplung fordert Schreibrechte an und muss von "
                    "einem Home-Assistant-Administrator bestätigt werden.",
                    title="CouchMate Core Dev Preview: Administrator erforderlich",
                    notification_id=f"{DOMAIN}_pairing_admin_required",
                )
                return
        session = manager.approve(code)
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_pairing_{session.session_id}"
        )
        _LOGGER.info("Approved CouchMate pairing for %s", session.device_name)

    async def reject_pairing(call):
        """Reject a pending Apple TV pairing code."""
        code = str(call.data.get("code", ""))
        session = hass.data[DOMAIN][PAIRING_MANAGER].cancel_by_code(code)
        if session is None:
            _LOGGER.warning("Pairing code not found: %s", code)
            return
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_pairing_{session.session_id}"
        )
        _LOGGER.info("Rejected CouchMate pairing for %s", session.device_name)

    hass.services.async_register(DOMAIN, "add_entity", add_entity)
    hass.services.async_register(DOMAIN, "remove_entity", remove_entity)
    hass.services.async_register(DOMAIN, "set_entities", set_entities)
    hass.services.async_register(DOMAIN, "uninstall", uninstall)
    hass.services.async_register(DOMAIN, "approve_pairing", approve_pairing)
    hass.services.async_register(DOMAIN, "reject_pairing", reject_pairing)
