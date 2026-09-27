# Changelog

## 27.09.2026 – Core 1.4.0-beta.15 · Energie-Einspeisung

- Die Energie-API übernimmt Netz-Einspeisung aus Home Assistants `stat_energy_to` beziehungsweise älteren `flow_to`-Einträgen auch dann, wenn eine weitere konfigurierte Einspeisequelle heute keine Recorder-Daten liefert. Vorhandene Werte werden summiert; `energy_dashboard.coverage` und `hourly[].coverage` kennzeichnen unvollständige Quellgruppen mit `configured_sources` und `reporting_sources`.
- Für die laufende Stunde bleiben stündliche Energiewerte verfügbar, wenn eine Quelle keine Fünf-Minuten-Statistiken liefert. Liegen beide Auflösungen vor, wird die Stunde nicht doppelt gezählt. Ein gemessener Wert von `0 kWh` bleibt erhalten.
- Der berechnete Hausverbrauch wird bei unvollständigen Quellgruppen weiterhin nicht als vollständiger Wert ausgegeben. Der Core-Endpoint enthält keine Stromzähler-Rohstände und benötigt weiterhin eine konfigurierte Home-Assistant-Energieansicht.
- Geprüft mit 17 Energie-Regressionstests, 16 Flow-, 9 Konfigurator- und 25 Sicherheits-Tests sowie der Release-Validierung.

## 27.09.2026 – Core-Konfigurator

Änderungen gegenüber `Optional-Dashboards-2026-09-26`. Diese Konfigurator-Revision trug die Version `1.4.0-beta.14`; das Paket enthält inzwischen den oben beschriebenen Energiefix als `1.4.0-beta.15`.

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
