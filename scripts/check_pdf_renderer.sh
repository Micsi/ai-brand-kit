#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
runtime=${BRANDKIT_PDF_RUNTIME:-$HOME/.agents/runtime-store/brandkit-pdf-renderer/1.2.28}
if [ -x "$runtime/.venv/bin/python" ]; then python_bin="$runtime/.venv/bin/python"
elif [ -n "${PYTHON_BIN:-}" ] && [ -x "$PYTHON_BIN" ]; then python_bin=$PYTHON_BIN
else
  echo "brandkit-pdf-renderer 1.2.28 is not installed; run toolchain/pdf-renderer/install-from-lock HOME WHEELHOUSE ISSUER_KEY" >&2
  exit 69
fi
cd "$root"
PYTHONDONTWRITEBYTECODE=1 "$python_bin" -B scripts/markdown_contract.py validate-contracts
PYTHONDONTWRITEBYTECODE=1 "$python_bin" -B -m unittest discover -v -s tests -p 'test_brand_voice_pdf.py'
printf '%s\n' "Brand Voice PDF contracts and adversarial checks passed."
