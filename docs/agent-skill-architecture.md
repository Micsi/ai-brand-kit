# Agentenübergreifende Skill-Architektur

Status: vorgeschlagener Architekturvertrag, Version 1.0.0. Er wird erst nach
Human Review zur Installationsgrundlage. Die maschinenlesbare Source of Truth ist
[`agent-skill-architecture.v1.json`](../agent-skill-architecture.v1.json); dieses
Dokument erklärt die Entscheidungen. Änderungen an Regeln erfolgen zuerst im
Manifest und müssen den Validator bestehen.

## Ziel und Invarianten

Claude, Codex und Hermes konsumieren dieselbe Skill-Implementierung. Eine
Installation hat genau einen kanonischen Inhaltspfad; agentenspezifische Pfade
sind native Discovery-Pfade, Konfigurationsreferenzen oder Symlinks auf diesen
Inhalt. Getrennte Agent-Kopien und getrennte Laufzeitumgebungen desselben Skills
sind ungültig.

Globale Workflow-Engines enthalten keine Marke. Brand-Pakete enthalten Daten
und Assets, aber keine Engine-Forks. Kommerzielle Projekte enthalten ihre
Aktivierung, Inputs, Overrides, Zwischenstände und Ausgaben. Damit bleiben
Engine-Update, Marken-Update und Projektarbeit getrennte Vorgänge.

## Lokal belegter Stand

Die Pfade wurden am 26. August 2026 read-only geprüft; Konfigurationswerte und
geheime Inhalte wurden nicht ausgegeben.

| Agent | lokal geprüft | belegte Discovery |
|---|---|---|
| Claude Code 2.1.223 | `~/.claude/skills/` existiert; einzelne Einträge sind bereits Symlinks | global `~/.claude/skills/`, projektlokal `<repo>/.claude/skills/` |
| Codex CLI 0.144.3 | `~/.agents/skills/` und `~/.codex/skills/` existieren | global `~/.agents/skills/`, projektlokal `<repo>/.agents/skills/`; die `.codex`-Ablage bleibt für systemeigene Inhalte bestehen |
| Hermes 0.20.5 | `~/.hermes/config.yaml` enthält `skills.external_dirs` mit `~/.agents/skills/` | global lokaler Profil-Root plus konfigurierte externe Roots; projektlokal `.hermes/skills` und `.agents/skills` nur für explizit vertraute Git-Roots |

Der Hermes-CLI- und Laufzeitcode belegt außerdem: Trust wird unter
`skills.trusted_project_dirs` gespeichert, die nächste `.git`-Wurzel ist die
Projektidentität und projektlokale Skills gewinnen bei Namensgleichheit gegen
Profil-Skills. Dieser Vertrag nutzt diese Override-Regel bewusst nicht:
verschiedene Quellen mit demselben Skill-Namen sind schon vor dem Start ein
Fehler.

Im Sidecar `/Volumes/Daten/Projekte/BrandKit` ist der LLMWiki-Bridge-Standard
bereits aktiv: `.claude/skills/` enthält ausgewählte Links, `.codex/skills`
zeigt auf diese Fassade. Im Code-Repository selbst existierte bei der Prüfung
noch keiner der drei projektlokalen Skill-Roots. Diese Runde installiert oder
vertraut keinen davon.

## Kanonische Roots und Bridges

### Global

Kanonische Quelle ist `~/.agents/skills/<skill-name>`. Ein aktivierter Eintrag
kann auf einen unveränderlichen Release unter
`~/.agents/skill-store/<skill-name>/<version>` zeigen. Das ist Versionsspeicher,
keine Agent-Kopie.

| Agent | Discovery/Bridge | Regel |
|---|---|---|
| Claude | `~/.claude/skills/<skill-name>` → `~/.agents/skills/<skill-name>` | pro freigegebenem Skill ein Symlink; der gesamte Claude-Root wird nicht ersetzt |
| Codex | native Discovery von `~/.agents/skills/` | keine zusätzliche globale Bridge; so entstehen keine doppelten Discovery-Aliasse neben systemeigenen Codex-Skills |
| Hermes | `skills.external_dirs` enthält `~/.agents/skills/` | Konfigurationsreferenz, keine Kopie; Änderung erst nach Installationsfreigabe |

Der relative globale Claude-Link lautet aus dem Eintragsordner typischerweise
`../../.agents/skills/<skill-name>`. Die Installation muss Ziel und Realpath
vor Aktivierung prüfen; ein Beispielpfad ist kein blind auszuführender Befehl.

### Projektlokal: Standardprofil

Kanonische Quelle ist `<repo>/.agents/skills/`. Sie enthält nur wirklich
projektgebundene Skills oder eine bewusst isolierte Entwicklungsversion. Ein
global sichtbarer Skill wird nicht zusätzlich projektlokal eingetragen.

| Agent | Pfad | Regel |
|---|---|---|
| Claude | `<repo>/.claude/skills` → `../.agents/skills` | genau ein Root-Symlink; vom Elternpfad `<repo>/.claude/` aufgelöst endet er innerhalb desselben Repositories |
| Codex | `<repo>/.agents/skills` | native Discovery, keine zweite `.codex`-Bridge |
| Hermes | `<repo>/.agents/skills` | native Discovery erst nach Review und `hermes skills trust <repo>` für exakt diesen Git-Root |

Hermes-Trust ist eine Ausführungserlaubnis, keine Qualitätsprüfung. Vor dem
Trust werden Quelle, Symlink-Ziele, Scripts, Hooks, Abhängigkeiten und
Netzwerkverhalten geprüft. Trust eines Elternordners ersetzt den exakten
Repo-Root nicht. Ein Klon oder Worktree an einem neuen Realpath benötigt eine
neue Entscheidung. Bei Entzug wird derselbe exakte Root unvertraut gemacht.

Claude- und Codex-Workspace-Trust bleiben zusätzliche Agent-Sicherheitsgrenzen;
die Skill-Architektur umgeht sie nicht.

### LLMWiki-Profil: ausdrückliche Kompatibilitätsregel

LLMWiki-Projekte haben einen älteren, weiterhin verbindlichen Kontextstandard:
`.claude/skills/` ist dort die kanonische **Projektfassade**, und
`.codex/skills` zeigt auf `../.claude/skills`. Dieser Begriff beschreibt die
projektlokale Auswahl, nicht eine zweite globale physische Quelle.

Für Hermes wird in genau diesem Profil zusätzlich
`.agents/skills` → `../.claude/skills` gesetzt. So sehen alle drei Agenten
dieselben ausgewählten Einträge. Jeder Eintrag unter `.claude/skills/` bleibt
ein einzelner Link auf seine eigentliche kanonische Quelle; es gibt keine
Dateikopie. Damit gilt:

- allgemeine und kommerzielle Projekte: `.agents/skills` ist Projektquelle;
- ausdrücklich als LLMWiki deklarierte Projekte: `.claude/skills` bleibt die
  bestehende Projektfassade, `.agents` und `.codex` sind Bridges;
- global bleibt in beiden Fällen `~/.agents/skills` die agentenübergreifende
  Quelle für allgemeine Skills.

Eine Migration bestehender LLMWiki-Repositories ist damit nicht Teil dieses
Vertrags. Ein Projekt darf nur eines der beiden Profile deklarieren.

## Namensidentität, Trust und Präzedenz

Skill-Identität ist ausschließlich `name` im Frontmatter, nicht der
Verzeichnisname. Vor jeder Aktivierung inventarisiert der Installer alle für
den Agenten sichtbaren Roots und löst Symlinks auf.

1. Mehrere Pfade mit demselben Namen und demselben Realpath sind Bridges und
   werden als ein Skill behandelt.
2. Mehrere Realpaths mit demselben Namen sind ein Fehler. Installation,
   Update, Trust und Session-Smoke-Test stoppen mit beiden Pfaden im Bericht.
3. Ein projektlokaler Fork darf einen globalen Skill nicht still überschreiben.
   Für Entwicklung wird die globale Aktivierung zuerst nach Human Review
   deaktiviert oder der Fork eindeutig umbenannt.
4. Reservierte oder agenteneigene Skills werden nicht überschrieben.
5. Laufzeitpräzedenz eines Providers ist nur Diagnoseinformation und niemals
   Konfliktlösung dieses Vertrags.

Symlink-Ziele müssen in der freigegebenen Installationsmatrix stehen und nach
Realpath-Auflösung in einem erlaubten Quell- oder Release-Root liegen. Tote,
zyklische oder aus dem Allowlist-Scope ausbrechende Links blockieren. Der
Preflight behandelt jeden sichtbaren Skill als geschlossenen Dateibaum: Er
prüft den Realpath von `SKILL.md` und inventarisiert rekursiv jeden Symlink im
Paket, einschließlich Scripts, Hooks und Referenzen. Erst danach wird Inhalt
gelesen. Aliasse sind nur innerhalb der expliziten Projekt-, Quell- oder
unveränderlichen Release-Roots zulässig.

Das Frontmatter wird vollständig als YAML geparst. Ein öffnendes und ein
schließendes `---`, genau ein skalarer String-Schlüssel `name`, eindeutige
Mapping-Schlüssel und syntaktisch gültiges YAML sind Pflicht. Ein früh
gefundener Name ohne geschlossenen Header ist keine Identität.

## Scope-Matrix

Die vollständige maschinenlesbare Liste steht im Manifest. `global` bedeutet
agentenübergreifend wiederverwendbare Engine, `project-local` bedeutet an den
Projektkontext gebundener Workflow, `optional` bedeutet keine Installation ohne
separate Auswahl und Pin.

| Skill oder kuratierte Einheit | Status | Scope | Begründung |
|---|---|---|---|
| `brandkit-setup` | vorhanden | global | providerneutrale Fähigkeitsprüfung |
| `brandkit-interview` | vorhanden | global | allgemeiner Interview-Workflow; erzeugte Markendaten bleiben im Zielprojekt |
| `brandkit-build` | vorhanden | global | allgemeine Build-/Validierungsengine; konsumiert eine explizite Marke |
| `brandkit-spacing` | vorhanden | global | read-only Engine für gehashte Spacing-Inventur, Token-/Ausnahme-Zuordnung und Rollenbefunde; UI-Mutation bleibt bei Impeccable |
| `brandkit-publish` | vorhanden | global | allgemeine Ausgabepipeline; Outputs bleiben projektlokal |
| `humanizer-de` | geplant | global | eine gemeinsame Engine und Python-Laufzeit; Systemwerkzeuge bleiben explizite externe Abhängigkeiten |
| `brand-voice-pdf` | aktiv | global | markenfreie Orchestrierung unter `~/.agents/skill-store/brand-voice-pdf/1.0.0`; gepinnter Offline-Renderer unter `~/.agents/runtime-store/brandkit-pdf-renderer/1.0.0` |
| `brandkit-impeccable` | aktiv | global | Designvertrag-Adapter ohne Impeccable-Quellen oder eingebaute Marke |
| `impeccable` | aktiv, extern gepinnt | optional | read-only Release `~/.agents/skill-store/impeccable/4.1.1+fcd7622.brandkit6`; committed Tree-Pin, eine atomare aktive Quelle, Claude/Codex/Hermes-Aliase, HMAC-/zeitgebundene Capability-Gates und validierter Rollback |
| `federated-wiki-ops` | extern vorhanden | project-local | nur in LLMWiki-Projekten über deren Kompatibilitätsprofil |
| kuratierter externer Skill-Satz | geplant | optional | jeder einzelne Skill wird außerhalb dieses Repos bewertet, gepinnt und freigegeben |
| projektgebundene Web-/Such-Helfer | geplant | project-local | nur dort aktiv, wo Projektvoraussetzungen und Owner eindeutig sind |

Die letzte Zeile ist eine Scope-Klasse, kein Sammel-Skill zur pauschalen
Installation. Sobald konkrete Namen kuratiert sind, ersetzt je ein eigener
Manifest-Eintrag diese Klasse. Ein Skill mit Überschneidung zu einem bereits
installierten Workflow braucht vor Aktivierung einen eindeutigen Owner.

`brandkit-spacing` wird unter genau diesem Frontmatter-Namen materialisiert.
Codex liest den kanonischen globalen Eintrag, Claude dessen Symlink und Hermes
denselben konfigurierten Root. Ein projektlokaler gleichnamiger Eintrag auf
demselben Realpath ist nur ein Alias; ein anderer Realpath ist keine
Praezedenzregel, sondern eine fail-closed Kollision. Damit kann kein Provider
unbemerkt eine abweichende Spacing-Definition bevorzugen.

Seine Kollisionsgrenze wird aus dem Requesttext ermittelt: reine Inventur und
Rollenpruefung gehoeren `brandkit-spacing`; UI-Fix und visuelle
Rhythmuskorrektur gehoeren `brandkit-impeccable -> impeccable layout`;
`DESIGN.md`, Brand-Paket und PDF behalten ihre jeweiligen Owner. Gemischte
Requests folgen dem mutierenden oder normativen Owner. Eine mitgelieferte
Intent-Bezeichnung hat keinen Einfluss auf die Route.

## Engine, Brand-Paket und Projekt

| Zone | Darf enthalten | Darf nicht enthalten |
|---|---|---|
| globale Engine | Skill-Anweisungen, providerneutrale Scripts, Schemas, gemeinsame Laufzeit, generische Fixtures | Markenassets, Kundeninputs, Projektausgaben, Secrets |
| privates Brand-Paket | Stimme, Design-Tokens, Fonts, Logos, Dokumenttypen, Rechteangaben, Brand-ID und Version | Renderer-Fork, Kundeninhalt, Projektausgaben, Secrets |
| kommerzielles Projekt | explizite Brand-Aktivierung und Pins, Inputs, genehmigte Overrides, Zwischenstände, Outputs | stiller Marken-Fallback, projektübergreifende Output-Pfade |

Ein Run liest `.brandkit/project.json`, verlangt genau eine Brand-ID und eine
auflösbare Version und hält die benutzten Pins in `.brandkit/skill-lock.json`.
Fehlende, mehrdeutige oder nicht zum Lock passende Auflösung stoppt vor dem
ersten Schreibschritt.

Arbeitsdaten liegen unter
`.brandkit/work/<brand-id>/<run-id>`, Ausgaben unter
`output/<brand-id>/<run-id>`, Caches unter
`.brandkit/cache/<project-root-realpath-hash>/<brand-id>/<brand-version>/<engine-version>`.
Damit kann weder eine gleichnamige Marke noch dieselbe Engine in einem anderen
Projekt fremde Zwischenstände wiederverwenden. Secrets bleiben außerhalb von
Skill, Brand-Paket und Lock und werden nur über die jeweilige Laufzeit injiziert.

Der read-only Projektzustandscheck verlangt im exakten Git-Root eine
Aktivierungsdatei mit genau einer Marke, einen Lock mit eindeutigen Pins und je
Pin `name`, `version`, `source_locator`, `content_sha256` plus explizitem
unveränderlichem Release-Pfad. `source_locator` hat geschlossen die Form
`snapshot:<normalisierter-relativpfad>@sha256:<64-kleinbuchstabige-Hexzeichen>`;
sein eingebetteter Hash muss mit `content_sha256` übereinstimmen. Damit wird kein
Git-Commit behauptet, der die gelockten Bytes nicht enthält. Branches, Kanäle und
dirty Worktree-Zustände als angebliche Commit-Pins sind ungültig. Fehlende Felder,
Hashabweichungen, mehrdeutige Pins oder nicht materialisierte Work-, Cache- und
Output-Roots blockieren vor dem Run.

## Installation, Update und Rollback

Vor jeder Mutation wird eine Installationsmatrix zur Freigabe vorgelegt. Sie
nennt mindestens Skill-Name, Scope, kanonische Quelle, Quellrevision,
Content-SHA-256, Zielversion, Agent-Discovery, Symlink-/Konfigurationsänderung,
Abhängigkeiten, Trust-Auswirkung, Kollisionsergebnis und Rollback-Ziel.

Jede aktive Version hat im Lock die Felder `name`, `version`,
`source_locator` und `content_sha256`. Bewegliche Versionen sind ungültig;
Quellen akzeptieren ausschließlich den im Manifest festgelegten, mit dem
Content-Hash übereinstimmenden Snapshot-Locator.
Ein Update läuft als kontrollierter Wechsel:

1. neuen Release in einen neuen unveränderlichen Versionspfad beziehen;
2. Hash, Skill-Metadaten, Scripts, Abhängigkeiten und Lizenz prüfen;
3. Diff gegen den aktiven Pin sowie Discovery-/Kollisionsbericht erzeugen;
4. Validator und agentenspezifische Discovery-Smoke-Tests ohne Mutation der
   aktiven Links ausführen;
5. Matrix und Diff freigeben lassen;
6. Lock schreiben und den kanonischen Aktivierungslink atomar umschalten;
7. Claude, Codex und Hermes separat auf Discovery und Skill-Aufruf prüfen.

Der vorherige Release und Lock bleiben erhalten. Rollback stellt den alten Lock
und Symlink-Ziel atomar wieder her und wiederholt die drei Discovery-Tests. Ein
Brand-Paket-Update verändert nur den Pin des freigebenden Projekts; globale
Engine-Updates und andere Projekte bleiben unberührt.

## Abgrenzung zu Golden Documents und End-to-End-QA (Teil 12)

Dieser Architekturvertrag ist eine **Voraussetzung**, aber ausdrücklich kein
Erfüllungsnachweis für die Benchmark-Gates M8, U3 oder X4. Deren Nachweise
gehören zu Teil 12 „Golden Documents und End-to-End-QA“: ein versioniertes
`provider-manifest.v1.json`, die beiden Schemas für Manifest und normalisierte
Payload, deterministische Doppel-Builds, Installations-/Discovery-Smokes und
der bytegleiche normalisierte Rückvergleich für Codex, Claude, Cursor, Gemini
und GitHub. Bis Teil 12 diese Artefakte und Tests liefert, bleiben M8, U3 und X4
offen. Das vorliegende Architekturmanifest ersetzt keinen dieser Nachweise.

## Abnahme und Validator

Der Vertrag ist strukturell grün, wenn der CI-fähige Runner in einer sauberen,
temporären Python-Umgebung den Validator, Architekturtests, Brand-Paket-FS-Tests,
Scanner-Regressions und den globalen Commercial-Scan erfolgreich ausführt. Die
Abhängigkeiten werden ausschließlich als Wheels mit `--require-hashes` und
`--only-binary=:all:` aus dem bytegenauen Lock installiert:

```bash
scripts/check_agent_skill_architecture.sh
```

Der Runner installiert ausschließlich die exakten Versionen aus
`requirements-architecture-ci.lock.txt` und führt danach
`python scripts/validate_agent_skill_architecture.py` sowie
`python -m unittest tests/test_agent_skill_architecture.py` aus.

Der Fresh Runner ist geschlossen auf **CPython 3.12 unter macOS arm64**
festgelegt. Die einzelnen Hashes in `requirements-architecture-ci.lock.txt`
binden genau die dafür aufgelösten Wheels; Python 3.13 und 3.14 sind für diesen
Runner nicht unterstützt und dürfen nicht stillschweigend andere Wheel-Dateien
unter demselben Lock auswählen.

Der Validator validiert das Manifest zuerst vollständig als JSON Schema Draft
2020-12 und prüft danach die drei Providerverträge, beide kanonischen Roots, das
LLMWiki-Profil, Fail-closed-Kollisionen, Trust-Gates, vollständige Erfassung der
vorhandenen Repo-Skills, Scope-Werte, Inhaltszonen, Mehrprojekt-Cache-Key und
Versions-/Rollback-Invarianten. Die Regressionstests beweisen zusätzlich, dass
Namensduplikate, Engine-Inhalt mit Marke, eine doppelte Codex-Bridge,
bewegliche Versionen und mehrdeutige Markenauflösung rot werden.

Mit `--preflight-root`, `--profile`, `--global-root`,
`--claude-global-root` und `--allow-root` führt dieselbe CLI einen rein lesenden
Installations-Preflight auf echten Dateisystempfaden aus. Er verlangt die
profilgerechten Bridges, löst Roots und Einträge strikt per Realpath auf, liest
geschlossenes YAML-`frontmatter.name`, inventarisiert den gesamten Skill-Baum,
blockiert tote, zyklische oder nicht erlaubte Links und
unterscheidet erlaubte Aliasse desselben Realpaths von gleichnamigen Skills aus
verschiedenen Quellen. Der rekursive Paketwalker verfolgt den aktuellen
Realpath-Ahnenstack: Links auf einen Ahnen (auch über mehrere Verzeichnisse)
schlagen fehl, Aliasse auf bereits vollständig inventarisierte, nicht-ahnige
Release-Unterbäume bleiben zulässig. Hermes muss den kanonischen globalen Root in
`external_dirs` sehen und exakt den nächsten Git-Root des Projekts als vertraut
deklarieren.

`--hermes-mode planned` nimmt die geplanten Werte ausschließlich über
`--hermes-external-dir` und `--hermes-trusted-project-dir` entgegen. Diese
synthetischen Argumente sind kein Istbeleg. `--hermes-mode actual` verlangt
stattdessen `--hermes-state-file` mit einer expliziten Hermes-YAML-Konfiguration
oder provider-nativen JSON-Ausgabe, liest sie read-only und lehnt synthetische
Trust-Argumente ab. Fehlerberichte enthalten nur normalisierte Pfade, niemals
sonstige Konfigurationswerte oder Secrets.

`--validate-project-state <git-root> --release-root <root>` prüft Aktivierung,
Pins, Hashes, Release-/Brand-Pfade und die materialisierten isolierten Roots.
Die FS-Regressionen verwenden zwei echte temporäre Git-Repositories und
beweisen zusätzlich atomaren Linkwechsel, absichtlichen Smoke-Fehler sowie
Rollback auf bytegleichen Lock und ursprünglichen Release-Realpath.

Der Validator ersetzt keine Human-Freigabe und keine provider-nativen
Discovery-Smokes aus Teil 12. Die Installation bleibt bis zur Prüfung dieser
Matrix gesperrt.
