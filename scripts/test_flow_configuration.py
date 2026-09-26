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
            device.manufacturer, device.model = None, None
        states = {
            entity_id: SimpleNamespace(entity_id=entity_id, name=entity_id, state="off", attributes={}, last_changed=now, last_updated=now)
            for entity_id in self.hass.entity_registry.entities
        }
        self.states = states
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

    def add_timer(self, entity_id="timer.cooking", *, area_id=None, disabled=False):
        now = datetime.now(timezone.utc)
        self.hass.entity_registry.entities[entity_id] = SimpleNamespace(
            entity_id=entity_id, area_id=area_id, device_id=None,
            disabled=disabled, name=None, original_name=None, icon=None,
            original_icon=None, device_class=None, unit_of_measurement=None,
        )
        self.states[entity_id] = SimpleNamespace(
            entity_id=entity_id, name="Cooking", state="active",
            attributes={
                "duration": "0:10:00",
                "finishes_at": "2026-09-23T10:10:00+00:00",
                "remaining": "0:08:00",
            }, last_changed=now, last_updated=now,
        )

    async def test_device_less_timer_is_selectable_and_has_room_in_client_payload(self):
        self.add_timer()
        data = await fixtures.CONFIGURATOR.CouchMateConfiguratorDataView().get(
            fixtures.Request(self.hass, {})
        )
        kitchen = next(area for area in data["areas"] if area["id"] == "kitchen")
        self.assertEqual(kitchen["timer_candidates"][0]["entity_id"], "timer.cooking")
        self.assertTrue(kitchen["timer_candidates"][0]["area_unassigned"])

        self.payload["selection_model"]["areas"]["kitchen"] = {
            "timer_entities": ["timer.cooking"], "devices": {},
        }
        await self.save()
        await CORE.async_unload_entry(self.hass, self.entry)
        await CORE.async_setup_entry(self.hass, self.entry)
        response = await self.client_response()
        timer = next(item for item in response["entities"] if item["entity_id"] == "timer.cooking")
        self.assertEqual(timer["area_id"], "kitchen")
        self.assertEqual(timer["area_name"], "Kitchen")
        self.assertEqual(timer["attributes"]["finishes_at"], "2026-09-23T10:10:00+00:00")
        self.assertIn("timer.cooking", response["explicit_entity_ids"])
        self.assertIn({"id": "kitchen", "name": "Kitchen"}, response["areas"])

    async def test_timer_selection_rejects_foreign_disabled_and_other_domains(self):
        self.add_timer("timer.foreign", area_id="garden")
        self.add_timer("timer.disabled", disabled=True)
        self.payload["selection_model"]["areas"]["kitchen"] = {
            "timer_entities": ["timer.foreign", "timer.disabled", "switch.kitchen", "timer.missing"],
            "devices": {},
        }
        await self.save()
        self.assertNotIn("timer_entities", self.entry.data[CORE.CONF_SELECTION_MODEL]["areas"].get("kitchen", {}))
        self.assertEqual((await self.client_response())["entities"], [])

    async def test_unassigned_timer_can_be_selected_in_only_one_room(self):
        self.add_timer()
        self.hass.area_registry.areas["living"] = SimpleNamespace(id="living", name="Living")
        self.payload["selection_model"]["areas"] = {
            "kitchen": {"timer_entities": ["timer.cooking"], "devices": {}},
            "living": {"timer_entities": ["timer.cooking"], "devices": {}},
        }
        await self.save()
        saved = self.entry.data[CORE.CONF_SELECTION_MODEL]["areas"]
        self.assertEqual(saved["kitchen"]["timer_entities"], ["timer.cooking"])
        self.assertNotIn("timer_entities", saved.get("living", {}))
        response = await self.client_response()
        timer = next(item for item in response["entities"] if item["entity_id"] == "timer.cooking")
        self.assertEqual(timer["area_id"], "kitchen")

    def add_washdata_device(self, *, device_id="washer", area_id=None, entry_id="wash-entry"):
        device = SimpleNamespace(
            id=device_id, area_id=area_id, identifiers={("ha_washdata", entry_id)},
            name="Washing Machine", name_by_user=None, manufacturer="WashData", model=None,
        )
        self.hass.device_registry.devices[device_id] = device
        now = datetime.now(timezone.utc)
        for role, key, state in (
            ("state", "washer_state", "running"),
            ("program", "washer_program", "Cotton"),
            ("time_remaining", "time_remaining", "23"),
            ("cycle_progress", "cycle_progress", "62"),
            ("current_phase", "current_phase", "Rinse"),
            ("total_duration", "total_duration", "60"),
        ):
            # Deliberately renamed entity IDs: the Core must use unique IDs.
            entity_id = f"sensor.custom_{device_id}_{role}"
            self.hass.entity_registry.entities[entity_id] = SimpleNamespace(
                entity_id=entity_id, device_id=device_id, area_id=None,
                unique_id=f"{entry_id}_{key}", platform="ha_washdata",
                disabled=False, name=None, original_name=None, icon=None,
                original_icon=None, device_class=None, unit_of_measurement=None,
            )
            self.states[entity_id] = SimpleNamespace(
                entity_id=entity_id, name=role, state=state,
                attributes={"unit_of_measurement": "min" if role in ("time_remaining", "total_duration") else None},
                last_changed=now, last_updated=now,
            )
        return device

    async def test_area_less_washdata_group_survives_restart_and_maps_renamed_sensors(self):
        self.add_washdata_device()
        data = await fixtures.CONFIGURATOR.CouchMateConfiguratorDataView().get(
            fixtures.Request(self.hass, {})
        )
        kitchen = next(area for area in data["areas"] if area["id"] == "kitchen")
        self.assertEqual(kitchen["washdata_candidates"], [{
            "device_id": "washer", "name": "Washing Machine", "area_unassigned": True,
        }])
        self.payload["selection_model"]["areas"]["kitchen"] = {
            "washdata_devices": ["washer"], "devices": {},
        }
        await self.save()
        await CORE.async_unload_entry(self.hass, self.entry)
        await CORE.async_setup_entry(self.hass, self.entry)
        response = await self.client_response()
        self.assertEqual(len(response["washdata_appliances"]), 1)
        appliance = response["washdata_appliances"][0]
        self.assertEqual(appliance["name"], "Washing Machine")
        self.assertEqual(appliance["area_id"], "kitchen")
        self.assertEqual(appliance["sensor_entity_ids"]["time_remaining"], "sensor.custom_washer_time_remaining")
        self.assertEqual(appliance["sensor_entity_ids"]["state"], "sensor.custom_washer_state")
        self.assertEqual(len(appliance["sensor_entity_ids"]), 6)
        self.assertIn("sensor.custom_washer_time_remaining", response["explicit_entity_ids"])
        self.assertIn("sensor.custom_washer_time_remaining", [item["entity_id"] for item in response["entities"]])
        self.assertTrue(all(item["area_id"] == "kitchen" for item in response["entities"]))

    async def test_washdata_selection_rejects_other_integration_and_foreign_room(self):
        self.add_washdata_device(device_id="foreign", area_id="garden")
        plain = self.add_washdata_device(device_id="not_washdata")
        plain.identifiers = {("other_integration", "wash-entry")}
        self.payload["selection_model"]["areas"]["kitchen"] = {
            "washdata_devices": ["foreign", "not_washdata", "missing"],
            "devices": {},
        }
        await self.save()
        self.assertNotIn("washdata_devices", self.entry.data[CORE.CONF_SELECTION_MODEL]["areas"].get("kitchen", {}))
        self.assertEqual((await self.client_response())["washdata_appliances"], [])

    async def test_unassigned_washdata_device_can_be_selected_once(self):
        self.add_washdata_device()
        self.hass.area_registry.areas["living"] = SimpleNamespace(id="living", name="Living")
        self.payload["selection_model"]["areas"] = {
            "kitchen": {"washdata_devices": ["washer"], "devices": {}},
            "living": {"washdata_devices": ["washer"], "devices": {}},
        }
        await self.save()
        saved = self.entry.data[CORE.CONF_SELECTION_MODEL]["areas"]
        self.assertEqual(saved["kitchen"]["washdata_devices"], ["washer"])
        self.assertNotIn("washdata_devices", saved.get("living", {}))
        self.assertEqual((await self.client_response())["washdata_appliances"][0]["area_id"], "kitchen")

    async def test_area_assigned_full_washdata_device_has_role_map(self):
        self.add_washdata_device(area_id="kitchen")
        self.payload["selection_model"]["areas"]["kitchen"] = {
            "devices": {"washer": {"mode": "all", "entities": []}},
        }
        await self.save()
        response = await self.client_response()
        appliance = response["washdata_appliances"][0]
        self.assertEqual(appliance["area_id"], "kitchen")
        self.assertEqual(appliance["sensor_entity_ids"]["program"], "sensor.custom_washer_program")
        self.assertIn("washer", response["full_device_ids"])

    async def test_newly_enabled_washdata_sensor_appears_without_resaving(self):
        self.add_washdata_device()
        phase = self.hass.entity_registry.entities["sensor.custom_washer_current_phase"]
        phase.disabled = True
        self.payload["selection_model"]["areas"]["kitchen"] = {
            "washdata_devices": ["washer"], "devices": {},
        }
        await self.save()
        self.assertNotIn("current_phase", (await self.client_response())["washdata_appliances"][0]["sensor_entity_ids"])
        phase.disabled = False
        response = await self.client_response()
        self.assertEqual(
            response["washdata_appliances"][0]["sensor_entity_ids"]["current_phase"],
            "sensor.custom_washer_current_phase",
        )
        self.assertIn("sensor.custom_washer_current_phase", response["explicit_entity_ids"])

    async def test_timer_service_whitelist_and_duration_validation(self):
        self.add_timer()
        self.payload["selection_model"]["areas"]["kitchen"] = {
            "timer_entities": ["timer.cooking"], "devices": {},
        }
        await self.save()
        await self.client_response()  # Install the paired-token adapter.
        calls = []

        async def call(*args, **kwargs):
            calls.append((args, kwargs))

        self.hass.services.async_call = call

        async def invoke(service, data=None, entity_ids=None):
            async def body():
                return {"domain": "timer", "service": service,
                        "entity_ids": entity_ids or ["timer.cooking"], "data": data or {}}
            request = SimpleNamespace(
                app={"hass": self.hass},
                headers={"Authorization": "Bearer paired-token"},
                json=body,
            )
            return await API.CouchMateClientServiceView().post(request)

        for service, data, expected in (
            ("start", {}, {}),
            ("start", {"duration": "00:05:00"}, {"duration": 300}),
            ("start", {"duration": 60.0}, {"duration": 60}),
            ("pause", {}, {}),
            ("cancel", {}, {}),
            ("finish", {}, {}),
            ("change", {"duration": -60}, {"duration": -60}),
            ("change", {"duration": -60.0}, {"duration": -60}),
        ):
            response = await invoke(service, data)
            self.assertTrue(response["success"])
            self.assertEqual(calls[-1][0], ("timer", service, expected))
            self.assertEqual(calls[-1][1]["target"], {"entity_id": ["timer.cooking"]})

        before = len(calls)
        for service, data in (
            ("reload", {}),
            ("pause", {"duration": 60}),
            ("change", {}),
            ("change", {"duration": 0}),
            ("start", {"duration": -5}),
            ("start", {"duration": "00:60:00"}),
            ("start", {"duration": "--5"}),
            ("start", {"duration": True}),
            ("start", {"duration": 60.5}),
            ("start", {"duration": float("nan")}),
            ("change", {"duration": float("inf")}),
            ("start", {"entity_id": "timer.foreign"}),
        ):
            response = await invoke(service, data)
            self.assertIn("error", response)
        self.assertEqual((await invoke("start", entity_ids=["timer.foreign"]))["error"], "entity_not_selected")
        self.assertEqual(len(calls), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
