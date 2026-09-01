# Brand Kit Skills

Vier portable Kern-Skills führen von der technischen Bestandsaufnahme bis zu
markengerechten Webseiten, PDFs und optional bearbeiteten Bildern. Der
optionale `brandkit-spacing`-Adapter prüft Abstands-Tokens und Rollen.

## Ergebnis

Das Brand Kit enthält verbindliche Regeln, Farben und Schriften als ausführbare
CSS-Tokens, eine Wordmark und Referenzflächen. Eine kurze `AGENTS.md` sorgt
dafür, dass kompatible Coding Agents die Regeln im Projektkontext laden.

## Ablauf

| Skill | Wann | Ergebnis |
|---|---|---|
| `brandkit-setup` | einmal | Prüft die lokal verfügbaren Fähigkeiten |
| `brandkit-interview` | einmal je Marke | Erarbeitet die ausführliche Rohfassung |
| `brandkit-build` | einmal je Marke | Verdichtet die Rohfassung zum benutzbaren Kit |
| `brandkit-publish` | wiederholt | Baut Webseiten, PDFs und optional Bilder |

## Verwendung

Stelle die benötigten Verzeichnisse unter `skills/` dem verwendeten Agenten gemäß
dessen lokaler Skill-Konvention bereit. Ist kein Skill-System verfügbar, lies
die jeweilige `SKILL.md` und folge ihr direkt. Beginne mit
`brandkit-setup`; jeder Skill verweist am Ende auf den nächsten Schritt.

Die Skills antworten in der Sprache der Anfrage.

Für Abstands-Tokens und die Prüfung visueller Gruppierung ergänzt
`brandkit-spacing` den Kernablauf, ohne dessen Sequenz zu verlängern. Er besitzt
nur Inventar, Skala, semantische Rollen und Audit. `brandkit-build` bleibt Owner
normativer Brand-Pakete, `brandkit-impeccable` von `DESIGN.md`, Impeccable der
allgemeinen UI-Gestaltung und der PDF-Workflow des Dokumentlayouts.

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
