#!/usr/bin/env bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_home=$(mktemp -d)
trap 'rm -rf "$test_home"' EXIT

# Build a clone-backed fixture so the test exercises the GitHub installation path.
fixture="$test_home/source"
mkdir -p "$fixture"
tar --exclude=.git --exclude=__pycache__ -cf - -C "$repo_root" . | tar -xf - -C "$fixture"
git -C "$fixture" init -q
git -C "$fixture" config user.name "Imagegen packaging test"
git -C "$fixture" config user.email "test@example.com"
git -C "$fixture" add -A
git -C "$fixture" commit -qm "fixture"

# Simulate the incomplete installation produced by the root-level SKILL.md layout.
installed="$test_home/.pi/agent/skills/imagegen"
mkdir -p "$installed"
printf '%s\n' 'incomplete install' > "$installed/SKILL.md"

HOME="$test_home" npx --yes skills@1.5.17 add "file://$fixture" \
  --skill imagegen \
  --agent pi \
  --global \
  --yes >/dev/null

test -f "$installed/SKILL.md"
test -f "$installed/scripts/image_gen.py"
test -f "$installed/references/cli.md"
test -f "$installed/references/image-api.md"

env \
  -u PI_IMAGEGEN_BASE_URL \
  -u OPENAI_BASE_URL \
  -u PI_IMAGEGEN_API_KEY \
  -u OPENAI_API_KEY \
  -u PI_IMAGEGEN_MODEL \
  -u PI_IMAGEGEN_OUTPUT_DIR \
  -u PI_CODING_AGENT_DIR \
  -u IMAGE_GEN_MODEL \
  PYTHONPYCACHEPREFIX="$test_home/pycache" python "$installed/scripts/image_gen.py" \
  generate \
  --prompt "A tiny red cube on a white background" \
  --out "$test_home/cube.png" \
  --dry-run > "$test_home/dry-run.json"

python - "$test_home/dry-run.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

assert payload["endpoint"].endswith("/images/generations")
assert payload["outputs"][0].endswith("/cube.png")
assert payload["prompt"] == "Primary request: A tiny red cube on a white background"
PY

env \
  -u PI_IMAGEGEN_BASE_URL \
  -u OPENAI_BASE_URL \
  -u PI_IMAGEGEN_API_KEY \
  -u OPENAI_API_KEY \
  -u PI_IMAGEGEN_MODEL \
  -u PI_IMAGEGEN_OUTPUT_DIR \
  -u PI_CODING_AGENT_DIR \
  -u IMAGEGEN_SCRIPT \
  -u IMAGE_GEN_MODEL \
  PYTHONPYCACHEPREFIX="$test_home/pycache" \
  python -m unittest discover -s "$repo_root/tests" -v

printf 'Imagegen skill packaging and behavior verified.\n'
