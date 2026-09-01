---
name: brandkit-setup
description: Prüft vor einem BrandKit-Lauf die angeforderten Ausgaben, vorhandenen Eingaben und lokalen Fähigkeiten. Nutze den Skill für den Start oder einen erneuten Readiness-Check; er installiert nichts und trifft keine Markenentscheidung.
license: MIT
---

# BrandKit-Setup

Owner: Readiness und Scope. Markenentscheidungen gehören `brandkit-interview`,
Kit-Erzeugung `brandkit-build`, Artefaktproduktion `brandkit-publish`.

## Eingabe und Ausgabe

- Eingabe: gewünschte Ausgabetypen, Zielprojekt, vorhandene Brand-Quellen und
  ausdrücklich gewünschte optionale Funktionen.
- Ausgabe: `.brandkit/work/<run-id>/setup-readiness.md` mit Scope, geprüfter
  Fähigkeit, Evidenz, Fallback und Status je Anforderung.

## Workflow

1. Scope festhalten. Prüfe nur Fähigkeiten, die eine gewünschte Ausgabe
   tatsächlich benötigt. Fertig, wenn jede Ausgabe und optionale Funktion
   eindeutig `required`, `optional` oder `out-of-scope` ist.
2. Fähigkeiten read-only prüfen: Dateizugriff; bei Bedarf Git, Python/Pillow,
   Renderer, Metadatenwerkzeug oder Bildgenerierung. Nenne nur beobachtete
   Befehle/Versionen oder `nicht geprüft`. Fertig, wenn jede `required`-
   Fähigkeit Evidenz oder einen ausführbaren Fallback besitzt.
3. Readiness-Artefakt schreiben. Externe Einrichtung, Kauf, Login und
   Installation bleiben eigene, ausdrücklich beauftragte Vorgänge. Fertig,
   wenn der nächste Schritt und alle Blocker sichtbar sind.

## Gate

`brandkit-interview` darf starten, wenn Schreiben möglich ist und der Scope
feststeht. Fehlende optionale Fähigkeiten reduzieren nur den Scope. Stoppe bei
fehlendem Schreibziel, unklarem Projektroot oder einer fehlenden Fähigkeit ohne
akzeptierten Fallback. Erfinde weder Toolstatus noch Versionen.
