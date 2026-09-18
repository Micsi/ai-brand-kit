# Receipt-gebundener Vektorworkflow

Dieser Pfad führt genau eine deklarierte **Mutation** aus. Eine weitere Ausgabe
oder Variante benötigt einen eigenen Receipt.

## Ablauf

1. **Autorität.** Validiere Visual Brief, Observation und Receipt gegen ihre
   Schemas. Vergleiche Operation, Tool-Schema, Projekt, Run, Dokument,
   Stagingroot, Eingabehashes, Zeiten und Nonce bytegenau. Der Schritt ist
   abgeschlossen, wenn alle drei Artefakte aktuell sind und genau dieselbe
   Operation beschreiben.
2. **Pfadgrenze.** Löse jede Ein- und Ausgabe über ihre Dateiidentität auf.
   Belege, dass das eine Ausgabeziel im gebundenen
   `.brandkit/work/<run-id>/visual-decisions/staging/<document-id>/` liegt und
   keine Quelle oder versiegelte Interviewdatei aliasiert. Der Schritt ist
   abgeschlossen, wenn die Schreibmenge exakt ein Ziel enthält.
3. **Verbrauch.** Verbrauche den Receipt atomar im externen Replay-Ledger. Der
   Schritt ist abgeschlossen, wenn dieselbe Nonce in parallelen und späteren
   Aufrufen fail-closed abgewiesen wird.
4. **Ausführung.** Lies `preamble` und nur die für die deklarierte Operation
   benötigten SDK-Themen. Führe `create-logo-variant`,
   `create-lockup-variant`, `create-icon-variant` oder `export-svg` mit den
   Constraints aus dem gebundenen Visual Brief aus. `create-icon-variant`
   erzeugt die Datei genau eines Variantenwerts eines Icons, etwa `licht-an`;
   jeder weitere Wert derselben Achse braucht seinen eigenen Receipt. Der
   Schritt ist abgeschlossen, wenn Affinity genau das deklarierte editierbare
   oder exportierte Ziel erzeugt hat.
5. **Evidenz.** Protokolliere Ausgabehash, Maße, Farbraum und Exportparameter.
   Prüfe die für das Ziel anwendbaren Eigenschaften: SVG-Struktur,
   Kleinformat, Monochromdarstellung, Hell-/Dunkelgrund, Innenräume und Lockup.
   Der Schritt ist abgeschlossen, wenn technische Prüfungen und Human-Review
   eindeutig `pass|fail|not-run` ausweisen und auf denselben Ausgabehash zeigen.
6. **Unverändertheit.** Vergleiche alle gebundenen Quellen mit ihren
   Eingabehashes und inventarisiere die Staging-Schreibmenge erneut. Der Schritt
   ist abgeschlossen, wenn Quellen bytegleich und ausschließlich deklarierte
   Staging-Dateien neu oder verändert sind.
7. **Handoff.** Übergib Kandidaten-ID, Artefakt und Evidenz an
   `brandkit-visual-decisions`; Empfehlung und Owner-Auswahl bleiben getrennte
   Felder. Der Schritt ist abgeschlossen, wenn der unselektierte
   Entscheidungsbogen den exakten Ausgabehash bindet.

## Gate

Die Mutation ist erfolgreich, wenn genau ein deklarierter Output existiert,
Receipt und Evidenz dessen Bytes binden, alle Quellen unverändert sind und der
Handoff keine Auswahl vorwegnimmt. Bei jedem anderen Zustand bleibt der
Receipt verbraucht, der fehlgeschlagene Output gesperrt und die visuelle Domäne
offen; ein erneuter Versuch benötigt einen neuen Receipt.
