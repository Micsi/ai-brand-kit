# Brand Kit Skills

Fünf portable Kern-Skills führen von der technischen Bestandsaufnahme bis zu
markengerechten Webseiten, PDFs und optional bearbeiteten Bildern. Optionale
Adapter ergänzen den Ablauf: `brandkit-spacing` prüft Abstands-Tokens und
Rollen; `brandkit-affinity` führt kontrollierte Vektorarbeit in Affinity
Studio aus.

Für eine vertiefte CI-Discovery startet der Owner optional
`$brandkit-interview`. Nach einem Bundle-/Bridge-Check ruft dieser Skill direkt
über mehrere Fragerunden `grilling` auf und persistiert die offene Frontier.
Erst nach ihrem Abschluss folgt `domain-modeling`; den explicit-only Upstream-Wrapper
ruft er nie auf. BrandKit installiert oder aktiviert dabei nichts. Alle Discovery-
Dokumente bleiben unter `.brandkit/work/<run-id>/discovery`, und nur bestätigte
Entscheidungen gelangen nach einer lesbaren Chat-Review und expliziten
Bestätigung über einen intern erzeugten, owner-signierten Receipt in die
Rohfassung. Der Owner muss kein Handoff-JSON öffnen und keinen Confirm-Befehl
ausführen. Der ausführbare Ablauf mit Bundle-Check, Review und Consume
steht im Discovery-Handoff-Playbook; ohne vorhandenes Bündel bleibt das native
Interview verfügbar. Die projektlokale Claude-/Codex-Aktivierung mit sofort
ausführbaren Befehlen ist ein separater bewusster Schritt im Handoff-Playbook.

## Ergebnis

Das Brand Kit enthält verbindliche Regeln, Farben und Schriften als ausführbare
CSS-Tokens, eine Wordmark und Referenzflächen. Eine kurze `AGENTS.md` sorgt
dafür, dass kompatible Coding Agents die Regeln im Projektkontext laden.

## Ablauf

| Skill | Wann | Ergebnis |
|---|---|---|
| `brandkit-setup` | einmal | Prüft die lokal verfügbaren Fähigkeiten |
| `brandkit-interview` | einmal je Marke | Erarbeitet die ausführliche Rohfassung |
| `brandkit-visual-decisions` | bei offenen visuellen Domänen | Erzeugt geprüfte, nichtnormative Auswahlkandidaten |
| `brandkit-affinity` | bei Affinity-Prüfung oder Vektorarbeit | Prüft den lokalen Server und erzeugt Receipt-gebundene Staging-Artefakte |
| `brandkit-build` | einmal je Marke | Verdichtet die Rohfassung zum benutzbaren Kit |
| `brandkit-publish` | wiederholt | Baut Webseiten, PDFs und optional Bilder |

## Verwendung

Stelle die benötigten Verzeichnisse unter `skills/` dem verwendeten Agenten gemäß
dessen lokaler Skill-Konvention bereit. Ist kein Skill-System verfügbar, lies
die jeweilige `SKILL.md` und folge ihr direkt. Beginne mit
`brandkit-setup`; jeder Skill verweist am Ende auf den nächsten Schritt.

Die Skills antworten in der Sprache der Anfrage.

Ohne das optionale Discovery-Bündel führt `brandkit-interview` denselben
vollständigen nativen Fragenpfad aus. Ein fehlender oder uneinheitlich
installierter externer Skill ist deshalb kein CI-Blocker.

Für Abstands-Tokens und die Prüfung visueller Gruppierung ergänzt
`brandkit-spacing` den Kernablauf, ohne dessen Sequenz zu verlängern. Er besitzt
nur Inventar, Skala, semantische Rollen und Audit. `brandkit-build` bleibt Owner
normativer Brand-Pakete, `brandkit-impeccable` von `DESIGN.md`, Impeccable der
allgemeinen UI-Gestaltung und der PDF-Workflow des Dokumentlayouts.

Für Logo-, Lockup-, Favicon- und andere Vektorarbeiten ergänzt
`brandkit-affinity` den Visual-Decisions-Ablauf. Claude verbindet sich über die
projektlokale `.mcp.json`; Codex wird über `bin/codex-brandkit` gestartet und
erhält den gepinnten SSE-zu-stdio-Bridge-Prozess nur für dieses Projekt. Eine
read-only Observation ist verpflichtend. Mutationen bleiben auf
`.brandkit/work/<run-id>/visual-decisions/staging/<document-id>` beschränkt und
benötigen einen projekt- und rungebundenen Operations-Receipt.

Offene Farb-, Typografie-, Code-Font-, Logo- und Lockup-Entscheidungen laufen
zwischen Interview und Build über `brandkit-visual-decisions`. Der Skill zeigt
nur geprüfte, nichtnormative Kandidaten. Der Owner wählt im Chat IDs oder
`open`/`not-applicable`; Interview löst die Werte intern auf, zeigt eine
gefüllte Review und übernimmt erst nach einer zweiten expliziten Bestätigung
den Receipt-gebundenen Block. Erforderliche offene Domänen ergeben höchstens
`package-draft-only`.

Der optionale Impeccable-Adapter materialisiert ausschließlich den Release aus
`impeccable-adapter.v1.json#/adapted_release/marker/version`. Der Repository-Check
liest diese kanonische Version direkt und verhindert abweichende README-Kopien;
Claude, Codex und Hermes entdecken denselben aktivierten Release-Realpath.

## Voraussetzungen

`brandkit-setup` prüft nur die Fähigkeiten, die für den gewünschten Auftrag
benötigt werden:

| Fähigkeit | Benötigt für |
|---|---|
| Dateizugriff und Textbearbeitung | Brand Kit |
| Git | optionale Versionsgeschichte |
| Python mit Pillow | optionale Bildkennzeichnung und Web-Optimierung |
| HTML-zu-PDF-Werkzeug | PDFs aus HTML |
| Metadaten-Werkzeug | optionale maschinenlesbare Bildkennzeichnung |
| verfügbares Bildwerkzeug | optionale Bildgenerierung |

Fehlende optionale Fähigkeiten blockieren die übrigen Ergebnisse nicht. Eine
externe Einrichtung erfolgt nur auf ausdrücklichen Auftrag der Person.

## Bilder

Bildgenerierung ist optional. Wenn ein passendes Werkzeug bereits verfügbar
ist, kann `brandkit-publish` es mit den Vorgaben aus `BRAND.md` verwenden. Ist
keines verfügbar, arbeitet der Skill mit bereitgestellten oder vorhandenen
Bildern weiter oder liefert das Ergebnis ohne Bild.

Das mitgelieferte Skript kann ein vorhandenes Bild sichtbar kennzeichnen,
zuschneiden und fürs Web verkleinern:

```bash
python skills/brandkit-publish/scripts/ai_label.py roh.png \
  -o hero.jpg --width 1120 --aspect 16:9 --iptc
```

Die Hinweise zur Kennzeichnung sind allgemeine technische Informationen und
kein Rechtsrat.

## Repository-Check

Der eng definierte Check durchsucht alle versionierten und noch nicht
versionierten Textdateien nach den früher verwendeten Werbe-, Vermittlungs- und
fest verdrahteten Werkzeugbegriffen. Externe URLs sind bis auf den benötigten
IPTC-Vokabularwert ebenfalls ausgeschlossen:

```bash
python scripts/check_commercial_content.py
```

## Lizenz und Urheberschaft

MIT, siehe [LICENSE](LICENSE). Copyright 2026 Arnold Oberleiter.
