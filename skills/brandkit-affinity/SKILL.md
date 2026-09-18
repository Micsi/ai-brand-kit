---
name: brandkit-affinity
description: Steuert den nativen Affinity-MCP für BrandKit. Nutze den Skill, um eine projektlokale Affinity-Verbindung zu beobachten oder eine freigegebene Logo-, Lockup- beziehungsweise Vektoroperation Receipt-gebunden im Run-Staging auszuführen.
license: MIT
---

# BrandKit Affinity

## Routing

| Route | Playbook |
|---|---|
| Verbindung beobachten oder projektlokal aktivieren | [`references/setup-and-probe.md`](references/setup-and-probe.md) |
| Logo-, Lockup- oder Vektoroperation ausführen | zuerst [`references/setup-and-probe.md`](references/setup-and-probe.md), dann [`references/vector-workflow.md`](references/vector-workflow.md) |

Lies nur den Playbook-Pfad, den der Auftrag erreicht. Der Mutationspfad beginnt
mit dem Setup-Playbook und wechselt erst nach einer gültigen Observation in den
Vektorworkflow.

## Eingabe und Ausgabe

- Beobachtung: Projektroot, Run-ID und lokale Serverkonfiguration hinein;
  `brandkit-affinity-observation.v1` mit Server-, Versions-, Tool- und
  Schemaidentität heraus.
- Mutation: Visual Brief, gültige Observation und genau ein unverbrauchter
  `brandkit-affinity-mutation-receipt.v1` hinein; genau das deklarierte
  Staging-Artefakt samt Evidenz heraus.

## Gemeinsamer Vertrag

**Native.** Verwende den in Affinity eingebauten MCP-Server. Seine Preamble und
die benötigte SDK-Dokumentation bestimmen die ausführbaren Operationen.

**Observation.** Jeder neue Serverkontakt beginnt read-only und endet mit einer
kurzlebigen, projekt- und rungebundenen Observation. `connected` allein ist
keine Mutationsfreigabe.

**Receipt.** Jede Mutation besitzt genau einen aktuellen Receipt für Operation,
Tool-Schema, Projekt, Run, Dokument, Ein- und Ausgabe. Schreibe ausschließlich
nach `.brandkit/work/<run-id>/visual-decisions/staging/<document-id>/` und halte
alle Quellen read-only.

**Handoff.** Übergib erzeugte Kandidaten an `brandkit-visual-decisions`.
`brandkit-interview` bleibt die einzige Stelle, die eine spätere
Owner-Bestätigung normativ konsumiert.

## Gate

Fertig ist die Beobachtung erst, wenn genau ein nativer Loopback-Server samt
Tool-Schemaidentität belegt ist oder der Befund geschlossen
`unknown|unavailable|ambiguous` lautet. Fertig ist eine Mutation erst, wenn
Receipt, Staging-Artefakt und Evidenz dieselben Bytes binden und jede Quelle
unverändert ist. Jeder andere Zustand blockiert Vektorarbeit; Farb- und
Typografieanalyse kann ohne Vektormutation fortfahren.
