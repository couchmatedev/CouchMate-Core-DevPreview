# Changelog

## 27.09.2026 – Core-Konfigurator

Änderungen gegenüber `Optional-Dashboards-2026-09-26`. Die Integrationsversion bleibt `1.4.0-beta.14`; geändert wurde `custom_components/couchmate/configurator.py`.

### Neu

- **Geräte & Funktionen** hat fünf Reiter: **Räume**, **Klima**, **Sicherheit & Kameras**, **Geräte** und **Energie**. Die bisherige gemeinsame Auswahl und die Schaltfläche **Auswahl speichern** gelten weiter für alle Reiter.
- Räume lassen sich suchen. Im Klima- und Geräte-Reiter kann der Raum direkt gewechselt werden.
- Geräte lassen sich nach Name, Typ, Hersteller, Modell und Entität suchen sowie wahlweise nach **Name** oder **Typ, dann Name** sortieren. Die Sortierwahl bleibt im Browser gespeichert.
- Raumkacheln zeigen ein zum Home-Assistant-Bereichs-Icon passendes Symbol, sofern es erkannt wird, etwa Herd für `mdi:stove` und Sofa für `mdi:sofa`. Sonst entscheidet der Raumname; unbekannte Räume erhalten ein Haussymbol.
- Gerätekacheln erhalten Symbole nach Bezeichnung und Entitätstyp, unter anderem für Rollläden, Thermostate, Kameras, Alarmanlagen, Türkontakte, Waschmaschinen und Staubsauger. Szenen und Skripte haben eigene Symbole.

### Umgeordnet

- Thermostat, Raumtemperatur und Luftfeuchtigkeit liegen unter **Klima**; globale Wetter- und Thermostatvorgaben sind dort aufklappbar.
- Alarmanlagen, Kameras/Bilder und Tür-/Fensterkontakte liegen unter **Sicherheit & Kameras** in getrennten aufklappbaren Gruppen.
- Timer/WashData und Hero/Flow/Karten sind im Geräte-Reiter aufklappbar. Die Geräteliste steht vor diesen Einstellungen.
- Das optionale Energie-Dashboard lässt sich im eigenen Reiter **Energie** aktivieren; der bisherige aufklappbare Bereich unter **Geräte** entfällt.
- Auf schmalen Bildschirmen stehen die Reiter in zwei Spalten; Auswahlfelder nutzen eine Spalte statt seitlich überzulaufen.

### Korrigiert

- Die Sortierung nach Typ verwendet denselben erkannten Gerätetyp wie das Geräte-Icon. Geräte mit mehreren Entitätsarten werden dadurch nicht mehr nach der zufällig ersten Entität eingeordnet.
- `mdi:silverware-fork-knife` wird als Esszimmer-Symbol statt als Küchensymbol erkannt.
