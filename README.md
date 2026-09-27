# CouchMate Core – übersichtlicher Konfigurator (27.09.2026)

Dieses Paket enthält die aktualisierte Home-Assistant-Integration unter
`custom_components/couchmate`. Es ist ein **Core-Update** und enthält keine
neuen iOS- oder tvOS-App-Builds. Der Core wurde nicht in Home Assistant
installiert.

## Neuer Konfigurator

**Geräte & Funktionen** ist in **Räume**, **Klima**, **Sicherheit & Kameras**
und **Geräte** aufgeteilt. Klima und Geräte haben eine direkte Raumauswahl.
Räume lassen sich suchen; Geräte lassen sich suchen und wahlweise nach Name
oder Typ sortieren. Timer, WashData, Hero/Flow und das Energie-Dashboard sind
aufklappbar. Die Auswahl und die zentrale Schaltfläche **Auswahl speichern**
gelten weiterhin für alle Reiter gemeinsam.

Raumkacheln leiten ein passendes Icon aus dem in Home Assistant gewählten
Bereichs-Icon ab, wenn dessen Bedeutung erkannt wird. Sonst wird der Raumname
verwendet. Geräte nutzen Icons passend zu Bezeichnung und Typ; unbekannte
Geräte erhalten ein neutrales Symbol. Die Icons im Web-Konfigurator sind
lokale SVGs und keine SF-Symbol-Dateien.

## Stand und Prüfung

Die Integrationsversion ist `1.4.0-beta.14`. Die lokalen Core-Tests für
Speichern, Flow, Sicherheit und Energie, die Prüfung des eingebetteten
JavaScript und die Release-Validierung sind erfolgreich. Die Bedienoberfläche
wurde noch nicht mit einer echten Home-Assistant-Instanz visuell geprüft.

Der Quellstand liegt in
`/Users/fabianvocke/Documents/CouchMate-Core-DevPreview`.
Das ältere Paket `Optional-Dashboards-2026-09-26` bleibt unverändert.
