---
name: brandkit-build
description: Verdichtet eine freigegebene Markenrohfassung in ein validierbares Brand Kit. Nutze den Skill nach dem Interview-Gate für BRAND.md, Tokens, Assets, Beispiele und kanonische Agentenregeln; er produziert keine Kundenpublikation.
license: MIT
---

# BrandKit bauen

Owner: normatives Brand-Paket. Produkttexte und Veröffentlichungen gehören `brandkit-publish`; UI-Entscheidungen gehören nach Übergabe `brandkit-impeccable`.

## Eingabe und Ausgabe

- Eingabe: freigegebene `brand-raw.md`, Quellenpointer und `setup-readiness.md`.
- Ausgabe: Brand-Paket gemäß `brand-package.v1` mit `BRAND.md`, `REFERENCE.md`, ausführbaren Tokens, erlaubten Assets/Rechten, Beispielen und genau einer kanonischen `AGENTS.md`.
- Claude-Brücke: `CLAUDE.md` ist im Projektroot ein relativer Symlink mit exakt dem Ziel `AGENTS.md`.
- Hermes-Brücke: Hermes wird mit dem Projektroot als CWD gestartet und lädt dort `AGENTS.md` als Projektkontext. Es wird keine `.hermes.md`, `HERMES.md` oder Instruktionskopie erzeugt.

## Workflow

1. Interview-Gate prüfen. Offene konkrete Werte bleiben sichtbar offen; keine Farbe, Schrift, Maße, Stimme, Referenz oder Rechtsposition erfinden.
2. `BRAND.md` auf verbindliche Regeln verdichten; Begründungen bleiben in `REFERENCE.md`. Widersprüche stoppen den Build. Bei neuen oder strittigen Abstandsrollen liefert `brandkit-spacing` einen belegten Vorschlag; nur dieser Build materialisiert und versioniert daraus normative `shared_visual_tokens.spacing`.
3. Nur belegte Tokens und rights-geprüfte Assets materialisieren. Beispielwerte tragen sichtbar `EXAMPLE`, offene Arbeitswerte `TODO(owner)`; beide dürfen niemals als normative Tokens ausgegeben werden.
4. `python3 scripts/materialize_brandkit_instructions.py --project-root <git-root>` ausschließlich mit dem tatsächlichen Git-Root ausführen. Das Skript prüft alle Ziele per lstat/Realpath, schreibt `AGENTS.md` atomar, setzt den eingeschlossenen `CLAUDE.md -> AGENTS.md`-Symlink und verweigert `.hermes.md`, `HERMES.md`, `AGENTS.override.md`, `agents.md` sowie jede Quelle, die Hermes v0.20.5 in seiner Root→CWD-Prioritätskette vorziehen oder zusätzlich mergen würde. Der installierte `prompt_builder.build_context_files_prompt` bestätigt den effektiven Prompt. Hermes aus genau diesem Git-Root starten; dessen Projekt-Instruktionsdiscovery ist von Skill-Discovery getrennt.
5. Beispiele gegen `BRAND.md` prüfen und das Brand-Paket validieren. Fertig, wenn jeder normative Wert auf die Rohfassung/Quelle zurückführbar ist und keine TODO als verbindlicher Wert erscheint.

## Gate

`brandkit-publish` erhält nur ein auflösbares, validiertes und gepinntes Paket. Stoppe bei fehlender Provenienz, konfliktbehafteten Werten, ungeklärten Assetrechten, fehlendem Brand-Pin oder einer nichtkanonischen Agentenregel-Kopie. Kommerzielle Inhalte bleiben projektlokal; der Skill verlangt keinen bestimmten Anbieter.

Für UI-Arbeit übergib ausschließlich Brand-Pin plus projektlokale `.brandkit/impeccable-product.json` an `brandkit-impeccable`. Dieser Skill gibt keine parallelen UI- oder DESIGN.md-Anweisungen.
