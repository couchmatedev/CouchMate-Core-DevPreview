#!/usr/bin/env python3
"""Exercise real layout storage/API code with in-memory Home Assistant adapters.

Run with Python 3.9+ and no third-party dependencies. The small framework
adapters cover only storage, bearer validation and JSON HTTP responses; the
production configuration manager and API handlers are imported unchanged.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest

sys.dont_write_bytecode = True
PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "couchmate"


class MemoryStore:
    def __init__(self, hass, version, key):
        self.hass, self.key = hass, key

    async def async_load(self):
        return deepcopy(self.hass.persisted.get(self.key))

    async def async_save(self, value):
        if self.hass.fail_save:
            raise OSError("storage unavailable")
        await asyncio.sleep(0)
        self.hass.persisted[self.key] = deepcopy(value)

    async def async_remove(self):
        self.hass.persisted.pop(self.key, None)


class JsonResponse:
    def __init__(self, payload=None, *, status=200, headers=None):
        self.status = status
        self.headers = dict(headers or {})
        self.body = json.dumps(payload).encode()

    @property
    def payload(self):
        return json.loads(self.body)


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    sys.modules[name] = result
    return result


def load(name, filename):
    qualified = f"_couchmate_layout_test.{name}"
    spec = importlib.util.spec_from_file_location(qualified, PACKAGE / filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = result
    spec.loader.exec_module(result)
    return result


def import_production_modules():
    module("_couchmate_layout_test", __path__=[str(PACKAGE)])
    module("homeassistant", __path__=[])
    module("homeassistant.core", HomeAssistant=object)
    module("homeassistant.helpers", __path__=[])
    module("homeassistant.helpers.storage", Store=MemoryStore)
    for registry in ("area_registry", "device_registry", "entity_registry"):
        module(f"homeassistant.helpers.{registry}")
    module("homeassistant.components", __path__=[])
    module("homeassistant.components.http", HomeAssistantView=object)
    module("homeassistant.components.persistent_notification")
    module("aiohttp", web=SimpleNamespace(Response=JsonResponse, json_response=JsonResponse))
    module(
        "_couchmate_layout_test.backgrounds",
        BackgroundManager=object,
        BackgroundProcessingUnavailable=type("BackgroundProcessingUnavailable", (Exception,), {}),
        BackgroundValidationError=type("BackgroundValidationError", (Exception,), {}),
        MAX_UPLOAD_BYTES=10 * 1024 * 1024,
        VARIANT_1080P="1080p", VARIANT_2160P="2160p", VARIANT_THUMBNAIL="thumbnail",
    )
    module("_couchmate_layout_test.pairing", PairingManager=object, PairingSession=object, PairingStatus=object)
    constants = load("const", "const.py")
    configuration = load("configuration", "configuration.py")
    api = load("configuration_api", "configuration_api.py")
    return constants, configuration, api


CONST, CONFIG, API = import_production_modules()


class PairedClients:
    capabilities = {
        "reader": set(),
        "writer": {"dashboard:write"},
        "settings-writer": {"configuration:write"},
        "background-writer": {"backgrounds:write"},
    }

    async def async_validate_client_token(self, token):
        return token if token in self.capabilities else None

    def client_has_capability(self, client_id, capability):
        return capability in self.capabilities.get(client_id, set())


class Request:
    def __init__(self, hass, token="writer", body=None):
        self.app = {"hass": hass}
        self.headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        self.body = body

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class DashboardLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = SimpleNamespace(persisted={}, fail_save=False, data={})
        self.manager = CONFIG.ConfigurationManager(self.hass)
        await self.manager.async_initialize()
        self.hass.data[CONST.DOMAIN] = {
            CONST.CONFIGURATION_MANAGER: self.manager,
            CONST.PAIRING_MANAGER: PairedClients(),
        }
        self.view = API.V2ClientDashboardLayoutView()

    def update(self, order, *, scope="rooms", room_id=None, client="writer", **extra):
        current = self.manager.client_dashboard_layout(client)
        result = {
            "base_revision": current["revision"], "profile_id": current["profile_id"],
            "scope": scope, "order": order, **extra,
        }
        if room_id is not None:
            result["room_id"] = room_id
        return result

    async def save(self, order, **kwargs):
        return await self.manager.async_update_client_dashboard_layout("writer", self.update(order, **kwargs))

    async def test_persistence_and_shared_profile(self):
        saved = await self.save(["room-b", "room-a"])
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(self.manager.client_dashboard_layout("reader"), saved)
        reloaded = CONFIG.ConfigurationManager(self.hass)
        await reloaded.async_initialize()
        self.assertEqual(reloaded.client_dashboard_layout("writer"), saved)
        saved["room_order"].clear()
        self.assertEqual(reloaded.client_dashboard_layout("writer")["room_order"], ["room-b", "room-a"])

    async def test_scope_preserves_other_settings_and_orders(self):
        await self.manager.async_set_home({"rooms": {}, "theme": "night"}, None)
        await self.manager.async_update_assigned_profile_settings("writer", {"hero": ["camera.door"]}, None)
        await self.save(["room-b", "room-a"])
        await self.save(["widget-a", "widget-b"], scope="widgets", room_id="room-a")
        await self.save(["widget-c"], scope="widgets", room_id="room-b")
        result = await self.save(["widget-b", "widget-a"], scope="widgets", room_id="room-a")
        self.assertEqual(result["room_order"], ["room-b", "room-a"])
        self.assertEqual(result["widget_orders"]["room-b"], ["widget-c"])
        self.assertEqual(self.manager.client_snapshot("writer")["profile"]["settings"], {"hero": ["camera.door"]})
        self.assertEqual(self.manager.snapshot()["home"]["theme"], "night")
        await self.manager.async_update_assigned_profile_settings("writer", {"new": True}, None)
        self.assertEqual(self.manager.client_dashboard_layout("writer")["widget_orders"], result["widget_orders"])

    async def test_current_tvos_hero_keys_survive_settings_and_restart(self):
        # tvOS 1.1 uses area-based Hero scopes and raw entity IDs. Layout
        # ordering must never select additional devices or rewrite card styles.
        settings = {"companion": {
            "hero_entity_order": {"living_room": ["light.ceiling", "switch.floor"]},
            "hero_layouts": {"living_room": {"thermostat_card_style": "ring"}},
        }}
        await self.manager.async_update_assigned_profile_settings("writer", settings, None)
        await self.save(["switch.office"], scope="widgets", room_id="hero|office")
        response = await self.view.put(Request(self.hass, body=self.update(
            ["switch.floor", "light.ceiling", "switch.temporarily_absent"],
            scope="widgets", room_id="hero|living_room",
        )))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.payload["widget_orders"]["hero|office"], ["switch.office"])
        self.assertEqual(self.manager.client_snapshot("reader")["profile"]["settings"], settings)
        await self.manager.async_update_assigned_profile_settings("settings-writer", settings, None)
        restored = CONFIG.ConfigurationManager(self.hass)
        await restored.async_initialize()
        self.assertEqual(restored.client_dashboard_layout("reader"), response.payload)

    async def test_widget_room_limit_is_atomic(self):
        document = self.manager.snapshot()
        document["profiles"]["default"]["dashboard_layout"]["widget_orders"] = {
            f"hero|room-{index}": [f"switch.device_{index}"] for index in range(256)
        }
        self.hass.persisted[CONST.CONFIGURATION_STORAGE_KEY] = document
        restored = CONFIG.ConfigurationManager(self.hass)
        await restored.async_initialize()
        before = restored.snapshot()
        with self.assertRaises(CONFIG.ValidationError):
            await restored.async_update_client_dashboard_layout("writer", {
                "base_revision": before["revision"], "scope": "widgets",
                "room_id": "hero|overflow", "order": ["switch.overflow"],
            })
        self.assertEqual(restored.snapshot(), before)
        self.assertEqual(self.hass.persisted[CONST.CONFIGURATION_STORAGE_KEY], before)

    async def test_all_layout_errors_are_uncacheable(self):
        requests = [
            Request(self.hass, None, self.update([])),
            Request(self.hass, "reader", self.update([])),
            Request(self.hass, body=ValueError("invalid JSON")),
        ]
        for request, status in zip(requests, (401, 403, 400)):
            response = await self.view.put(request)
            self.assertEqual(response.status, status)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    async def test_no_op_and_reset(self):
        self.assertEqual((await self.save([]))["revision"], 0)
        await self.save(["room-a"])
        saved = await self.save(["tile"], scope="widgets", room_id="room-a")
        self.assertEqual(await self.save(["tile"], scope="widgets", room_id="room-a"), saved)
        self.assertEqual((await self.save([], scope="widgets", room_id="room-a"))["widget_orders"], {})
        self.assertEqual((await self.save([]))["room_order"], [])

    async def test_profile_copy_isolation_and_delete(self):
        await self.save(["room-a"])
        profile = await self.manager.async_create_profile("Upstairs", "default")
        await self.manager.async_assign_profile("writer", profile["id"])
        self.assertEqual(self.manager.client_dashboard_layout("writer")["room_order"], ["room-a"])
        await self.save(["room-b"])
        self.assertEqual(self.manager.client_dashboard_layout("reader")["room_order"], ["room-a"])
        self.assertEqual(self.manager.client_dashboard_layout("writer")["room_order"], ["room-b"])
        await self.manager.async_delete_profile(profile["id"], None)
        self.assertEqual(self.manager.client_dashboard_layout("writer")["room_order"], ["room-a"])

    async def test_profile_assignment_rejects_inflight_and_rebased_edits(self):
        profile = await self.manager.async_create_profile("Upstairs")
        old_update = self.update(["old-room"])
        await self.manager.async_assign_profile("writer", profile["id"])
        with self.assertRaises(CONFIG.ConflictError):
            await self.manager.async_update_client_dashboard_layout("writer", old_update)
        old_update["base_revision"] = self.manager.snapshot()["revision"]
        with self.assertRaises(CONFIG.ConflictError):
            await self.manager.async_update_client_dashboard_layout("writer", old_update)
        self.assertEqual(self.manager.client_dashboard_layout("writer")["room_order"], [])

    async def test_concurrent_writers_and_scope_rebase(self):
        rooms = self.update(["room-b", "room-a"])
        widgets = self.update(["tile-b", "tile-a"], scope="widgets", room_id="room-a")
        outcomes = await asyncio.gather(
            self.manager.async_update_client_dashboard_layout("writer", rooms),
            self.manager.async_update_client_dashboard_layout("reader", widgets),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, CONFIG.ConflictError) for result in outcomes), 1)
        widgets["base_revision"] = self.manager.snapshot()["revision"]
        result = await self.manager.async_update_client_dashboard_layout("reader", widgets)
        self.assertEqual(result["room_order"], ["room-b", "room-a"])
        self.assertEqual(result["widget_orders"], {"room-a": ["tile-b", "tile-a"]})

    async def test_legacy_document_migrates_without_replacing_settings(self):
        legacy = self.manager.snapshot()
        del legacy["profiles"]["default"]["dashboard_layout"]
        legacy["profiles"]["default"]["settings"] = {"room_order": ["legacy-setting"], "hero": True}
        self.hass.persisted[CONST.CONFIGURATION_STORAGE_KEY] = legacy
        restored = CONFIG.ConfigurationManager(self.hass)
        await restored.async_initialize()
        self.assertEqual(restored.client_dashboard_layout("writer")["room_order"], [])
        self.assertEqual(restored.client_snapshot("writer")["profile"]["settings"], legacy["profiles"]["default"]["settings"])

    async def test_invalid_orders_do_not_mutate(self):
        invalid = [
            self.update(["duplicate", "duplicate"]), self.update([1]), self.update([""]),
            self.update([" leading"]), self.update(["control\n"]), self.update(["x" * 257]),
            self.update([str(index) for index in range(2049)]), self.update("not-an-array"),
            self.update([], scope="invalid"), self.update([], scope="widgets"),
            self.update([], base_revision=True), self.update([], base_revision=None),
            self.update([], settings={"unauthorized": True}), self.update([], room_id="not-widgets"),
        ]
        before = self.manager.snapshot()
        for value in invalid:
            with self.subTest(value=str(value)[:80]):
                with self.assertRaises(CONFIG.ValidationError):
                    await self.manager.async_update_client_dashboard_layout("writer", value)
        self.assertEqual(self.manager.snapshot(), before)

    async def test_failed_storage_keeps_previous_document(self):
        before = self.manager.snapshot()
        self.hass.fail_save = True
        with self.assertRaises(OSError):
            await self.save(["room-a"])
        self.assertEqual(self.manager.snapshot(), before)

    async def test_http_authentication_and_read_access(self):
        for token in (None, "invalid"):
            self.assertEqual((await self.view.get(Request(self.hass, token))).status, 401)
            self.assertEqual((await self.view.put(Request(self.hass, token, self.update([])))).status, 401)
        response = await self.view.get(Request(self.hass, "reader"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.payload, {"revision": 0, "profile_id": "default", "room_order": [], "widget_orders": {}})

    async def test_http_capabilities_are_narrow(self):
        for token in ("reader", "background-writer"):
            response = await self.view.put(Request(self.hass, token, self.update(["room-a"])))
            self.assertEqual(response.status, 403)
            self.assertEqual(response.payload["capability"], "dashboard:write")
        for token in ("writer", "settings-writer"):
            response = await self.view.put(Request(self.hass, token, self.update([token])))
            self.assertEqual(response.status, 200)
            self.assertEqual(response.payload["room_order"], [token])
        settings_response = await API.V2ClientSettingsView().put(Request(self.hass, "writer", {"settings": {}}))
        profile_response = await API.V2ClientProfileView().put(Request(self.hass, "writer", {"profile_id": "default"}))
        self.assertEqual(settings_response.status, 403)
        self.assertEqual(profile_response.status, 403)

    async def test_http_conflict_contains_current_layout(self):
        stale = self.update(["room-b"])
        await self.save(["room-a"])
        response = await self.view.put(Request(self.hass, body=stale))
        self.assertEqual(response.status, 409)
        self.assertEqual(response.payload["error"], "revision_conflict")
        self.assertEqual(response.payload["layout"], self.manager.client_dashboard_layout("writer"))
        self.assertEqual(response.payload["current_revision"], 1)

    async def test_http_rejects_malformed_body(self):
        for body in (None, [], ValueError("bad JSON"), self.update([], extra_field=True)):
            response = await self.view.put(Request(self.hass, body=body))
            self.assertEqual(response.status, 400)
        self.assertEqual(self.manager.snapshot()["revision"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
