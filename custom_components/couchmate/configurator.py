"""Graphical room, device and entity configurator for CouchMate."""
from __future__ import annotations

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .const import (
    DOMAIN,
    CONF_AREAS,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_EXCLUDED_ENTITIES,
    CONF_ROOM_TEMPERATURES,
    CONF_ROOM_HUMIDITIES,
    CONF_SELECTION_MODEL,
    SELECTION_MODEL_VERSION,
)
from .storage import async_save_entities


def _name(entry, state, fallback):
    return entry.name or entry.original_name or (state.name if state else None) or fallback


def _is_humidity_entity(entity, state) -> bool:
    if entity.entity_id.startswith("sensor."):
        device_class = getattr(entity, "device_class", None) or (state.attributes.get("device_class") if state else None)
        unit = state.attributes.get("unit_of_measurement") if state else None
        return device_class == "humidity" or unit == "%" and "humid" in entity.entity_id.lower()
    return False


def _is_temperature_entity(entity, state) -> bool:
    if entity.entity_id.startswith("sensor."):
        device_class = getattr(entity, "device_class", None) or (state.attributes.get("device_class") if state else None)
        unit = state.attributes.get("unit_of_measurement") if state else None
        return device_class == "temperature" or unit in ("°C", "°F")
    return False


def _admin_required(request) -> web.Response | None:
    """Keep the global CouchMate selection restricted to HA administrators."""
    user = request.get("hass_user")
    if user is not None and getattr(user, "is_admin", False):
        return None
    return web.json_response({"error": "admin_required"}, status=403)


class CouchMateConfiguratorView(HomeAssistantView):
    url = "/couchmate/configurator"
    name = "couchmate:configurator"
    requires_auth = False

    async def get(self, request):
        return web.Response(text=HTML, content_type="text/html")


class CouchMateConfiguratorDataView(HomeAssistantView):
    url = "/api/couchmate/configurator/data"
    name = "api:couchmate:configurator:data"
    requires_auth = True

    async def get(self, request):
        denied = _admin_required(request)
        if denied is not None:
            return denied
        hass = request.app["hass"]
        areas = ar.async_get(hass)
        devices = dr.async_get(hass)
        entities = er.async_get(hass)
        current = hass.data.get(DOMAIN, {})
        device_area = {device.id: device.area_id for device in devices.devices.values()}
        room_temperatures = current.get("room_temperatures", {})
        room_humidities = current.get("room_humidities", {})
        selection_model = current.get("selection_model", {})
        model_areas = dict(selection_model.get("areas", {})) if selection_model.get("version") == SELECTION_MODEL_VERSION else {}
        explicit_entities = set(current.get("explicit_entities", []))
        fully_selected_devices = set(current.get("devices", []))

        result_areas = []
        for area in sorted(areas.areas.values(), key=lambda x: x.name.casefold()):
            area_devices = []
            temperature_candidates = []
            humidity_candidates = []

            for entity in entities.entities.values():
                if entity.disabled:
                    continue
                entity_area = entity.area_id or (device_area.get(entity.device_id) if entity.device_id else None)
                if entity_area != area.id:
                    continue
                state = hass.states.get(entity.entity_id)
                if _is_temperature_entity(entity, state):
                    temperature_candidates.append({
                        "entity_id": entity.entity_id,
                        "name": _name(entity, state, entity.entity_id),
                    })
                if _is_humidity_entity(entity, state):
                    humidity_candidates.append({
                        "entity_id": entity.entity_id,
                        "name": _name(entity, state, entity.entity_id),
                    })

            for device in devices.devices.values():
                if device.area_id != area.id:
                    continue
                device_entities = []
                for entity in entities.entities.values():
                    if entity.disabled or entity.device_id != device.id:
                        continue
                    state = hass.states.get(entity.entity_id)
                    device_entities.append({
                        "entity_id": entity.entity_id,
                        "name": _name(entity, state, entity.entity_id),
                        "domain": entity.entity_id.split(".", 1)[0],
                        "selected": entity.entity_id in explicit_entities,
                        "excluded": entity.entity_id in current.get("excluded_entities", []),
                    })
                model_device = dict(dict(model_areas.get(area.id, {})).get("devices", {})).get(device.id, {})
                mode = model_device.get("mode") if isinstance(model_device, dict) else None
                if mode not in ("all", "entities"):
                    mode = "entities" if any(item["selected"] for item in device_entities) else ("all" if device.id in fully_selected_devices else "none")
                area_devices.append({
                    "id": device.id,
                    "name": device.name_by_user or device.name or device.id,
                    "manufacturer": device.manufacturer or "",
                    "model": device.model or "",
                    "selection_mode": mode,
                    "selected_count": sum(1 for item in device_entities if item["selected"]),
                    "entities": sorted(device_entities, key=lambda x: x["name"].casefold()),
                })
            result_areas.append({
                "id": area.id,
                "name": area.name,
                "selected": bool(model_areas.get(area.id)) or bool(room_temperatures.get(area.id)) or bool(room_humidities.get(area.id)),
                "temperature_entity": room_temperatures.get(area.id),
                "temperature_candidates": sorted(temperature_candidates, key=lambda x: x["name"].casefold()),
                "humidity_entity": room_humidities.get(area.id),
                "humidity_candidates": sorted(humidity_candidates, key=lambda x: x["name"].casefold()),
                "devices": sorted(area_devices, key=lambda x: x["name"].casefold()),
            })
        return self.json({"areas": result_areas})


class CouchMateConfiguratorSaveView(HomeAssistantView):
    url = "/api/couchmate/configurator/save"
    name = "api:couchmate:configurator:save"
    requires_auth = True

    async def post(self, request):
        denied = _admin_required(request)
        if denied is not None:
            return denied
        hass = request.app["hass"]
        payload = await request.json()
        raw_model = dict(payload.get("selection_model", {}))
        raw_areas = dict(raw_model.get("areas", {}))

        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
        area_registry = ar.async_get(hass)

        model_areas: dict[str, dict] = {}
        selected_devices: list[str] = []
        explicit_entities: list[str] = []
        temperatures: dict[str, str] = {}
        humidities: dict[str, str] = {}

        for area_id, raw_area in raw_areas.items():
            area_id = str(area_id)
            if area_registry.async_get_area(area_id) is None or not isinstance(raw_area, dict):
                continue
            area_cfg: dict = {"devices": {}}

            temperature = raw_area.get("temperature")
            if temperature and entity_registry.async_get(str(temperature)):
                temperatures[area_id] = str(temperature)
                area_cfg["temperature"] = str(temperature)

            humidity = raw_area.get("humidity")
            if humidity and entity_registry.async_get(str(humidity)):
                humidities[area_id] = str(humidity)
                area_cfg["humidity"] = str(humidity)

            for device_id, raw_device in dict(raw_area.get("devices", {})).items():
                device_id = str(device_id)
                device = device_registry.async_get(device_id)
                if device is None or not isinstance(raw_device, dict):
                    continue
                mode = raw_device.get("mode")
                if mode == "all":
                    selected_devices.append(device_id)
                    area_cfg["devices"][device_id] = {"mode": "all", "entities": []}
                    continue
                if mode != "entities":
                    continue
                valid_entities: list[str] = []
                for entity_id in raw_device.get("entities", []):
                    entity_id = str(entity_id)
                    entry = entity_registry.async_get(entity_id)
                    if entry is None or entry.device_id != device_id:
                        continue
                    valid_entities.append(entity_id)
                valid_entities = list(dict.fromkeys(valid_entities))
                if valid_entities:
                    explicit_entities.extend(valid_entities)
                    area_cfg["devices"][device_id] = {"mode": "entities", "entities": valid_entities}

            if area_cfg.get("temperature") or area_cfg.get("humidity") or area_cfg["devices"]:
                model_areas[area_id] = area_cfg

        selected_devices = list(dict.fromkeys(selected_devices))
        explicit_entities = list(dict.fromkeys(explicit_entities))
        selection_model = {"version": SELECTION_MODEL_VERSION, "areas": model_areas}

        # Version 2 is authoritative: no broad area selection is stored. A
        # device is expanded only when its explicit mode is "all"; otherwise
        # only the exact checked entity ids are exposed.
        data = {
            CONF_AREAS: [],
            CONF_DEVICES: selected_devices,
            CONF_ENTITIES: explicit_entities,
            CONF_EXCLUDED_ENTITIES: [],
            CONF_ROOM_TEMPERATURES: temperatures,
            CONF_ROOM_HUMIDITIES: humidities,
            CONF_SELECTION_MODEL: selection_model,
        }
        await async_save_entities(hass, data)
        from . import _resolve_filter
        resolved = sorted(_resolve_filter(
            hass,
            areas=[],
            devices=selected_devices,
            entities=explicit_entities,
            excluded_entities=[],
        ))
        runtime = hass.data.setdefault(DOMAIN, {})
        runtime.update({
            "areas": [],
            "devices": selected_devices,
            "explicit_entities": explicit_entities,
            "excluded_entities": [],
            "room_temperatures": temperatures,
            "room_humidities": humidities,
            "selection_model": selection_model,
            "entities": resolved,
        })
        entry = runtime.get("entry")
        if entry:
            hass.config_entries.async_update_entry(entry, data=data)
        return self.json({
            "success": True,
            "resolved_count": len(resolved),
            "temperature_count": len(temperatures),
            "humidity_count": len(humidities),
            "area_count": len(model_areas),
            "device_count": len(selected_devices),
            "entity_count": len(explicit_entities),
        })


async def async_setup_configurator(hass):
    hass.http.register_view(CouchMateConfiguratorView())
    hass.http.register_view(CouchMateConfiguratorDataView())
    hass.http.register_view(CouchMateConfiguratorSaveView())


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CouchMate Core Dev Preview – Konfigurator</title><style>
:root{color-scheme:dark;--bg:#0f1416;--card:#1a2023;--card2:#20282c;--muted:#a7b1b6;--accent:#71d6c5;--line:#39464c;--ok:#75d6a2;--danger:#ff8a8a}*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:#f5f7f7}.wrap{max-width:1500px;margin:auto;padding:42px 52px 80px}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:28px;position:sticky;top:0;background:linear-gradient(var(--bg) 82%,transparent);padding:10px 0 28px;z-index:5}.top-actions{display:flex;gap:10px;align-items:center}.manage-link{color:#fff;text-decoration:none;border:1px solid var(--line);background:var(--card);border-radius:18px;padding:16px 19px;font-weight:700;white-space:nowrap}.manage-link:hover{background:var(--card2);border-color:#607078}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:20px}.tile{border:1px solid var(--line);border-radius:26px;background:var(--card);padding:26px;cursor:pointer;text-align:left;color:inherit;min-height:190px;transition:.16s ease}.tile:hover{border-color:#607078;background:var(--card2);transform:translateY(-1px)}.tile.active{outline:3px solid var(--accent);background:#17312e}.tile.partial{outline:2px solid var(--accent);background:#172523}.tile .state{display:inline-block;margin-top:12px;padding:6px 10px;border-radius:999px;background:#111719;color:var(--muted);font-size:14px}.tile.active .state,.tile.partial .state{color:var(--accent)}.icon{width:48px;height:48px;color:var(--accent);margin-bottom:26px}.icon svg{width:100%;height:100%;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.sub{color:var(--muted);font-size:17px;line-height:1.35}.section{margin-top:38px}.entities,.temperatures,.humidities{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}.entity,.temperature,.humidity{display:flex;gap:16px;align-items:center;border:1px solid var(--line);border-radius:20px;padding:21px 22px;background:var(--card);min-height:104px;cursor:pointer;overflow:hidden}.entity:hover,.temperature:hover,.humidity:hover{background:var(--card2)}.entity input,.temperature input,.humidity input{width:22px;height:22px;accent-color:var(--accent);flex:0 0 auto}.entity b,.temperature b,.humidity b{font-size:19px}.entity .sub,.temperature .sub,.humidity .sub{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block;max-width:100%}button.primary{background:var(--accent);color:#061411;border:0;border-radius:18px;padding:17px 26px;font-size:16px;font-weight:750;cursor:pointer;min-width:190px}button.primary:disabled{opacity:.65;cursor:wait}.hidden{display:none!important}.crumb{color:var(--muted);margin:10px 0 28px;font-size:19px}.back{background:none;border:0;color:var(--accent);font-size:18px;cursor:pointer;padding:0}.temperature-panel,.humidity-panel{border:1px solid var(--line);border-radius:26px;padding:24px;background:#141a1d;margin:0 0 32px}.temperature-panel h3,.humidity-panel h3{margin:0 0 6px;font-size:21px}.temperature-panel p,.humidity-panel p{margin:0 0 20px}.toast{position:fixed;right:28px;bottom:28px;max-width:520px;border:1px solid var(--line);border-radius:20px;padding:18px 22px;background:#20282c;box-shadow:0 18px 50px rgba(0,0,0,.35);z-index:20}.toast.ok{border-color:var(--ok)}.toast.error{border-color:var(--danger)}.toast strong{display:block;font-size:18px;margin-bottom:5px}h1{font-size:40px;margin:0 0 18px}h2{font-size:30px;margin:0 0 24px}h3{font-size:19px;margin:0 0 12px;font-weight:700}@media(max-width:700px){.wrap{padding:24px 18px 70px}.top{position:static;display:block}.top-actions{display:grid;margin-top:20px}.top button,.manage-link{width:100%;margin:0;text-align:center}.grid{grid-template-columns:1fr}.entities,.temperatures,.humidities{grid-template-columns:1fr}.toast{left:18px;right:18px;bottom:18px}h1{font-size:31px}}
</style></head><body><main class="wrap"><div class="top"><div><h1>CouchMate Core Dev Preview</h1><div class="sub">Raum → Gerät → benötigte Funktionen</div></div><div class="top-actions"><a class="manage-link" href="/couchmate/management">Apple TVs & Design</a><button id="save" class="primary">Auswahl speichern</button></div></div><section id="areas" class="section"><h2>Räume</h2><div id="areaGrid" class="grid"></div></section><section id="devices" class="section hidden"><button class="back" id="backAreas">← Räume</button><div class="crumb" id="areaName"></div><div id="temperaturePanel" class="temperature-panel"><h3>Raumtemperatur</h3><p class="sub">Wähle den Sensor, der in CouchMate als Temperatur dieses Raums verwendet werden soll.</p><div id="temperatureGrid" class="temperatures"></div></div><div id="humidityPanel" class="humidity-panel"><h3>Luftfeuchtigkeit</h3><p class="sub">Wähle den Sensor, der in CouchMate als Luftfeuchtigkeit dieses Raums verwendet werden soll.</p><div id="humidityGrid" class="humidities"></div></div><h2>Geräte</h2><div id="deviceGrid" class="grid"></div></section><section id="entities" class="section hidden"><button class="back" id="backDevices">← Geräte</button><div class="crumb" id="deviceName"></div><div class="temperature-panel"><label class="entity"><input id="selectWholeDevice" type="checkbox"><span><b>Ganzes Gerät verwenden</b><span class="sub">Alle aktuellen und zukünftigen Funktionen dieses Geräts an CouchMate übertragen.</span></span></label></div><h2>Funktionen</h2><div id="entityGrid" class="entities"></div></section></main><div id="toast" class="toast hidden" role="status" aria-live="polite"></div><script>
let data,area,device;
const selected={devices:new Map(),temperatures:{},humidities:{}};
const paths={room:'<path d="M4 10.5 12 4l8 6.5V20H4z"/><path d="M9 20v-6h6v6"/>',light:'<path d="M9 18h6"/><path d="M10 22h4"/><path d="M8.5 14.5A6 6 0 1 1 15.5 14.5c-1 .8-1.5 1.8-1.5 3h-4c0-1.2-.5-2.2-1.5-3Z"/>',switch:'<path d="M7 2v5M17 2v5"/><path d="M5 7h14v7a7 7 0 0 1-14 0Z"/><path d="M9 21h6"/>',media_player:'<rect x="3" y="5" width="18" height="13" rx="2"/><path d="m10 9 5 2.5-5 2.5Z"/><path d="M8 22h8"/>',climate:'<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0Z"/><path d="M12 11v6"/>',cover:'<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M4 8h16M4 13h16M4 18h16"/>',fan:'<circle cx="12" cy="12" r="2"/><path d="M12 10c-1-4 1-7 4-7 2 3 1 6-2 8M14 12c4-1 7 1 7 4-3 2-6 1-8-2M12 14c1 4-1 7-4 7-2-3-1-6 2-8M10 12c-4 1-7-1-7-4 3-2 6-1 8 2"/>',sensor:'<path d="M4 19V5M4 19h16"/><path d="m7 15 3-4 3 2 5-7"/>',binary_sensor:'<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/>',default:'<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M9 9h6v6H9z"/>'};
function icon(kind){return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[kind]||paths.default}</svg>`}
function roomKind(name){const n=name.toLowerCase();if(n.includes('wohn'))return 'media_player';if(n.includes('küche')||n.includes('kueche'))return 'switch';if(n.includes('schlaf'))return 'light';if(n.includes('bad')||n.includes('wc'))return 'climate';if(n.includes('garten'))return 'sensor';if(n.includes('garage'))return 'cover';return 'room'}
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const auth=()=>{try{const t=JSON.parse(localStorage.getItem('hassTokens')||'{}');return t.access_token?{'Authorization':'Bearer '+t.access_token}:{} }catch(e){return {}}};
async function api(url,options={}){options.headers={...(options.headers||{}),...auth()};const r=await fetch(url,options);if(r.status===401||r.status===403)throw new Error('AUTH');if(!r.ok)throw new Error('HTTP '+r.status);return r}
function showToast(type,title,text){toast.className='toast '+type;toast.innerHTML=`<strong>${esc(title)}</strong><span>${esc(text)}</span>`;clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>toast.classList.add('hidden'),6000)}
function deviceState(d){return selected.devices.get(d.id)||{mode:'none',entities:new Set()}}
function setDeviceState(id,state){if(state.mode==='none'||(state.mode==='entities'&&!state.entities.size))selected.devices.delete(id);else selected.devices.set(id,state)}
api('/api/couchmate/configurator/data').then(r=>r.json()).then(j=>{data=j;for(const a of data.areas){if(a.temperature_entity)selected.temperatures[a.id]=a.temperature_entity;if(a.humidity_entity)selected.humidities[a.id]=a.humidity_entity;for(const d of a.devices){const ids=new Set(d.entities.filter(e=>e.selected).map(e=>e.entity_id));if(d.selection_mode==='all')selected.devices.set(d.id,{mode:'all',entities:new Set()});else if(ids.size)selected.devices.set(d.id,{mode:'entities',entities:ids})}}renderAreas()}).catch(e=>showToast('error','Konfigurator konnte nicht geladen werden',e.message==='AUTH'?'Öffne die Seite im selben Browser, in dem du bei Home Assistant angemeldet bist.':e.message));
function tile(title,sub,kind,state,fn){const b=document.createElement('button');const cls=state==='all'?' active':state==='partial'?' partial':'';b.className='tile'+cls;const status=state==='all'?'Alle Funktionen':state==='partial'?sub.selection:'';b.innerHTML=`<div class="icon">${icon(kind)}</div><h3>${esc(title)}</h3><div class="sub">${esc(sub.text)}</div>${status?`<span class="state">${esc(status)}</span>`:''}`;b.onclick=fn;return b}
function areaSelectionState(a){let count=0,all=0;for(const d of a.devices){const st=deviceState(d);if(st.mode==='all')all++;else if(st.mode==='entities')count+=st.entities.size}if(all)return 'partial';if(count||selected.temperatures[a.id]||selected.humidities[a.id])return 'partial';return 'none'}
function deviceSelectionState(d){const st=deviceState(d);return st.mode==='all'?'all':st.mode==='entities'&&st.entities.size?'partial':'none'}
function renderAreas(){areas.classList.remove('hidden');devices.classList.add('hidden');entities.classList.add('hidden');areaGrid.innerHTML='';for(const a of data.areas){const selectedCount=a.devices.reduce((n,d)=>{const st=deviceState(d);return n+(st.mode==='all'?d.entities.length:st.mode==='entities'?st.entities.size:0)},0);areaGrid.append(tile(a.name,{text:`${a.devices.length} Geräte`,selection:selectedCount?`${selectedCount} Funktionen gewählt`:''},roomKind(a.name),areaSelectionState(a),()=>{area=a;renderDevices()}))}}
function renderDevices(){areas.classList.add('hidden');devices.classList.remove('hidden');entities.classList.add('hidden');areaName.textContent=area.name;deviceGrid.innerHTML='';temperatureGrid.innerHTML='';humidityGrid.innerHTML='';if(!area.temperature_candidates.length){temperaturePanel.classList.add('hidden')}else{temperaturePanel.classList.remove('hidden');const none=document.createElement('label');none.className='temperature';none.innerHTML=`<input type="radio" name="roomTemp"><span><b>Keine Raumtemperatur</b><span class="sub">Für diesen Raum nicht anzeigen</span></span>`;none.querySelector('input').checked=!selected.temperatures[area.id];none.querySelector('input').onchange=()=>delete selected.temperatures[area.id];temperatureGrid.append(none);for(const t of area.temperature_candidates){const row=document.createElement('label');row.className='temperature';row.innerHTML=`<input type="radio" name="roomTemp"><span><b>${esc(t.name)}</b><span class="sub">${esc(t.entity_id)}</span></span>`;const input=row.querySelector('input');input.checked=selected.temperatures[area.id]===t.entity_id;input.onchange=()=>selected.temperatures[area.id]=t.entity_id;temperatureGrid.append(row)}}if(!area.humidity_candidates.length){humidityPanel.classList.add('hidden')}else{humidityPanel.classList.remove('hidden');const none=document.createElement('label');none.className='humidity';none.innerHTML=`<input type="radio" name="roomHumidity"><span><b>Keine Luftfeuchtigkeit</b><span class="sub">Für diesen Raum nicht anzeigen</span></span>`;none.querySelector('input').checked=!selected.humidities[area.id];none.querySelector('input').onchange=()=>delete selected.humidities[area.id];humidityGrid.append(none);for(const h of area.humidity_candidates){const row=document.createElement('label');row.className='humidity';row.innerHTML=`<input type="radio" name="roomHumidity"><span><b>${esc(h.name)}</b><span class="sub">${esc(h.entity_id)}</span></span>`;const input=row.querySelector('input');input.checked=selected.humidities[area.id]===h.entity_id;input.onchange=()=>selected.humidities[area.id]=h.entity_id;humidityGrid.append(row)}}for(const d of area.devices){const domain=d.entities[0]?.domain||'default';const st=deviceState(d);const subtitle=[d.manufacturer,d.model].filter(Boolean).join(' · ')||`${d.entities.length} Funktionen`;deviceGrid.append(tile(d.name,{text:subtitle,selection:st.mode==='entities'?`${st.entities.size} ${st.entities.size===1?'Funktion':'Funktionen'} gewählt`:''},domain,deviceSelectionState(d),()=>{device=d;renderEntities()}))}}
function renderEntities(){devices.classList.add('hidden');entities.classList.remove('hidden');deviceName.textContent=`${area.name} · ${device.name}`;entityGrid.innerHTML='';let st=deviceState(device);selectWholeDevice.checked=st.mode==='all';selectWholeDevice.onchange=()=>{if(selectWholeDevice.checked)setDeviceState(device.id,{mode:'all',entities:new Set()});else setDeviceState(device.id,{mode:'none',entities:new Set()});renderEntities()};for(const e of device.entities){st=deviceState(device);const row=document.createElement('label');row.className='entity';row.innerHTML=`<input type="checkbox" ${(st.mode==='all'||(st.mode==='entities'&&st.entities.has(e.entity_id)))?'checked':''} ${st.mode==='all'?'disabled':''}><span><b>${esc(e.name)}</b><span class="sub">${esc(e.entity_id)}</span></span>`;const c=row.querySelector('input');c.onchange=()=>{const current=deviceState(device);const ids=current.mode==='entities'?new Set(current.entities):new Set();if(c.checked)ids.add(e.entity_id);else ids.delete(e.entity_id);setDeviceState(device.id,{mode:'entities',entities:ids})};entityGrid.append(row)}}
function buildSelectionModel(){const model={version:2,areas:{}};for(const a of data.areas){const cfg={devices:{}};if(selected.temperatures[a.id])cfg.temperature=selected.temperatures[a.id];if(selected.humidities[a.id])cfg.humidity=selected.humidities[a.id];for(const d of a.devices){const st=deviceState(d);if(st.mode==='all')cfg.devices[d.id]={mode:'all',entities:[]};else if(st.mode==='entities'&&st.entities.size)cfg.devices[d.id]={mode:'entities',entities:[...st.entities]}}if(cfg.temperature||cfg.humidity||Object.keys(cfg.devices).length)model.areas[a.id]=cfg}return model}
backAreas.onclick=renderAreas;backDevices.onclick=renderDevices;save.onclick=async()=>{save.disabled=true;save.textContent='Speichert …';showToast('', 'Auswahl wird gespeichert','Bitte einen Moment warten.');try{const r=await api('/api/couchmate/configurator/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selection_model:buildSelectionModel()})});const j=await r.json();if(!j.success)throw new Error('Unbekannter Speicherfehler');showToast('ok','Auswahl erfolgreich gespeichert',`${j.area_count} Bereiche · ${j.device_count} ganze Geräte · ${j.entity_count} einzelne Funktionen · ${j.temperature_count} Raumtemperaturen · ${j.humidity_count} Luftfeuchtigkeiten`)}catch(e){showToast('error','Speichern fehlgeschlagen',e.message==='AUTH'?'Die Home-Assistant-Anmeldung ist abgelaufen. Bitte Home Assistant neu laden.':e.message)}finally{save.disabled=false;save.textContent='Auswahl speichern'}};
</script></body></html>'''
