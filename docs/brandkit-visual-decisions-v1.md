# BrandKit Visual Decisions v1

`brandkit-visual-decisions` schließt die nichtnormative Strecke zwischen
Interview und Build. Der Skill erzeugt vergleichbare Farb-, Typografie- und —
bei freigegebenem Affinity Studio MCP — Logo-/Lockup-Kandidaten. Er schreibt
weder `brand-raw.md` noch ein Brand Package.

## Anwenderablauf

1. `$brandkit-interview` erfasst belegte Aussagen. Nur offene visuelle Domänen
   erzeugen einen Visual Brief.
2. `$brandkit-visual-decisions` prüft zuerst Affinity Studio MCP read-only,
   erzeugt Kandidaten und zeigt einen responsiven Offline-Bogen ohne Auswahl.
3. Der Owner nennt Candidate-IDs oder `open|not-applicable` im Chat.
4. `brandkit-interview:prepare-visual-confirmation` löst die IDs selbst aus dem
   hashgebundenen Bundle auf und zeigt eine gefüllte Review mit exakten Werten.
5. Der Owner bestätigt diese Review oder korrigiert sie. Erst der bestehende
   Ablauf `confirm -> consume` übernimmt die bestätigten Werte normativ in
   `brand-raw.md`; danach darf `brandkit-build` sie materialisieren.

Der Owner muss kein JSON lesen. Empfehlung, Auswahlvorschlag und bestätigte
Entscheidung sind getrennte Artefakte. Eine Empfehlung ist niemals
vorselektiert.

## Fähigkeiten und Grenzen

Impeccable wird nur aus dem geprüften Pin in `impeccable-adapter.v1.json`
run-lokal aufgelöst. Der Prebuild-Pfad verwendet `colorize`, `typeset`, `adapt`
und `critique`; er installiert oder aktiviert nichts global. `extract` bleibt
nach Owner-Bestätigung, Build, Pin und post-package Sync.

Deterministisch prüfbar sind unter anderem Schemas, Hashes, Pfade, WCAG-Math,
Fontmetadaten/-rechte/-glyphen, sichere SVG-Struktur und Offline-HTML. Ohne
einen unveränderlich gepinnten Renderer bleiben tatsächlicher Font-Fallback,
Pixel-Clipping, Kleinstdetails und Screenshot-Reflow sichtbar `not-run` und
werden nicht als technischer PASS behauptet. Subjektive Lesbarkeit und
Eignung benötigen eine an Bundle und Bogen gebundene Human-Review.

Die aktuelle read-only Prüfung fand keinen aufrufbaren Affinity-Studio-MCP-
Server. Deshalb dürfen Palette, Typografie und statischer Bogen entstehen;
neue Logo-, Lockup-, Favicon- oder Monochrom-Assets bleiben blockiert. Es gibt
keinen stillen ImageGen-, Raster- oder generischen SVG-Ersatz. Der sichere
nächste Schritt ist, Affinity Studios offiziellen lokalen AI Connector zu
aktivieren, ihn session-/projektgebunden einzutragen, Identität/Version/Tool-
Schemas zunächst read-only zu prüfen und erst danach einen separaten,
dokumentgebundenen Mutations-Grant zu erteilen.

## Quellenfreigabe

`visual-decision-sources.v1.json` ist der release-gebundene
Quellenentscheid. Ausgewählt ist ausschließlich der bereits gepinnte
Impeccable-Release. Herkunft, Commit-/Tree-/Blob-Pins, Apache-2.0-Lizenz,
Capability-Fläche, bekannte Sicherheitsgrenzen und unabhängige
Reputationssignale sind geschlossen gebunden. Andere untersuchte Skills
bleiben abgelehnt, nicht installiert und ohne Runtime-Route. Reputation dient
nur der Vorauswahl; lokale Byte-, Lizenz- und Sicherheitsprüfung bleibt das
Freigabe-Gate.

## Build-Ausgänge

- Keine offene visuelle Domäne: Visual Decisions wird übersprungen.
- Alle erforderlichen Werte bestätigt und konsumiert: Build kann
  `package-ready` erreichen.
- Erforderlicher Logo-/Lockup-Wert wegen fehlendem Affinity offen: Build darf
  höchstens `package-draft-only` liefern und keinen Platzhalter normativ machen.
- Jede offene Owner-TODO blockiert Publish.

Der versiegelte Run `.runtime/brandkit-ci-smoke/.brandkit/work/first-ci` ist
kein Test- oder Ausgabeziel. Neue Smokes verwenden einen neuen Run-Namen.
