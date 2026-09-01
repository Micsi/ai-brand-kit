#!/bin/sh
set -eu

repo=$(git rev-parse --show-toplevel)
git_dir=$(git rev-parse --absolute-git-dir)
cleanroom=$(mktemp -d "${TMPDIR:-/tmp}/brandkit-skills-index.XXXXXX")
check_env=$(mktemp -d "${TMPDIR:-/tmp}/brandkit-skills-venv.XXXXXX")
trap 'rm -rf "$cleanroom" "$check_env"' EXIT HUP INT TERM

git --git-dir="$git_dir" --work-tree="$repo" checkout-index --all --prefix="$cleanroom/"
cd "$cleanroom"
python3.12 -m venv "$check_env/venv"
"$check_env/venv/bin/python" -m pip install --disable-pip-version-check -q \
  --require-hashes --only-binary=:all: -r requirements-architecture-ci.lock.txt
"$check_env/venv/bin/python" scripts/validate_brandkit_skills.py
"$check_env/venv/bin/python" -m unittest tests/test_brandkit_skills.py
