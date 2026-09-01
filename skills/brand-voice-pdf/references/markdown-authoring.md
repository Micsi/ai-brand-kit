# Document templates

All files use `markdown-document.v1` frontmatter and the closed syntax in
`docs/markdown-contract-v1.md`. Use stable lowercase IDs. Put claims in
`facts`, their provenance in `sources`, local images in `assets`, and evidence
in `rights`. Natural sentences are mutable; structural and evidence-bearing
bytes are protected.

- Report: `executive-summary`, optional `scope` and `methodology`, `findings`,
  optional `recommendations`, `conclusion`, optional `sources`.
- Offer: `context`, `scope`, `deliverables`, optional `schedule` and
  `assumptions`, `terms`, optional `next-steps`.
- One-pager: `summary`, `key-points`, optional `evidence` and `next-step`.
- Freeform: `content`, optional `sources`.

Available blocks are headings, prose, ordered/unordered lists, links,
footnotes, tables, local images, typed callouts, Mermaid-v1 flowcharts,
Graphviz-v1 directed graphs, and an
explicit page break. Presentation callouts are `note`, `tip`, `warning`,
`caution`, and `important`; select `appearance=default|simple|minimal` and
`icon=true|false`. A page break follows section content and precedes an H2 or
H3. A one-pager has no page break. A figure fence carries a stable ID, title,
description, and optional declared `source_id`; local links target that ID.
Graphviz accepts only `digraph`, `rankdir`, literal labelled nodes, and directed
edges. The validator closes the grammar before the pinned offline renderer runs.
Unsupported content becomes a blocking TODO, not approximate output.
