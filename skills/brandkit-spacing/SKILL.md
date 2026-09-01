---
name: brandkit-spacing
description: Inventarisiert markengebundene Abstaende read-only, prueft ihre Token- und Rollenzuordnung oder erstellt unbelegte Token-Vorschlaege. Nutze den Skill fuer eine ausdrueckliche Spacing-Inventur oder Rollenpruefung; UI-Aenderungen gehen an Impeccable, PDF-Layout an brand-voice-pdf.
license: MIT
---

# BrandKit-Spacing

Owner: Spacing-Inventar, primitive Skala, semantische Abstandsrollen und
Spacing-Audit. Der Skill ist ein optionaler Design-Adapter, kein Schritt der
Kernsequenz `setup -> interview -> build -> publish`.

## Eingabe und Ausgabe

- Eingabe: expliziter Brand-Pin, Zielmedium (`shared`, `ui`, `web` oder `pdf`),
  Zielbereich und entweder vorhandene Tokens oder ein Spacing-Inventar mit
  Fundstellen und aufgeloesten Werten.
- Ausgabe bei Audit: geschlossenes, gehashtes Inventar und geschlossenes Resultat
  mit Fundstelle, Ist-Wert, Medium sowie genau einer Token-Zuordnung oder
  begruendeten Ausnahme je Inventarzeile.
- Ausgabe bei Entwurf: unbestaetigte primitive Skala, semantische Rollen,
  Medium und Provenienz als Vorschlag fuer `brandkit-build`.
- Ausgabe bei bestehendem Vertrag: validierte Rollen und Befunde fuer
  `brandkit-impeccable`, niemals eine zweite `DESIGN.md`.

## Normative Quelle

Ein gepinntes Brand-Paket ist fuer Markenwerte normativ. Portable Werte kommen
nur aus `shared_visual_tokens.spacing`; `ui.*`, `web.*` und `pdf.*` bleiben
mediumspezifisch. `pdf.tokens.spacing_scale`, Seitenraender, Pagination,
Flowable-Abstaende und Dokumenttyp-Layout gehoeren dem PDF-Renderer und werden
nicht zu Shared-Tokens hochgestuft.

Fehlt ein normativer Wert, ist ein Vorschlag erlaubt. Kennzeichne ihn samt
Quelle als unbestaetigt und uebergib ihn an `brandkit-build`. Zahlenheuristiken
sind nur in einem ausdruecklich angeforderten, nichtnormativen Vorschlagsmodus
zulaessig und muessen ihre externe oder nutzerbereitgestellte Quelle nennen.

## Rollen und Audit

Trenne primitive Werte von Rollen. Rollen wie `control-inline-gap`,
`field-stack-gap`, `card-padding`, `group-gap` oder `section-gap` verweisen auf
Primitive. Fuehre nur Rollen ein, die im Zielbereich wiederkehren. Jeder
Breakpoint- oder Density-Wert bleibt auf der freigegebenen Skala.

Pruefe den vollstaendigen Zielbereich:

1. Erfasse wirksame `margin`, `padding`, `gap` und Insets mit Fundstelle und
   aufgeloestem Wert.
2. Ordne jeden Wert einem Token oder einer begruendeten Ausnahme zu.
3. Vergleiche gleiche Komponenten und Zustaende auf dieselben Rollen.
4. Pruefe Gruppierung und Lesepfad in allen unterstuetzten Breakpoints und
   Density-Modi.

Der Skill bleibt read-only. Fuer UI/Web erzeugt er hoechstens einen validierten
Patch-Vorschlag; `brandkit-impeccable -> impeccable layout` uebernimmt jede
Mutation. Normative Brand-Werte werden ausschliesslich als neues Brand-Paket
mit neuer Version und menschlicher Freigabe materialisiert. PDF- und
Dokumenttyp-Tokens werden nur vom Renderer-/Publish-Owner geaendert.

## Handoffs

- Neuer oder geaenderter Markenwert -> `brandkit-build` fuer Provenienz,
  Versionierung, Validierung und Freigabe.
- Validierte UI-/Web-Rollen, Auditbefunde oder Patch-Vorschlag ->
  `brandkit-impeccable -> impeccable layout`; nur diese Route mutiert UI-Code.
- PDF-Abstandsanforderung -> `brand-voice-pdf` beziehungsweise
  `brandkit-publish`; dieser Skill liefert hoechstens bereits freigegebene
  `shared_visual_tokens.spacing` als read-only Input.

## Gate

Stoppe bei fehlendem oder mehrdeutigem Brand-Pin, widerspruechlichen
normativen Quellen, Vermischung der Medium-Namensraeume, unbekannter Provenienz
oder einer verlangten Mutation ausserhalb der oben genannten Rechte. Ein
Spacing-Audit ist abgeschlossen, wenn jeder wirksame Abstand im Zielbereich
einem Token oder einer begruendeten Ausnahme zugeordnet und jeder offene
Markenwert als Review-TODO benannt ist.
