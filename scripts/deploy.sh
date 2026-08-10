#!/usr/bin/env bash
#
# deploy — the only place `railway up` may be run.
#
# Bypassing this script is how production died: `railway up` from a shell pushed a repo whose new
# root package.json made nixpacks build an image with no Python. Railway said "Deploy complete."
# It meant an image was pushed. It did not mean Python existed inside it.
#
# So this script believes exactly one thing: GET /healthz reporting the SHA it just shipped. Not the
# CLI's exit code, and emphatically not an HTTP 200 — an unauthenticated GET of any path on Bean
# returns 200 with the passcode page, so "curl succeeded" and "the app works" are unrelated facts.
#
# THE OPERATOR'S SERVICE MUST STAY SOURCE-DISCONNECTED IN RAILWAY. Its Settings → Source panel is
# deliberately empty, and "Connect Repo" is the wrong button on that page forever. Connecting one
# turns on deploy-on-push, which routes around every line below: no preflight, no config snapshot on
# the volume, no /healthz check, no rollback. It also deploys whatever the branch happens to be
# holding — on 2026-08-05 that would have shipped a live tenant thirteen times in a day, several of
# them mid-refactor. The demo service is GitHub-connected on purpose (it is disposable and public);
# this one is not, and the emptiness of that panel is the safeguard.
#
#   1. preflight must pass (scripts/preflight.sh)
#   2. no modified TRACKED files, no unpushed commits (untracked files are fine and expected)
#   3. snapshot the operator's config to a timestamped backup ON THE VOLUME, before anything ships
#   4. deploy
#   5. poll /healthz until it reports the shipped SHA, a parsing config, and a writable volume
#   6. on mismatch or timeout: roll back to the previous SHA, confirm it is serving, exit nonzero
#
# Rollback re-uploads the previous commit's tree. This CLI has no `deployment rollback`, and
# `redeploy` only redeploys the latest — which is the broken one.
#
# Usage:  BEAN_PUBLIC_URL=https://your-host BEAN_CUSTOMER=<tenant> ./scripts/deploy.sh
#
#         Both are REQUIRED for a remote deploy and neither has a useful default — see the note on
#         HEALTH_URL below. Put them in your shell profile or a deploy env file; if you forget, the
#         health poll hits loopback, fails, and the deploy rolls back rather than lying to you.
#
#         BEAN_PREV_SHA=<sha> ./scripts/deploy.sh    # when prod cannot name its own SHA: it
#                                                    # predates /healthz, or a bare `railway up`
#                                                    # shipped it and left _build_sha.txt unwritten

set -uo pipefail
cd "$(dirname "$0")/.."

# WHERE THIS DEPLOYS TO IS CONFIGURATION, NOT A CONSTANT. Both of these used to be one specific
# instance's hostname and one specific tenant's folder, hardcoded — so a fork running this script
# would have polled somebody else's production for the SHA *it* just shipped (and happily called the
# deploy green off a stranger's health check), then tried to snapshot a config directory that does
# not exist on its volume.
#
# The health default is loopback on the server's own default port: useless for a real remote deploy,
# which is the point — the poll fails, the deploy rolls back, and you find out immediately. A
# plausible-looking wrong host is the failure mode worth engineering against.
HEALTH_URL="${BEAN_HEALTH_URL:-${BEAN_PUBLIC_URL:-http://127.0.0.1:8011}/healthz}"
SERVICE="${BEAN_SERVICE:-bean}"
# The tenant folder on the volume, mirroring bean/paths.py (data_root / customer). Prod sets
# BEAN_CUSTOMER; BEAN_VOLUME_CONFIG overrides the whole path when the layout differs.
VOLUME_CONFIG="${BEAN_VOLUME_CONFIG:-/data/${BEAN_CUSTOMER:-maplemoss}/config.json}"
POLL_TIMEOUT="${BEAN_POLL_TIMEOUT:-240}"
POLL_INTERVAL=5

SHIP_SHA="$(git rev-parse HEAD)"
ORIGINAL_REF="$(git rev-parse --abbrev-ref HEAD)"
SHA_FILE="bean/_build_sha.txt"

say()  { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }

cleanup() {
  # Never leave the operator on a detached HEAD because a deploy failed.
  local now; now="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$now" != "$ORIGINAL_REF" ]; then
    git switch "$ORIGINAL_REF" > /dev/null 2>&1 && echo "  (restored branch $ORIGINAL_REF)"
  fi
  rm -f "$SHA_FILE"
}
trap cleanup EXIT

# `/healthz` answers with JSON. The passcode page answers with HTML and the SAME 200. Parse, never
# trust the status line. Prints, space-separated:
#   <sha> <config_ok> <volume_writable> <corrections> <stalled> <drafted%>
# or nothing at all when the endpoint is absent (a build that predates it) or the body is not JSON.
#
# The last two are the USEFULNESS numbers — a deploy can go green while Bean drafts 0 of 49 real
# emails, which is exactly what happened for weeks. They are PRINTED, never gated on (see the `ok`
# note in bean/server.py::_health): a Bean that drafts nothing is not fixed by rolling back the code.
#
# `config_nodes` / `taught_leaves` / `answer_leaves` used to be parsed here too. They counted the
# routing tree, which no longer exists — /healthz stopped reporting them, and a field that always
# prints "None" reads as a broken deploy rather than a deleted metric.
health() {
  curl -fsS --max-time 10 "$HEALTH_URL" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(d, dict) or "sha" not in d:
    sys.exit(1)
print(d["sha"], d.get("config_ok"), d.get("volume_writable"),
      d.get("corrections_loaded"),
      d.get("drafting_stalled"), d.get("drafted_fraction"))
' 2>/dev/null
}

poll_for_sha() {  # $1 = sha to wait for; returns 0 on match
  local want="$1" deadline=$(( $(date +%s) + POLL_TIMEOUT )) out
  while [ "$(date +%s)" -lt "$deadline" ]; do
    out="$(health)"
    if [ -n "$out" ]; then
      set -- $out
      if [ "$1" = "$want" ] && [ "$2" = "True" ] && [ "$3" = "True" ]; then
        ok "/healthz: sha=${1:0:7} config_ok=$2 volume_writable=$3 corrections=$4"
        # Loud, and after the green tick on purpose: this is the half of health the deploy does NOT
        # gate on, so it has to be impossible to miss on the way past.
        if [ "$5" = "True" ]; then
          bad "DRAFTING STALLED — mail is arriving and Bean has drafted NONE of it"
          printf '     A green deploy does not mean a useful Bean. Check the notebook: %s\n' "$HEALTH_URL"
        else
          ok "drafting: drafted_fraction=$6 drafting_stalled=$5"
        fi
        return 0
      fi
      printf '  … serving sha=%s config_ok=%s volume=%s (want %s)\n' "${1:0:7}" "$2" "$3" "${want:0:7}"
    else
      printf '  … /healthz not answering with JSON yet\n'
    fi
    sleep "$POLL_INTERVAL"
  done
  return 1
}

rollback() {
  local prev="$1"
  say "ROLLING BACK to ${prev:0:7}"
  git switch --detach "$prev" > /dev/null 2>&1 || { bad "cannot check out $prev"; return 1; }
  echo "$prev" > "$SHA_FILE"
  railway up --ci --service "$SERVICE" 2>&1 | tail -5
  git switch "$ORIGINAL_REF" > /dev/null 2>&1

  # Confirm the old SHA is serving. If the previous build predates /healthz it cannot say its own
  # name — then the proof is that the endpoint is gone AND the app still answers. Say which one.
  local deadline=$(( $(date +%s) + POLL_TIMEOUT )) out code
  while [ "$(date +%s)" -lt "$deadline" ]; do
    out="$(health)"
    if [ -n "$out" ]; then
      set -- $out
      if [ "$1" = "$prev" ]; then ok "rollback confirmed: /healthz reports ${prev:0:7}"; return 0; fi
      printf '  … still serving %s\n' "${1:0:7}"
    else
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$HEALTH_URL")"
      if [ "$code" = "200" ] || [ "$code" = "404" ]; then
        ok "rollback confirmed: the app answers and /healthz is absent — that build predates it"
        printf '      (it cannot report its own SHA; verified by absence, not by silence)\n'
        return 0
      fi
    fi
    sleep "$POLL_INTERVAL"
  done
  bad "ROLLBACK UNCONFIRMED — prod may be down. Check $HEALTH_URL by hand."
  return 1
}

# ---- 1. preflight -----------------------------------------------------------------------------
say "preflight"
if ! ./scripts/preflight.sh > /tmp/bean-deploy-preflight.log 2>&1; then
  bad "preflight failed — refusing to deploy"; tail -25 /tmp/bean-deploy-preflight.log; exit 1
fi
ok "preflight passed"

# ---- 2. the tree must be shippable ------------------------------------------------------------
say "tree state"
if ! git diff --quiet || ! git diff --cached --quiet; then
  bad "modified tracked files — commit or stash them:"; git status --short | grep -v '^??'; exit 1
fi
ok "no modified tracked files (untracked files are fine)"

if ! git rev-parse --abbrev-ref '@{u}' > /dev/null 2>&1; then
  bad "this branch has no upstream. Push it first — a SHA that exists only on this laptop cannot be rolled back to."; exit 1
fi
if [ -n "$(git log --oneline '@{u}..HEAD')" ]; then
  bad "unpushed commits — push before deploying:"; git log --oneline '@{u}..HEAD' | sed 's/^/      /'; exit 1
fi
ok "HEAD ${SHIP_SHA:0:7} is pushed"

# ---- 3. what is prod serving now? -------------------------------------------------------------
say "current production"
PREV_OUT="$(health)"
if [ -n "$PREV_OUT" ]; then
  PREV_SHA="$(echo "$PREV_OUT" | cut -d' ' -f1)"
  ok "prod reports ${PREV_SHA:0:7}"
else
  PREV_SHA="${BEAN_PREV_SHA:-}"
  if [ -z "$PREV_SHA" ]; then
    bad "/healthz did not answer with JSON and BEAN_PREV_SHA is unset."
    bad "Without a rollback target this deploy is one-way. Refusing."
    exit 1
  fi
  ok "prod predates /healthz; rolling back to BEAN_PREV_SHA=${PREV_SHA:0:7} if this fails"
fi
if ! git cat-file -e "${PREV_SHA}^{commit}" 2>/dev/null; then
  # Prod can answer /healthz perfectly and still not know its own SHA. This script is the only
  # writer of bean/_build_sha.txt, so a container shipped by a bare `railway up` reports "unknown"
  # forever. That is a deploy this script did not make — not a corrupt prod — and the old branch
  # only consulted BEAN_PREV_SHA when /healthz was SILENT, so answering-but-unidentifiable fell
  # through to a flat refusal. One out-of-band deploy then permanently locked out the safe path
  # back. Let the operator name the target prod cannot name itself; still refuse when nobody can.
  if [ -n "${BEAN_PREV_SHA:-}" ] && git cat-file -e "${BEAN_PREV_SHA}^{commit}" 2>/dev/null; then
    ok "prod cannot name its SHA (reports '$PREV_SHA') — rolling back to BEAN_PREV_SHA=${BEAN_PREV_SHA:0:7} if this fails"
    PREV_SHA="$BEAN_PREV_SHA"
  else
    bad "rollback target $PREV_SHA is not a commit in this repo, and BEAN_PREV_SHA is unset or not a commit."
    bad "Without a rollback target this deploy is one-way. Refusing."; exit 1
  fi
fi
if [ "$PREV_SHA" = "$SHIP_SHA" ]; then ok "already serving ${SHIP_SHA:0:7} — nothing to do"; exit 0; fi

# ---- 4. back up the operator's config, on the volume, before anything ships --------------------
say "snapshot config.json on the volume"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${VOLUME_CONFIG}.bak-deploy-${STAMP}"
# --service is NOT optional here, though it reads that way. Every other railway call in this script
# names the service; this one used to rely on whatever the CLI was ambiently linked to. Link the CLI
# to another service in the project — `railway service link bean-demo`, one command, easy to do
# while poking at a different service — and this line snapshots THAT service's volume, or fails on a
# service with no volume at all, while the deploy that follows still ships to $SERVICE. The backup
# that exists to make the deploy reversible would be a backup of the wrong machine.
if ! railway ssh --service "$SERVICE" "cp '$VOLUME_CONFIG' '$BACKUP' && wc -c < '$BACKUP'" > /tmp/bean-deploy-backup.log 2>&1; then
  bad "could not snapshot $VOLUME_CONFIG — refusing to deploy"; cat /tmp/bean-deploy-backup.log; exit 1
fi
ok "backed up to $BACKUP ($(tr -dc '0-9' < /tmp/bean-deploy-backup.log) bytes)"

# ---- 5. deploy --------------------------------------------------------------------------------
say "deploy ${SHIP_SHA:0:7}"
# Railway leaves RAILWAY_GIT_COMMIT_SHA empty for CLI uploads, so hand the SHA to the image. This
# file is untracked and NOT gitignored on purpose: `railway up` honours .gitignore.
echo "$SHIP_SHA" > "$SHA_FILE"
railway up --ci --service "$SERVICE" 2>&1 | tail -8
ok "upload finished — which proves nothing until /healthz agrees"

# ---- 6. verify, or roll back ------------------------------------------------------------------
say "verify via /healthz (the only thing we believe)"
if poll_for_sha "$SHIP_SHA"; then
  say "DEPLOYED ${SHIP_SHA:0:7}"
  health | awk '{printf "  sha=%s config_ok=%s volume_writable=%s corrections_loaded=%s\n", $1, $2, $3, $4}'
  exit 0
fi

bad "prod never reported ${SHIP_SHA:0:7} within ${POLL_TIMEOUT}s"
rollback "$PREV_SHA"
exit 1
