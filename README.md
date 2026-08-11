# CouchMate Core Dev Preview

> Dauerhaftes, parallel installierbares Beta-Repository für Tester neuer CouchMate-Funktionen.

Dieses Repository bleibt als eigenständiger Beta-Kanal bestehen. Neue Funktionen werden hier vor ihrer Übernahme in den stabilen `CouchMate Core` bereitgestellt und gemeinsam getestet. Beta-Versionen können sich kurzfristig ändern und Fehler enthalten; für den regulären Betrieb bleibt der stabile Core die empfohlene Version.

`CouchMate Core Dev Preview` läuft mit der eigenen Home-Assistant-Domain `couchmate_dev` neben dem regulären `CouchMate Core`. Auswahl, Profile, Kopplungen, Dienste, API-Routen und Raumbilder werden getrennt gespeichert. Das Entfernen der Dev Preview löscht keine Daten des regulären Core.

## Installation über HACS

1. Öffne **HACS → Integrationen**.
2. Öffne das Menü oben rechts und wähle **Benutzerdefinierte Repositories**.
3. Füge `https://github.com/RAFd3v-HA/CouchMate-Core-DevPreview` als Kategorie **Integration** hinzu.
4. Installiere **CouchMate Core Dev Preview** und starte Home Assistant neu.
5. Öffne **Einstellungen → Geräte & Dienste → Integration hinzufügen** und wähle **CouchMate Core Dev Preview**.

Die Dev Preview erscheint mit einer eigenen Sidebar unter **CouchMate Core Dev Preview**. Ihre Dienste beginnen mit `couchmate_dev.`; die Dev-Preview-API liegt unter `/api/couchmate_dev`.

## Kopplungsanfragen

Offene Kopplungsanfragen werden unter **Apple TVs & Design** direkt in der Home-Assistant-Sidebar angezeigt. Administratoren können dort den Gerätenamen, Kopplungscode, die verbleibende Zeit und angeforderte Zusatzrechte prüfen sowie die Anfrage zulassen oder ablehnen.

## Hintergrund-Vererbung

In **Apple TVs & Design** kann ein gemeinsamer Wohnungs-Hintergrund gewählt werden. Räume ohne eigenes Bild übernehmen ihn automatisch. Ein eigenes Raumbild hat Vorrang; wird diese Raum-Ausnahme entfernt, verwendet der Raum wieder den Wohnungs-Hintergrund. Das Entfernen des Wohnungs-Hintergrunds verändert vorhandene Raum-Ausnahmen nicht.

Die veröffentlichte CouchMate-App verwendet weiterhin ausschließlich den regulären Core. Ein CouchMate2-Entwicklungsbuild muss ausdrücklich den Dev-Preview-Namespace verwenden und wird separat gekoppelt.

---

> Permanent, parallel-installable beta repository for testers of new CouchMate features.

This repository remains available as a dedicated beta channel. New features are released and tested here before they are considered for the stable `CouchMate Core`. Beta versions may change at short notice and can contain bugs; the stable Core remains the recommended version for regular use.

`CouchMate Core Dev Preview` uses its own Home Assistant domain, `couchmate_dev`, and can run beside the regular `CouchMate Core`. Selections, profiles, pairings, services, API routes, and room backgrounds are stored independently. Removing the dev preview does not delete regular Core data.

## Install with HACS

1. Open **HACS → Integrations**.
2. Open the top-right menu and select **Custom repositories**.
3. Add `https://github.com/RAFd3v-HA/CouchMate-Core-DevPreview` with category **Integration**.
4. Install **CouchMate Core Dev Preview** and restart Home Assistant.
5. Open **Settings → Devices & services → Add integration** and select **CouchMate Core Dev Preview**.

The dev preview has its own **CouchMate Core Dev Preview** sidebar entry. Its services start with `couchmate_dev.`, and its API is available under `/api/couchmate_dev`.

## Pairing requests

Pending pairing requests appear directly under **Apple TVs & Design** in the Home Assistant sidebar. Administrators can review the device name, pairing code, remaining time, and requested additional permissions, then approve or reject the request.

## Background inheritance

**Apple TVs & Design** can define one shared home background. Rooms without their own image inherit it automatically. A room-specific image takes precedence; removing that room override makes the room inherit the home background again. Removing the home background does not alter existing room overrides.

The released CouchMate app continues to use only the regular Core. A CouchMate2 development build must explicitly use the dev-preview namespace and pair separately.
