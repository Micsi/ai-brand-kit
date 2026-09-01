# Markdown Contract v1 — Agent Reference

Use `scripts/markdown_contract.py` as the executable authority. The complete standalone valid document is `fixtures/markdown/valid-report.md`; the complete standalone invalid document is `fixtures/markdown/invalid-local-link.md`. Expected outcomes and policy pointers are closed in `fixtures/markdown/diagnostic-matrix.v1.json`; executable mutations remain in `fixtures/markdown/invalid-cases.v1.json`.

| Boundary | Closed rule | Failure |
|---|---|---|
| YAML | Unique string keys; no anchors, aliases, or explicit tags | frontmatter diagnostic |
| headings/links | One attributed H1; H1–H3 only; local anchors resolve against every global semantic ID | ID diagnostic |
| tables | Header, separator, alignment, and every row have the exact same column count | table diagnostic |
| callouts | Exact fields, no nesting, nonempty body | callout diagnostic |
| values | Scientific, signed, percent, decimal, and locale-grouped numbers require protected fact syntax | bare-number diagnostic |
| Mermaid | Iterative entity/percent decode plus NFKC applies equally to code, title, and description; conservative `flowchart` v1 has no URL/scheme, HTML, config, interaction, image, icon, or styling | Mermaid-v1 diagnostic |
| assets | Strict PNG/JPEG decode or canonical SVG bytes (UTF-8, no BOM/declaration/doctype/comment/PI/edge whitespace, exactly one root); MIME, extension, bytes, hash, asset ID, and rights evidence agree | asset/rights diagnostic |
| document types | Strict JSON rejects NaN/Infinity; extensions can only narrow the four base types | document-type diagnostic |

The Humanizer plan is one disjoint, gapless partition of the complete UTF-8 byte stream. Every entry carries a stable ID derived from AST path, role, and ordinal plus `old_hash`. Only `rewrite_humanizer(baseline, key_path, [(segment_id, old_hash, replacement)], ...)` may rewrite, and only mutable single-line prose IDs are accepted. Reconstruction must preserve every protected byte and the complete AST skeleton: block order and nesting, list markers, table separator/alignment, paragraph/blank boundaries, callout/fence delimiters, IDs, facts, URLs, quotes, footnotes, code, tables, and frontmatter.

Humanizer baselines are separate producer-evidence `humanizer-baseline.v1` envelopes. Their source, contract, document type, exact materialized Brand-Package manifest hash and registry-pinned package tree hash, tool version, nonce, and creation time are provenance-hashed and HMAC-bound. The final input must differ from its baseline.

Publication accepts renderer evidence only under the fixed committed `renderer-trust.v1.json`: exact issuer-key fingerprint, engine name/version, and renderer-lock path/hash. Its truthful production status is currently `not-installed`, so every production publication attempt fails closed until that renderer exists; neither the Python API nor CLI has a caller-controlled trust channel. Positive signature mechanics live only in a separate test harness under `tests/`, which production does not import. The output hash is checked before strict pinned-`pypdf` cross-reference/catalog/page/text parsing. A canonical PDF has exactly one `startxref`, one terminal `%%EOF`, and no trailing bytes. Pagination evidence must exactly equal labels extracted from the hashed PDF: one-pagers contain one page and no `Seite`, while multi-page title pages are unlabelled and content pages contain exactly `Seite X von Y` in sequence. Thus document-type pagination is not production-trusted while the production renderer remains unavailable. Open-work markers fail across nested metadata, rights, content, and render evidence.

Presentation callouts are `note`, `tip`, `warning`, `caution`, and `important`.
Each accepts `appearance=default|simple|minimal` and `icon=true|false`; omitted
values resolve to `default` and `true`. The renderer preserves the selected
appearance and icon state. `metric`, `quote`, `legal`, and `verbatim` retain
their evidence-specific closed fields and accept no presentation attributes.

Commands:

```sh
python scripts/markdown_contract.py validate-contracts
python scripts/markdown_contract.py validate fixtures/markdown/valid-report.md --asset-root fixtures/markdown
```

For Mermaid, `toolchain/mermaid-svg.v1.lock.json` pins `package.json`, the complete package lock and all 268 transitive integrity records, configs, scripts, Chrome, and the exact external read-only artifact-bundle path/tree/manifest. Every cache file has a SHA-256 entry. Release verification is offline and fail-closed:

```sh
PYTHON_BIN=PATH_TO_LOCKED_PYTHON toolchain/mermaid-svg/verify-clean-rooms /Volumes/Daten/Projekte/BrandKit/external-artifacts/mermaid-svg/BUNDLE_TREE_SHA256
```

The verifier first validates the immutable bundle, reads that same read-only cache from two fresh install rooms, runs `npm ci --offline` behind dead loopback proxies, renders the same accepted diagram, compares SVG bytes, and prints the shared SHA-256. The full runner sets the one exact locked bundle path; there is no skip path.

Graphviz uses a separate pinned `toolchain/graphviz-svg.v1.lock.json` and an
immutable `/Volumes/Daten` CAS. The Markdown fence accepts only a closed
directed-graph subset: literal labelled nodes, directed edges, and `rankdir`.
After iterative NFKC/entity/percent decoding, schemes, URLs, HTML, comments,
includes, file/image attributes, interaction, and shell forms fail closed.
Every figure retains its ID, caption, description, optional declared source,
PDF destination, and HTML anchor; missing, duplicate, or foreign references
block publication.
