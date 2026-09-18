---
name: brandkit-visual-decisions
description: Erzeugt nichtnormative, vergleichbare Kandidaten für offene Markenfarben, Typografie und Logo-/Lockup-Fragen. Nutze den Skill nach dem BrandKit-Interview und vor dem Build, wenn der Owner visuelle Werte erst anhand eines Entscheidungsbogens auswählen muss.
license: MIT
---

# BrandKit Visual Decisions

## Routing

| Route | Primäres Playbook |
|---|---|
| Kandidaten erzeugen, prüfen und zur Auswahl stellen | [`references/workflow.md`](references/workflow.md) |
| Offline-Entscheidungsbogen erzeugen oder prüfen | [`references/decision-sheet.md`](references/decision-sheet.md) |

Owner: nachvollziehbare Kandidaten, getrennte Empfehlung und Vergleich. Nur
`brandkit-interview` darf eine spätere Owner-Bestätigung normativ in
`brand-raw.md` übernehmen; `brandkit-build` materialisiert erst danach.

## Eingabe und Ausgabe

- Eingabe: hashgebundener Visual Brief aus dem Interview, klassifizierte
  Quellen, lokale Rechtebelege und ein read-only Affinity-MCP-Befund.
- Ausgabe: Kandidaten-Bundle, technische Prüfberichte, unverbindliche
  Empfehlung sowie unselektierter responsiver Offline-Entscheidungsbogen unter
  `.brandkit/work/<run-id>/visual-decisions/`.

## Workflow

1. Lies [`references/workflow.md`](references/workflow.md) und validiere Brief,
   Quellen, Rechte und Pins vor der ersten Kandidatenerzeugung.
2. Rufe für die Affinity-Prüfung und jede Vektorarbeit `brandkit-affinity` auf.
   Dessen read-only Observation ist der Befund dieses Workflows. `unknown` oder
   `ambiguous` ist kein Nichtvorhandensein. Eine Mutation braucht zusätzlich
   einen eigenen, projekt- und rungebundenen Operations-Receipt.
3. Erzeuge und prüfe nur die offenen Domänen. Trenne Kandidaten, Empfehlung
   und Owner-Auswahl strukturell. Kein Kandidat wird vorselektiert.
4. Lies für die Vergleichsausgabe
   [`references/decision-sheet.md`](references/decision-sheet.md). Zeige dem
   Owner den Bogen und frage nach Candidate-IDs beziehungsweise
   `open|not-applicable`. Schriften nennt der Bogen mit ihrem typografischen
   Familiennamen (`name`-ID 16, sonst ID 1), nicht mit dem Namen eines
   Schnitts. Weist der Core einen Font-Report ab, weil er noch den Namen der
   Standardinstanz pinnt, baue das Bundle neu — die Reports lassen sich nicht
   migrieren, und ohne Neuaufbau entsteht kein Bogen. Die Specimens laufen je
   Rolle mit gleicher **gezeichneter** x-Höhe und nicht mit gleichem Grad:
   gelesen wird die x-Höhe an der Instanz, die die Rolle wirklich zeichnet, und
   die optische Größenachse wird dafür abgeschaltet. Der Grad je Kandidat folgt
   daraus, steht im Bogen und ist keine Empfehlung. Je Rolle beginnt die erste
   Zeile bei allen Kandidaten auf derselben Grundlinie, und der Zeilenabstand
   bleibt ein absoluter Wert, damit nebeneinander stehende Blöcke oben gleich
   anfangen und Zeile für Zeile nebeneinander laufen. Je Schrift nennt der Bogen
   außerdem, welche Gewichte die Datei hergibt — variable Achse oder starres
   Gewicht — und welche er davon setzt. Ziffernsätze der
   Schrift führt der Bogen aus den Fonttabellen vor; trägt eine Schrift keinen
   zweiten, sagt er das ausdrücklich.
5. Übergib diese Auswahl ohne konkrete Caller-Werte an
   `brandkit-interview:prepare-visual-confirmation`. Als `candidate_review`
   bindest du genau `sheet/index.html` des Bogens, den der Owner gesehen hat.
   Hat sich Bundle, Brief oder Affinity-Beobachtung seit dem Rendern geändert,
   entferne `sheet/`, rendere den Bogen neu und zeige ihn dem Owner erneut;
   einen veralteten Bogen weist die Operation ab, bevor sie etwas verbraucht.
   Zeige anschließend die gefüllte Review mit den aufgelösten Werten, bei
   Schriften mit den Familiennamen, und frage erneut nach Bestätigung oder
   Korrekturen.
6. Erst eine Bestätigung der aktuellen gefüllten Review erlaubt den bestehenden
   Interview-Pfad `confirm -> consume`. Korrekturen regenerieren Handoff und
   Review; alte Owner-Receipts sind ungültig. Wird eine
   `prepare-visual-confirmation` **wegen deiner Eingabe** abgewiesen, verbraucht
   sie nichts und hinterlässt kein halbes Artefakt — korrigiere die Auswahl und
   wiederhole den Aufruf unverändert. Das gilt auch für die Namen, die du
   bindest: `chat_statement` und `candidate_review` dürfen nicht auf
   `handoff.json` oder `filled-review.md` zeigen, denn das sind die beiden
   Dateien, die die Operation selbst schreibt — sie weist eine solche Bindung
   ab, bevor sie etwas verbraucht. Unbedingt ist die Zusage nicht: wird der
   Workspace **während** des laufenden Aufrufs von außen verändert, kann auch
   hinter dem Verbrauch noch abgewiesen werden. Nach einer **erfolgreichen**
   `prepare` sind Affinity-Mutation-Receipts verbraucht; eine Korrektur braucht
   dann neu ausgestellte Receipts und einen neuen Bogen.

## Gate

Jeder Kandidat braucht geschlossene Provenienz, Rechte, immutable Pins und alle
anwendbaren deterministischen Prüfungen. Rendererabhängige Prüfungen heißen
ohne gepinnten Renderer `not-run`, nie `pass`; Lesbarkeit und visuelle Eignung
bleiben ein hashgebundenes Human-Review. Fehlt Affinity Studio MCP, dürfen
Farb-/Typografieanalyse und der statische Bogen weiterlaufen, aber keine neuen
Logo-, Lockup-, Favicon- oder Monochrom-Assets entstehen. Weise auf die sichere
Affinity-Aktivierung hin; ImageGen, Raster- oder generische SVG-Erzeugung sind
kein Ersatz. Ein erforderlicher offener Grafikwert begrenzt den Build auf
`package-draft-only` und blockiert Publish.
