# Impeccable Adapter v1

Normativ sind `impeccable-adapter.v1.json`, die geschlossenen Schemas und
`scripts/impeccable_adapter.py`. Der Adapter installiert keinen beweglichen
Download und kopiert keinen Upstream-Code in den BrandKit-Adapter-Skill.

## Pin und vollständige Inventur

Der dauerhafte Checkout `external-sources/impeccable` steht detached und sauber
auf Commit `fcd7622cd2d8e2b09344ba8ede9fcac82cec4e70`, Commit-Tree
`e5721fadc3ef0f9b2386b6f4330ba8490dd451bc` und kanonischem Skill-Tree
`0024cd8b54c3e23531bdeaed20409b4ac569e2cc`; seine Push-URL ist `DISABLED`.
Es gibt keinen unbelegten GitHub-Archivhash. Stattdessen pinnt der Vertrag die
155 benötigten Git-Blobs (kanonischer Skill plus LICENSE und NOTICE) durch den
reproduzierbaren, lokal berechneten Pfad/OID-Set-Hash
`1becaf85df5b7de153dffac775c3f4a5cc6b6029e7260b8dc162b8babc3351cc`.

`impeccable-upstream-inventory.v1.json` listet jeden Referenz- und Skriptpfad;
`verify` vergleicht diese Listen exakt mit dem Checkout und prüft zusätzlich 17 generierte Providerausgaben einschließlich
`.hermes`, 23 Routen, 39 Referenzen, 108 Skriptdateien, vier Rollen plus
`openai.yaml` und Hook-Manifeste für Claude, Codex, Cursor, GitHub Copilot und
Grok. Die maschinenlesbare Runtime-Inventur umfasst Hooks, Browser, Live,
Bildgenerierung, Staleness/Doctor und Pin-Administration.

```bash
python scripts/impeccable_adapter.py verify
```

## Release-Lifecycle und Discovery

`materialize` erzeugt deterministisch
`~/.agents/skill-store/impeccable/4.1.1+fcd7622.brandkit6`, legt LICENSE und NOTICE bei,
setzt den Release read-only und vergleicht ihn mit dem unabhängig committed
Tree-Hash. Ein lokal neu berechneter Marker kann diesen Pin nicht ersetzen. Vor der
Aktivierung prüft ein Kollisionsgate `frontmatter.name`; ein abweichender
Realpath wird nicht überschrieben. Alle Provideraliases zeigen auf den einzigen
atomar umgeschalteten Pointer `~/.agents/skill-store/impeccable/current`:

- Codex: `~/.agents/skills/impeccable`
- Claude: `~/.claude/skills/impeccable`
- Hermes: `~/.hermes/skills/impeccable`

Der vorherige Pointer und die angelegten Aliase stehen in einem HMAC-versiegelten
Aktivierungsjournal. Pointer, State und Provideraliases dürfen nur die
geschlossenen Pfade unter Home beziehungsweise dem Release-Store verwenden;
reguläre oder externe `current`-Objekte stoppen. `rollback` validiert den
gesamten State vor der ersten Mutation.

```bash
python scripts/impeccable_adapter.py materialize
python scripts/impeccable_adapter.py activate
python scripts/impeccable_adapter.py status
python scripts/impeccable_adapter.py rollback
```

## Technisches Default-off

Der aktive Provider-Skill enthält einen verbindlichen Capability-Gate-Hinweis;
alle inventarisierten serverseitigen Entry-Points für Hooks, Update/Pin, Browser,
Live, Bildgenerierung und Network sind Wrapper oder import-sichere Direktgates.
Die Live-Assemble-/Serve-/Inject-Routen werden vor der Bundle-Erzeugung erneut
geprüft. Die drei Browserdateien bleiben echte IIFEs. Ein opaker, maximal 30
Sekunden gültiger Session-Identifier bindet den öffentlichen Bundle-Kontext
serverseitig an den getrennten Live-Routentoken (`route_token`) und Grant-Nonce
(`grant_nonce`), Origin, Projekt-Realpath und -Commit, Brand-Hash, Grant- und
Key-ID, Releasehash, exakt `browser`, `live`, `network` und Issued-/Expiry-Zeit.
Vor jedem exportierten Browser-Helper und jedem über die gekapselten
DOM-/Window-Objekte wirkenden Zugriff validiert der lokale Server diese Bindung
und den aktuellen Grant erneut; ohne Erfolg erfolgt keine Mutation. HMAC-/Private
Keys werden nie in das Bundle geschrieben. Die Routen enden mit Exit 77,
solange kein HMAC-authentischer und zeitlich gültiger Grant vorliegt. Er bindet
Grant-ID, Nonce, einmaliges Approval-Receipt, Projekt-Realpath und -Commit, Brand-Pin, Upstream-Commit,
adaptierten Release-Hash sowie exakt die erlaubten Capabilities. Das 0600-Secret
liegt außerhalb des Projekts. Revocation- und Receipt-Consumption-Ledger liegen
ebenfalls dort und sind HMAC-versiegelt; zurückkopierte Grants bleiben widerrufen.
`rotate-secret` führt alte und neue Key-ID, exakte Ziel-Allowlist, Preimages,
Staging und Hashes in einem mit dem alten Schlüssel authentisierten, geschlossenen
Recovery-Journal. Der Secret-Swap ist der Commit-Punkt; synchrone Fehler rollen
sofort zurück, Prozessabbrüche werden vor `status`, `rollback`, `revoke`, `approve`
oder `grant` deterministisch zurück- beziehungsweise vorwärtsgerollt. Die Rotation
blockiert bei einem offenen Document-Journal.
Bildgenerierung braucht zusätzlich `network`; der Live-Server braucht `live`
und `network`. `.impeccable/config.json` hält Hook und Updatecheck aus. Kein
Provider ist Pflicht.

Diese Kontrolle schützt gegen erfundene oder manipulierte Bundle-Kontexte,
fremde Projekte, Ablauf, Capability-/Token-Änderung und serverseitige Revocation.
Sie kann keine vollständig kompromittierte Browser-Realm isolieren: bereits
gleichprivilegiertes Same-Origin-JavaScript kann Plattform-APIs vor dem Laden
ersetzen oder gültige Bearer-Daten benutzen. Diese Browsergrenze ist kein Ersatz
für Origin-Isolation, CSP und die Vertrauensprüfung der Seite selbst.

```bash
receipt=$(python scripts/impeccable_adapter.py approve --project . \
  --capability live --capability browser --capability network \
  --evidence "current explicit user instruction" --ttl 3600 | jq -r .receipt_id)
python scripts/impeccable_adapter.py grant --project . --approval-receipt "$receipt"
python scripts/impeccable_adapter.py revoke --project .
python scripts/impeccable_adapter.py rotate-secret
```

`approve` ist die technische Materialisierung der aktuellen ausdrücklichen
User-Anordnung als konkretes Receipt-Evidence. Freier Text beim `grant` allein
ist keine Freigabe und wird nicht akzeptiert.

## Projektkontext

Produktfakten kommen ausschließlich aus der geschlossenen, projektlokalen
`.brandkit/impeccable-product.json`. Fehlt sie oder ist `platform` nicht exakt
`web`, `ios`, `android` oder `adaptive`, blockiert der Sync. `PRODUCT.md` trägt
exakt `<!-- impeccable:product-schema 1 -->` und die aktuellen Upstream-
Abschnitte; es enthält keine Runtime-TODOs.

Nur ein explizites Brand-Paketfeld `shared_visual_tokens` darf portable
Primitives liefern. Das v1-Paket enthält dieses Feld derzeit nicht, daher wird
kein `pdf.*`-Wert als shared hochgestuft. Normative Primitives leben nur im
DESIGN.md-Frontmatter. Zusätzlich verlangt der geschlossene Produktinput fünf
bis zehn repräsentative Komponenten mit ID, Name, Kategorie, Beschreibung und
konkreten Tokens; fehlende oder doppelte Primitive blockieren statt Inhalte zu
erfinden. Verschachtelte Typography-/Component-Tokens werden als
echte YAML-Maps serialisiert. Die v2-Sidecar folgt dem gepinnten Upstream-Schema
mit deterministischem `generatedAt`; die `brandkitAdapter`-Extension ergänzt
nur Source-, Asset- und Rights-Pointer und dupliziert keine Primitives. Sidecar,
Policy, Product-Input, Capability-Grant und UI-Inventur
haben geschlossene lokale Schemas; die Artefaktnamen sind im Adapter-Schema
geschlossen.

`brandkit-spacing` darf `shared_visual_tokens.spacing` und ein gehashtes
Spacing-Inventar read-only pruefen oder Rollenbefunde und Patch-Vorschlaege
liefern. Jeder UI-Patch geht an `brandkit-impeccable -> impeccable layout`.
Der Adapter bleibt alleiniger Generator und Pruefer von `DESIGN.md`; der
Spacing-Skill schreibt weder UI-Code noch dieses Artefakt noch PDF- oder
Dokumenttyp-Tokens.

## Existing UI und Transaktion

Sobald UI-relevante CSS/HTML/Astro/Server-Template-/JS/TS/Tailwind-, Swift-, Kotlin/Compose- oder
Flutter-Dateien vorhanden sind, ist eine vollständige
`.brandkit/ui-token-inventory.json` Pflicht. Sie bindet jede reguläre, confined
Datei, alle gescannten Token-Lexeme samt Position, Gitroot, Projekt-Commit,
Brand-Pin und einen Inventar-Selbsthash. Symlinks sind nicht vertrauenswürdig;
übliche JS-/TS-Properties für Font, unitlose Spacing-/Gap-Werte, Color-Schemes,
Radius, Shadow und Motion werden semantisch klassifiziert; tokenartige unbekannte
Properties erzwingen Review. Kommentare, Prosa und Stringliterale sind keine
Property-Token. Eine leere Tokenliste gilt nur bei tatsächlich leerem Scannergebnis. Eine fehlende, unvollständige
oder stale Inventur endet mit Exit 3, atomarem Review und ohne Reverse-Sync.

Alle Zielpfadkomponenten werden per `lstat` und `realpath` gegen den exakten
Gitroot geprüft. Der Mehrdatei-Sync führt Preimage-Hashes, Stagingdateien und
ein Recovery-Journal. Geschlossenes Schema, exakte Artefakt-Allowlist,
Projekt-/Commit-/Brand-/Transaction-/Run-Bindung, confined Backup-/Stage-Pfade,
Pre-/Postimage-Hashes und HMAC werden vor der ersten Recovery-Mutation geprüft.
Ein echter Prozessabbruch nach partiellen Replaces wird bytegenau recovered;
fremde oder manipulierte Journale mutieren nichts.
