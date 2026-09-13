"""Configuration and management flows for CouchMate Core Dev Preview."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    BooleanSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_AREAS,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_EXCLUDED_ENTITIES,
    DOMAIN,
    PAIRING_MANAGER,
)
from .storage import async_load_entities, async_save_entities

CONF_CONFIRM = "confirm"
CONF_PAIRING_CODE = "pairing_code"
CONF_CLIENTS = "clients"


def _short(items: list[str], limit: int = 10) -> str:
    if not items:
        return "–"
    return ", ".join(items[:limit]) + (
        f" (+{len(items) - limit} weitere)" if len(items) > limit else ""
    )


def _area_options(hass) -> list[SelectOptionDict]:
    registry = ar.async_get(hass)
    return [
        SelectOptionDict(value=area.id, label=area.name)
        for area in sorted(registry.areas.values(), key=lambda item: item.name.casefold())
    ]


def _device_options(hass) -> list[SelectOptionDict]:
    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    options: list[SelectOptionDict] = []
    for device in devices.devices.values():
        name = device.name_by_user or device.name or device.id
        area = areas.async_get_area(device.area_id) if device.area_id else None
        label = f"{area.name} · {name}" if area else f"Ohne Bereich · {name}"
        options.append(SelectOptionDict(value=device.id, label=label))
    return sorted(options, key=lambda item: item["label"].casefold())


def _entity_options(hass) -> list[SelectOptionDict]:
    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    options: list[SelectOptionDict] = []
    for entry in entities.entities.values():
        if entry.disabled:
            continue
        state = hass.states.get(entry.entity_id)
        name = (
            entry.name
            or entry.original_name
            or (state.name if state else None)
            or entry.entity_id
        )
        device = devices.async_get(entry.device_id) if entry.device_id else None
        area_id = entry.area_id or (device.area_id if device else None)
        area = areas.async_get_area(area_id) if area_id else None
        area_name = area.name if area else "Ohne Bereich"
        domain = entry.entity_id.split(".", 1)[0]
        label = f"{area_name} · {name} · {domain} · {entry.entity_id}"
        options.append(SelectOptionDict(value=entry.entity_id, label=label))
    return sorted(options, key=lambda item: item["label"].casefold())


def _selector(options: list[SelectOptionDict], *, multiple: bool = True) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=options,
            multiple=multiple,
            mode=SelectSelectorMode.LIST,
        )
    )


class _SelectionMixin:
    _areas: list[str]
    _devices: list[str]
    _entities: list[str]
    _excluded_entities: list[str]

    def _summary(self) -> dict[str, str]:
        area_reg = ar.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        entity_reg = er.async_get(self.hass)
        area_names = [
            area.name
            for area_id in self._areas
            if (area := area_reg.async_get_area(area_id))
        ]
        device_names: list[str] = []
        for device_id in self._devices:
            if device := device_reg.async_get(device_id):
                device_names.append(device.name_by_user or device.name or device_id)
        entity_names: list[str] = []
        for entity_id in self._entities:
            entry = entity_reg.async_get(entity_id)
            state = self.hass.states.get(entity_id)
            entity_names.append(
                (entry.name if entry else None)
                or (entry.original_name if entry else None)
                or (state.name if state else None)
                or entity_id
            )
        from . import _resolve_filter

        resolved = _resolve_filter(
            self.hass,
            areas=self._areas,
            devices=self._devices,
            entities=self._entities,
            excluded_entities=self._excluded_entities,
        )
        return {
            "area_count": str(len(self._areas)),
            "device_count": str(len(self._devices)),
            "entity_count": str(len(self._entities)),
            "excluded_count": str(len(self._excluded_entities)),
            "resolved_count": str(len(resolved)),
            "areas": _short(area_names),
            "devices": _short(device_names),
            "entities": _short(entity_names),
            "excluded": _short(self._excluded_entities),
        }

    async def _save(self) -> None:
        data = {
            CONF_AREAS: self._areas,
            CONF_DEVICES: self._devices,
            CONF_ENTITIES: self._entities,
            CONF_EXCLUDED_ENTITIES: self._excluded_entities,
        }
        await async_save_entities(self.hass, data)
        if DOMAIN in self.hass.data:
            from . import _resolve_filter

            self.hass.data[DOMAIN]["entities"] = sorted(
                _resolve_filter(
                    self.hass,
                    areas=self._areas,
                    devices=self._devices,
                    entities=self._entities,
                    excluded_entities=self._excluded_entities,
                )
            )
            self.hass.data[DOMAIN]["areas"] = list(self._areas)
            self.hass.data[DOMAIN]["devices"] = list(self._devices)
            self.hass.data[DOMAIN]["explicit_entities"] = list(self._entities)
            self.hass.data[DOMAIN]["excluded_entities"] = list(
                self._excluded_entities
            )


class CouchMateConfigFlow(_SelectionMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Configure CouchMate Core Dev Preview."""

    VERSION = 5

    def __init__(self) -> None:
        self._areas = []
        self._devices = []
        self._entities = []
        self._excluded_entities = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._areas = list(user_input.get(CONF_AREAS, []))
            return await self.async_step_devices()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Optional(CONF_AREAS, default=[]): _selector(_area_options(self.hass))}
            ),
        )

    async def async_step_devices(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._devices = list(user_input.get(CONF_DEVICES, []))
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {vol.Optional(CONF_DEVICES, default=[]): _selector(_device_options(self.hass))}
            ),
        )

    async def async_step_entities(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._entities = list(user_input.get(CONF_ENTITIES, []))
            return await self.async_step_exclusions()
        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema(
                {vol.Optional(CONF_ENTITIES, default=[]): _selector(_entity_options(self.hass))}
            ),
        )

    async def async_step_exclusions(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._excluded_entities = list(user_input.get(CONF_EXCLUDED_ENTITIES, []))
            return await self.async_step_summary()
        return self.async_show_form(
            step_id="exclusions",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_EXCLUDED_ENTITIES, default=[]): _selector(
                        _entity_options(self.hass)
                    )
                }
            ),
        )

    async def async_step_summary(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            await self._save()
            data = {
                CONF_AREAS: self._areas,
                CONF_DEVICES: self._devices,
                CONF_ENTITIES: self._entities,
                CONF_EXCLUDED_ENTITIES: self._excluded_entities,
            }
            return self.async_create_entry(title="CouchMate Core Dev Preview", data=data)
        return self.async_show_form(
            step_id="summary",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CONFIRM, default=True): BooleanSelector(
                        BooleanSelectorConfig()
                    )
                }
            ),
            description_placeholders=self._summary(),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return CouchMateOptionsFlow()


class CouchMateOptionsFlow(_SelectionMixin, config_entries.OptionsFlow):
    """Manage selections, pairing requests, and paired clients."""

    def __init__(self) -> None:
        self._areas = []
        self._devices = []
        self._entities = []
        self._excluded_entities = []
        self._loaded = False

    async def _load(self) -> None:
        if self._loaded:
            return
        current = await async_load_entities(self.hass)
        self._areas = list(
            current.get(CONF_AREAS, self.config_entry.data.get(CONF_AREAS, []))
        )
        self._devices = list(
            current.get(CONF_DEVICES, self.config_entry.data.get(CONF_DEVICES, []))
        )
        self._entities = list(
            current.get(CONF_ENTITIES, self.config_entry.data.get(CONF_ENTITIES, []))
        )
        self._excluded_entities = list(
            current.get(
                CONF_EXCLUDED_ENTITIES,
                self.config_entry.data.get(CONF_EXCLUDED_ENTITIES, []),
            )
        )
        self._loaded = True

    def _pairing_manager(self):
        return self.hass.data[DOMAIN][PAIRING_MANAGER]

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        await self._load()
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "selection",
                "approve_pairing",
                "reject_pairing",
                "paired_clients",
            ],
        )

    async def async_step_selection(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._areas = list(user_input.get(CONF_AREAS, []))
            return await self.async_step_devices()
        return self.async_show_form(
            step_id="selection",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_AREAS, default=self._areas): _selector(
                        _area_options(self.hass)
                    )
                }
            ),
            description_placeholders=self._summary(),
        )

    async def async_step_devices(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._devices = list(user_input.get(CONF_DEVICES, []))
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DEVICES, default=self._devices): _selector(
                        _device_options(self.hass)
                    )
                }
            ),
        )

    async def async_step_entities(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._entities = list(user_input.get(CONF_ENTITIES, []))
            return await self.async_step_exclusions()
        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ENTITIES, default=self._entities): _selector(
                        _entity_options(self.hass)
                    )
                }
            ),
        )

    async def async_step_exclusions(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._excluded_entities = list(user_input.get(CONF_EXCLUDED_ENTITIES, []))
            return await self.async_step_summary()
        return self.async_show_form(
            step_id="exclusions",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_EXCLUDED_ENTITIES, default=self._excluded_entities
                    ): _selector(_entity_options(self.hass))
                }
            ),
        )

    async def async_step_summary(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            await self._save()
            data = {
                CONF_AREAS: self._areas,
                CONF_DEVICES: self._devices,
                CONF_ENTITIES: self._entities,
                CONF_EXCLUDED_ENTITIES: self._excluded_entities,
            }
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="summary",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CONFIRM, default=True): BooleanSelector(
                        BooleanSelectorConfig()
                    )
                }
            ),
            description_placeholders=self._summary(),
        )

    def _pending_options(self) -> list[SelectOptionDict]:
        options = []
        for session in self._pairing_manager().list_pending_sessions():
            rights = []
            if "configuration:write" in session.capabilities:
                rights.append("Profile & Einstellungen")
            if "dashboard:write" in session.capabilities:
                rights.append("Dashboard-Kacheln anordnen")
            if "backgrounds:write" in session.capabilities:
                rights.append("Raumbilder")
            rights_label = f" · Rechte: {', '.join(rights)}" if rights else ""
            options.append(
                SelectOptionDict(
                    value=session.code,
                    label=(
                        f"{session.device_name} · {session.code} · "
                        f"{session.remaining_seconds}s{rights_label}"
                    ),
                )
            )
        return options

    async def async_step_approve_pairing(
        self, user_input: dict[str, Any] | None = None
    ):
        options = self._pending_options()
        if not options:
            return self.async_abort(reason="no_pending_pairings")
        if user_input is not None:
            session = self._pairing_manager().approve(
                str(user_input[CONF_PAIRING_CODE])
            )
            if session is None:
                return self.async_show_form(
                    step_id="approve_pairing",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_PAIRING_CODE): _selector(
                                options, multiple=False
                            )
                        }
                    ),
                    errors={"base": "pairing_not_found"},
                )
            persistent_notification.async_dismiss(
                self.hass, f"{DOMAIN}_pairing_{session.session_id}"
            )
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="approve_pairing",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_CODE): _selector(
                        options, multiple=False
                    )
                }
            ),
        )

    async def async_step_reject_pairing(
        self, user_input: dict[str, Any] | None = None
    ):
        options = self._pending_options()
        if not options:
            return self.async_abort(reason="no_pending_pairings")
        if user_input is not None:
            session = self._pairing_manager().cancel_by_code(
                str(user_input[CONF_PAIRING_CODE])
            )
            if session is None:
                return self.async_show_form(
                    step_id="reject_pairing",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_PAIRING_CODE): _selector(
                                options, multiple=False
                            )
                        }
                    ),
                    errors={"base": "pairing_not_found"},
                )
            persistent_notification.async_dismiss(
                self.hass, f"{DOMAIN}_pairing_{session.session_id}"
            )
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="reject_pairing",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_CODE): _selector(
                        options, multiple=False
                    )
                }
            ),
        )

    async def async_step_paired_clients(
        self, user_input: dict[str, Any] | None = None
    ):
        clients = self._pairing_manager().list_clients()
        options = [
            SelectOptionDict(
                value=client["client_id"],
                label=(
                    f"{client.get('device_name', 'CouchMate')} · "
                    f"{client.get('client_id')}"
                ),
            )
            for client in clients
        ]
        if not options:
            return self.async_abort(reason="no_paired_clients")
        if user_input is not None:
            for client_id in user_input.get(CONF_CLIENTS, []):
                await self._pairing_manager().async_revoke_client(client_id)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="paired_clients",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_CLIENTS, default=[]): _selector(
                        options, multiple=True
                    )
                }
            ),
        )
