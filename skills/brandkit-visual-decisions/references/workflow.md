# Visual-decisions playbook

## Zustände

Interview klassifiziert `color`, `typography`, `code-font`, `logo` und
`lockup` als `confirmed`, `open` oder `not-applicable` und markiert ihre
Erforderlichkeit. Ohne offene Domäne überspringe diesen Skill; er erzeugt dann
weder Brief noch Kandidaten noch Receipt.

Bei offenen Domänen gilt:

1. Validiere den Visual Brief und jede referenzierte Datei über
   `scripts/brandkit_visual_decisions.py`; Pfad-, Symlink-, Hash-, Schema-,
   Provenienz-, Rechte- oder Pinfehler blockieren.
2. Rufe `brandkit-affinity` auf und protokolliere vor jedem Vergleichsartefakt
   dessen vollständige read-only Affinity-MCP-/Plugin-Inventur. Nur eine erfolgreiche Suche darf
   `unavailable` liefern; Berechtigungs- oder Identitätsfehler bleiben
   `unknown|ambiguous`.
3. Löse Impeccable ausschließlich run-lokal aus dem in
   `impeccable-adapter.v1.json` gepinnten Checkout auf. Verwende nur
   `colorize`, `typeset`, `adapt` und `critique`; binde geladene Bytes,
   Provider/Modell, Rohresultat und Output-Hashes. Schreibe und aktiviere nichts
   unter `~/.agents`, `~/.claude` oder `~/.hermes`. Modelloutput bleibt bis zu
   den Domain-Prüfungen untrusted. `extract` gehört erst nach bestätigtem
   Interview-Handoff, Build, Pin und post-package Impeccable-Sync.
4. Farbe: semantic roles und WCAG-Kontrast werden deterministisch aus den final
   serialisierten sRGB-Werten berechnet. Jede vorgesehene Text-/Flächen-,
   `on-*`-, Focus- und Statuspaarung muss ihren AA-Schwellwert erfüllen.
5. Typografie: Serif, Sans und Mono verwenden exakt denselben Specimen-Text,
   denselben Inhalt und dieselben Kartenmaße. Der Schriftgrad ist ausdrücklich
   **nicht** gleich: er folgt je Kandidat aus der x-Höhe, damit die gezeichneten
   Buchstaben gleich groß sind; der Zeilenabstand bleibt absolut gleich, und die
   erste Grundlinie liegt je Rolle bei allen Kandidaten an derselben Stelle.
   Der Bogen weist je Schrift aus, welche Gewichte sie hergibt und welche er
   setzt. Prüfe
   lokale Fontbytes, Metadaten, Gewichte/Achsen, deutsche Zeichen, Ziffern/Code,
   Embed-Bits, Lizenz und Source-Pin.
6. Logo/Lockup: Owner-SVG darf read-only statisch geprüft werden. Neue Vektoren
   oder Ableitungen entstehen ausschließlich über `brandkit-affinity`, einen
   erneut erkannten, gepinnten Affinity-Server und einen separaten Operations-Receipt. Bei
   `unavailable` bleibt diese Domäne offen und der konkrete Aktivierungsweg
   wird angezeigt; kein ImageGen-/Raster-/SVG-Fallback.
7. Erzeuge das geschlossene Bundle und den unselektierten Bogen. Empfehlungen
   referenzieren Candidate-IDs, besitzen aber kein Auswahlfeld.

## Owner-Übergang

Zeige Bogenpfad, Kandidaten-IDs, Blocker und die vorgeschriebenen Human-Checks.
Bitte um Candidate-IDs oder `open|not-applicable`, nicht um JSON-Arbeit.

Die Antwort ist nur ein Auswahlvorschlag. Übergib ausschließlich IDs/outcomes
an die bestehende Interview-Operation `prepare-visual-confirmation`. Interview
löst die konkreten Werte aus dem gebundenen Bundle auf, rendert eine gefüllte
V2-Review und zeigt sie im Chat. Erst wenn der Owner genau diese Review
bestätigt, folgen das bestehende `confirm` und `consume`. Bei Korrektur beginne
erneut bei `prepare-visual-confirmation`; `brand-raw.md` bleibt bis zum
erfolgreichen Consume bytegleich.

Was das für Affinity-authored Kandidaten genau heißt: die Operation prüft die
Auswahl vollständig, **bevor** sie die Mutation-Receipts verbraucht. Sie baut
dazu den fertigen Handoff oberhalb des Verbrauchs und entscheidet jede Regel,
die über ihn entscheidbar ist, gegen die Bytes, die sie zu schreiben im Begriff
ist — auch die Regeln, die beim späteren Rücklesen noch einmal gelten. Eine
`prepare-visual-confirmation`, die **wegen der übergebenen Eingabe** abgewiesen
wird, lässt deshalb das externe Replay-Ledger byte-identisch, hinterlässt weder
`handoff.json` noch `filled-review.md` noch eine Quellendatei, der Bogen bleibt
erneut renderbar, und der korrigierte Aufruf läuft mit denselben Receipts durch.

Hinter dem Verbrauch steht nicht nur ein einzelner Wächter, und der frühere
Wortlaut dieses Abschnitts hat genau das behauptet. Richtig ist: hinter dem
Verbrauch schreibt die Operation ihre Artefakte und liest den Handoff von der
Platte zurück, und dabei gelten dieselben Regeln noch einmal — diesmal über den
Plattenzustand *nach* den Schreibvorgängen statt über die gestagten Bytes davor.
Diese zweite Entscheidung kann von der ersten nur abweichen, wenn sich zwischen
beiden etwas an den gelesenen Dateien geändert hat. Damit **deine** Eingabe das
nicht bewirken kann, gilt oberhalb des Verbrauchs eine eigene Regel: kein
Inventarmitglied des Handoffs darf auf einem Pfad liegen, den die Operation
selbst schreibt, es sei denn mit genau denselben Bytes (`visual handoff
inventory member lies on a path this operation overwrites`). Ohne sie genügte
es, die eigene Chat-Aussage oder die Candidate-Review `handoff.json` oder
`filled-review.md` zu nennen: die Operation hätte die Datei nach dem Verbrauch
überschrieben und ihr eigenes Mitglied nicht wiedererkannt.

Bleibt der eine Wächter im Rumpf, der die beiden Validierungspässe miteinander
vergleicht (`candidate bundle changed between verification and commit`). Er und
das Rücklesen können nur auslösen, wenn das Bundle beziehungsweise der
Workspace **während** der laufenden Operation von außen verändert wird — keine
Eingabe dieses Aufrufs erreicht sie.

Verbraucht wird erst, wenn die Operation gelingt — danach ist eine weitere
`prepare` für dieselben Receipts abgewiesen (`Affinity mutation receipt was
already consumed`), und eine Korrektur ab diesem Punkt braucht neu ausgestellte
Receipts und einen neuen Bogen.

## Ergebnislogik

- `candidates-ready`: alle offenen erforderlichen Domänen haben eligible
  Kandidaten; nach Confirm/Consume kann Build package-ready werden.
- `partial`: bestätigbare Kandidaten bleiben nutzbar, blockierte Domänen offen.
  Eine erforderliche offene Domäne erlaubt höchstens `package-draft-only`.
- `blocked`: keine eligible Auswahl für die erforderliche offene Domäne.
- Eine optionale offene Domäne kann im Paket fehlen, blockiert als Owner-TODO
  aber Publish, bis der Owner sie entscheidet oder `not-applicable` setzt.

Statische Prüfer, gepinnte Renderer und Human-Review sind getrennte Autoritäten.
Ein fehlender Renderer wird sichtbar `not-run`; ein Mensch kann subjektive
Lesbarkeit/Eignung bestätigen, aber keinen technischen Renderer-PASS fingieren.
