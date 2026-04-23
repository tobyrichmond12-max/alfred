#!/usr/bin/env bash
# Quick health check across Alfred's moving parts. Run after a deploy or a
# sleepy morning when you want to know Alfred is alive end to end without
# waking him up over voice.
#
# Usage: ./scripts/smoke_test.sh
#
# Exit code is non-zero if anything fails. Each check prints its own line
# so the transcript reads top-to-bottom.

set -uo pipefail

BRIDGE="https://<jetson-tailscale-hostname>"
ALFRED_HOME="/mnt/nvme/alfred"
fails=0

ok()   { echo "  OK  $*"; }
warn() { echo "  WARN $*"; }
fail() { echo "  FAIL $*"; fails=$((fails + 1)); }

echo "[1/8] bridge /health"
payload=$(curl -sS --max-time 5 "$BRIDGE/health" || true)
if [[ -z "$payload" ]]; then
  fail "no response"
elif echo "$payload" | grep -q '"ok":true'; then
  ok "$payload"
  if echo "$payload" | grep -q '"state_stale":true'; then
    warn "state_stale=true, sync cron may be dead"
  fi
else
  fail "$payload"
fi

echo "[2/8] bridge /test"
if curl -sS --max-time 5 "$BRIDGE/test" | grep -q "alive"; then
  ok "alive"
else
  fail "/test did not reply 'alive'"
fi

echo "[3/8] systemd alfred-bridge active"
if systemctl is-active --quiet alfred-bridge; then
  ok "active"
else
  fail "alfred-bridge not active"
fi

echo "[4/8] cron health"
last_sync=$(tail -n 1 "$ALFRED_HOME/logs/sync.log" 2>/dev/null || echo "")
if echo "$last_sync" | grep -q "Saved"; then
  ok "$last_sync"
else
  warn "latest sync.log line: ${last_sync:-empty}"
fi

echo "[5/8] current_state.json parseable"
if python3 -c "import json; json.load(open('$ALFRED_HOME/current_state.json'))"; then
  ok "state parses"
else
  fail "state does not parse"
fi

echo "[6/8] memory.db row counts"
if [[ -f "$ALFRED_HOME/data/memory.db" ]]; then
  total=$(sqlite3 "$ALFRED_HOME/data/memory.db" "SELECT COUNT(*) FROM memories")
  slugged=$(sqlite3 "$ALFRED_HOME/data/memory.db" "SELECT COUNT(*) FROM memories WHERE slug IS NOT NULL")
  ok "memories total=$total, vault-indexed=$slugged"
else
  fail "memory.db missing"
fi

echo "[7/8] push subscriptions"
if [[ -f "$ALFRED_HOME/data/push_subscriptions.json" ]]; then
  count=$(python3 -c "import json; print(len(json.load(open('$ALFRED_HOME/data/push_subscriptions.json'))))" 2>/dev/null || echo 0)
  if [[ "$count" -gt 0 ]]; then
    ok "$count PWA subscriber(s)"
  else
    warn "0 PWA subscribers, ntfy is the active push path"
  fi
else
  warn "push_subscriptions.json missing"
fi

echo "[8/8] laptop MCP reachable"
if python3 -c "import sys; sys.path.insert(0,'$ALFRED_HOME/core'); from screen import get_screen_state; s=get_screen_state(); sys.exit(0 if s.get('ok') else 1)" 2>/dev/null; then
  ok "laptop reachable"
else
  warn "laptop MCP unreachable (laptop off or off tailnet, not fatal)"
fi

echo
if [[ $fails -eq 0 ]]; then
  echo "smoke test OK"
  exit 0
fi
echo "smoke test failed with $fails error(s)"
exit 1
