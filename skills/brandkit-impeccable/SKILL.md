---
name: brandkit-impeccable
description: Erzeugt und prueft den projektlokalen Impeccable-Designkontext aus genau einem aktivierten, unveraenderlich gepinnten Brand-Paket. Verwende diesen Skill vor Impeccable-Arbeit in einem BrandKit-Projekt oder zur Konfliktpruefung bestehender PRODUCT.md, DESIGN.md und UI-Tokens.
license: MIT
---

# BrandKit-Impeccable-Adapter

## Routing

| Route | Primäres Playbook |
|---|---|
| Designkontext synchronisieren | [`references/workflow.md`](references/workflow.md) |

Dieser Skill orchestriert zwei getrennte Quellen. Er enthaelt keinen
Impeccable-Quelltext und keine Marke. Impeccable wird separat aus dem in
`impeccable-adapter.v1.json` gepinnten Release installiert und nur nach
explizitem Projekt-Opt-in aktiviert.

## Ablauf

1. Den echten Pin mit `impeccable_adapter.py verify` prüfen; Installation mit
   `materialize`, `activate` und `status` verwalten.
2. BrandKit-Aktivierung und Lock mit `brand_package.py resolve` pruefen.
3. Die explizite `.brandkit/impeccable-product.json` und bei bestehender UI die
   vollständige `.brandkit/ui-token-inventory.json` bereitstellen.
4. Bei neuem, geaendertem oder strittigem Spacing den Vertrag mit
   `brandkit-spacing` aufloesen und pruefen.
5. `python scripts/impeccable_adapter.py sync --project <git-root>` ausfuehren.
6. Bei Exit 3 den Review-Diff lesen und stoppen. Nie `DESIGN.md` oder UI-Werte
   in das Brand-Paket zurueckschreiben.
7. Erst nach gruenem Sync und ausdruecklicher Routenwahl Impeccable aufrufen.
8. Hooks, Updatecheck, Browser/Live, Bildgenerierung und Netz bleiben technisch
   gesperrt, bis eine geschlossene projektgebundene Capability-Datei die
   einzelne Funktion ausdruecklich aktiviert.
9. Vor dem Brand-Paket darf `brandkit-visual-decisions` ausschließlich den
   gepinnten Resolver aus dem aktuellen Run-Workspace für `colorize`,
   `typeset`, `adapt` und `critique` verwenden. Kein Lookup unter `~/.agents`,
   keine globale Installation und kein `extract`; `extract` bleibt bis nach
   einem validierten Brand-Paket verboten.

## Zustaendigkeiten

- BrandKit: Brand-ID/-Version, Stimme, Tokens, Assets und Rechte; erzeugt den
  Designvertrag und entscheidet Konflikte fail-closed.
- BrandKit-Spacing: prueft Spacing-Inventar und Rollen oder liefert einen
  unbestaetigten Vorschlag; erzeugt keine `DESIGN.md` und gestaltet keine UI.
- Impeccable: UI planen, bauen, pruefen oder polieren innerhalb des Vertrags.
- Humanizer: bestehende deutsche Prosa nach fachlicher und visueller Freigabe
  redigieren; nie normative Tokens oder Produktfakten aendern.
- PDF: freigegebene Inhalte mit `pdf.*`-Tokens rendern und technisch/visuell
  pruefen; `ui.*` und `web.*` duerfen `pdf.*` nicht ueberschreiben.

Reihenfolge: BrandKit resolve/sync -> Impeccable-Route -> fachliches Review ->
Humanizer (falls deutsch) -> PDF (falls PDF-Ausgabe) -> finales Review.
