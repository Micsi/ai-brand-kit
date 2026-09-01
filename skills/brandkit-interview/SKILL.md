---
name: brandkit-interview
description: Erhebt belegte Markenentscheidungen und Konflikte in einer Rohfassung. Nutze den Skill nach dem Readiness-Check, wenn Positionierung, Stimme, visuelle Rollen, Formate oder Ausschlüsse noch geklärt werden müssen.
license: MIT
---

# BrandKit-Interview

Owner: Aussagen der Markenquelle erfassen, nicht Designwerte ergänzen oder das finale Kit bauen.

## Eingabe und Ausgabe

- Eingabe: `setup-readiness.md`, bereitgestellte Primärquellen und Antworten des Marken-Owners.
- Ausgabe: `.brandkit/work/<run-id>/brand-raw.md` mit Provenienz je konkreter Entscheidung, Konfliktregister und offenen Punkten.

## Workflow

1. Quelleninventar anlegen. Trenne `owner supplied`, `official supplied`, `observed` und `unknown`. Fertig, wenn jede verwendbare Quelle klassifiziert ist.
2. In kurzen Runden nur Lücken fragen: Kern/Zielgruppe, Persönlichkeit und Gegenbild, Markenstruktur, Farbe, Typografie, Raster/Spacing, Komponenten, Sprache/Verbotsliste, Zielformate und Bildregeln. Warte zwischen Runden. Fertig, wenn jede Achse entschieden, ausdrücklich offen oder nicht relevant ist.
3. Konflikte dem Owner vorlegen. Keine Quelle still priorisieren. Fertig, wenn jeder Konflikt entschieden oder blockierend markiert ist.
4. Rohfassung schreiben. Konkrete Werte erhalten ihren Quellenpointer. Unbelegte Werte werden nicht plausibilisiert. Ein sichtbares `TODO(owner): <konkrete fehlende Entscheidung>` ist nur hier und in Arbeitsartefakten zulässig.

## Gate

`brandkit-build` darf erst starten, wenn Identität, Zielgruppe, Stimme, visuelle Rollen, Zielformate und harte Ausschlüsse entweder belegt oder als explizite Owner-TODOs sichtbar sind und kein ungelöster Widerspruch einen verbindlichen Wert betrifft. Stoppe bei widersprüchlicher Markenidentität, fehlendem Owner oder einer Rechtsaussage ohne bereitgestellte offizielle Quelle; für Letztere nur `TODO(legal-review)` erfassen.
