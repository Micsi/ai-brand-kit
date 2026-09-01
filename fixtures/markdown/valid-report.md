---
schema_version: markdown-document.v1
document_id: fixture-report
document_type: report
title: Vertragsbericht
lang: de-DE
status: review
facts:
  - id: metric-value
    value: "42 %"
    source_ids: [source-one]
sources:
  - id: source-one
    locator: urn:brandkit:fixture:source-one
    sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
assets:
  - id: diagram
    path: assets/diagram.svg
    media_type: image/svg+xml
    sha256: 4b6886f23a350748f85bf941b7a0568aa7375bf806e354842c5bd61f3c053e6b
    rights_id: diagram-rights
rights:
  - id: diagram-rights
    evidence_path: rights/diagram.rights.json
    evidence_sha256: 0aed6a8f0ccaf360c75114251d64889dad4e34160bfed839384b51f9f531e61a
todos: []
---
# Vertragsbericht {#title}

## Kurzfassung {#executive-summary}

Natürliche Prosa darf der Humanizer ändern. Die [Befunde](#findings) bleiben verlinkt.

::: {.note #note-main title="Hinweis"}
Ein neutraler Hinweis.
:::

::: {.important #important-main title="Wichtig"}
Eine wichtige Einordnung.
:::

::: {.warning #warning-main title="Warnung"}
Eine klar benannte Warnung.
:::

::: {.metric #metric-main title="Messwert" label="Anteil" value_fact=metric-value source_id=source-one}
Der gebundene Wert lautet {{number:metric-value|42 %}}.[^source-one]
:::

::: {.quote #quote-main title="Zitat" source_id=source-one}
Die belegte Aussage bleibt als Zitat geschützt.
:::

::: {.legal #legal-main title="Rechtlicher Hinweis" source_id=source-one}
Die Quellenbindung ist verpflichtend.
:::

::: {.verbatim #verbatim-main title="Originaltext"}
Dieser Inhalt bleibt bytegenau: 123 <tag> urn:fixture:verbatim
:::

## Umfang {#scope}

- Erster Punkt
  - Unterpunkt
    1. Tiefster erlaubter Punkt
- Zweiter Punkt

| Rolle | Wert |
| --- | --- |
| Anteil | {{fact:metric-value|42 %}} |

![Neutrales Rechteck](asset:diagram "Eine neutrale graue Testfläche"){#image-diagram rights=diagram-rights}

```{mermaid #flow-main title="Ablauf" description="Zwei verbundene neutrale Schritte"}
flowchart TD
  A[Start] --> B[Ende]
```

::: page-break

## Befunde {#findings}

Die Fakten-ID {{id:fixture-fact|metric-value}} und die Quelle {{source:source-one|Quelle A}} bleiben unverändert.

1. Geordneter Befund
2. Weiterer Befund

## Fazit {#conclusion}

Das Fazit enthält ausschließlich natürliche Prosa.

[^source-one]: Unveränderlicher Quellenhinweis zur neutralen Fixture.
