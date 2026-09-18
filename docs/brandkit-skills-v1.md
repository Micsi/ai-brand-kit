# BrandKit Skills v1

Normativ sind `brandkit-skill-contract.v1.json`, die fünf Kern-Skills, der
optionale Adapter `skills/brandkit-spacing/SKILL.md`, der Affinity-Adapter
`skills/brandkit-affinity/SKILL.md` und
`scripts/validate_brandkit_skills.py`.

Der Skillvertrag wird geschlossen gegen
`schemas/brandkit-skill-contract.v1.schema.json` validiert. Sein Abschnitt
`public_contracts` bindet die von Skills genannten CLI-Subcommands und
Python-Einstiegspunkte an den aktuellen Python-AST, das Brand-Paket an
`brand-package.v1` sowie den vollständigen Dokumenttyp-Katalog an die fünf
versionierten Verträge. Fehlende, umbenannte oder zusätzlich eingecheckte
Oberflächen sind Vertragsdrift und schlagen fehl.

`progressive_disclosure.routes` ist die kanonische Ladematrix: Eine Route hat
genau ein primäres Playbook und eine geschlossene, disjunkte Liste erlaubter
Kontextdateien. Sie deckt alle acht Repo-Skills genau einmal ab. Der
projektlokal erzeugte `DESIGN.md`-Vertrag nennt Brand Package
`shared_visual_tokens` ausdrücklich als einzige normative Quelle portabler
visueller Werte und materialisiert Leitidee, Rollen/Tokens, Typografie,
Spacing, Komponenten, Einsatzregeln und Anti-Referenzen, ohne PDF-Tokens in
den portablen Namespace zu heben.

Der zugehörige Katalog bindet jede Primär- und Querschnittsreferenz mit ID,
Rolle und SHA-256. Rollen-/Pfadkombinationen sind geschlossen; Selbstreferenz,
fremde Repo-Dateien, unbekannte oder unbenutzte Katalogeinträge blockieren.
Für den gepinnten Impeccable-Release wird außerdem jede Command-Route auf allen
vier Plattformwerten als ausführbarer Load-Trace geprüft. Ein Trace enthält
genau einen Context-Lauf und ein Primärplaybook. `init` lädt Native-Guidance
erst nach `PRODUCT.md`; `adaptive` fasst iOS und Android in genau einem späten
Native-Load-Ereignis zusammen.

`skill-contract-release.v1.json` attestiert Vertrag, Schema, Skills,
Playbooks, Produktionsskripte, Dokumenttypen und Testfixtures gemeinsam. Der
Manifesthash ist unabhängig in der Contract-Test-Suite verankert. Damit reicht
es nicht, nach einer Vertragsmanipulation nur den Dry-Run-Inputhash
nachzuziehen.

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

`brandkit-affinity` bleibt ebenfalls außerhalb der Kernsequenz. Der Adapter
beobachtet den nativen, lokalen Affinity-MCP-Server zunächst read-only. Neue
Logo-/Lockup-Vektoren und Exporte werden nur nach einem projekt- und
rungebundenen Mutation-Receipt in das run-lokale Staging geschrieben. Claude
nutzt die projektlokale `.mcp.json`; Codex nutzt den gepinnten Bridge-Prozess
über `bin/codex-brandkit`. Globale MCP-Konfigurationen werden nicht verändert.

Rechtsaussagen brauchen eine vom Nutzer bereitgestellte offizielle Quelle.
Ohne sie entsteht kein Claim, sondern `TODO(legal-review)`. Impeccable erhält
nur den Übergabevertrag von `brandkit-impeccable`: Brand-Pin und
`.brandkit/impeccable-product.json`; PRODUCT.md/DESIGN.md müssen vor der Route
grün sein. Die fünf Skills enthalten keine zweite UI-Anleitung.

## Optionale CI-Discovery

Nach dem Readiness-Check kann der Owner `$brandkit-interview` ausdrücklich mit
vertiefter Discovery starten. Der Wrapper, `grilling` und
`domain-modeling` müssen aus `github:mattpocock/skills` am im Vertrag
festgelegten Commit und mit der geschlossenen kanonischen Datei-/Hashmenge
stammen. Setup prüft einen konkret benannten clean/detached Checkout mit
`brandkit_discovery.py prepare`. Der zurückgegebene Trace bindet das exakte
Run-CWD. Solange die persistierte Frontier offene Punkte enthält, bleibt jeder
Folgeturn in `grilling`; erst abgeschlossene oder ausdrücklich offen markierte
Punkte erlauben `domain-modeling`.
`brandkit-interview` ruft den explicit-only Wrapper nie auf; er bleibt nur die
gepinnten Orchestrierungsreferenz. Setup installiert oder aktiviert keinen
Skill. Fehlt Checkout, Bridge, Abhängigkeit oder Pflichtartefakt, ist der nächste Schritt
entweder das native Interview oder ein eigener ausdrücklicher
Installations-/Aktivierungsauftrag.
Fehlt ein Teil oder sind Quelle beziehungsweise Pin gemischt, bleibt das
native `brandkit-interview` der vollständige Fallback.

`CONTEXT.md` und `docs/adr/` bleiben im Discovery-Verzeichnis. Der geschlossene
`domain-modeling` muss echte, nichtleere und relevante `CONTEXT.md`- und
`docs/adr/*.md`-Dateien erzeugen. Der Handoff bindet diese Dokumente, alle
Quellen und Entscheidungen geschlossen nach Pfad, Größe und SHA-256; ein
isoliertes JSON ist kein Erfolg.
Das Handoff-JSON bleibt intern. Interview rendert eine lesbare Chat-Review mit
Entscheidungen, Quellen, offenen Punkten, getrennten Empfehlungen und
Dokumentpfaden und fragt konkret nach Bestätigung oder Korrekturen. Erst nach
expliziter Chat-Bestätigung führt der Agent intern `confirm` mit Review-Digest,
Confirmation-ID und owner-only HMAC-Key sowie unmittelbar danach `consume` aus.
Der Owner öffnet weder JSON noch Shell. Interview übernimmt ausschließlich dessen authentifizierte
Entscheidungen und Quellenbytes. Unbestätigte Empfehlungen dürfen keine
normativen Werte in `brand-raw.md` erzeugen. Der vollständige native
Elf-Achsen-Pfad bleibt für alle nicht abgedeckten Punkte erhalten.

Offene visuelle Domänen werden anschließend an `brandkit-visual-decisions`
übergeben. Dessen Sheet ist unselected und nicht normativ. Der Owner nennt im
Chat ausschließlich Kandidaten-IDs oder `open`/`not-applicable`; Interview
prüft das aktuelle Run-Bundle, löst Werte und Hashes selbst auf und zeigt eine
gefüllte Review. Erst eine danach ausdrücklich bestätigte Review darf den
vorhandenen Receipt-/Consume-Pfad und den deterministischen `brand-raw.md`-
Block erzeugen. Korrekturen ersetzen Handoff und Review und entwerten alte
Receipts. Erforderliche offene Domänen beschränken den Build auf
`package-draft-only`; `package-ready` verlangt byteidentischen Konsum oder
keine offene visuelle Domäne. Der Prebuild-Impeccable-Resolver ist run-local;
`~/.agents` und `extract` sind vor dem Paket ausgeschlossen.

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
Die fünf Kern-`brandkit-*`-Skills sind eigenständige kanonische Skills, keine
umbenannten Impeccable-Ausgaben.

## Prüfung

Die Impeccable-Matrix wird aus der gepinnten materialisierten `SKILL.md`
einschließlich Command-Tabelle, Aliassen, Sonderrouten und transitiven
Playbook-Links abgeleitet. Der Validator führt den echten `context.mjs` in
isolierten Web-, iOS-, Android- und Adaptive-Projekten aus. Für `init` blendet
der kanonische Defer-Wrapper ein vorhandenes PRODUCT während dieses einen
Context-Laufs aus; erst das neu geschriebene PRODUCT autorisiert genau einen
späten Native-Load. Alle anderen Native-Routen behalten den beobachteten
frühen Context-Load. `live` ist Web-only; Pin/Unpin sind administrative
`pin.mjs`-Routen ohne Design-Context.

Vor jeder Nutzung wird eine materialisierte Impeccable-Quelle descriptor-sicher
gegen Commit `fcd7622...`, Upstream-Tree, Skill-Tree und die geschlossene
Git-Pfad-/Blobmenge geprüft. Ein per Environment gewählter Pfad ist nur bei
bytegleicher Materialisierung zulässig. Der produktive Befehl
`impeccable_adapter.py init-defer` führt den PRODUCT-freien Context genau
einmal aus, schreibt das neue PRODUCT atomar und liest erst danach die realen
gepinnten Native-Referenzen; Fehler stellen das alte PRODUCT wieder her.

Die Release-Prüfung ist ohne externe Inputs fail-closed. Der Aufrufer liefert
Pfad und SHA-256 des Owner-HMAC-signierten Release-Statements sowie den
read-only Owner-Key und das immutable historische Part-1-Snapshot-Archiv. Das
Statement bindet den Release-Manifest-Hash an Snapshot, Root, Signatur,
geschlossene Membermenge und Bootstrap-Pin. Der produktive Validator extrahiert
das Snapshot geschlossen und führt den extern gepinnten Bootstrap erneut aus;
erst dessen semantisch identisches `status: pass` autorisiert den Release. Ein Pin im
Repository oder in dessen Tests wird nicht als Trust-Root akzeptiert.

```sh
python scripts/validate_brandkit_skills.py \
  --release-attestation /operator/path/skill-contract-release-attestation.json \
  --expected-release-attestation-sha256 <extern-gelieferter-sha256> \
  --owner-key /operator/read-only/brand-voice-pdf-owner.key \
  --part1-snapshot /operator/immutable/part1-f5-snapshot.tar.gz \
  --release-evidence /operator/path/release-evidence.md
```

### Die Evidence hängt an der Signatur

Das Statement bindet neben dem Manifest auch den **SHA-256 der Evidence**, für
die es ausgestellt wurde, und zwar im signierten Rumpf. Grund ist ein
eingetretener Fall: eine Evidence wurde nach dem Urteil ersetzt, und weil die
Beurteilung keinen Digest nannte, blieb der Austausch unbemerkt. Gebunden
bricht ein späterer Austausch die Signatur, statt unsichtbar zu bleiben. Die
Prüfung ist auch hier ohne externe Eingabe fail-closed: ohne
`--release-evidence` beziehungsweise `BRANDKIT_RELEASE_EVIDENCE` wird
abgewiesen, und ein Rumpf ohne `evidence_sha256` gilt als nicht geschlossen.

Damit sich die beiden Digests nicht gegenseitig voraussetzen, gilt eine feste
**Reihenfolge**:

1. Evidence schreiben — **ohne** den Digest ihrer Attestation darin.
2. Evidence hashen.
3. Attestation ausstellen, die diesen Evidence-Digest neben dem
   Manifest-Digest im signierten Rumpf führt.
4. Die Beurteilung nennt **Digest und Zeilenzahl** der Fassung, die sie
   beurteilt hat.

Braucht die Evidence einen Verweis auf ihre Attestation, dann ausschließlich
über deren **Dateinamen**: der ändert sich mit dem Inhalt nicht, ein Digest
schon — und ein Digest im Text zöge genau den Zirkel nach sich, den diese
Reihenfolge auflöst.

`fixtures/brandkit-skills/dry-runs.v1.json` deckt alle fünf Kern-Skills und den
Spacing-Adapter ab. `fixtures/brandkit-skills/collision-dry-runs.v1.json`
leitet die eindeutigen Routen aus dem Requesttext fuer Spacing-Audit,
UI-Redesign, Brand-Paket-Build, PDF-Publikation und DESIGN.md sowie vier
Mischfaelle ab. Ein manipulierter Request wird gegen dieselbe Ableitung rot.
Die Fehlerfixtures decken die
Fehlerfälle fehlende Fakten, Brand-Konflikt, Legal-TODO, geschützte
Faktenmutation und unzulässige Impeccable-Parallelübergabe ab.

```sh
# Vollvalidator mit den oben gezeigten externen Trust-Argumenten
python -m unittest tests/test_brandkit_skills.py
scripts/check_brandkit_skills.sh
```

Der letzte Befehl exportiert ausschließlich den Git-Index in einen temporären
Cleanroom. Änderungen, die nicht im Index stehen, sind absichtlich unsichtbar.
