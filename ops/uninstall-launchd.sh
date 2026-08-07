#!/usr/bin/env bash
#
# Remove the local-fitness launchd jobs (macOS).
# Unloads each agent and deletes the installed plist. Safe to run if a job
# was never installed.
#
# Usage:  ./ops/uninstall-launchd.sh            # both jobs
#         ./ops/uninstall-launchd.sh briefmail  # just the evening email job
set -euo pipefail

if [[ $# -gt 0 ]]; then JOBS=("$@"); else JOBS=(brief briefmail); fi

for job in "${JOBS[@]}"; do
  LABEL="com.localfitness.$job"
  PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

  if [[ -f "$PLIST_DEST" ]]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo "Removed $LABEL ($PLIST_DEST)"
  else
    echo "$LABEL not installed (no plist at $PLIST_DEST) — nothing to do."
  fi
done
