# Optional discovery orchestration

The owner starts this route explicitly with `$brandkit-interview`. BrandKit
never invokes the upstream `grill-with-docs` wrapper: it is a pinned
orchestration reference whose `disable-model-invocation: true` remains in
force. After readiness succeeds, `brandkit-interview` directly invokes the two
model-invoked dependencies in this order: `grilling`, then `domain-modeling`.

## One-time project-local activation

Use an already approved clean/detached upstream checkout. Set all three paths
to exact absolute real directories. These commands create project-local links
only; they do not modify global skill roots and deliberately fail if a target
already exists:

```sh
PROJECT_ROOT=/absolute/path/to/brand-project
BRANDKIT_ROOT=/absolute/path/to/ai-brand-kit
BUNDLE_ROOT=/absolute/path/to/mattpocock-skills-checkout

python "$BRANDKIT_ROOT/scripts/brandkit_discovery.py" check-bundle \
  --bundle-root "$BUNDLE_ROOT"

mkdir -p "$PROJECT_ROOT/.agents/skills" "$PROJECT_ROOT/.claude"
for SKILL_NAME in brandkit-setup brandkit-interview brandkit-build brandkit-publish brandkit-spacing brandkit-impeccable brand-voice-pdf; do
  test ! -e "$PROJECT_ROOT/.agents/skills/$SKILL_NAME"
  ln -s "$BRANDKIT_ROOT/skills/$SKILL_NAME" "$PROJECT_ROOT/.agents/skills/$SKILL_NAME"
done
ln -s "$BUNDLE_ROOT/skills/productivity/grilling" "$PROJECT_ROOT/.agents/skills/grilling"
ln -s "$BUNDLE_ROOT/skills/engineering/domain-modeling" "$PROJECT_ROOT/.agents/skills/domain-modeling"
ln -s "$BUNDLE_ROOT/skills/engineering/grill-with-docs" "$PROJECT_ROOT/.agents/skills/grill-with-docs"
test ! -e "$PROJECT_ROOT/.claude/skills"
ln -s ../.agents/skills "$PROJECT_ROOT/.claude/skills"
```

Codex discovers `<project>/.agents/skills`; Claude follows the reviewed
`.claude/skills -> ../.agents/skills` bridge. Do not use `-f`, replace an
existing path, or continue after a collision. Re-open the agent in the exact
project root after activation.

## Run

The explicit `$brandkit-interview` invocation runs this readiness/trace command:

```sh
python "$BRANDKIT_ROOT/scripts/brandkit_discovery.py" prepare \
  --project-root "$PROJECT_ROOT" --bundle-root "$BUNDLE_ROOT" --run-id <run-id> \
  --owner-key <safe-owner-key>
```

Only a clean detached checkout at the exact source/commit/member hashes and the
exact project bridges produces a trace. The first trace stays in `grilling`.
After every answer, internally update `interview-progress.md` with the closed
partition of open, covered and explicitly-open axes, then run `prepare` again.
Keep invoking `grilling` while any frontier remains; never request a Handoff or
confirmation after merely one answer. The canonical progress artifact binds an
incrementing `Answer turns` value; fewer than two answer turns remain in
`grilling` even when every axis is claimed covered. Only a later trace with no open frontier may
invoke `domain-modeling`. `domain-modeling` must write a substantive
Each internal `progress` call supplies `--answer-sha256` for the exact persisted
answer/event bytes and the same safe `--owner-key`; the authenticated project/run
predecessor chain and separate current-head ledger must validate before use.
The Owner key and ledger must remain outside the project realpath. Existing
heads are non-resettable; restart only with a fresh run-id. Derived, distinct
HMAC subkeys protect progress state, head ledger and Owner receipt. The readable
review and receipt bind the chain id, exact current head digest and turn.
`CONTEXT.md` beginning with `# Context` and at least one substantive
`docs/adr/*.md` beginning with `# ADR`. During the interview, materialize every
owner answer and relevant Grilling-dialog excerpt as a real UTF-8 file such as
`sources/owner-answer-01.txt` or `sources/grilling-01.txt`; classify each file
as `owner-supplied` or `observed` in the handoff. A source ID without those
bytes is not a source. Build `handoff.json` with every document, source and decision in its
closed path/size/SHA-256 inventory. JSON without those real non-empty files is
failure and selects the native fallback.

The handoff JSON is strictly internal. Run `render-review` internally and show
its Markdown in chat: clear decisions with their classified sources, explicit
open points, unconfirmed recommendations in a separate section, and paths to
CONTEXT/ADRs. Ask exactly: “Diese Entscheidungen bestätigen? Korrekturen?”
Never ask the owner to open JSON or run a shell command.

Only an explicit chat approval authorizes the agent to run internal `confirm`
with a fresh confirmation ID and statement `Diese Entscheidungen bestätige
ich.`, immediately followed by `consume`. The receipt binds both the rendered
review digest and confirmation. After a correction regenerate the internal
handoff and review; their changed hashes invalidate every old receipt.

Write only returned decisions and sources plus CONTEXT/ADR provenance to
`.brandkit/work/<run-id>/brand-raw.md`; never copy recommendations.
Each consumed source retains its authenticated `id`, `classification`, `path`,
`size`, `sha256` and exact base64 bytes, so every decision `source_ids` entry
must resolve uniquely to the returned source set.

Ask only
uncovered axes: core/audience, personality, anti-character, brand architecture,
color, typography, grid/spacing, components, language/prohibitions, target
formats and image rules. Missing bundle, activation, skill, required artifact,
handoff or receipt selects this complete native eleven-axis path.
