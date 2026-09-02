# Design-context sync playbook

Verify the pinned adapter and Brand Package, then require the explicit product
input (and a complete UI inventory when UI exists). Run
`scripts/impeccable_adapter.py sync --project <git-root>` and stop on review
exit 3. `PRODUCT.md`, `DESIGN.md` and the sidecar are derived outputs; never
reverse-sync their values into the immutable Brand Package.
