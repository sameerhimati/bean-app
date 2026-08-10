#!/usr/bin/env bash
#
# preflight — everything that must be true before Bean may be deployed.
#
# One script, no arguments. Exits 0 or nonzero. Every check below corresponds to a way this project
# has actually broken, not to a way it might. Each was proven by reverting the fix and watching this
# script go red; a check nobody watched fail is not a check.
#
#   a. a repo-root file that flips Railway's language detection  (this took prod down)
#   b. any non-live test failing, or any web/*.jsx that does not parse
#   c. the silent-success signature: a swallowed failure reported as success
#   d. a non-atomic whole-file write to the data volume
#   e. an auth check that returns True when its secret is absent
#
# Run it with `make preflight`, or let scripts/deploy.sh run it for you.

set -uo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
FAILED=0

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }

echo "preflight: $(git rev-parse --short HEAD)"
echo

# ---- a. nixpacks must still see a Python app --------------------------------------------------
# A package.json at the repo root made nixpacks detect a Node project and build an image with no
# Python interpreter at all: `python -m bean.server` → `python: command not found` → 502. The
# builder guesses from the presence of one file, so the check is the presence of one file.
echo "[a] Railway language detection"
LANG_MARKERS=(package.json package-lock.json yarn.lock pnpm-lock.yaml bun.lockb deno.json
              go.mod Gemfile Cargo.toml composer.json pom.xml build.gradle mix.exs
              Package.swift project.clj Nixpacks.toml)
found_marker=0
for f in "${LANG_MARKERS[@]}"; do
  if [ -e "$f" ]; then fail "repo-root $f would make nixpacks build a non-Python image"; found_marker=1; fi
done
if [ ! -e requirements.txt ] && [ ! -e pyproject.toml ]; then
  fail "no requirements.txt or pyproject.toml at the root — nixpacks has nothing to detect Python from"
elif [ "$found_marker" -eq 0 ]; then
  pass "python is the only language marker at the repo root"
fi

# ---- b. the offline suite, and the browser code parses ----------------------------------------
# pytest.ini deselects the live (token-spending) tests by config. No --ignore flags anywhere.
echo
echo "[b] tests and browser code"
if "$PYTHON" -m pytest -q > /tmp/bean-preflight-pytest.log 2>&1; then
  pass "$(tail -1 /tmp/bean-preflight-pytest.log | tr -s ' ')"
else
  fail "the offline suite is red:"
  tail -15 /tmp/bean-preflight-pytest.log | sed 's/^/      /'
fi

if command -v node > /dev/null 2>&1; then
  jsx_bad=0
  jsx_tmp="$(mktemp -d)"
  for f in web/*.jsx; do
    # The app's .jsx files are plain React.createElement — real JavaScript, no transform, loaded as
    # classic <script> tags. A syntax error here is a blank page for the operator, and nothing else
    # repo would catch it. `node --check` refuses the .jsx extension, so parse a .js copy.
    cp "$f" "$jsx_tmp/$(basename "$f" .jsx).js"
    if ! node --check "$jsx_tmp/$(basename "$f" .jsx).js" > /tmp/bean-preflight-node.log 2>&1; then
      fail "$f does not parse:"; sed 's/^/      /' /tmp/bean-preflight-node.log; jsx_bad=1
    fi
  done
  rm -r "$jsx_tmp"
  [ "$jsx_bad" -eq 0 ] && pass "web/*.jsx parse ($(ls web/*.jsx | wc -l | tr -d ' ') files)"
else
  fail "node is not installed — cannot verify web/*.jsx parses"
fi

# ---- c, d, e. the structural checks -----------------------------------------------------------
echo
echo "[c,d,e] structural checks"
if "$PYTHON" scripts/preflight_checks.py; then
  pass "no unannotated silent-success, no non-atomic volume writes, no unguarded fail-open auth"
else
  fail "see above"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32mpreflight OK\033[0m\n'
else
  printf '\033[31mpreflight FAILED — not safe to deploy\033[0m\n'
fi
exit "$FAILED"
