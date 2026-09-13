#!/usr/bin/env python3
"""Exercise Flow save/restart and the real paired-client entity API."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import sys
from types import SimpleNamespace
import unittest

sys.dont_write_bytecode = True
import test_configurator_save as fixtures


def load_client_api():
    fixtures.module("voluptuous")
    fixtures.module("homeassistant.components.camera", async_get_image=fixtures.noop, async_request_stream=fixtures.noop)
    fixtures.module("homeassistant.components.image", async_get_image=fixtures.noop)
    fixtures.module("homeassistant.exceptions", HomeAssistantError=Exception)
    sys.modules[f"{fixtures.PACKAGE_NAME}.pairing"].PairingStatus = object
    sys.modules[f"{fixtures.PACKAGE_NAME}.diagnostics"].SCREENSHOT_MAX_BYTES = 1_000_000
    sys.modules["aiohttp"].web.json_response = lambda payload, **kwargs: payload
    spec = importlib.util.spec_from_file_location(f"{fixtures.PACKAGE_NAME}.flow_client_api", fixtures.PACKAGE / "api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


API = load_client_api()
CORE = fixtures.CORE


class FlowConfigurationTests(unittest.IsolatedAsyncioTestCase):
    save = fixtures.ConfiguratorSaveTests.save

    async def asyncSetUp(self):
        await fixtures.ConfiguratorSaveTests.asyncSetUp(self)
        now = datetime.now(timezone.utc)
        self.hass.area_registry.areas["kitchen"] = SimpleNamespace(id="kitchen", name="Kitchen")
        self.hass.area_registry.async_get_area = self.hass.area_registry.areas.get
        for entity in self.hass.entity_registry.entities.values():
            for attribute in ("name", "original_name", "icon", "original_icon", "device_class", "unit_of_measurement"):
                setattr(entity, attribute, None)
        for device in self.hass.device_registry.devices.values():
            device.name, device.name_by_user = "Lamp", None
        states = {
            entity_id: SimpleNamespace(entity_id=entity_id, name=entity_id, state="off", attributes={}, last_changed=now, last_updated=now)
            for entity_id in self.hass.entity_registry.entities
        }
        self.hass.states = SimpleNamespace(get=states.get, async_all=lambda domain: [])
        self.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"))

    async def client_response(self):
        async def validate(token):
            return "apple-tv" if token == "paired-token" else None
        self.hass.data[CORE.DOMAIN][CORE.PAIRING_MANAGER] = SimpleNamespace(async_validate_client_token=validate)
        self.hass.data[CORE.DOMAIN][CORE.DIAGNOSTICS_MANAGER] = SimpleNamespace(pending_for_target=lambda client_id: None)
        request = SimpleNamespace(app={"hass": self.hass}, headers={"Authorization": "Bearer paired-token"})
        return await API.CouchMateClientEntitiesView().get(request)

    async def test_legacy_selection_keeps_automatic_and_order(self):
        await self.save()
        response = await self.client_response()
        self.assertEqual(response["flow_modes"], {"kitchen": "automatic"})
        self.assertEqual(response["flow_entity_order"], {"kitchen": ["switch.kitchen"]})
        self.assertIn("switch.kitchen", [entity["entity_id"] for entity in response["entities"]])

    async def test_custom_content_survives_restart_and_client_response(self):
        area = self.payload["selection_model"]["areas"]["kitchen"]
        area["flow_mode"] = "custom"
        area["flow_entities"] = ["switch.kitchen", "light.kitchen"]
        await self.save()
        await CORE.async_unload_entry(self.hass, self.entry)
        await CORE.async_setup_entry(self.hass, self.entry)
        response = await self.client_response()
        self.assertEqual(response["flow_modes"], {"kitchen": "custom"})
        self.assertEqual(response["flow_entity_order"]["kitchen"], ["switch.kitchen", "light.kitchen"])

    async def test_modes_persist_without_selected_content(self):
        for mode in ("custom", "hidden"):
            self.payload["selection_model"]["areas"]["kitchen"] = {"flow_mode": mode, "devices": {}}
            await self.save()
            saved = deepcopy(self.entry.data[CORE.CONF_SELECTION_MODEL])
            self.assertEqual(saved["areas"]["kitchen"]["flow_mode"], mode)
            response = await self.client_response()
            self.assertEqual(response["flow_modes"], {"kitchen": mode})
            self.assertEqual(response["entities"], [])

    async def test_right_hero_order_survives_restart_and_client_response(self):
        area = self.payload["selection_model"]["areas"]["kitchen"]
        area["hero_right_entities"] = ["switch.kitchen", "light.kitchen", "switch.kitchen"]
        await self.save()
        await CORE.async_unload_entry(self.hass, self.entry)
        await CORE.async_setup_entry(self.hass, self.entry)
        response = await self.client_response()
        self.assertEqual(response["hero_right_entity_order"], {"kitchen": ["switch.kitchen", "light.kitchen"]})
        self.assertTrue({"switch.kitchen", "light.kitchen"}.issubset(response["explicit_entity_ids"]))
        self.assertNotIn("light.kitchen", response["hero_entity_order"].get("kitchen", []))

    async def test_right_only_room_and_removing_selection(self):
        self.payload["selection_model"]["areas"]["kitchen"] = {"hero_right_entities": ["switch.kitchen"]}
        await self.save()
        response = await self.client_response()
        self.assertEqual(response["hero_right_entity_order"], {"kitchen": ["switch.kitchen"]})
        self.assertEqual(response["explicit_entity_ids"], ["switch.kitchen"])
        self.payload["selection_model"]["areas"]["kitchen"] = {"hero_right_entities": []}
        await self.save()
        response = await self.client_response()
        self.assertEqual(response["hero_right_entity_order"], {})
        self.assertEqual(response["entities"], [])

    async def test_right_selection_rejects_invalid_disabled_and_foreign_entities(self):
        registry = self.hass.entity_registry.entities
        registry["light.foreign"] = SimpleNamespace(entity_id="light.foreign", area_id="garden", device_id=None, disabled=False)
        registry["light.disabled"] = SimpleNamespace(entity_id="light.disabled", area_id="kitchen", device_id=None, disabled=True)
        registry["camera.kitchen"] = SimpleNamespace(entity_id="camera.kitchen", area_id="kitchen", device_id=None, disabled=False)
        for raw in ("switch.kitchen", {}, None, ["light.foreign", "camera.kitchen", "light.disabled", "light.missing", None, {}]):
            self.payload["selection_model"]["areas"]["kitchen"]["hero_right_entities"] = raw
            await self.save()
            self.assertNotIn("hero_right_entities", self.entry.data[CORE.CONF_SELECTION_MODEL]["areas"]["kitchen"])

    async def test_invalid_modes_preserve_legacy_behavior(self):
        for invalid in ("invalid", None, ["custom"], {"mode": "hidden"}):
            self.payload["selection_model"]["areas"]["kitchen"]["flow_mode"] = invalid
            await self.save()
            self.assertEqual((await self.client_response())["flow_modes"], {"kitchen": "automatic"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
