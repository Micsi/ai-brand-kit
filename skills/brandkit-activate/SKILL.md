---
name: brandkit-activate
description: Aktiviert ein gebautes Brand-Paket in einem Zielprojekt und listet die verfügbaren Pakete auf. Nutze den Skill, wenn ein Projekt eine Marke übernehmen, auf eine andere Version wechseln oder eine Aktivierung zurückrollen soll; er baut keine Marke und trifft keine Markenentscheidung.
license: MIT
---

# BrandKit aktivieren

## Routing

| Route | Primäres Playbook |
|---|---|
| Paket aktivieren, wechseln, zurückrollen | [`references/workflow.md`](references/workflow.md) |

Owner: Aktivierung im Zielprojekt. Kit-Erzeugung gehört `brandkit-build`,
Artefaktproduktion `brandkit-publish`, UI-Arbeit `brandkit-impeccable`.

## Eingabe und Ausgabe

- Eingabe: Zielprojekt, ein validiertes Paket (Pfad oder Registry-Eintrag) und
  die Engine-Version als exakte SemVer.
- Ausgabe: `.brandkit/` im Zielprojekt mit unveränderlicher Paketkopie unter
  `releases/<brand>/<version>/<content-sha256>/`, Aktivierung und Skill-Lock
  unter `states/<hash>/`, `current` als Symlink darauf und ein Journal für den
  Rückweg.

## Workflow

1. Paket wählen. Nenne dem Owner, was erreichbar ist: lokal gebaute Pakete
   (Verzeichnisse mit `brand-package.json`) und Registry-Einträge aus einem
   `brand-registry-index.v1` mit `brand_id`, `version` und `content_sha256`.
   Ein Registry-Paket wird zuerst mit
   `brand_package.py materialize --index <index> --brand-id <id> --version <v>
   --destination <ziel>` ausgepackt; das Ziel muss vorher fehlen. Fertig, wenn
   genau ein Paket mit Pfad, Marke und Version feststeht.
2. Paket prüfen, bevor es irgendetwas berührt:
   `brand_package.py validate <paket>`. Die Ausgabe nennt `brand_id`, `version`
   und `content_sha256` — der Inhaltsdigest ist die Identität, unter der das
   Paket später liegt. Fertig, wenn die Prüfung fehlerfrei durchläuft.
3. Wurzel bestimmen. Die Aktivierung verankert sich an genau einem Verzeichnis
   und schreibt nichts außerhalb. Ein Git-Root oder ein bereits vorhandenes
   `.brandkit/` ist die Wurzel; sonst genügt das ausdrücklich benannte
   Verzeichnis, solange kein Elternverzeichnis bereits verankert ist. Fertig,
   wenn der Owner die Wurzel benannt hat und sie nicht unterhalb einer anderen
   liegt.
4. Aktivieren: `brand_package.py pin --project <wurzel> --package <paket>
   --engine-version <semver>`. Das kopiert das Paket unveränderlich, schreibt
   Aktivierung und Lock und setzt `current`. Eine vorhandene Aktivierung wird
   ins Journal gestellt statt überschrieben. Fertig, wenn `.brandkit/current`
   auf den neuen Zustand zeigt.
5. Auflösen und übergeben: `brand_package.py resolve --project <wurzel>
   --engine-version <semver> --run-id <lauf> --create-roots`. Die Auflösung
   prüft das gepinnte Paket erneut gegen Marke, Version und Inhaltsdigest und
   liefert die vier Arbeitswurzeln `work`, `cache`, `temp` und `output`.
   Fertig, wenn `source` die erwartete Herkunft nennt und die Wurzeln stehen.
6. Zurückrollen, wenn der Owner es verlangt:
   `brand_package.py rollback --project <wurzel>`. Der Rückweg stellt die
   vorherigen Bytes wieder her und scheitert bei manipuliertem Journal oder
   fehlendem Vorgängerpaket. Fertig, wenn Aktivierung und Lock byte-genau dem
   Vorzustand entsprechen.

## Gate

`brandkit-publish`, `brandkit-impeccable` und `brandkit-spacing` erhalten nur
eine aufgelöste Aktivierung. Stoppe bei ungültigem Paket, uneindeutigem
Registry-Treffer, bereits verankertem Elternverzeichnis, abweichender
Engine-Version oder einem Paket, dessen Inhaltsdigest nicht zum Pin passt.
Erfinde weder Marke noch Version; ein Paket wird nie verändert, um zu passen.

Für UI-Arbeit über `brandkit-impeccable` bleibt ein Git-Repository nötig: Der
Adapter bindet den Commit in ein signiertes Fähigkeitszertifikat. Die
Aktivierung selbst verlangt kein Repository.
