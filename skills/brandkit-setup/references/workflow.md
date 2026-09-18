# Readiness playbook

Record requested outputs, the exact Git/project root, supplied sources and only
the capabilities those outputs require. Observe versions rather than guessing.
Return one evidenced status or executable fallback per required capability;
missing write target, ambiguous root or a requirement without fallback blocks.
If the owner opts into the external discovery route, load the declared handoff
reference, ask for the real path of the already approved checkout, and execute
`python scripts/brandkit_discovery.py check-bundle --bundle-root <real-path>`.
Only a clean detached checkout at the exact contract commit with the closed
three-subtree file inventory is ready. Point to the project-local activation
commands in the handoff reference. The owner then explicitly starts
`$brandkit-interview`; its `prepare` trace directly sequences `grilling` before
`domain-modeling` and never invokes the wrapper. If no verified bundle exists,
offer the native interview or a separate explicit activation handoff. This
readiness pass performs no installation and no skill invocation.
Visual readiness records Affinity as observed read-only or unavailable and
allows only a closed current-run Impeccable resolver; never global prebuild or
pre-package `extract`.
