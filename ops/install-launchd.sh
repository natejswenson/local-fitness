#!/usr/bin/env bash
#
# Install the local-fitness launchd jobs (macOS).
#
# Resolves host-specific absolute paths (the `uv` binary + this repo's
# root), fills them into each ops/*.plist.template, writes the results to
# ~/Library/LaunchAgents/, and (re)loads them. Idempotent: re-running
# unloads any existing job first.
#
# Two jobs:
#   com.localfitness.brief      06:30 (+09:30 backstop) — `fitness brief
#                               --if-missing`, generates and saves the day's
#                               brief. Needs a Claude credential.
#   com.localfitness.briefmail  19:00 (+20:00 backstop) — `fitness brief-email
#                               --if-unsent`, pulls fresh Garmin data,
#                               regenerates the brief against a full day of
#                               it, and emails it. Needs a Claude credential
#                               AND SMTP settings in <repo>/.env.
#
# Both are LaunchAgents, not LaunchDaemons, and must stay that way: the
# bundled Claude SDK CLI reads its credential from the login keychain, which
# is reachable from the user's security session and not from a system daemon.
#
# Usage:  ./ops/install-launchd.sh            # both jobs
#         ./ops/install-launchd.sh brief      # just the morning job
#         ./ops/install-launchd.sh briefmail  # just the evening email job
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer is macOS-only (launchd). On Linux, schedule" >&2
  echo "'uv run fitness brief' and 'uv run fitness brief-email' with" >&2
  echo "cron/systemd instead." >&2
  exit 1
fi

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  echo "Could not find 'uv' on PATH. Install uv (https://docs.astral.sh/uv/)" >&2
  echo "then re-run this script." >&2
  exit 1
fi

# Which jobs to install. No argument = all of them.
if [[ $# -gt 0 ]]; then JOBS=("$@"); else JOBS=(brief briefmail); fi

if [[ ! -f "$REPO_ROOT/.env" ]]; then
  echo "warning: $REPO_ROOT/.env not found — the scheduled jobs read their" >&2
  echo "         credentials from it (the CLI auto-loads .env)." >&2
  echo "         Installing anyway; create .env before the first fire." >&2
fi

# The evening job cannot send without a password, and discovering that at
# 19:00 via a log file is worse than being told now.
if [[ " ${JOBS[*]} " == *" briefmail "* ]]; then
  if ! grep -qE '^\s*LOCAL_FITNESS_SMTP_PASSWORD=\S' "$REPO_ROOT/.env" 2>/dev/null; then
    echo "warning: LOCAL_FITNESS_SMTP_PASSWORD is unset or empty in .env." >&2
    echo "         com.localfitness.briefmail will exit(2) without sending." >&2
    echo "         Get an app password: myaccount.google.com/apppasswords" >&2
  fi
fi

mkdir -p "$REPO_ROOT/logs"
mkdir -p "$HOME/Library/LaunchAgents"

for job in "${JOBS[@]}"; do
  LABEL="com.localfitness.$job"
  TEMPLATE="$SCRIPT_DIR/$LABEL.plist.template"
  PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

  if [[ ! -f "$TEMPLATE" ]]; then
    echo "No template for '$job' at $TEMPLATE" >&2
    exit 1
  fi

  # Render the template with the resolved absolute paths. Using a non-`/`
  # sed delimiter so paths containing `/` substitute cleanly.
  sed -e "s|__UV_BIN__|$UV_BIN|g" \
      -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
      "$TEMPLATE" > "$PLIST_DEST"

  # Reload: unload an existing instance (ignore "not loaded"), then load.
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
  launchctl load -w "$PLIST_DEST"

  echo "Installed $LABEL"
  echo "  plist:  $PLIST_DEST"
  case "$job" in
    brief)
      echo "  runs:   $UV_BIN run --directory $REPO_ROOT fitness brief --if-missing"
      echo "  when:   daily 06:30, backstop 09:30"
      ;;
    briefmail)
      echo "  runs:   $UV_BIN run --directory $REPO_ROOT fitness brief-email --if-unsent"
      echo "  when:   daily 19:00, backstop 20:00"
      ;;
  esac
  echo "  logs:   $REPO_ROOT/logs/$job.launchd.{out,err}.log"
  echo
done

echo "Run one now to verify:  launchctl start com.localfitness.briefmail"
echo "Dry-run the email:      uv run fitness brief-email --no-pull --no-generate \\"
echo "                          --dry-run /tmp/brief.eml"
echo "Uninstall:              ./ops/uninstall-launchd.sh"
