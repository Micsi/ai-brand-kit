---
name: brand-voice-pdf
description: Create a branded document or PDF from an explicit BrandKit package and approved document type. Use for reports, offers, one-pagers, and free-form branded documents; route BrandKit setup or package-building requests to brandkit-build.
---

# Brand Voice PDF

Produce one evidence-bound PDF from one explicit project activation. Read
[`references/workflow.md`](references/workflow.md) before the first write.

Stop before writing unless the project state and the canonical
`brand-voice-workflow-lock.v2` resolve byte-exactly to one activation, Brand
Package, document type, `humanizer-de` release, renderer trust root, Markdown
contract, and owner-approval key. A brand name or current-directory guess is
not activation.

Keep `draft.md`, `final.md`, `humanizer-baseline.json`,
`humanizer-audit.json`, `render-result.json`, and the PDF as distinct files in
the run-scoped paths defined by the architecture contract. The renderer accepts
only `final.md`; publication accepts only signed evidence for its exact bytes.
The pinned Brand Package and document type own PDF spacing and layout. Consume
only explicit `shared_visual_tokens.spacing` as portable read-only input;
`brandkit-spacing` may audit that source but may not change renderer spacing,
page geometry, pagination, or document-type layout.

Read [`references/markdown-authoring.md`](references/markdown-authoring.md) when
drafting report, offer, one-pager, or freeform Markdown. Treat every contract
diagnostic as a blocking error; express unresolved content as a frontmatter
TODO and keep the document out of release state.

Invoke the pinned `humanizer-de` separately for every mutable prose segment.
Bind each segment ID, old hash, raw audit/rewrite evidence, result bytes, and
changed flag. The only accepted states are `audit-only/no-change` (one call,
zero confirmed edits, no rewrite artifact; every rejected candidate has a
recorded reason and a clean evidence report) and
`audit+rewrite/changed` (two calls, at least one confirmed edit, rewrite
artifact present). Facts, numbers, URLs, sources, directives, headings, tables,
diagrams, quotes, verbatim blocks, IDs, and line structure remain byte-identical.

Run the installed `scripts/run` in two phases. `prepare` takes explicit project,
draft, asset root, run, brand, issuer key, workflow lock, and owner key. It
produces final Markdown, Humanizer evidence, and `approval-request.json`, but no
PDF. After the owner signs that exact request, `publish` takes the same identity
plus `--owner-approval` and `--mermaid-cas`; it re-resolves every input before
allowing PDF creation. Generate the canonical lock with `--print-workflow-lock`
before `prepare`. The workflow
executes the reviewed Claude provider and calls the pinned renderer; it does
not generate, patch, or select renderer code. Completion requires a valid signed
`render-result.v1`, strict publication gate, Poppler PNG for every page, and a
QA report with zero safe-area, overflow, overlap, orphan, missing-glyph, and
measured-contrast findings.
