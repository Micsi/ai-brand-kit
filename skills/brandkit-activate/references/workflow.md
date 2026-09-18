# Brand Package activation playbook

Name exactly one package: a local directory holding `brand-package.json`, or a
`brand-registry-index.v1` entry unpacked with `brand_package.py materialize`
into an absent destination. Validate it before anything else with
`brand_package.py validate`; the printed `content_sha256` is the identity the
activation pins. Anchor at one root — a Git root or an existing `.brandkit/`,
otherwise the named directory, never below an already anchored parent. Activate
with `pin`, resolve with `resolve --create-roots`, return with `rollback`; each
refuses a package whose brand, version or content digest differs from the pin.
Activation needs no repository; `brandkit-impeccable` still does, because it
binds the commit into a signed capability grant.
