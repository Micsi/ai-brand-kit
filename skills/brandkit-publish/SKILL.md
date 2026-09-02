---
name: brandkit-publish
description: Erzeugt und prüft ein konkretes Web-, Markdown-, PDF- oder Bildartefakt aus einem validierten Brand-Paket. Nutze den Skill für die finale Produktion; offene Fakten, Rechtsprüfung, TODOs oder Vertragsverletzungen blockieren Veröffentlichung.
license: MIT
---

# BrandKit publizieren

## Routing

| Route | Primäres Playbook |
|---|---|
| Artefakt publizieren | [`references/workflow.md`](references/workflow.md) |

Owner: mutable Projektinhalte orchestrieren, rendern und abnehmen. Das Brand-Paket bleibt read-only. UI-Design gehört ausschließlich dem `brandkit-impeccable`-Adapter.

Claude, Codex und Hermes rufen denselben `brandkit-publish`-Skill auf. Das Humanizer-Ausführungsprofil bleibt davon getrennt: Als LLM-Provider ist ausschließlich Claude Code 2.1.223 mit seinem geschlossenen Envelope-Vertrag unterstützt; Codex CLI ist für diesen Humanizer-Release nicht unterstützt und wird fail-closed abgewiesen.

## Eingabe und Ausgabe

- Eingabe: aktivierter `brand_pin`, freigegebener Projektinhalt, Dokumenttyp, Zielmedium und vorhandene Rechte-/Review-Evidenz.
- Ausgabe: `.brandkit/work/<brand-id>/<run-id>/` für Zwischenstände sowie `output/<brand-id>/<run-id>/` für geprüfte Artefakte, Prüfbericht und unveränderte Fakten-/Quellenledger.
  Maschinenlesbare Nachweise heißen `fact_ledger` und `humanizer_evidence`.

## Workflow und Gates

1. Brand-Paket auflösen und read-only laden. Bei Brandkonflikt, fehlendem Pin oder fehlendem Input stoppen.
2. Faktenledger bilden. Zahlen, Namen, Daten, Preise, Zitate, Quellen, Rechts-/Normverweise und Kundenstimmen sind geschützt. Fehlende Fakten dürfen im Arbeitsentwurf nur als sichtbares `TODO(owner)` stehen.
3. Rechtsclaims ausschließlich aus vom Nutzer bereitgestellten offiziellen Quellen übernehmen. Sonst `TODO(legal-review)` setzen und Publikation blockieren; der Skill gibt keine eigene Rechtsauslegung.
4. Bei UI-Arbeit ausschließlich Brand-Pin und `.brandkit/impeccable-product.json` an `brandkit-impeccable` übergeben. Erst nach grünem PRODUCT.md/DESIGN.md-Sync dessen gewählte Route ausführen. `brandkit-spacing` ist nur bei einem expliziten Spacing-Audit oder strittigen Abstandsrollen vorgeschaltet; es erzeugt keine parallelen Designregeln.
5. Für Markdown zuerst `scripts/markdown_contract.py` validieren und den disjunkten, lückenlosen Segmentplan erzeugen. Nur als mutable markierte, einzeilige Prosa darf redigiert werden; Frontmatter, Fakten, Links, Zitate, Code, Tabellen, Callouts, IDs und Struktur bleiben bytegeschützt.
6. Für diesen deutschen Publish-Workflow ist `humanizer_applicability` immer explizit `required`; ein stilles `null` oder ein evidenzloser Nicht-Humanizer-Pfad ist unzulässig. Die aktive Release aus `toolchain/humanizer-de/toolchain.lock.json` auflösen und den belegten Zwei-Aufruf-Runner aus deren gepinntem Root starten: `python3 scripts/humanizer_two_pass.py --file <mutable-input.md> --out-dir <leeres-evidence-verzeichnis> --mode <locker|sachlich|formal> --provider claude --model <modell>`. Der Runner trennt Audit und Rewrite, bestätigt Kandidaten vor dem zweiten Aufruf und führt `scripts/evidence_lint.py` gegen Vorher/Nachher aus. Ein angenommener Edit-Lauf liefert `original.md`, `preflight.json`, `audit-call.json`, `audit-ledger.json`, `confirmed-ledger.json`, `edits.json`, `rewrite-call.json`, `evidence-ledger.json`, `evidence-report.json`, `changes.diff`, `report.json` und `result.md`; beim Null-Edit fehlt nur `rewrite-call.json`. Ein abgelehnter Lauf liefert `rejected.md` statt `result.md`. Nur `report.json` mit `accepted: true`, ohne `protected_violations` und ohne `evidence_blockers`, plus `result.md`, darf weiterlaufen.
7. Runner-Artefakte gegen ihre geschlossenen BrandKit-Evidence-Schemas und jeden gepinnten SHA-256 prüfen. Provider, Modell, Modus, Skill-, Katalog-, Runner- und Prompt-Hashes müssen dem realen Runner-Report entsprechen. `references/patterns.md` wird mit Revision, SHA-256 und der exakt daraus gelesenen kanonischen ID-Liste `1` bis `72` gebunden; unbekannte IDs wie `9999`, Aliase wie `Muster 5`, Duplikate und fehlerhafte Listen stoppen den Lauf. Für Modellaufrufe gilt ausschließlich der geschlossene Claude-Code-2.1.223-Envelope-Vertrag; ein Codex-CLI-Envelope oder ein anderer Provider wird nicht als Erfolgspfad akzeptiert. Zusätzliche rohe Felder werden nicht ignoriert. Die SHA-256-Werte der vollständigen `audit-call.json` und `rewrite-call.json` werden in Report und Provenienz gebunden. Kandidaten aus `audit-ledger.json` mit der echten Runner-Bestätigung neu ableiten. `result.md` ausschließlich mit der gepinnten `apply_edits`-Funktion aus `original.md`, `confirmed-ledger.json` und `edits.json` rekonstruieren und bytegleich vergleichen. `changes.diff` mit der gepinnten Diff-Funktion neu erzeugen. Danach das BrandKit-eigene, nicht runner-native `brandkit-provenance.json` geschlossen materialisieren: Schema-Version, Before-/After-Hash, `candidate_id`, `source`, `action`, Pattern-IDs, exakte Unicode-Codepoint-Positionen `start_codepoint`/`end_codepoint`, Hash der alten und neuen Teilzeichenfolge, rohe Modell-Call-Hashes sowie Katalog-/Runner-Revision und -Hash. Dieses Artefakt aus tatsächlichem Vorhertext, bestätigten Edits und rekonstruiertem Resultat erneut ableiten, byte- und semantikgleich vergleichen und überlappende Spannen verwerfen. `evidence-ledger.json` über den exakten gepinnten Aufruf `scripts/evidence_lint.py --before-file <original.md> --write-ledger <ledger>` und `evidence-report.json` über `--before-file <original.md> --after-file <result.md> --fail-on never` neu erzeugen und bytegleich vergleichen. Danach Änderungen ausschließlich über `rewrite_humanizer(...)` in die erlaubten Markdown-Segmente anwenden. `old_hash` authentisiert nur die unveränderte Baseline des adressierten Segments; eine echte Ersetzung hat neue Bytes und damit einen neuen Hash. Der geschützte Komplementbereich und das AST-Skelett müssen beim erneuten Markdown-Vertragstest unverändert bleiben. Mutation eines geschützten Fakts/Segments, fehlende Evidenz oder generisches Glätten ohne bestätigten Cluster stoppt den Lauf.
8. Zielformat mit verfügbarer, gepinnter Toolchain erzeugen. Bilder nur auf ausdrücklichen Wunsch und nach Brand-/Rechteprüfung; `scripts/ai_label.py` ist optionale Verarbeitung, kein Rechtsnachweis und kein Vendor-Mandat.
9. Technische und visuelle QA ausführen. Vor Veröffentlichung über den gesamten Input, Metadaten, Rechte und Render-Evidenz nach `TODO`, `[TODO`, `TODO(owner)` und `TODO(legal-review)` suchen.

## Publication Gate

Veröffentliche nur bei grünem Brand-Pin, Faktenledger, Rechtecheck, Markdown-Vertrag, Humanizer-Evidenz, Renderer-Evidenz und visueller Prüfung. Jeder sichtbare oder verschachtelte TODO, jede geschützte Mutation, ungeprüfte Rechtsbehauptung oder ein fehlerhafter Impeccable-Handoff blockiert. Berichte Blocker konkret; ersetze sie niemals durch plausible Fakten.
