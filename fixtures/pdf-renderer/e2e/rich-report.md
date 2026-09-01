---
schema_version: markdown-document.v1
document_id: golden-rich-report
document_type: report
title: Rich-Block-Nachweis
lang: de-DE
status: release
facts:
  - id: metric-value
    value: "42 %"
    source_ids: [source-one]
sources:
  - id: source-one
    locator: urn:brandkit:e2e:source-one
    sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
assets:
  - id: diagram
    path: assets/diagram.svg
    media_type: image/svg+xml
    sha256: 02d29918955c53625dd4bcab435ba0ed00d9e441a2280e5548f49ead899ccee8
    rights_id: diagram-rights
  - id: e2e-raster
    path: assets/raster.png
    media_type: image/png
    sha256: 9cd75a808e3b1be94516d46e3a7eab4c52d0d4623da1b779a5aee2528389d1cf
    rights_id: raster-rights
rights:
  - id: diagram-rights
    evidence_path: rights/diagram.rights.json
    evidence_sha256: 0aed6a8f0ccaf360c75114251d64889dad4e34160bfed839384b51f9f531e61a
  - id: raster-rights
    evidence_path: rights/raster.rights.json
    evidence_sha256: 7f3467e784cfa84990ce400e53db9ff028165868274c1cf5e0590c7b296c3a44
todos: []
---
# Rich-Block-Nachweis {#title}

## Kurzfassung {#executive-summary}

Die Matrix zeigt alle Präsentationsvarianten. Siehe @fig-flow-rich und @fig-dependencies-rich.

::: {.note #note-default title="Notiz Standard" appearance=default icon=true}
Lesbarer Inhalt der Notiz.
:::

::: {.note #note-simple title="Notiz Einfach" appearance=simple icon=true}
Lesbarer Inhalt der Notiz.
:::

::: {.note #note-minimal title="Eigener Titel ohne Icon" appearance=minimal icon=false}
Lesbarer Inhalt der Notiz.
:::

::: {.tip #tip-default title="Tipp Standard" appearance=default icon=true}
Lesbarer Inhalt des Tipps.
:::

::: {.tip #tip-simple title="Tipp Einfach" appearance=simple icon=true}
Lesbarer Inhalt des Tipps.
:::

::: {.tip #tip-minimal title="Tipp Minimal" appearance=minimal icon=true}
Lesbarer Inhalt des Tipps.
:::

::: {.warning #warning-default title="Warnung Standard" appearance=default icon=true}
Lesbarer Inhalt der Warnung.
:::

::: {.warning #warning-simple title="Warnung Einfach" appearance=simple icon=true}
Lesbarer Inhalt der Warnung.
:::

::: {.warning #warning-minimal title="Warnung Minimal" appearance=minimal icon=true}
Lesbarer Inhalt der Warnung.
:::

::: {.caution #caution-default title="Vorsicht Standard" appearance=default icon=true}
Lesbarer Inhalt des Vorsichtshinweises.
:::

::: {.caution #caution-simple title="Vorsicht Einfach" appearance=simple icon=true}
Lesbarer Inhalt des Vorsichtshinweises.
:::

::: {.caution #caution-minimal title="Vorsicht Minimal" appearance=minimal icon=true}
Lesbarer Inhalt des Vorsichtshinweises.
:::

::: {.important #important-default title="Wichtig Standard" appearance=default icon=true}
Lesbarer Inhalt des wichtigen Hinweises.
:::

::: {.important #important-simple title="Wichtig Einfach" appearance=simple icon=true}
Lesbarer Inhalt des wichtigen Hinweises.
:::

::: {.important #important-minimal title="Wichtig Minimal" appearance=minimal icon=true}
Lesbarer Inhalt des wichtigen Hinweises.
:::

::: page-break

## Befunde {#findings}

::: {.metric #metric-rich title="Messwert" label="Anteil" value_fact=metric-value source_id=source-one}
Der gebundene Wert lautet {{number:metric-value|42 %}}.[^source-one]
:::

::: {.quote #quote-rich title="Zitat" source_id=source-one}
Die belegte Aussage bleibt sichtbar mit ihrer Quelle verbunden.
:::

::: {.legal #legal-rich title="Rechtlicher Hinweis" source_id=source-one}
Der Herkunftshinweis darf in keiner Ausgabe verschwinden.
:::

| Block | Status |
| --- | --- |
| Callout | sichtbar |
| Tabelle | sichtbar |

![Neutrale Vektorgrafik](asset:diagram "Vektorgrafik mit sichtbarer Beschriftung"){#image-vector rights=diagram-rights}

![Neutrale Rastergrafik](asset:e2e-raster "Rastergrafik mit sichtbarer Beschriftung"){#image-raster rights=raster-rights}

```{mermaid #fig-flow-rich title="Ablauf" description="Zwei verbundene neutrale Schritte" source_id=source-one}
flowchart TD
  A[Start] --> B[Prüfung]
```

```{graphviz #fig-dependencies-rich title="Abhängigkeiten" description="Drei gerichtete neutrale Komponenten" source_id=source-one}
digraph G {
  rankdir=LR;
  input [label="Eingabe"];
  check [label="Prüfung"];
  output [label="Ausgabe"];
  input -> check;
  check -> output;
}
```

## Fazit {#conclusion}

PDF und HTML tragen dieselben semantischen Marker und sichtbaren Herkunftsfelder. Die Nachweise @fig-flow-rich und @fig-dependencies-rich bleiben dabei nummeriert und direkt erreichbar.

[^source-one]: Unveränderlicher Quellenhinweis zur neutralen Fixture.
