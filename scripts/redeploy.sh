#!/usr/bin/env bash
# Restart the bridge and monitor its log for anomalies.
#
# Usage:
#   scripts/redeploy.sh                # default 300s monitor window
#   scripts/redeploy.sh 60              # short window (smoke test)
#   scripts/redeploy.sh 0               # restart only, no monitor
#
# Exit code:
#   0  clean startup, no anomalies in window
#   1  bridge failed to start, or anomalies detected
#   2  startup crashed or PID missing after start
#
# Known-benign log patterns (filtered out, not treated as anomalies):
#   MCP 429 / MCP HTTP error 429          — already retried with backoff
#   reactions.add failed: missing_scope   — known limitation of user token
#   Rate limit reached .* Deferred        — internal throttle, auto-retries
#   Bot not in .* attempting invite       — normal first-post flow
set -euo pipefail

WINDOW=${1:-300}
LOG=${CLAUDE_REMOTE_LOG:-$HOME/.claude-remote/bridge.log}
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
BRIDGE="$REPO/bridge.py"

BENIGN_GREP=(
    -e "MCP HTTP error 429"
    -e "MCP 429 on"
    -e "reactions.add failed: missing_scope"
    -e "Rate limit reached .* Deferred"
    -e "Bot not in .* attempting invite"
    -e "Could not auto-resolve SLACK_USER_ID"
)

timestamp() { date +"%H:%M:%S"; }
log_info()  { printf "[%s] %s\n" "$(timestamp)" "$*"; }
log_warn()  { printf "[%s] !! %s\n" "$(timestamp)" "$*" >&2; }

if [[ ! -x "$PY" ]]; then
    log_warn "venv python not found at $PY"
    exit 2
fi

log_info "stopping bridge (if running)"
"$PY" "$BRIDGE" stop >/dev/null 2>&1 || true
sleep 1

# Mark the log position BEFORE starting, so we only scan new lines
BEFORE_BYTES=$(wc -c < "$LOG" 2>/dev/null | awk '{print $1+0}' || echo 0)

log_info "starting bridge"
if ! "$PY" "$BRIDGE" start --slack >/dev/null 2>&1; then
    log_warn "bridge.py start returned non-zero"
    exit 2
fi

# Wait for the startup banner to land in the log
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    if tail -c +"$((BEFORE_BYTES+1))" "$LOG" | grep -q "Slack mode:"; then
        break
    fi
done

MODE_LINE=$(tail -c +"$((BEFORE_BYTES+1))" "$LOG" | grep "Slack mode:" | tail -1 || true)
if [[ -z "$MODE_LINE" ]]; then
    log_warn "bridge did not print startup banner within 10s"
    tail -c +"$((BEFORE_BYTES+1))" "$LOG" | tail -20 >&2
    exit 2
fi
log_info "startup: ${MODE_LINE#*INFO }"

PID=$(pgrep -f "bridge.py run --slack" || true)
log_info "bridge pid: ${PID:-none}"

if [[ "$WINDOW" -eq 0 ]]; then
    log_info "window=0, skipping monitor"
    exit 0
fi

log_info "monitoring log for ${WINDOW}s for anomalies"

SCAN_FROM=$((BEFORE_BYTES+1))
DEADLINE=$(($(date +%s) + WINDOW))
INTERVAL=30
ANOMALIES_FILE=$(mktemp)
trap 'rm -f "$ANOMALIES_FILE"' EXIT

while [[ $(date +%s) -lt $DEADLINE ]]; do
    LEFT=$((DEADLINE - $(date +%s)))
    SLEEP=$(( LEFT < INTERVAL ? LEFT : INTERVAL ))
    [[ $SLEEP -gt 0 ]] && sleep "$SLEEP"

    # Anomaly = ERROR or WARNING not in benign list
    NEW=$(tail -c +"$SCAN_FROM" "$LOG" | grep -E " (ERROR|WARNING) " | grep -v "${BENIGN_GREP[@]}" || true)
    if [[ -n "$NEW" ]]; then
        echo "$NEW" >> "$ANOMALIES_FILE"
    fi

    ERRORS=$(tail -c +"$SCAN_FROM" "$LOG" | grep -cE " ERROR " || true)
    WARNS=$(tail -c +"$SCAN_FROM" "$LOG" | grep -cE " WARNING " || true)
    BENIGN=$(tail -c +"$SCAN_FROM" "$LOG" | grep -cE "$(IFS=\|; echo "${BENIGN_GREP[*]#-e }" | tr ' ' '|')" || true)
    POLLS=$(tail -c +"$SCAN_FROM" "$LOG" | grep -c "Cross-channel: found" || true)
    log_info "elapsed=$((WINDOW - LEFT))s errors=$ERRORS warns=$WARNS benign=$BENIGN mentions_seen=$POLLS"
done

ANOM_COUNT=$(wc -l < "$ANOMALIES_FILE" | awk '{print $1+0}')
if [[ "$ANOM_COUNT" -gt 0 ]]; then
    log_warn "anomalies detected ($ANOM_COUNT lines):"
    sort -u "$ANOMALIES_FILE" | head -20 >&2
    exit 1
fi

log_info "clean window - no anomalies"
exit 0
