# Offline-Entscheidungsbogen und Playground

Der Bogen ist eine nichtnormative Ansicht des validierten Candidate-Bundles und
das einzige Prüfartefakt der Bestätigung. Er speichert keine Auswahl. Der
Playground ist ein zweites, getrenntes Artefakt aus demselben Bundle zum freien
Kombinieren. Er ist nie Prüfartefakt.

## Ausgabe

- `render-sheet` schreibt ausschließlich unter
  `.brandkit/work/<run-id>/visual-decisions/sheet/`, `render-playground`
  ausschließlich unter `.brandkit/work/<run-id>/visual-decisions/playground/`.
- Beide rendern nur ein Bundle, das `validate_bundle` annimmt. Sie prüfen es
  unter der Ledger-Sperre, ohne Affinity-Receipts zu verbrauchen, und
  validieren jeden gebundenen Report erneut.
- Erzeuge `index.html`, `style.css`, lokale hashgebundene Assets und
  `manifest.json` mit deterministischen UTF-8/LF-Bytes. Assets heißen nach
  ihrem SHA-256.
- Der Bogen verwendet keine Scripts, Remote-URLs, CDNs, Analytics, Frames,
  Embeds, Base-Tags, Inline-Events, Data-URLs oder absendbaren Formulare.
- CSP sperrt standardmäßig alles und erlaubt Styles, Fonts und Bilder nur aus
  dem lokalen Verzeichnis. Jede Datei steht mit SHA-256 im Manifest.
- Owner-gelieferter freier Text — Familienname aus der `name`-Tabelle,
  Empfehlungsbegründung und Kandidaten-Label — wird abgewiesen, wenn er ein
  Zeichen trägt, das zwei Leser verschieden lesen: die C0-Steuerzeichen außer
  Tab, LF und CR — Form Feed U+000C eingeschlossen, weil `splitlines()`-Leser
  wie Terminal, `diff` und Log daran umbrechen und der Bogen für sie sonst mehr
  Zeilen hat als für einen Leser, der U+000A zählt —, die Zeilentrenner
  U+0085, U+2028 und U+2029 sowie die
  Bidi-Zeichen U+061C, U+200E, U+200F, U+202A–U+202E und U+2066–U+2069. Ein
  Bidi-Override kehrt sonst einen Namen optisch um, und der Owner bestätigte
  einen Namen, der anders aussieht als seine Bytes. Die Abweisung nennt das
  Feld, nicht die Seite, und geschieht dort, wo der Text eintritt. Dieselbe
  Klasse gilt für die HTML- und CSS-Mitglieder des Bogens und für die Mitglieder
  des Playgrounds.
- Kandidaten und Empfehlung stehen in getrennten Abschnitten. Es existiert
  kein `selected`, `checked`, `approved` oder versteckter Default.

## Kandidaten und Domänen

- Jede Domäne zeigt alle Kandidaten des Bundles. Jeder Kandidat liest seinen
  eigenen gebundenen Report. Es gibt keine Obergrenze, und die Seite wächst
  linear mit der Kandidatenzahl.
- Typografie und Code-Font müssen keinen gemeinsamen Font-Report teilen, Logo
  und Lockup keinen gemeinsamen Logo-Report.
- Eine Übersichtstabelle nennt je Domäne den Status im Visual Brief sowie die
  Zahl prüfbarer und blockierter Kandidaten.
- Hat eine offene Domäne keinen Kandidaten, heißt sie offen. Fehlt für Logo
  oder Lockup eine verfügbare Affinity-Capability, erscheint der benannte
  Blocker. Es gibt keine Ersatzgrafik.
- Eine im Brief nicht offene Domäne heißt „nicht offen". Blockierte Kandidaten
  erscheinen nur in der Blocker-Liste ihrer Domäne.

## Gleicher Vergleich

- Farbpaletten zeigen Rollen, Werte und die getesteten Paarungen als Tabelle
  aus dem Report, mit exakter Ratio und deterministischem Status.
- Serif, Sans und Mono zeigen byteidentische Specimens bei **gleicher
  gezeichneter x-Höhe** statt bei gleichem Schriftgrad. Bei gleichem Grad
  vergleicht der Bogen x-Höhen statt Schriften — gemessen reicht das Verhältnis
  x-Höhe zu Em über einen echten Bestand von 0,378 bis 0,732, bei den Textfonten
  darunter von 0,413 bis 0,550, also ein Drittel mehr Buchstabe bei derselben
  Angabe, und eine schmal laufende Schrift ist von einer klein laufenden nicht zu
  unterscheiden. In der Praxis würde der Grad angepasst; der Bogen passt ihn an
  und weist ihn aus.
- Die x-Höhe kommt aus `OS/2.sxHeight`; fehlt sie, ist sie 0 oder negativ, wird
  sie an der Glyphe `x` in Einheiten des Em gemessen. Fehlt beides, wird der
  Kandidat benannt abgewiesen — es gibt keinen Schätzwert und keine Namensliste.
- Die Metrik wird nicht unbesehen geglaubt. Zwei Schranken, beide aus Messung
  begründet und beide benannt abweisend: die x-Höhe muss **mindestens ein
  Viertel des Em** sein — über einen echten Bestand reicht das Verhältnis von
  0,378 bis 0,732, ein Viertel liegt also um den Faktor 1,5 unter der kleinsten
  echten Schrift —, und sie darf dem **Umriss ihrer eigenen Glyphe `x`** nicht um
  mehr als den Faktor vier widersprechen. Bricht die **angegebene** Metrik die
  Untergrenze, hält ihr eigener `x`-Umriss sie aber und widersprechen die beiden
  einander nicht um mehr als denselben Faktor, dann ist der Umriss die Quelle —
  derselbe Rückfall, den eine fehlende Metrik schon hat. Die Untergrenze weist
  offensichtlichen Unsinn ab, nicht eine Datei, deren gezeichnetes `x` klar über
  ihr liegt: gemessen über 848 echte Schnitte aus 439 Dateien ändert dieser
  Rückfall genau **eine** Datei, und jede weiterhin abgewiesene trägt kein
  lateinisches `x`. Dass dabei **ein Band** und nicht ein einzelner Randfall
  kippt, gehört zur Aussage: die wirksame Grenze ist `angegeben > Umriss/4`, also
  die Konsistenzschranke und nicht ein gepinnter Wert. Gegen einen 700er-Umriss
  kippen alle angegebenen Werte von 176 bis 249, gegen einen 500er die von 126
  bis 249, gegen einen 300er die von 76 bis 249. Für Dateien **ohne** brauchbares
  `x` wirkt die Untergrenze unverändert, und jede Abweisung bleibt benannt.
  Die reproduzierte 1/1000 bleibt abgewiesen, weil sie ihrem
  eigenen Umriss um den Faktor 700 widerspricht und den Rückfall gar nicht
  erreicht. Die zweite Schranke ist die einzige,
  die auch eine Metrik **über** ihrer eigenen Zeichnung sieht; sie ist weit
  gefasst, weil ein Überschwung eine Gestaltungsentscheidung ist und über
  denselben Bestand höchstens 15 % ausmacht.
- Gelesen wird die x-Höhe **je Rolle an der Instanz, die diese Rolle wirklich
  zeichnet**. Beide Stylesheets schalten die optische Größenachse ab
  (`font-optical-sizing: none`), damit der Browser nicht aus dem Grad eine
  `opsz`-Instanz wählt — der Grad ist genau das, was diese Normalisierung
  verändert. Damit bleibt das Gewicht die einzige bewegliche Achse, und weil eine
  Rolle nur **ein** Gewicht zeichnen darf, hat sie auch nur eine x-Höhe: Kopf-
  und Datenzelle einer Tabelle sind deshalb zwei Rollen und nicht eine.
- **Messbar heißt das:** je Rolle ist `Grad × x-Höhe(Rolle)/Em` über **alle**
  Kandidaten gleich, bis auf die Rundung des Grades auf ein Zehntausendstel rem.
  Beide Schranken folgen aus dieser Rundung und sind nicht geraten: ein einzelner
  Kandidat weicht vom Ziel um höchstens eine **halbe** Rundungsstufe mal seinem
  Verhältnis ab, und zwei Kandidaten liegen untereinander um höchstens eine
  **ganze** Stufe mal dem größten Verhältnis auseinander, weil sie in
  verschiedene Richtungen runden können. Bei einem 16-px-Wurzelgrad sind das
  Bruchteile eines Hundertstel Pixels. Der je Kandidat ausgewiesene Grad ist der
  gesetzte.
- **Und an der Glyphe gemessen heißt es:** dieselbe Gleichheit gilt für die
  Ink-Höhe des gezeichneten `x` — gemessen am Umriss der Instanz, die die Rolle
  setzt, nicht durch Rückrechnung des Grades. Offen bleibt dabei genau ein Term,
  und er gehört der Datei und nicht dem Bogen: der Abstand, den eine Schrift
  selbst zwischen ihrer angegebenen x-Höhe und dem Umriss ihres `x` hat. Über
  einen echten Bestand trifft das 3 von 14 Schriften mit höchstens 4 Einheiten
  von 1000, und die gezeichnete Spanne bleibt dann unter einem Prozent. Wer sie
  ganz schließen will, müsste den Umriss statt der Metrik zur Quelle machen — und
  damit die Aussage des Schriftgestalters gegen eine Messung tauschen.
- Der Zeilenabstand wird aus dem nominalen Grad der Rolle als **absolute**
  Länge gerechnet und ist deshalb für alle Kandidaten gleich. Von der
  Normalisierung unberührt bleiben außerdem Breiten, Laufweite, Inhalt und
  Kartenmaße.
- **Gleicher Anfang, nicht nur gleicher Schritt.** Je Rolle beginnt die erste
  Zeile bei allen Kandidaten auf **derselben Grundlinie**, gemessen ab
  Kartenoberkante. Ohne das säße die Grundlinie je Schrift an anderer Stelle in
  ihrer Zeile — der Schritt stimmte, der Anfang nicht, und der Versatz bliebe
  über den ganzen Block sichtbar. Die Kompensation folgt der Vertikalmetrik der
  Schrift an der gesetzten Instanz; welche Tabelle der Browser dafür heranzieht,
  ist **gemessen** und nicht angenommen: sechs Sondenschriften mit absichtlich
  widersprüchlichen `hhea`-, `OS/2`-Typo- und `usWin`-Werten wurden gesetzt und
  ihre gezeichnete Grundlinie zurückgelesen — die Engine folgte in allen sechs
  `hhea`, auch bei gesetztem `USE_TYPO_METRICS`. Der `lineGap` bewegt nur
  `line-height: normal` und geht nicht in die Teilung ein; er ist bei 275 von
  855 echten Schnitten ungleich null, eine Annahme hätte sie alle falsch
  kompensiert. Fehlt eine brauchbare Angabe, wird der Kandidat benannt
  abgewiesen. Verschoben wird die **Zeichnung**, nicht der Fluss, damit der
  absolute Schritt erhalten bleibt und auch Zeile *n* bei allen Kandidaten auf
  derselben Höhe liegt.
- **Messbar heißt das, und es sind zwei Schranken.** Die *ausgewiesene*
  Verschiebung ist ein **ganzes CSS-Pixel** — sie wird aus zwei ganzzahligen
  Grundlinien gebildet und liegt damit exakt auf dem rem-Raster; das ist die
  Zusage über das, was der Bogen schreibt. Die *gezeichnete* erste Grundlinie
  zweier Kandidaten liegt höchstens **ein ganzes CSS-Pixel** auseinander, bei dem
  Wurzelgrad, für den der Bogen geschrieben ist. Diese Schranke ist abgeleitet und
  nicht gesetzt, und sie bleibt unverändert.
- **Die Rundung der Engine wird mitgenommen, nicht als Restfehler erklärt**, und
  sie ist gemessen. Die Engine rundet Ober- und Unterlänge je auf ein **ganzes
  CSS-Pixel**, teilt mit diesen den Durchschuss und rundet die Grundlinie nach
  unten ab:

  ```
  Inhalt     = rund(Oberlänge) + rund(Unterlänge)
  Grundlinie = abrunden(halber Durchschuss + rund(Oberlänge))
  ```

  Gerundet wird in **ganzen CSS-Pixeln und nicht auf dem Gerätepixelraster**: die
  gezeichnete Grundlinie liegt bei einfacher und doppelter Pixeldichte an
  derselben Stelle, über 216 Blöcke mit gebrochenen Graden auf **0,0000 px**
  gemessen, mit einem Instrument, das gerasterte Tinte und Layout-Grundlinie auf
  0,0086 px gegeneinander hält. Weil beide Seiten ganze Pixel sind, ist die
  Verschiebung ein ganzes Pixel — und eine ganzzahlige Verschiebung kann eine
  Rolle, deren Kandidaten schon zusammenliegen, nicht mehr auseinanderziehen.
- **Das Ziel je Rolle gilt über die Flächen, die die Rolle wirklich zeichnen.**
  Überschriften setzt die Serifenlose, Fließtext, Liste, Tabelle und Zitat die
  Serife, Code die Dickengleiche; Ziffernzeilen zeigen Serife und Dickengleiche.
  Eine Fläche, die eine Rolle nie setzt, darf nicht bestimmen, wo diese Rolle
  sitzt — über alle Flächen gerechnet lag jede Überschrift ein ganzes Pixel tiefer
  als die zeichnenden Flächen es verlangen.
- **Die Messlage zur zweiten Schranke, offen ausgewiesen.** Gegen eine wirklich
  unkompensierte Fassung derselben Seiten, bei einfacher **und** doppelter
  Pixeldichte:

  ```
  specimen, body, table-cell, numerals   3,0000 px ohne  ->  0,0000 px mit
  heading, subheading, code              1,0000 px ohne  ->  0,0000 px mit
  table-head                             3,0000 px ohne  ->  1,0000 px mit
  ```

  Sieben von acht Rollen beginnen also **exakt** auf derselben Grundlinie, die
  achte innerhalb des zugesagten einen Pixels. **Warum die achte nicht null ist**,
  und das gehört zur Zusage: eine Fläche dieser Rolle trägt eine Unterlänge von
  3,500123 px, also 0,000123 px über einer Halbpixelgrenze. Dort ist die
  Zeilenkasten-Arithmetik der Engine aus der Datei **nicht** reproduzierbar —
  außerhalb der Halbpixelnähe trifft die Regel oben 390 von 390 gemessenen
  Blöcken, über alle 584 einschließlich der eigens dorthin gelegten trifft
  **keine** Regel alle. Die Schranke deckt diesen Fall, und sie wird deshalb
  nicht aufgeweicht.
- **Warum nicht nachgeführt wird.** Den Grad so zu verschieben, dass jede Fläche
  die Halbpixelnähe verlässt, kostet gezeichnete x-Höhe — **wie viel, hängt am
  gewählten Sicherheitsabstand**, und der gehört deshalb in die Begründung.
  Gemessen über die 19 Flächen dieses Bogens kostet der kleinste Abstand, der die
  irreproduzierbare Zone überhaupt verlässt (0,01 px), **0,023 px gezeichnete
  x-Höhe** (0,29 %); getrieben wird das von derselben Fläche, die den Restversatz
  trägt. Größere Abstände kosten mehr: 0,05 px kosten 0,114 px, 0,1 px kosten
  0,229 px, 0,25 px kosten 0,657 px. Dagegenzustellen ist die **gezeichnete**
  Streuung, die der Bogen heute ohnehin trägt — rund 0,08 px beziehungsweise
  0,89 % — und **nicht** die *deklarierte* x-Höhen-Schranke von rund 0,0008 px:
  die misst eine andere Größe, nämlich das, was der Bogen zusagt, statt das, was
  er zeichnet. Das Nachführen ist damit **nicht** um Größenordnungen teurer als
  der Bestand, sondern liegt in derselben Größenordnung. Es bleibt trotzdem
  ungetan, und zwar aus zwei anderen Gründen: es bewegt den **Grad** und damit
  eine ältere, primäre Zusage, und es verschiebt die Halbpixelzone lediglich,
  statt sie aufzuheben. Den Fall fail-closed abzuweisen kostet **heute keine
  einzige Datei** — über den Bestand liegt kein Wert *exakt* auf der
  Halbpixelgrenze, der nächste ist 0,000123 px davon entfernt —, und eine
  Abweisung, die nichts abweist, mildert auch nichts. Wirksam würde sie erst,
  wenn sie die **Nähe** verwürfe, und dann träfe sie gültige Schriften: bei
  0,01 px bereits sechs Flächen-Rollen-Paare dieses Bogens. Beides ist geprüft
  und **nicht** getan: ein Pixel Versatz in einer von acht Rollen ist der
  kleinere Eingriff als eine bewegte Zusage oder eine Abweisung aus
  Rundungsgründen. Wer die Grundlinien ganz exakt aufeinanderlegen will, müsste
  den absoluten Zeilenschritt aufgeben; das ist unten begründet und ausdrücklich
  nicht getan.
- Die modernen Mittel `text-box-trim` und `text-box-edge` greifen in der
  Zielumgebung, lösen den Fall aber **nicht**: gemessen legen sie die
  Grundlinien exakt aufeinander und nehmen dabei den Zeilenkasten mit
  (`trim-both` verkürzt eine dreizeilige Karte von 92,16 auf 13,13 px), und
  `trim-start` richtet nur die erste Zeile aus, während Zeile 2 und 3 um 4 und
  8 px auseinanderlaufen. Damit fiele genau die Eigenschaft, die „die Zeilen
  laufen nebeneinander" trägt. Sie sind deshalb nicht verwendet.
- Der ausgewiesene Grad ist eine Folge der Normalisierung und **keine
  Empfehlung**. Offen gelegt wird je Kandidat die x-Höhe, ihre Quelle, das
  Verhältnis und der resultierende Grad.
- Der Bogen führt die Ziffernsätze der Schrift vor, die er aus den
  Fonttabellen liest — `lnum`, `onum`, `pnum`, `tnum`, `zero`, mit derselben
  Strenge wie die Ligaturen: von einem Sprachsystem referenziert und mit
  mindestens einem Lookup. Je vorhandenem Feature erscheint eine Zeile
  `0123456789` in derselben Größe, benannt. Trägt eine Schrift keines, sagt der
  Bogen das ausdrücklich — „keine Information" darf nicht als „keine
  Varianten" gelesen werden. `tnum` steht zusätzlich in der Übersichtstabelle
  der Domäne, weil es über eine Zahlenspalte entscheidet. Für die
  Code-Font-Kandidaten gilt dasselbe.
- Der genannte Familienname ist der **typografische Familienname** der Schrift:
  `name`-ID 16, und nur wo die Schrift keinen angibt, ID 1. ID 1 benennt die
  Standardinstanz und trägt bei einer variablen Schrift, die unter 400 ruht,
  einen Schnitt im Namen — der Bogen nannte damit einen Schnitt, den er nicht
  setzt, denn die Specimens laufen mit 400. Unterfamilie, vollständiger Name und
  PostScript-Name benennen weiterhin die Instanz.
- Ein Font-Report, der noch den Namen der Standardinstanz pinnt, wird **benannt
  abgewiesen**, und das Bundle ist neu aufzubauen. Eine Migration im Bestand
  gibt es nicht: `metadata.extracted` wird byte-genau verglichen, der
  Reportdigest hängt im Kandidatenwert, dessen Digest im Candidate-Set und
  dessen Digest im Brief und in beiden Manifesten. Weil der Bogen beim ersten
  schlechten Report abbricht, ist er bis zum Neuaufbau ganz unrenderbar.
- Gewichte fordert der Bogen nur an, soweit die Schrift sie hat. Jede
  `@font-face`-Regel nennt den Bereich aus der Schrift selbst: bei einer
  variablen Schrift Minimum und Maximum der Achse `wght`, bei einer statischen
  ihr tatsächliches Gewicht. Überschrift, Unterüberschrift, Tabellenkopf und
  Codebeschriftung werden je Kandidat in diesen Bereich geklemmt. Der Bogen
  **zeigt** diesen Befund je Kandidat und Schnitt: ob die Datei eine variable
  Achse `wght` mit Minimum und Maximum hat oder ein starres Gewicht, und welche
  Gewichte der Bogen daraus tatsächlich setzt. Eine Schrift ohne Achse sagt das
  ausdrücklich — sie kann in Überschriften nicht zulegen. Beides steht auch in
  der Übersichtstabelle der Domäne, wie `tnum`, und im Playground gleichlautend.
  Die Angabe kommt aus den Fonttabellen, nie aus dem Namen, und aus derselben
  Ableitung wie die `@font-face`-Regel. Damit wird
  keine Schrift synthetisch verfettet, und eine Schrift ohne brauchbare
  Gewichtsangabe wird abgewiesen statt geraten. Gewicht und Grad sind damit die
  beiden Größen, die zwischen Kandidaten abweichen — beide, weil sie
  Eigenschaften der Datei sind und nicht des Bogens: das Gewicht aus dem
  Bereich der Schrift, der Grad aus ihrer x-Höhe. Alles andere ist gleich.
- Vorhandene Logo-/Lockup-Kandidaten zeigen SVG und Kleinformate von 16, 24, 32
  und 48 Pixeln auf hellem und dunklem Grund. Monochromfälle bleiben
  Human-Review.

## CI-Gesamteindruck je Kandidat

- Jeder Farbkandidat erscheint einmal angewandt: Überschrift, Fließtext,
  Primär- und Sekundäraktion, sichtbarer Fokus, hervorgehobene Kennzahl,
  Hinweis- und Fehlerzustand sowie eine Zahlentabelle. Aktionen und Fokus sind
  Abbildungen, keine Bedienelemente.
- Die Referenzbelegung der anderen Domänen ist ausgewiesen und keine
  Empfehlung. Farbansichten setzen generische Systemschriften, Überschriften
  serifenlos und Fließtext mit Serifen. Typografie- und Codeansichten stehen auf
  einer neutralen Graupalette des Bogens, die alle WCAG-Paarungen besteht. Keine
  Referenz stammt aus einem Kandidaten oder einer Empfehlung.
- Der Bogen enthält keine Kreuzung von Farbe, Typografie und Code. Freies
  Kombinieren übernimmt der Playground.

## Gesetzte Typografie und Codeblöcke

- Je Typografiekandidat stehen Überschrift und Unterüberschrift in der Sans,
  Absätze, Aufzählung, Tabelle und Zitat in der Serif. Inhalt und Maße sind in
  allen Kandidaten gleich.
- Je Code-Font-Kandidat steht derselbe Codeblock: Einrückung, Strings,
  Kommentare, Zahlen, Hash, Pfad, Log-Zeile, JSON, 0/O und 1/l/I, Operatoren und
  Pfeile. Der Block ist in der Karte vollständig lesbar: er bricht um, statt
  waagerecht zu scrollen, behält dabei Einrückung und Tabulatorweite und hat für
  alle Kandidaten dieselben Maße.
- Ligaturen werden aus der GSUB-Tabelle gelesen (`liga`, `calt`, von einem
  Sprachsystem referenziert und mit mindestens einem Lookup), nie aus dem
  Fontnamen. Hat der Font sie, steht derselbe Block mit und ohne Ligaturen in
  getrennten `pre`-Blöcken. Codeblöcke sind einfarbig.
- Zeichen des Beispieltexts, die ein Font nicht enthält, nennt der Bogen als
  Codepoints.

## Beispielinhalte

Der Visual Brief hat kein Feld für Beispieltexte. Bogen und Playground nutzen
deshalb einen generischen, markenfreien Standard für die `locale` des Briefs,
heute `de-DE`. Eine Locale ohne Standard wird abgewiesen. Kein Beispielinhalt
nennt eine Marke, eine Farbe oder eine Schriftfamilie.

## Dokumentform des Bogens

Die Form ist positiv aufgezählt, und alles außerhalb wird abgewiesen. Tabellen
sind strukturell geschlossen: `table` trägt optional `thead` und genau ein
explizites `tbody`, `tr` steht nur darin, `th` und `td` stehen nur in `tr` und
tragen keine Attribute. Direkt in `table`, `thead`, `tbody` und `tr` steht kein
Text. `p`, Überschriften und Zellen tragen nur `strong`, `code`, `span` und
`img`. `li` steht nur in `ul`. `pre` trägt nur Text und beginnt nicht mit einem
Zeilenumbruch. `span` und `code` tragen keine Klasse.

## Playground

- Zweck: Farbe, Typografie, Code und, falls vorhanden, Logo und Lockup frei
  kombinieren. Die Achsen sind die Domänen des Bundles mit prüfbaren
  Kandidaten. Jede Achse startet bei „Referenz, kein Kandidat".
- Er ist nicht normativ und so beschriftet. Er zeigt die Candidate-IDs der
  angezeigten Kombination, erzeugt aber keine Auswahl, keine Bestätigung und
  keinen Export.
- Er speichert nichts: kein `localStorage`, `sessionStorage`, IndexedDB,
  Cookie, Cache-API, Download oder Formular-Submit. Der Zustand lebt nur in der
  geöffneten Seite.
- CSP: `default-src 'none'; script-src 'self'; style-src 'self'; font-src
  'self'; img-src 'self'; connect-src 'none'; form-action 'none'; base-uri
  'none'; require-trusted-types-for 'script'`.
- Skripte sind nur `app.js`, eine feste Datei des Moduls, und `data.js` mit
  eingefrorenen, kanonischen JSON-Daten. Das DOM entsteht nur über
  `textContent` und eine geschlossene Attributmenge. Owner-geliefertes
  Material wie Familiennamen, Labels und Begründungen bleibt Text.
- Sein Codeblock ist wie der des Bogens vollständig lesbar: er bricht um, statt
  waagerecht zu scrollen, und behält dabei Einrückung und Tabulatorweite.
- Jede `@font-face`-Regel des Playgrounds nennt den Gewichtsbereich aus der
  Schrift selbst, und Überschrift, Unterüberschrift und Tabellenkopf werden je
  Kandidat in diesen Bereich geklemmt — genau wie im Bogen und aus derselben
  Funktion. Synthetisch verfettet wird dort nichts.
- Er normalisiert wie der Bogen: dieselbe x-Höhe je Rolle, dieselbe Quelle,
  derselbe absolute Zeilenabstand, aus denselben Ableitungen. Für dieselbe Datei
  setzt er in jeder gemeinsamen Rolle denselben Grad wie der Bogen, und er legt
  je Fläche x-Höhe, Quelle, Verhältnis, Grad und Gewichtsbereich offen, mit
  demselben Satz, dass der Grad keine Empfehlung ist. Er ist nicht normativ, der
  Bogen entscheidet.
- `validate --kind playground` prüft ihn, und `render-playground` validiert
  seine eigene Ausgabe.
- Öffne `playground/index.html` direkt oder über einen lokalen
  Loopback-Server. Die CSP wird dafür nicht gelockert.
- Nie Prüfartefakt: `prepare-visual-confirmation` bindet als `candidate_review`
  nur `sheet/index.html`, und nur, wenn `sheet/manifest.json` den Bogen für
  genau das gebundene Bundle, den Brief und die Affinity-Beobachtung ausweist.
  Alles andere, auch der Playground, eine Kopie davon oder ein veralteter Bogen,
  wird abgewiesen, bevor etwas verbraucht wird.

## Human-Review

Gib dem Owner den klickbaren `sheet/index.html`-Pfad. Den Playground darfst du
zusätzlich als nicht normative Hilfe nennen. Bitte um Prüfung des Bogens bei
320, 768 und 1440 CSS-Pixeln sowie 200 % Zoom. Die Review erfasst
Typografie-Lesbarkeit und Laufweite, deutsche Zeichen, Ziffern und Code,
responsiven Reflow und, falls vorhanden, Logo-Lesbarkeit bei 16, 24, 32 und 48
Pixeln sowie monochrom auf hell und dunkel.

Ändert sich danach das Bundle, der Brief oder die Affinity-Beobachtung, ist der
Bogen veraltet: entferne `sheet/`, rendere neu und zeige den neuen Bogen, bevor
der Owner auswählt. Die gefüllte Review nennt die aufgelösten Werte je Domäne,
bei Schriften die Familiennamen, damit ein Tausch auch dort sichtbar ist.

Ohne gepinnten Renderer bleiben Font-Load/Fallback, Pixel-Clipping,
Kleinstdetails und Screenshot-Reflow `not-run`; die Human-Review nennt ihre
eigene Autorität. Nach der Prüfung frage nur nach Candidate-IDs oder
`open|not-applicable`. Die konkrete Wertauflösung und zweite Bestätigungsrunde
gehören `brandkit-interview:prepare-visual-confirmation`, und gebunden wird nur
der Bogen.
