# CouchMate Core Dev Preview

> Dauerhafter Beta-Kanal für Tester neuer CouchMate-Funktionen. **Die Dev Preview ersetzt den stabilen Core und darf nicht parallel dazu installiert werden.**

> **Kein direktes Update von `1.3.0-beta.3` oder älter:** Entferne die frühere parallele Dev Preview auf allen Testsystemen, solange das Repository dort noch als `couchmate_dev` erkannt wird. Veröffentliche oder installiere diesen neuen Stand erst danach. Andernfalls kann HACS wegen des geänderten Zielordners den stabilen Core überschreiben oder beim Entfernen dessen Dateien löschen.

Dieses Repository stellt neue Core-Funktionen vor ihrer Übernahme in den stabilen `CouchMate Core` bereit. Beta-Versionen können sich kurzfristig ändern und Fehler enthalten; für den regulären Betrieb bleibt der stabile Core die empfohlene Version.

Der sichtbare Name bleibt **CouchMate Core Dev Preview**. Technisch verwendet die Dev Preview jedoch dieselbe Home-Assistant-Domain `couchmate`, dieselben Dienste `couchmate.*`, dieselbe API unter `/api/couchmate` und dieselben lokalen Speicher wie der stabile Core. Dadurch bleibt die veröffentlichte CouchMate-App ohne Änderung kompatibel und bestehende Kopplungen können erhalten bleiben.

Ein bereits vorhandener Eintrag unter **Geräte & Dienste** kann weiterhin den gespeicherten Titel „CouchMate Core“ anzeigen. Das ist beabsichtigt: Der Eintrag wird beim Kanalwechsel nicht neu angelegt. Maßgeblich für den aktiven Kanal sind die HACS-Version und die Sidebar.

## Wichtige Wechselregel

- Installiere immer nur **einen** Kanal: Stable oder Dev Preview.
- Lösche beim Kanalwechsel **nicht** den Eintrag unter **Einstellungen → Geräte & Dienste**.
- Rufe beim Kanalwechsel **nicht** den Dienst `couchmate.uninstall` auf. Beides würde Auswahl, Profile, Kopplungen und Hintergründe löschen.
- Wechsle ausschließlich die von HACS bereitgestellten Dateien und starte Home Assistant anschließend vollständig neu.
- Erstelle vor dem ersten Wechsel ein vollständiges Home-Assistant-Backup.

## Einmaliger Wechsel von der früheren parallelen Dev Preview

Versionen bis einschließlich `1.3.0-beta.3` verwendeten noch die separate Domain `couchmate_dev`. Entferne diesen alten Dev-Preview-Eintrag und dessen HACS-Download vor der Installation dieses Standes. Der reguläre `couchmate`-Eintrag bleibt bestehen.

Alte Testdaten unter `couchmate_dev` werden bewusst nicht automatisch mit den produktiven `couchmate`-Daten zusammengeführt. Sichere sie vorher, falls du sie behalten möchtest. So kann die Dev Preview niemals unbemerkt bestehende Kopplungen oder Einstellungen des stabilen Core überschreiben.

## Von Stable zur Dev Preview wechseln

1. Erstelle ein vollständiges Backup.
2. Behalte den vorhandenen **CouchMate Core**-Eintrag unter **Geräte & Dienste**.
3. Entferne in HACS nur den Download des stabilen Core; entferne nicht die Home-Assistant-Integration.
4. Füge `https://github.com/RAFd3v-HA/CouchMate-Core-DevPreview` als benutzerdefiniertes Repository der Kategorie **Integration** hinzu.
5. Installiere **CouchMate Core Dev Preview**. Achte darauf, dass in HACS niemals Stable und Dev Preview gleichzeitig als installiert geführt werden.
6. Starte Home Assistant vollständig neu. Ein einfaches Neuladen der Integration reicht nicht.

Bei einer frischen Installation ohne vorhandenen Core fügst du anschließend unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** die Integration **CouchMate Core Dev Preview** hinzu.

## Zur stabilen Version zurückkehren

Entferne in HACS nur die Dev-Preview-Dateien, installiere anschließend wieder den stabilen `CouchMate Core` und starte Home Assistant vollständig neu. Der bestehende `couchmate`-Eintrag und seine Daten bleiben dabei erhalten.

Auswahl und bestehende App-Kopplungen sind kanalübergreifend kompatibel. Profile und die neue Hintergrund-Vererbung gehören dagegen zum kommenden Core-Schema. Solange die stabile Version dieses Schema noch nicht enthält, solltest du diese Design-Daten während eines vorübergehenden Stable-Betriebs nicht ändern. Die Dev Preview bewahrt ihre Daten passiv auf und verwendet sie beim nächsten Wechsel zurück.

## Kopplungsanfragen

Offene Kopplungsanfragen werden unter **Apple TVs & Design** direkt in der Home-Assistant-Sidebar angezeigt. Administratoren können dort Gerätename, Kopplungscode, verbleibende Zeit und angeforderte Zusatzrechte prüfen sowie die Anfrage zulassen oder ablehnen.

## Hintergrund-Vererbung

In **Apple TVs & Design** kann ein gemeinsamer Wohnungs-Hintergrund gewählt werden. Räume ohne eigenes Bild übernehmen ihn automatisch. Ein eigenes Raumbild hat Vorrang; wird diese Raum-Ausnahme entfernt, verwendet der Raum wieder den Wohnungs-Hintergrund. Das Entfernen des Wohnungs-Hintergrunds verändert vorhandene Raum-Ausnahmen nicht.

---

> Permanent beta channel for testing new CouchMate features. **The Dev Preview replaces the stable Core and must not be installed alongside it.**

> **Do not update directly from `1.3.0-beta.3` or older:** Remove the former parallel Dev Preview from every test system while HACS still recognizes that repository as `couchmate_dev`. Publish or install this new release only afterward. Otherwise, the changed target folder can cause HACS to overwrite the stable Core or delete its files during removal.

This repository provides new Core features before they are considered for the stable `CouchMate Core`. Beta versions may change at short notice and can contain bugs; the stable Core remains recommended for regular use.

The visible name remains **CouchMate Core Dev Preview**. Technically, however, it uses the same Home Assistant domain `couchmate`, the same `couchmate.*` services, the same `/api/couchmate` API, and the same local storage as the stable Core. The released CouchMate app therefore remains compatible without modification, and existing pairings can be retained.

An existing entry under **Devices & services** may continue to show its stored title, “CouchMate Core.” This is intentional because the config entry is retained rather than recreated. Use the HACS version and sidebar to identify the active channel.

## Important switching rule

- Install only **one** channel at a time: Stable or Dev Preview.
- Do **not** delete the entry under **Settings → Devices & services** when switching channels.
- Do **not** call `couchmate.uninstall` when switching. Either action would delete selections, profiles, pairings, and backgrounds.
- Replace only the files managed by HACS, then perform a full Home Assistant restart.
- Create a complete Home Assistant backup before the first switch.

## One-time migration from the former parallel Dev Preview

Versions up to and including `1.3.0-beta.3` used the separate `couchmate_dev` domain. Remove that old Dev Preview config entry and its HACS download before installing this release. Keep the regular `couchmate` config entry.

Old test data stored under `couchmate_dev` is intentionally not merged automatically into production `couchmate` data. Back it up first if required. This prevents the Dev Preview from silently overwriting stable pairings or settings.

## Switch from Stable to Dev Preview

1. Create a complete backup.
2. Keep the existing **CouchMate Core** entry under **Devices & services**.
3. In HACS, remove only the stable Core download; do not remove the Home Assistant integration.
4. Add `https://github.com/RAFd3v-HA/CouchMate-Core-DevPreview` as a custom **Integration** repository.
5. Install **CouchMate Core Dev Preview**. Never leave Stable and Dev Preview marked as installed in HACS at the same time.
6. Perform a full Home Assistant restart. Reloading the integration is not sufficient.

For a fresh installation without an existing Core, add **CouchMate Core Dev Preview** afterward under **Settings → Devices & services → Add integration**.

## Return to Stable

Remove only the Dev Preview files in HACS, install the stable `CouchMate Core` files again, and perform a full Home Assistant restart. The existing `couchmate` config entry and its data remain in place.

Selections and existing app pairings are compatible across channels. Profiles and the new background inheritance belong to the upcoming Core schema. Until the stable release supports that schema, do not modify those design settings during a temporary Stable run. The Dev Preview keeps its data passively and uses it again when you switch back.

## Pairing requests

Pending pairing requests appear directly under **Apple TVs & Design** in the Home Assistant sidebar. Administrators can review the device name, pairing code, remaining time, and requested additional permissions, then approve or reject the request.

## Background inheritance

**Apple TVs & Design** can define one shared home background. Rooms without their own image inherit it automatically. A room-specific image takes precedence; removing that room override makes the room inherit the home background again. Removing the home background does not alter existing room overrides.
