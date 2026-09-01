# BrandKit Skills v1

Normativ sind `brandkit-skill-contract.v1.json`, die vier Kern-Skills, der
optionale Adapter `skills/brandkit-spacing/SKILL.md` und
`scripts/validate_brandkit_skills.py`.

## Sequenz und Grenzen

`setup -> interview -> build -> publish` ist eine Gate-Sequenz, kein Router
mit überlappenden Zuständigkeiten. Setup besitzt nur Readiness, Interview nur
Quellenerhebung, Build nur das normative Brand-Paket und Publish nur mutable
Projektinhalte und Ausgabe. Offene Werte bleiben als konkrete Owner- oder
Legal-Review-TODOs in Roh-/Arbeitsartefakten sichtbar. Jede TODO blockiert die
Veröffentlichung.

`brandkit-spacing` bleibt ausserhalb dieser Sequenz. Er wird nur fuer eine
ausdrueckliche read-only Spacing-Inventur, Rollenpruefung oder einen
unbestaetigten Token-Vorschlag geroutet. Normative Markenwerte materialisiert
allein `brandkit-build`; `DESIGN.md` materialisiert allein
`brandkit-impeccable`; UI-Patches gehen an
`brandkit-impeccable -> impeccable layout`, PDF-Layout an den PDF-Workflow.
Das geschlossene Inventar bindet Fundstelle, aufgeloesten Wert, Medium und
genau eine Token-Zuordnung oder begruendete Ausnahme je Eintrag durch einen
SHA-256-Input-Pin. Das geschlossene Resultat bindet Inventar-/Proposal-Hash,
Zuordnungszahlen, Befunde und Handoff. Die maschinenlesbare Boundary steht
unter `optional_adapters` und `routing` im Skillvertrag.

Rechtsaussagen brauchen eine vom Nutzer bereitgestellte offizielle Quelle.
Ohne sie entsteht kein Claim, sondern `TODO(legal-review)`. Impeccable erhält
nur den Übergabevertrag von `brandkit-impeccable`: Brand-Pin und
`.brandkit/impeccable-product.json`; PRODUCT.md/DESIGN.md müssen vor der Route
grün sein. Die vier Skills enthalten keine zweite UI-Anleitung.

Der Publish-Textpfad verwendet den Markdown-Vertrag und den tatsächlichen
`humanizer-de`-Mechanismus. Pass A umfasst Triage und den vollständigen
Evidence-/Artefakt-Pass 1. Pass B umfasst nur echte Cluster der Pässe 2–4 und
den Selbst-Audit in Pass 5. Der Rückschreibpfad akzeptiert ausschließlich
mutable Einzeilensegmente samt `old_hash` über `rewrite_humanizer`; geschützte
Fakten und Struktur bleiben bytegleich.

Die Publish-Prüfung ergänzt die Runner-Ausgabe um BrandKit-derived Evidence:
Der gepinnte Musterkatalog wird mit Revision, Hash und den kanonischen IDs
`1`–`72` geschlossen gebunden. Provider-Rohantworten folgen dem geschlossenen,
versionsgebundenen Vollschema `schemas/claude-code-2.1.223-result.schema.json`.
Es wurde aus einem echten Minimalaufruf mit Claude Code 2.1.223 und den
gepinnten Runner-Flags abgeleitet; die Call-Fixtures bewahren dieselben Felder
und Typen, ersetzen aber Sitzungs-/Modellkennungen und Messwerte deterministisch
durch neutrale Werte. Top-Level sowie `usage`, `modelUsage` und Permission-
Felder sind geschlossen. Codex CLI 0.144.3 ist für diesen Humanizer-Release als
LLM-Provider ausdrücklich nicht unterstützt und wird fail-closed abgewiesen;
Codex als ausführender Agent ist davon unabhängig. Die vollständigen Rohbyte-
Hashes der Claude-Calls stehen im Report und in `brandkit-provenance.json`.
Dieses deterministische
Provenienzartefakt bindet Before/After, bestätigte Kandidaten und Edits,
Unicode-Codepoint-Spannen, alte/neue Teilstring-Hashes sowie Katalog und Runner.
Es wird von BrandKit neu abgeleitet und ist kein runner-natives Artefakt.
`original.md` und `result.md` müssen strikt dekodierbares UTF-8 ohne BOM und nur
LF-Zeilenenden sein. Before-/After-Hashes werden direkt aus ihren Rohbytes
gebildet und müssen mit Manifest, Evidence-Lint und Report übereinstimmen;
Codepoint-Spannen entstehen erst aus dem danach dekodierten kanonischen Text.

## Skill-Name, Discovery und Providerdarstellung

Diese Mechanismen sind getrennt:

1. Der Discovery-Ordner bestimmt, wo ein Provider nach einem Skill sucht.
2. Die Skill-Identität dieses Repositories ist `frontmatter.name`
   (`docs/agent-skill-architecture.md`, Abschnitt „Namensidentität“);
   gleichnamige andere Realpaths blockieren. Ein Symlink/Bridge ändert daher
   keinen Aufrufnamen.
3. Ein Generator kann einen anderen Aufrufnamen nur materialisieren, wenn seine
   kanonische Transformregel sowohl Zielordner als auch ausgegebenes
   Frontmatter (oder einen ausdrücklich dokumentierten Wrapper/Alias) ändert.
   Das ist Provider-Metadaten-/Namenslogik, nicht bloße Wiederverwendung eines
   Bodies oder von Referenzassets.

Der aktuell gepinnte Impeccable-Upstream belegt den Normalfall ohne Umbenennung:
`skill/SKILL.src.md:2` heißt `impeccable`. Der Build liest die kanonischen
Quellen in `scripts/build.js:585-603`. Seine Factory übernimmt
`skill.name` unverändert als Zielordner und Frontmatter in
`scripts/lib/transformers/factory.js:268-275` und schreibt den Body nach
`...:305-316`. Entsprechend heißen alle 17 inventarisierten
Providerausgaben im gepinnten Checkout weiterhin `impeccable`.

Damit ist eine geänderte Invocation für diesen Pin nicht beobachtet. Sie wäre
nur durch eine explizite, testbare Generatortransformation, einen Wrapper mit
eigener Identität oder einen provider-nativen Alias erklärbar. Ein neuer Name
darf Body/Referenzassets aus einer kanonischen Quelle wiederverwenden, braucht
aber eigene deklarierte Manifest-/Transformidentität und Kollisionstests.
Provider-spezifische Kopien des Skill-Bodies sind keine zulässige Erklärung.
Die vier `brandkit-*`-Skills sind eigenständige kanonische Skills, keine
umbenannten Impeccable-Ausgaben.

## Prüfung

`fixtures/brandkit-skills/dry-runs.v1.json` deckt alle vier Kern-Skills und den
Spacing-Adapter ab. `fixtures/brandkit-skills/collision-dry-runs.v1.json`
leitet die eindeutigen Routen aus dem Requesttext fuer Spacing-Audit,
UI-Redesign, Brand-Paket-Build, PDF-Publikation und DESIGN.md sowie vier
Mischfaelle ab. Ein manipulierter Request wird gegen dieselbe Ableitung rot.
Die Fehlerfixtures decken die
Fehlerfälle fehlende Fakten, Brand-Konflikt, Legal-TODO, geschützte
Faktenmutation und unzulässige Impeccable-Parallelübergabe ab.

```sh
python scripts/validate_brandkit_skills.py
python -m unittest tests/test_brandkit_skills.py
scripts/check_brandkit_skills.sh
```

Der letzte Befehl exportiert ausschließlich den Git-Index in einen temporären
Cleanroom. Änderungen, die nicht im Index stehen, sind absichtlich unsichtbar.
