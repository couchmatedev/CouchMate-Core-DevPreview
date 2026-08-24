"""Diagnostic sensors for CouchMate."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import area_registry as ar, device_registry as dr
from .const import DOMAIN
async def async_setup_entry(hass,entry,async_add_entities):
    async_add_entities([SelectionSensor(hass,entry,k) for k in ("areas","devices","explicit_entities","excluded_entities","entities")],True)
class SelectionSensor(SensorEntity):
    _attr_entity_category=EntityCategory.DIAGNOSTIC
    _attr_has_entity_name=True
    def __init__(self,hass,entry,key):
        self.hass=hass; self._key=key
        self._attr_name={"areas":"Ausgewählte Bereiche","devices":"Ausgewählte Geräte","explicit_entities":"Einzeln ausgewählte Entitäten","excluded_entities":"Ausgeschlossene Entitäten","entities":"Freigegebene Entitäten gesamt"}[key]
        self._attr_unique_id=f"{entry.entry_id}_{key}_count"
        self._attr_icon={"areas":"mdi:floor-plan","devices":"mdi:devices","explicit_entities":"mdi:format-list-checks","excluded_entities":"mdi:playlist-remove","entities":"mdi:television-guide"}[key]
        self._attr_device_info=DeviceInfo(identifiers={(DOMAIN,entry.entry_id)},name="CouchMate Core Dev Preview",manufacturer="CouchMate",model="Home Assistant entity bridge",sw_version="1.4.0-beta.8")
    @property
    def native_value(self): return len(self.hass.data.get(DOMAIN,{}).get(self._key,[]))
    @property
    def extra_state_attributes(self):
        values=list(self.hass.data.get(DOMAIN,{}).get(self._key,[]))
        if self._key=="areas":
            reg=ar.async_get(self.hass); return {"bereiche":[reg.async_get_area(v).name if reg.async_get_area(v) else v for v in values],"area_ids":values}
        if self._key=="devices":
            reg=dr.async_get(self.hass); names=[]
            for v in values:
                d=reg.async_get(v); names.append((d.name_by_user or d.name) if d else v)
            return {"geräte":names,"device_ids":values}
        return {"entitäten":values}
