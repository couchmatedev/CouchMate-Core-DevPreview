# CouchMate Core Dev Preview

> Separater Beta-Kanal für neue CouchMate-Funktionen. Die Dev Preview verwendet ausschließlich `custom_components/couchmate_dev` und kann technisch parallel zum stabilen Core installiert werden.

Dieses Repository enthält genau eine Home-Assistant-Integration:

- Ordner: `custom_components/couchmate_dev`
- Domain: `couchmate_dev`
- Dienste: `couchmate_dev.*`
- API: `/api/couchmate_dev/*`
- Lokale Daten: `couchmate_dev*`

Der sichtbare Name bleibt **CouchMate Core Dev Preview**. Beta-Versionen können sich kurzfristig ändern und Fehler enthalten; für den regulären Betrieb bleibt der stabile Core empfohlen.

## Installation

1. Erstelle ein Home-Assistant-Backup.
2. Füge `https://github.com/couchmatedev/CouchMate-Core-DevPreview` in HACS als benutzerdefiniertes Repository der Kategorie **Integration** hinzu.
3. Installiere **CouchMate Core Dev Preview**.
4. Starte Home Assistant vollständig neu.
5. Füge unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** die Integration **CouchMate Core Dev Preview** hinzu.

Die Dev Preview besitzt eine eigene Domain und eigene Speicher. Kopplungen und Einstellungen des stabilen Core werden daher weder überschrieben noch automatisch übernommen.

## Verwaltung

Die Home-Assistant-Sidebar enthält die Bereiche **Geräte & Funktionen** sowie **Apple TVs & Design**. Dort können Räume, Geräte, Sensorquellen, Hero-Karten, Profile, gekoppelte Geräte und Hintergründe verwaltet werden.

---

> Separate beta channel for new CouchMate features. The Dev Preview uses only `custom_components/couchmate_dev` and can technically be installed alongside the stable Core.

This repository contains exactly one Home Assistant integration:

- Folder: `custom_components/couchmate_dev`
- Domain: `couchmate_dev`
- Services: `couchmate_dev.*`
- API: `/api/couchmate_dev/*`
- Local data: `couchmate_dev*`

The visible name remains **CouchMate Core Dev Preview**. Beta releases may change at short notice and can contain bugs; the stable Core remains recommended for regular use.

## Installation

1. Create a Home Assistant backup.
2. Add `https://github.com/couchmatedev/CouchMate-Core-DevPreview` to HACS as a custom **Integration** repository.
3. Install **CouchMate Core Dev Preview**.
4. Perform a full Home Assistant restart.
5. Add **CouchMate Core Dev Preview** under **Settings → Devices & services → Add integration**.

The Dev Preview uses its own domain and storage. Stable Core pairings and settings are neither overwritten nor imported automatically.

## Management

The Home Assistant sidebar provides **Devices & Functions** and **Apple TVs & Design** for rooms, devices, sensor sources, Hero cards, profiles, paired devices, and backgrounds.
