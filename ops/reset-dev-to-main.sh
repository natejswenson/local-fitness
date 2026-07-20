#!/usr/bin/env bash
# Reset `dev` onto `main` after a `dev -> main` promotion.
#
# Why this exists: promotions squash-merge `dev` into `main`, which leaves `dev`
# with a diverged history (identical tree, but ahead/behind by one commit). The
# next promotion PR then shows phantom diffs. This script force-resets `dev` to
# `main`'s SHA so the histories line up again.
#
# `dev` is a protected branch (no force pushes, linear history). The only way to
# force-update it is to briefly flip `allow_force_pushes` on, push, and flip it
# back — exactly the manual dance documented in CLAUDE.md, now scripted so it's
# identical whether a human or the CI Action runs it.
#
# Requires: `gh` authenticated with a token that has ADMIN (administration:write
# + contents:write) on the repo. The default Actions GITHUB_TOKEN does NOT have
# this — the workflow passes a PAT via GH_TOKEN. A repo admin's local `gh` works.
#
# Idempotent: if `dev` already equals `main`, it does nothing.
#
# Usage: ops/reset-dev-to-main.sh [owner/repo]
set -euo pipefail

REPO="${1:-${GITHUB_REPOSITORY:-natejswenson/local-fitness}}"
BASE="main"
TARGET="dev"
PROT_PATH="repos/${REPO}/branches/${TARGET}/protection"

base_sha=$(gh api "repos/${REPO}/git/ref/heads/${BASE}" --jq '.object.sha')
target_sha=$(gh api "repos/${REPO}/git/ref/heads/${TARGET}" --jq '.object.sha')

if [ "$base_sha" = "$target_sha" ]; then
  echo "✓ ${TARGET} already at ${BASE} (${base_sha:0:12}) — nothing to reset."
  exit 0
fi

echo "Resetting ${TARGET} (${target_sha:0:12}) → ${BASE} (${base_sha:0:12})"

snapshot="$(mktemp)"
gh api "$PROT_PATH" > "$snapshot"

# Re-marshal the protection GET payload into the shape the PUT endpoint expects,
# toggling only allow_force_pushes. Everything else is preserved verbatim so the
# branch's protection is byte-identical before and after.
build_body() {
  local force="$1"
  jq --argjson force "$force" '{
    required_status_checks: (if .required_status_checks then
      {strict: .required_status_checks.strict, contexts: (.required_status_checks.contexts // [])}
      else null end),
    enforce_admins: (.enforce_admins.enabled // false),
    required_pull_request_reviews: (if .required_pull_request_reviews then {
      dismiss_stale_reviews: (.required_pull_request_reviews.dismiss_stale_reviews // false),
      require_code_owner_reviews: (.required_pull_request_reviews.require_code_owner_reviews // false),
      required_approving_review_count: (.required_pull_request_reviews.required_approving_review_count // 0),
      require_last_push_approval: (.required_pull_request_reviews.require_last_push_approval // false)
    } else null end),
    restrictions: (if .restrictions then {
      users: [.restrictions.users[].login], teams: [.restrictions.teams[].slug], apps: [.restrictions.apps[].slug]
    } else null end),
    required_linear_history: (.required_linear_history.enabled // false),
    allow_force_pushes: ($force == 1),
    allow_deletions: (.allow_deletions.enabled // false),
    block_creations: (.block_creations.enabled // false),
    required_conversation_resolution: (.required_conversation_resolution.enabled // false)
  }' "$snapshot"
}

# Always restore protection, even if the force-update fails midway. The
# restore PUT itself retries: a single transient API failure here (two live
# HTTP 503s on 2026-07-20) used to leave the protected branch force-pushable
# until someone noticed by hand.
restore() {
  echo "Restoring ${TARGET} protection (allow_force_pushes=false)"
  for attempt in 1 2 3 4 5; do
    if build_body 0 | gh api -X PUT "$PROT_PATH" --input - > /dev/null; then
      return 0
    fi
    echo "protection restore failed (attempt ${attempt}/5) — retrying in 15s" >&2
    sleep 15
  done
  echo "✗ FAILED to restore ${TARGET} protection after 5 attempts —" >&2
  echo "  allow_force_pushes may still be ON; re-run this script or PUT ${PROT_PATH} manually." >&2
  return 1
}
trap restore EXIT

echo "Enabling force pushes on ${TARGET}"
build_body 1 | gh api -X PUT "$PROT_PATH" --input - > /dev/null

echo "Force-updating ${TARGET} → ${base_sha:0:12}"
gh api -X PATCH "repos/${REPO}/git/refs/heads/${TARGET}" -f sha="$base_sha" -F force=true > /dev/null

# trap restores protection here.
trap - EXIT
restore

final=$(gh api "repos/${REPO}/git/ref/heads/${TARGET}" --jq '.object.sha')
if [ "$final" = "$base_sha" ]; then
  echo "✓ ${TARGET} reset to ${BASE} (${final:0:12})"
else
  echo "✗ reset verification failed: ${TARGET} is ${final:0:12}, expected ${base_sha:0:12}" >&2
  exit 1
fi
