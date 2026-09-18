---
name: brandkit-interview
description: Erhebt belegte Markenentscheidungen und Konflikte in einer Rohfassung. Nutze den Skill nach dem Readiness-Check, wenn Positionierung, Stimme, visuelle Rollen, Formate oder Ausschlüsse noch geklärt werden müssen.
license: MIT
---

# BrandKit-Interview

## Routing

| Route | Primäres Playbook |
|---|---|
| Markenquellen erheben | [`references/workflow.md`](references/workflow.md) |
| Explizite vertiefte Discovery übernehmen | [`references/grill-with-docs-handoff.md`](references/grill-with-docs-handoff.md) |

Owner: Aussagen der Markenquelle erfassen, nicht Designwerte ergänzen oder das finale Kit bauen.

## Eingabe und Ausgabe

- Eingabe: `setup-readiness.md`, bereitgestellte Primärquellen, Antworten des Marken-Owners und optional ein validierter Discovery-Handoff.
- Ausgabe: `.brandkit/work/<run-id>/brand-raw.md` mit Provenienz je konkreter Entscheidung, Konfliktregister und offenen Punkten.

## Workflow

1. Quelleninventar anlegen. Trenne `owner supplied`, `official supplied`, `observed` und `unknown`. Fertig, wenn jede verwendbare Quelle klassifiziert ist.
2. Wenn der Owner `$brandkit-interview` ausdrücklich mit vertiefter Discovery
   startet, führe zuerst `brandkit_discovery.py prepare` aus. Nur bei einem
   erfolgreichen Trace wechsle in dessen exaktes `cwd`. Bleibe über beliebig
   viele Turns in `grilling`, speichere nach jeder Runde offene, abgedeckte und
   ausdrücklich offen gelassene Achsen in `interview-progress.md` und fahre
   deterministisch an der offenen Frontier fort. Eine erste Antwort beendet
   Grilling nie automatisch: Der kanonische Fortschritt zählt Antworten, und
   vor zwei Antwort-Turns bleibt der Trace auch bei leer gemeldeter Frontier
   zwingend in `grilling`. Übergib intern den SHA-256 der exakten Antwortbytes
   und den sicheren Owner-Key an `progress`; dessen HMAC-Vorgängerkette und
   separates, externes Head-Ledger müssen vor jedem Folgeschritt bestehen.
   Der Key darf nie im Projekt liegen; initialisiere einen vorhandenen Head nie
   neu, sondern verwende für einen Neustart eine frische Run-ID. Erst wenn danach keine ungeklärte Frontier verbleibt,
   wechselt `prepare` zu `domain-modeling`. Rufe
   `grill-with-docs` nie auf; sein upstream `disable-model-invocation` bleibt
   respektiert. Schreibe Owner-Antworten und den relevanten Grilling-Dialog
   fortlaufend als echte UTF-8-Dateien unter `sources/` und klassifiziere jede
   davon im Handoff; eine bloße Quellen-ID ist unzulässig. Fehlt Bundle,
   Aktivierung, CONTEXT, ADR, Quellenbyte oder gültiger
   Handoff, führe stattdessen den vollständigen nativen Elf-Achsen-Pfad aus.
3. Erzeuge den Handoff ausschließlich intern und fordere nie, dass der Owner
   JSON öffnet oder prüft. Führe intern `render-review` aus, zeige dessen
   lesbare Entscheidungen, Quellen, offene Punkte, getrennte Empfehlungen und
   Dokumentpfade im Chat und frage: „Diese Entscheidungen bestätigen?
   Korrekturen?“ Bei Korrekturen regeneriere Handoff und Review; ein alter
   Receipt ist ungültig.
4. Nur nach einer expliziten Bestätigung im Chat darfst du intern `confirm` mit
   Confirmation-ID und exaktem Statement und direkt danach `consume` ausführen.
   Fordere weder JSON- noch Shell-Arbeit vom Owner. Schreibe dessen Entscheidungen,
   Quellenbytes und Dokumentprovenienz nach `.brandkit/work/<run-id>/brand-raw.md`;
   Empfehlungen bleiben unbestätigt und nicht normativ. BrandKit signiert nie
   ohne diese explizite Chat-Bestätigung.
   Für offene visuelle Domänen rufe danach `brandkit-visual-decisions` auf. Die
   erste Owner-Nachricht enthält ausschließlich Kandidaten-IDs oder
   `open`/`not-applicable`; führe intern `prepare-visual-confirmation` aus,
   zeige die aufgelösten konkreten Werte in der gefüllten Review und frage
   erneut nach Bestätigung oder Korrektur. Nur die zweite, exakte Bestätigung
   darf denselben Receipt-/`consume`-Pfad verwenden. Schreibe den daraus
   erzeugten deterministischen visuellen Block samt Provenienz nach
   `brand-raw.md`; die Empfehlungsliste bleibt getrennt und nicht normativ.
5. In kurzen Runden nur ungedeckte Lücken der elf Achsen fragen: Kern und
   Zielgruppe, Persönlichkeit, Gegenbild, Markenstruktur, Farbe, Typografie,
   Raster/Spacing, Komponenten, Sprache/Verbotsliste, Zielformate und Bildregeln.
   Warte zwischen Runden. Fertig, wenn jede Achse entschieden, ausdrücklich
   offen oder nicht relevant ist.
6. Konflikte dem Owner vorlegen. Keine Quelle still priorisieren. Fertig, wenn jeder Konflikt entschieden oder blockierend markiert ist.
7. Rohfassung schreiben. Konkrete Werte erhalten ihren Quellenpointer. Unbelegte Werte werden nicht plausibilisiert. Ein sichtbares `TODO(owner): <konkrete fehlende Entscheidung>` ist nur hier und in Arbeitsartefakten zulässig.

## Gate

`brandkit-build` darf erst starten, wenn Identität, Zielgruppe, Stimme, visuelle Rollen, Zielformate und harte Ausschlüsse entweder belegt oder als explizite Owner-TODOs sichtbar sind und kein ungelöster Widerspruch einen verbindlichen Wert betrifft. Stoppe bei widersprüchlicher Markenidentität, fehlendem Owner oder einer Rechtsaussage ohne bereitgestellte offizielle Quelle; für Letztere nur `TODO(legal-review)` erfassen.
