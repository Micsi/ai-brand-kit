# Brand Package v1

Normative pointers:

- Closed package shape: `schemas/brand-package.v1.schema.json`
- Resolver, semantic validator, hashing and lifecycle CLI: `scripts/brand_package.py`
- Real-filesystem contract tests: `tests/test_brand_package.py`
- Architectural boundaries and pin policy: `docs/agent-skill-architecture.md`, section “Engine, Brand-Paket und Projekt”

## Package and activation

A package directory contains `brand-package.json` plus only positively declared
font and logo asset classes, dedicated rights evidence, and document-type contracts. Every
declared file is relative, regular, inside the package realpath, and hashed with
SHA-256. Symlinks, undeclared files, output/cache/temp/secret/customer paths,
missing rights evidence, duplicate IDs, and unknown manifest keys fail closed.
The manifest pins Brand ID, SemVer, voice profile, PDF theme and tokens, assets
and their rights, plus document-type versions and engine compatibility.
Typography roles reference either a declared font asset ID or one of the three
renderer-v1 core-font IDs pinned in the renderer lock: `builtin:Helvetica`,
`builtin:Helvetica-Bold`, or `builtin:Courier`; every other `builtin:*` value is invalid.
WOFF2 assets must have both private-data header fields set to zero. SVG text is
limited to at most one direct, attribute-free `title` and `desc`, each equal to
the declared asset ID; all other element text and non-whitespace tail text is
forbidden.

The exact Git root activates one package in `.brandkit/project.json` and locks
the exact engine/skill release in `.brandkit/skill-lock.json`:

```json
{
  "schema_version": "brand-project.v1",
  "brand": {
    "id": "neutral-example",
    "version": "1.0.0",
    "content_sha256": "<64 lowercase hex>"
  },
  "package_path": ".brandkit/releases/neutral-example/1.0.0/<hash>",
  "overrides": {"document_type": "report", "toc": true}
}
```

Only `document_type`, `toc`, and `section_numbering` are project overrides.
They select behavior already allowed by the pinned package/engine; they cannot
change assets, hashes, rights, paths, schema rules, or integrity checks.

## Resolution and isolation

Resolution is deterministic: explicit `--package`, then the exact content-addressed
`package_path` in the activation. A registry is deliberately not a resolver
fallback. It must first be queried through `materialize`, whose closed index entry
addresses exactly `releases/<id>/<version>/<content_sha256>`; the resulting bytes
must then be activated with `pin` before `resolve`. There is no default brand,
registry search, or recovery to a lower-precedence source.
The index file itself must be a regular non-symlink file. Its parent is resolved
to an absolute canonical directory before any immutable release locator is
interpreted, so the documented repository-relative `--index` form remains both
usable and confined to that registry root.

Cache namespaces include the exact engine version; work, temp, and output also
include a validated run ID. All include the project realpath hash, Brand ID,
Brand version, and package content hash. `resolve` is
read-only unless `--create-roots` is supplied.

```bash
python scripts/brand_package.py validate /immutable/releases/acme/1.2.3/hash
python scripts/brand_package.py pin --project "$PWD" --package /immutable/releases/acme/1.2.3/hash --engine-version 1.0.0
python scripts/brand_package.py materialize --index fixtures/brand-registry/index.v1.json --brand-id neutral-fixture --version 1.0.0 --destination /tmp/neutral-fixture
python scripts/brand_package.py pin --project "$PWD" --package /tmp/neutral-fixture --engine-version 1.0.0
python scripts/brand_package.py resolve --project "$PWD" --engine-version 1.0.0 --run-id build-001 --create-roots
```

Use the same commands in CI, without `--create-roots` for validation-only jobs.
CI must install `requirements-architecture-ci.lock.txt` and must not copy package
contents into logs or artifacts.

## Update, rollback, and migration

`pin` validates a new immutable package and writes activation plus skill lock into
one content-addressed state directory before atomically switching their shared
`current` pointer. It retains the previous state. The authenticated journal binds
both states to the canonical project realpath and its SHA-256. `rollback`
atomically swaps the shared pointer back. Neither command mutates a registry,
package, global engine, or another project.

```bash
python scripts/brand_package.py pin --project "$PWD" --package /private/brands/acme/1.3.0/hash --engine-version 1.0.0
python scripts/brand_package.py rollback --project "$PWD"
python scripts/brand_package.py migrate --project "$PWD" --legacy legacy-brand.json --package /immutable/package --engine-version 1.0.0
```

Migration intentionally accepts only a legacy `{brand_id, version}` activation.
Assets and rights must first be assembled into and validated as a complete v1
package; customer content, secrets, build intermediates, and generated outputs
are never migrated into it. Exact production brand values remain TODO until an
authorized primary source and rights evidence are supplied.
