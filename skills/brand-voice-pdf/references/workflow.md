# Run contract

Inputs are a project Git root, briefing/fact ledger, explicit brand activation,
locked Brand Package, one document-type ID, and a run ID. The activation and
lock must agree on brand ID/version/content hash and immutable locator. The
Brand Package must declare the selected type/version and satisfy the shared
BrandKit package schema.

The installed `scripts/run` is the normative entrypoint. Before the first run,
capture its canonical `--print-workflow-lock` output inside the exact project.
`prepare` requires the draft, asset root, fresh run ID, explicit brand ID,
renderer issuer key, workflow lock, and owner key. It writes no PDF and returns
an Approval Request bound to final Markdown, run, project, brand, document type,
and workflow lock. Only then may the owner sign. `publish` requires that later
approval and the pinned Mermaid CAS. The reviewed provider is `claude`; Codex and Hermes
agents invoke the same workflow, while Codex CLI 0.144.3 is not asserted as a
Humanizer model provider.

The resolver selects isolated content-addressed work roots. The run keeps
`draft.md`, baseline, all per-segment Humanizer raw evidence, `final.md`, audit,
renderer work, PDF, page PNGs, QA, render result, and run summary beneath that
trusted root. Never reuse a run ID or another project's directory.

Sequence and completion:

1. Resolve every pin without mutation. Complete when identity, version, hash,
   rights, document type, Humanizer, and renderer trust are unambiguous.
2. Validate the external draft without mutation.
3. In `prepare`, create the signed baseline, run Humanizer on mutable segments, apply its
   confirmed ledger, and write `final.md` plus `humanizer-audit.json`. Complete
   when protected bytes and AST skeleton are identical and every factual value
   matches the input ledger.
4. Emit `approval-request.json` and stop. Neither a draft nor a request authorizes rendering.
5. In `publish`, require the later owner decision to bind final/run/project/lock
   and zero TODOs. Re-resolve all trust inputs, then render only those bytes.
6. Run the publication gate; re-resolve all inputs again and render every page to PNG with Poppler. Complete
   when PDF hash, classic xref, metadata, page count, asset inventory, footer
   labels, text extraction, visual QA, and signed evidence all pass.

The title page of a multi-page document has no page label. Content pages use
`Seite X von Y`. A one-pager is exactly one page and contains no `Seite` label.
Unknown syntax, missing tokens, unresolved rights, open TODOs, warnings, or a
failed diagram stop the run. Mermaid and Graphviz share one figure registry:
their IDs, captions, PDF destinations, HTML anchors, and local references must
match exactly.
