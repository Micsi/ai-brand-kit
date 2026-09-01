# Humanizer-DE toolchain lock

Dieser Ordner reproduziert den transformierten Skill-Inhalt und seine Python-
3.12-Wheel-Umgebung auf macOS/arm64. Er reproduziert **nicht** Homebrew oder das
Hunspell-Wörterbuch. Der Installer prüft bei Homebrew ausschließlich die
installierten Versionsstrings; nur die beiden installierten Wörterbuchdateien
werden bytegenau per SHA-256 geprüft. Er installiert keine beweglichen Formeln
und stoppt, wenn dieser externe Zustand vom Lock abweicht.

Release `5.22.2-brandkit.2` stammt aus der festgeschriebenen Git-Revision. Die
Transformation benennt den verschachtelten Bundle-Wrapper in
`SKILL.bundle.md` um und passt dessen Doctor-Pfad an. Damit liegt in der gesamten
Provider-Discovery-Zone genau eine `SKILL.md` mit dem kanonischen Namen
`humanizer-de`; es gibt keinen zweiten internen Discovery-Entry.
`content_sha256` pinnt den resultierenden geschlossenen Baum.

Die Python-Umgebung enthält genau 46 Distributionen: 45 gehashte Runtime-Wheels
aus `requirements-precise.lock.txt` und das von CPython `ensurepip` isoliert
eingebrachte `pip==26.0`. Vor `venv` werden Version und SHA-256 des gebündelten
Bootstrap-Wheels geprüft; nach Installation werden Versionsliste, Anzahl und
Gesamthash aller 46 installierten Distributionen geprüft. Es gibt kein
ungezähltes Build-Wheel.

Der Installer leitet die sichtbaren Roots aus den realen Codex-Marketplace-,
Claude-Plugin-/Marketplace- und Hermes-`external_dirs`-Registrierungen plus den
Provider-Standardroots ab. Er gleicht sie vor jeder Mutation mit der Lock-
Allowlist ab und blockiert unbekannte Roots. Das Inventar loggt nur Rootpfade
und Skill-Frontmatter, nie Konfigurationsinhalte oder Secrets.

```bash
toolchain/humanizer-de/install-from-lock --preflight-only
```

Die kontrollierte Migration validiert Quelle, Content, Symlinkbaum, Python-
Wheels, Full-Doctor und Stage-Smoke vor jeder Aktivierungsänderung. Bekannte
alte gleichnamige Claude-Bäume werden danach reversibel unter
`~/.agents/skill-archive/humanizer-de/` deaktiviert. Aktivierungslinks und
`~/.agents/.skill-lock.json` wechseln atomar; jeder Post-Smoke-Fehler stellt
Lockbytes, Linkziele, Legacy-Bäume und Marketplace-Registrierung wieder her,
entfernt den neuen Release und leere Archive und meldet alle Restorefehler
gesammelt. `--test-rollback` injiziert Fehler in mehreren Phasen derselben
Produktions-Transaktionsfunktion.

```bash
toolchain/humanizer-de/install-from-lock --migrate-legacy
toolchain/humanizer-de/install-from-lock --test-rollback
toolchain/humanizer-de/smoke
```

Der aktive Lock-Eintrag enthält `name`, `version`, `source_revision`,
`content_sha256` und den unveränderlichen Release-Pfad. Der Provider-Smoke nutzt
`codex debug prompt-input` und verursacht keinen Modellaufruf.
