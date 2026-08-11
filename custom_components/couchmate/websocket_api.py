"""WebSocket API for CouchMate filtered subscriptions."""
from __future__ import annotations
from typing import Any
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from .const import DOMAIN, WS_TYPE_GET_ENTITIES, WS_TYPE_SUBSCRIBE_FILTERED, WS_TYPE_UPDATE_ENTITIES
from .storage import async_save_entities

async def async_setup_websocket_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_subscribe_filtered)
    websocket_api.async_register_command(hass, handle_get_entities)
    websocket_api.async_register_command(hass, handle_update_entities)

def _state_to_dict(state: State) -> dict[str, Any]:
    return {"entity_id":state.entity_id,"state":state.state,"attributes":dict(state.attributes),"last_changed":state.last_changed.isoformat(),"last_updated":state.last_updated.isoformat()}

@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_SUBSCRIBE_FILTERED})
@callback
def handle_subscribe_filtered(hass, connection, msg):
    allowed = list(hass.data.get(DOMAIN, {}).get("entities", []))
    connection.send_result(msg["id"], {"states":[_state_to_dict(s) for eid in allowed if (s:=hass.states.get(eid))]})
    @callback
    def forward(event):
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        connection.send_message(websocket_api.messages.event_message(msg["id"], {"event_type":"state_changed","data":{"entity_id":event.data.get("entity_id"),"old_state":_state_to_dict(old) if old else None,"new_state":_state_to_dict(new) if new else None},"origin":event.origin,"time_fired":event.time_fired.isoformat()}))
    connection.subscriptions[msg["id"]] = async_track_state_change_event(hass, allowed, forward)

@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_GET_ENTITIES})
@callback
def handle_get_entities(hass, connection, msg):
    allowed = hass.data.get(DOMAIN, {}).get("entities", [])
    connection.send_result(msg["id"], {"entities":[_state_to_dict(s) for eid in allowed if (s:=hass.states.get(eid))]})

@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_UPDATE_ENTITIES, vol.Required("entities"):[str]})
@callback
def handle_update_entities(hass, connection, msg):
    valid = [eid for eid in msg["entities"] if hass.states.get(eid)]
    hass.data.setdefault(DOMAIN, {})["entities"] = valid
    hass.async_create_task(async_save_entities(hass, {"entities":valid,"areas":[],"devices":[]}))
    connection.send_result(msg["id"], {"success":True,"entities":valid})
