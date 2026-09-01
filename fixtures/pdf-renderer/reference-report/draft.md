---
schema_version: markdown-document.v1
document_id: reference-report
document_type: report
title: Klarer Projektbericht
lang: de-DE
status: release
facts:
  - id: completion-rate
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
# Klarer Projektbericht {#title}

## Kurzfassung {#executive-summary}

Wir von BrandKit liefern laut Dokumentation Status zweihundert und nennen den Ablauf „verlässlich“. Insgesamt ist das Ergebnis wirklich sehr gut.

::: {.important #important-main title="Kernpunkt"}
Die Einordnung bleibt knapp und überprüfbar.
:::

## Umfang {#scope}

- Abgegrenzter Betrachtungszeitraum
- Dokumentierte Prüfschritte
- Nachvollziehbare Entscheidungspunkte

| Bereich | Status |
| --- | --- |
| Umsetzung | {{fact:completion-rate|42 %}} |
| Prüfung | abgeschlossen |

![Neutrale Projektgrafik](asset:diagram "Eine abstrahierte Testfläche ohne Kundendaten"){#image-diagram rights=diagram-rights}

```{mermaid #flow-main title="Ablauf" description="Drei verbundene neutrale Arbeitsschritte"}
flowchart LR
  A[Briefing] --> B[Prüfung]
  B[Prüfung] --> C[Ausgabe]
```

::: page-break

## Befunde {#findings}

Der dokumentierte Anteil beträgt {{number:completion-rate|42 %}} und ist an {{source:source-one|Quelle A}} gebunden.[^source-one]

1. Der Eingang wurde vollständig erfasst.
2. Die Regeln wurden vor der Ausgabe geprüft.
3. Das Ergebnis bleibt reproduzierbar.

::: {.note #note-main title="Einordnung"}
Die Darstellung verwendet ausschließlich neutrale Beispieldaten.
:::

## Empfehlungen {#recommendations}

Die nächste Prüfung sollte denselben Vertrag und dieselben Pins verwenden.

## Fazit {#conclusion}

Der Ablauf trennt Inhalt, Prüfung und Ausgabe sichtbar voneinander.

[^source-one]: Unveränderlicher Quellenhinweis für die neutrale Referenz.
