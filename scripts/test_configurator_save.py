#!/usr/bin/env python3
"""Verify saves keep the Core panel mounted while selections persist.

Run with Python 3.9+ and no third-party dependencies. Home Assistant adapters
are in-memory; setup, save, storage, configuration, and unload code are real.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest

sys.dont_write_bytecode = True
PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "couchmate"
PACKAGE_NAME = "_couchmate_save_test"


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    sys.modules[name] = result
    return result


async def noop(*args, **kwargs):
    return True


class MemoryStore:
    def __init__(self, hass, version, key):
        self.hass, self.key = hass, key

    async def async_load(self):
        return deepcopy(self.hass.persisted.get(self.key))

    async def async_save(self, value):
        self.hass.persisted[self.key] = deepcopy(value)


class HomeAssistantView:
    @staticmethod
    def json(payload):
        return payload


class Manager:
    def __init__(self, *args):
        pass

    async_initialize = noop
    async_cleanup = noop


def import_production():
    module("homeassistant", __path__=[])
    module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
    module("homeassistant.config_entries", ConfigEntry=object)
    module("homeassistant.const", EVENT_STATE_CHANGED="state_changed", Platform=SimpleNamespace(SENSOR="sensor"))
    module("homeassistant.helpers", __path__=[])
    module("homeassistant.helpers.storage", Store=MemoryStore)
    module("homeassistant.helpers.event", async_track_state_change_event=lambda *args: None)
    for registry in ("area_registry", "device_registry", "entity_registry"):
        module(f"homeassistant.helpers.{registry}", async_get=lambda hass, key=registry: getattr(hass, key))
    module("homeassistant.components", __path__=[])
    module("homeassistant.components.persistent_notification")
    module("homeassistant.components.websocket_api")
    module("homeassistant.components.http", HomeAssistantView=HomeAssistantView)
    module(
        "homeassistant.components.frontend",
        async_register_built_in_panel=lambda hass, **kwargs: hass.panels.add(kwargs["frontend_url_path"]),
        async_remove_panel=lambda hass, path, **kwargs: (hass.panels.discard(path), hass.removed_panels.append(path)),
    )
    module("aiohttp", web=SimpleNamespace(Response=object))
    for name, setup in (
        ("api", "async_setup_api"),
        ("websocket_api", "async_setup_websocket_api"),
        ("configuration_api", "async_setup_configuration_api"),
        ("management", "async_setup_management"),
    ):
        module(f"{PACKAGE_NAME}.{name}", **{setup: noop})
    module(f"{PACKAGE_NAME}.pairing", PairingManager=Manager)
    module(f"{PACKAGE_NAME}.backgrounds", BackgroundManager=Manager)
    module(f"{PACKAGE_NAME}.diagnostics", DiagnosticsManager=Manager)
    spec = importlib.util.spec_from_file_location(PACKAGE_NAME, PACKAGE / "__init__.py", submodule_search_locations=[str(PACKAGE)])
    result = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = result
    spec.loader.exec_module(result)
    return result, sys.modules[f"{PACKAGE_NAME}.configurator"]


CORE, CONFIGURATOR = import_production()


class Entry:
    entry_id = "core"

    def __init__(self):
        self.data, self.options, self.listeners = {}, {}, []

    def add_update_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def async_on_unload(self, unsubscribe):
        pass


class ConfigEntries:
    async_forward_entry_setups = noop
    async_unload_platforms = noop

    def __init__(self, hass):
        self.hass, self.tasks = hass, []

    def async_update_entry(self, entry, *, data=None, options=None):
        if data is not None:
            entry.data = deepcopy(data)
        if options is not None:
            entry.options = deepcopy(options)
        self.tasks.extend(asyncio.create_task(listener(self.hass, entry)) for listener in entry.listeners)

    async def drain(self):
        tasks, self.tasks = self.tasks, []
        await asyncio.gather(*tasks)


class Request:
    def __init__(self, hass, payload):
        self.app, self.payload = {"hass": hass}, payload

    def get(self, key):
        return SimpleNamespace(is_admin=True) if key == "hass_user" else None

    async def json(self):
        return deepcopy(self.payload)


class ConfiguratorSaveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        entities = {
            entity_id: SimpleNamespace(entity_id=entity_id, device_id="lamp", area_id="kitchen", disabled=False)
            for entity_id in ("light.kitchen", "switch.kitchen")
        }
        devices = {"lamp": SimpleNamespace(id="lamp", area_id="kitchen")}
        self.hass = SimpleNamespace(
            data={}, persisted={}, panels=set(), removed_panels=[],
            area_registry=SimpleNamespace(areas={"kitchen": object()}, async_get_area=lambda area_id: area_id == "kitchen" or None),
            entity_registry=SimpleNamespace(entities=entities, async_get=entities.get),
            device_registry=SimpleNamespace(devices=devices, async_get=devices.get),
            services=SimpleNamespace(async_register=lambda *args: None, async_remove=lambda *args: None),
            http=SimpleNamespace(register_view=lambda view: None),
        )
        self.hass.config_entries = ConfigEntries(self.hass)
        self.entry = Entry()
        self.assertTrue(await CORE.async_setup_entry(self.hass, self.entry))
        self.payload = {"selection_model": {"areas": {"kitchen": {
            "devices": {"lamp": {"mode": "entities", "entities": ["light.kitchen"]}},
            "hero_order": ["light.kitchen"],
            "flow_entities": ["switch.kitchen"],
            "hero_layout": {"device_card_style": "tile"},
        }}}}

    async def save(self):
        response = await CONFIGURATOR.CouchMateConfiguratorSaveView().post(Request(self.hass, self.payload))
        await self.hass.config_entries.drain()
        self.assertTrue(response["success"])
        return response

    async def test_save_keeps_panel_and_managers_and_persists_selection(self):
        runtime = self.hass.data[CORE.DOMAIN]
        manager = runtime[CORE.CONFIGURATION_MANAGER]
        await self.save()
        self.assertEqual(self.hass.removed_panels, [])
        self.assertIs(self.hass.data[CORE.DOMAIN], runtime)
        self.assertIs(runtime[CORE.CONFIGURATION_MANAGER], manager)
        self.assertEqual(self.hass.panels, {"couchmate"})
        self.assertEqual(self.hass.persisted[CORE.STORAGE_KEY], self.entry.data)
        self.assertEqual(runtime["entities"], ["light.kitchen", "switch.kitchen"])
        self.assertEqual(runtime["selection_model"], self.entry.data[CORE.CONF_SELECTION_MODEL])
        profile = manager.snapshot()["profiles"]["default"]["settings"]["companion"]
        self.assertEqual(profile["hero_entity_order"]["kitchen"], ["light.kitchen"])
        self.assertEqual(profile["hero_layouts"]["kitchen"]["device_card_style"], "tile")

    async def test_second_save_applies_changed_selection_without_unmounting(self):
        await self.save()
        self.payload["selection_model"]["areas"] = {}
        await self.save()
        self.assertEqual(self.hass.data[CORE.DOMAIN]["entities"], [])
        self.assertEqual(self.entry.data[CORE.CONF_SELECTION_MODEL]["areas"], {})
        self.assertEqual(self.hass.removed_panels, [])

    async def test_custom_and_hidden_flow_persist_without_entities_or_devices(self):
        for mode in ("custom", "hidden"):
            with self.subTest(mode=mode):
                self.payload["selection_model"]["areas"] = {"kitchen": {"flow_mode": mode}}
                response = await self.save()
                runtime = self.hass.data[CORE.DOMAIN]
                self.assertEqual(response["area_count"], 1)
                self.assertEqual(runtime["entities"], [])
                self.assertEqual(runtime["devices"], [])
                self.assertEqual(runtime["selection_model"]["areas"]["kitchen"]["flow_mode"], mode)
                self.assertEqual(self.hass.persisted[CORE.STORAGE_KEY], self.entry.data)
                self.assertEqual(self.entry.data[CORE.CONF_SELECTION_MODEL]["areas"]["kitchen"]["flow_mode"], mode)
                self.assertEqual(self.hass.removed_panels, [])

    async def test_omitted_and_invalid_flow_modes_default_to_automatic(self):
        room = self.payload["selection_model"]["areas"]["kitchen"]
        for mode in ("omitted", "unsupported", None, {}, []):
            with self.subTest(mode=mode):
                if mode == "omitted":
                    room.pop("flow_mode", None)
                else:
                    room["flow_mode"] = mode
                await self.save()
                saved = self.entry.data[CORE.CONF_SELECTION_MODEL]["areas"]["kitchen"]
                self.assertEqual(saved["flow_mode"], "automatic")
                self.assertEqual(saved["flow_entities"], ["switch.kitchen"])
                self.assertEqual(self.hass.persisted[CORE.STORAGE_KEY], self.entry.data)
                self.assertEqual(self.hass.removed_panels, [])

    async def test_disabled_flow_entities_are_not_exposed(self):
        self.hass.entity_registry.entities["switch.kitchen"].disabled = True
        await self.save()
        saved = self.entry.data[CORE.CONF_SELECTION_MODEL]["areas"]["kitchen"]
        self.assertNotIn("switch.kitchen", saved.get("flow_entities", []))
        self.assertEqual(self.hass.data[CORE.DOMAIN]["entities"], ["light.kitchen"])
        self.assertEqual(self.hass.removed_panels, [])

    async def test_selection_not_already_applied_still_reloads(self):
        await self.save()
        changed = deepcopy(self.entry.data)
        changed[CORE.CONF_WEATHER_ENTITY] = "weather.home"
        self.hass.config_entries.async_update_entry(self.entry, data=changed)
        await self.hass.config_entries.drain()
        self.assertEqual(self.hass.removed_panels, ["couchmate"])

    async def test_changed_options_still_reload(self):
        await self.save()
        self.hass.config_entries.async_update_entry(self.entry, options={"changed": True})
        await self.hass.config_entries.drain()
        self.assertEqual(self.hass.removed_panels, ["couchmate"])

    async def test_unload_still_removes_panel(self):
        await self.save()
        self.assertTrue(await CORE.async_unload_entry(self.hass, self.entry))
        self.assertEqual(self.hass.removed_panels, ["couchmate"])
        self.assertEqual(self.hass.panels, set())
        self.assertNotIn(CORE.DOMAIN, self.hass.data)

    async def test_saved_selection_survives_restart(self):
        await self.save()
        self.assertTrue(await CORE.async_unload_entry(self.hass, self.entry))
        self.assertTrue(await CORE.async_setup_entry(self.hass, self.entry))
        runtime = self.hass.data[CORE.DOMAIN]
        self.assertEqual(set(runtime["entities"]), {"light.kitchen", "switch.kitchen"})
        self.assertEqual(runtime["selection_model"], self.entry.data[CORE.CONF_SELECTION_MODEL])
        profile = runtime[CORE.CONFIGURATION_MANAGER].snapshot()["profiles"]["default"]
        self.assertEqual(profile["settings"]["companion"]["hero_layouts"]["kitchen"]["device_card_style"], "tile")


if __name__ == "__main__":
    unittest.main(verbosity=2)
