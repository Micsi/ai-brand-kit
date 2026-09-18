---
name: brandkit-setup
description: Prüft vor einem BrandKit-Lauf die angeforderten Ausgaben, vorhandenen Eingaben und lokalen Fähigkeiten. Nutze den Skill für den Start oder einen erneuten Readiness-Check; er installiert nichts und trifft keine Markenentscheidung.
license: MIT
---

# BrandKit-Setup

## Routing

| Route | Primäres Playbook |
|---|---|
| Readiness prüfen | [`references/workflow.md`](references/workflow.md) |

Wenn der Owner die vertiefte Discovery ausdrücklich wählt, lies zusätzlich
[`../brandkit-interview/references/grill-with-docs-handoff.md`](../brandkit-interview/references/grill-with-docs-handoff.md).

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
3. Optionale Discovery prüfen. Nur nach ausdrücklicher Wahl inventarisiere
   `grill-with-docs`, `grilling` und `domain-modeling` als ein Bündel gegen
   dieselbe freigegebene Quelle und denselben Pin. Fordere den realen Pfad des
   bereits freigegebenen, clean/detached Checkouts an und führe den im
   Handoff-Playbook genannten `check-bundle`-Befehl aus. Bei Erfolg verweise auf
   die projektlokale Bridge und den expliziten Owner-Aufruf
   `$brandkit-interview`; erst dessen `prepare`-Trace legt das Run-CWD fest.
   Ist das Bündel nicht vorhanden,
   biete entweder den nativen Interviewpfad oder einen separaten ausdrücklichen
   Installations-/Aktivierungsauftrag an; installiere nichts und rufe keinen
   der Skills auf. Kurz: installiere nichts und rufe keinen der drei Skills auf.
4. Readiness-Artefakt schreiben. Externe Einrichtung, Kauf, Login und
   Installation bleiben eigene, ausdrücklich beauftragte Vorgänge. Fertig,
   wenn der nächste Schritt und alle Blocker sichtbar sind.
   Für visuelle Kandidaten dokumentiere Affinity ausschließlich als
   beobachtete read-only Capability oder als `unavailable`; nie als
   impliziten Installationsgrund. Der Impeccable-Prebuild-Resolver darf nur aus
   dem aktuellen Run-Workspace stammen und `extract` bleibt vor dem Paket
   verboten.

## Gate

`brandkit-interview` darf starten, wenn Schreiben möglich ist und der Scope
feststeht. Fehlende optionale Fähigkeiten reduzieren nur den Scope. Stoppe bei
fehlendem Schreibziel, unklarem Projektroot oder einer fehlenden Fähigkeit ohne
akzeptierten Fallback. Erfinde weder Toolstatus noch Versionen.
