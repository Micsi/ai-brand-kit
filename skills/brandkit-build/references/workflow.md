# Brand Package build playbook

Validate the interview gate, then materialize only sourced values, rights-bound
assets and declared document contracts under `brand-package.v1`. Validate with
`scripts/brand_package.py validate`. Materialize the one canonical `AGENTS.md`
with `scripts/materialize_brandkit_instructions.py --project-root <git-root>`;
instruction copies, missing provenance, rights gaps or a missing pin block.
