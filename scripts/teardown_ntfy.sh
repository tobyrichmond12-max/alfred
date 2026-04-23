#!/usr/bin/env bash
# Tear down the self-hosted ntfy fallback once PWA Web Push is validated.
#
# Safety: refuses to run if data/push_subscriptions.json is empty, since
# removing ntfy while no PWA subscribers exist would leave Alfred with
# no push channel at all. Override with --force if you really mean it.
#
# What this script does:
#   1. Confirm at least one PWA subscriber is registered (unless --force).
#   2. Stop and remove the `ntfy` Docker container.
#   3. Drop the `tailscale serve --https=8443` proxy rule.
#   4. Strip the `NTFY_URL` / `NTFY_TOPIC` entries from core/notify.py call
#      sites (manual step, flagged).
#
# Usage:
#   ./scripts/teardown_ntfy.sh           # safe, gated on PWA subs
#   ./scripts/teardown_ntfy.sh --force   # skip the subscriber check

set -euo pipefail

ALFRED_HOME="/mnt/nvme/alfred"
SUBS_FILE="$ALFRED_HOME/data/push_subscriptions.json"

force=0
if [[ "${1:-}" == "--force" ]]; then
  force=1
fi

if [[ $force -eq 0 ]]; then
  if [[ ! -s "$SUBS_FILE" ]]; then
    echo "refusing: $SUBS_FILE missing or empty."
    echo "enable notifications on the PWA first, or rerun with --force."
    exit 1
  fi
  count=$(python3 -c "import json; print(len(json.load(open('$SUBS_FILE'))))" 2>/dev/null || echo 0)
  if [[ "$count" -lt 1 ]]; then
    echo "refusing: 0 PWA subscribers in $SUBS_FILE."
    echo "enable notifications on the PWA first, or rerun with --force."
    exit 1
  fi
  echo "proceeding: $count PWA subscriber(s) registered."
fi

echo
echo "[1/3] stopping and removing ntfy container"
if docker ps -a --format '{{.Names}}' | grep -qx ntfy; then
  docker stop ntfy || true
  docker rm ntfy || true
else
  echo "  (no ntfy container found; already clean)"
fi

echo
echo "[2/3] dropping tailscale serve :8443 rule"
if sudo tailscale serve status 2>/dev/null | grep -q ":8443"; then
  sudo tailscale serve --https=8443 off
else
  echo "  (no :8443 serve rule found; already clean)"
fi

echo
echo "[3/3] remaining manual step:"
echo "  trim NTFY_URL / NTFY_TOPIC env vars from .env if you set them."
echo "  core/notify.py still exposes push()/push_urgent()/push_routine() but"
echo "  they will just return False once ntfy is gone. reflect.py and"
echo "  weekly_review.py tolerate that, push_web is the live transport."
echo
echo "done."
