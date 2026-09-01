#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CHECK_ENV=$(mktemp -d "${TMPDIR:-/tmp}/brandkit-architecture-check.XXXXXX")
trap 'rm -rf "$CHECK_ENV"' EXIT

if ! command -v python3.12 >/dev/null 2>&1; then
  echo "CPython 3.12 is required for the architecture check" >&2
  exit 1
fi
ARCHITECTURE_PYTHON=$(command -v python3.12)
if [ "$("$ARCHITECTURE_PYTHON" -c 'import platform; print(platform.python_implementation(), platform.python_version_tuple()[:2])')" != "CPython ('3', '12')" ]; then
  echo "python3.12 must resolve to CPython 3.12" >&2
  exit 1
fi
if [ "$(uname -s)-$(uname -m)" != "Darwin-arm64" ]; then
  echo "the architecture dependency lock supports macOS arm64 only" >&2
  exit 1
fi

"$ARCHITECTURE_PYTHON" -m venv "$CHECK_ENV/venv"
"$CHECK_ENV/venv/bin/python" -m pip install --disable-pip-version-check -q \
  --require-hashes --only-binary=:all: \
  -r "$ROOT/requirements-architecture-ci.lock.txt"
cd "$ROOT"
export BRANDKIT_MERMAID_CAS=/Volumes/Daten/Projekte/BrandKit/external-artifacts/mermaid-svg/3dcbf4ebfcbe0fb08e5b7261dbabf72b56c62517c875bbacd7b9dc75c2971120
"$CHECK_ENV/venv/bin/python" scripts/validate_agent_skill_architecture.py
"$CHECK_ENV/venv/bin/python" scripts/markdown_contract.py validate-contracts
"$CHECK_ENV/venv/bin/python" -m unittest -v tests/test_agent_skill_architecture.py
"$CHECK_ENV/venv/bin/python" -m unittest -v tests/test_brand_package.py
"$CHECK_ENV/venv/bin/python" -m unittest -v tests/test_impeccable_adapter.py
"$CHECK_ENV/venv/bin/python" -m unittest -v tests/test_markdown_contract.py
"$CHECK_ENV/venv/bin/python" -m unittest -v scripts/test_check_commercial_content.py
"$CHECK_ENV/venv/bin/python" scripts/check_commercial_content.py
