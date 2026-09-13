# CouchMate Core Dev Preview

> Separater Beta-Kanal für neue CouchMate-Funktionen. Die Dev Preview verwendet bereits den endgültigen Namespace `couchmate` und wird anstelle des stabilen Core installiert, nicht parallel zum stabilen Core.

Dieses Repository enthält genau eine Home-Assistant-Integration:

- Ordner: `custom_components/couchmate`
- Domain: `couchmate`
- Dienste: `couchmate.*`
- API: `/api/couchmate/*`
- Lokale Daten: `couchmate*`

Der sichtbare Name bleibt **CouchMate Core Dev Preview**. Beta-Versionen können sich kurzfristig ändern und Fehler enthalten; für den regulären Betrieb bleibt der stabile Core empfohlen.

## Installation

1. Erstelle ein Home-Assistant-Backup.
2. Füge `https://github.com/couchmatedev/CouchMate-Core-DevPreview` in HACS als benutzerdefiniertes Repository der Kategorie **Integration** hinzu.
3. Installiere **CouchMate Core Dev Preview**.
4. Starte Home Assistant vollständig neu.
5. Füge unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** die Integration **CouchMate Core Dev Preview** hinzu.

Die Dev Preview verwendet dieselbe endgültige Domain, dieselben API-Pfade und dieselben Speicherbezeichner wie das spätere Release. Dadurch müssen Apps, Kopplungen und Einstellungen beim Übergang vom Preview- zum Release-Repository nicht migriert werden.

## Verwaltung

Die Home-Assistant-Sidebar enthält die Bereiche **Geräte & Funktionen** sowie **Apple TVs & Design**. Dort können Räume, Geräte, Sensorquellen, Hero-Karten, Profile, gekoppelte Geräte und Hintergründe verwaltet werden.

### Dashboard-Kacheln synchronisieren · 1.4.0-beta.12

Apple TVs können die Reihenfolge von Räumen und Widgets je Raum im zugewiesenen Core-Profil speichern. Apple TVs mit demselben Profil lesen dieselbe Anordnung; separate Profile bleiben unabhängig. Die Reihenfolge bleibt nach einem Core-Neustart erhalten. Neue Kacheln ohne gespeicherten Eintrag werden von den Apps an die bestehende Reihenfolge angehängt.

Die Kopplung muss das Recht **Dashboard-Kacheln anordnen** (`dashboard:write`) anfordern und ein Home-Assistant-Administrator muss es bestätigen. Bereits gekoppelte Apple TVs ohne dieses Recht müssen erneut gekoppelt werden, um Änderungen zu speichern. Vorhandenes `configuration:write` berechtigt ebenfalls zum Anordnen. Das schmale `dashboard:write` erlaubt weder allgemeine Profileinstellungen noch Profilzuweisungen oder Hintergrundänderungen.

Der [API-Vertrag](docs/dashboard-layout-api.md) beschreibt Abruf, Konfliktbehandlung und gezielte Änderungen. Lokale Prüfung: `python3 -B scripts/test_dashboard_layout.py` und `python3 -B scripts/validate_release.py`.

---

> Separate beta channel for new CouchMate features. The Dev Preview already uses the final `couchmate` namespace and is installed instead of the stable Core, not alongside the stable Core.

This repository contains exactly one Home Assistant integration:

- Folder: `custom_components/couchmate`
- Domain: `couchmate`
- Services: `couchmate.*`
- API: `/api/couchmate/*`
- Local data: `couchmate*`

The visible name remains **CouchMate Core Dev Preview**. Beta releases may change at short notice and can contain bugs; the stable Core remains recommended for regular use.

## Installation

1. Create a Home Assistant backup.
2. Add `https://github.com/couchmatedev/CouchMate-Core-DevPreview` to HACS as a custom **Integration** repository.
3. Install **CouchMate Core Dev Preview**.
4. Perform a full Home Assistant restart.
5. Add **CouchMate Core Dev Preview** under **Settings → Devices & services → Add integration**.

The Dev Preview uses the same final domain, API routes, and storage identifiers as the later release. Apps, pairings, and settings therefore require no migration when moving from the preview repository to the release repository.

## Management

The Home Assistant sidebar provides **Devices & Functions** and **Apple TVs & Design** for rooms, devices, sensor sources, Hero cards, profiles, paired devices, and backgrounds.
