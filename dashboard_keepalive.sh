#!/bin/bash
# dashboard_keepalive.sh
#
# Keeps the dashboard backend alive across Mac sleep/wake/network changes:
#   1. Starts FastAPI on :8000 if not already running
#   2. Starts cloudflared Quick Tunnel
#   3. Captures the random *.trycloudflare.com URL it generates
#   4. Updates Vercel env NEXT_PUBLIC_API_URL and redeploys the production
#      build of swing-trader-dashboard to point at the new URL
#   5. Blocks on cloudflared. When it dies, exits non-zero so launchd restarts
#      this script — which loops back to step 2 with a fresh URL
#
# Designed to run under launchd (see com.nithun.dashboardtunnel.plist) with
# KeepAlive=true so any failure is auto-recovered.

set -uo pipefail

PROJECT_DIR="/Users/nithun/Documents/Swing trader"
DASHBOARD_DIR="/Users/nithun/swing-trader-dashboard"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
CLOUDFLARED="/opt/homebrew/bin/cloudflared"
VERCEL="/usr/local/bin/vercel"

LOG_DIR="$HOME/Library/Logs/SwingTrader"
TUNNEL_LOG="$LOG_DIR/cloudflared.log"
API_LOG="$LOG_DIR/api.log"
KEEPALIVE_LOG="$LOG_DIR/keepalive.log"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$KEEPALIVE_LOG"
}

log "─── dashboard_keepalive starting ───"

# ── 1. Ensure FastAPI is up ────────────────────────────────────────────────────
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    log "FastAPI already on :8000"
else
    log "Starting FastAPI..."
    cd "$PROJECT_DIR"
    nohup "$VENV_PYTHON" main.py api > "$API_LOG" 2>&1 &
    disown
    sleep 4
    if ! lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
        log "ERROR: FastAPI failed to start; see $API_LOG"
        exit 1
    fi
fi

# ── 2. Kill any orphan cloudflared from a previous run ────────────────────────
# (only the trycloudflare ones — leave any other cloudflared services alone)
pkill -f "cloudflared.*trycloudflare\|cloudflared tunnel --url http://localhost:8000" 2>/dev/null || true
sleep 1

# ── 3. Start cloudflared and capture its URL ──────────────────────────────────
: > "$TUNNEL_LOG"
log "Starting cloudflared..."
"$CLOUDFLARED" tunnel --url http://localhost:8000 > "$TUNNEL_LOG" 2>&1 &
CF_PID=$!

NEW_URL=""
for _ in $(seq 1 30); do
    NEW_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)
    if [ -n "$NEW_URL" ]; then
        break
    fi
    if ! kill -0 "$CF_PID" 2>/dev/null; then
        log "ERROR: cloudflared died before producing URL"
        exit 1
    fi
    sleep 1
done

if [ -z "$NEW_URL" ]; then
    log "ERROR: timed out waiting for cloudflared URL"
    kill "$CF_PID" 2>/dev/null || true
    exit 1
fi

log "New tunnel URL: $NEW_URL"

# Health check before bothering Vercel
sleep 3
if ! curl -s -m 10 -o /dev/null -w "%{http_code}" "$NEW_URL/api/health" | grep -q '^200$'; then
    log "WARN: tunnel up but /api/health didn't return 200; updating Vercel anyway"
fi

# ── 4. Update Vercel env and redeploy production ──────────────────────────────
cd "$DASHBOARD_DIR"
log "Removing old NEXT_PUBLIC_API_URL..."
"$VERCEL" env rm NEXT_PUBLIC_API_URL production --yes >>"$KEEPALIVE_LOG" 2>&1 || true
log "Setting new NEXT_PUBLIC_API_URL=$NEW_URL"
"$VERCEL" env add NEXT_PUBLIC_API_URL production --value="$NEW_URL" --no-sensitive --yes >>"$KEEPALIVE_LOG" 2>&1
log "Triggering production redeploy..."
"$VERCEL" --prod --yes 2>&1 | tail -20 >>"$KEEPALIVE_LOG"
log "Vercel redeploy complete"

# ── 5. Health-check loop while cloudflared runs ──────────────────────────────
# cloudflared can be alive yet broken — when its QUIC control stream fails
# (a sleep/wake glitch we hit repeatedly) it stays up in a retry loop and a
# bare `wait $CF_PID` would block forever, silently leaving the dashboard
# offline. Poll the public URL and force a respawn if it goes unhealthy.

HEALTH_FAIL_THRESHOLD=3
HEALTH_CHECK_INTERVAL=120
HEALTH_FAILS=0

while kill -0 "$CF_PID" 2>/dev/null; do
    sleep "$HEALTH_CHECK_INTERVAL"
    code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$NEW_URL/api/health" 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then
        HEALTH_FAILS=0
    else
        HEALTH_FAILS=$((HEALTH_FAILS + 1))
        log "Health check failed (HTTP $code, $HEALTH_FAILS/$HEALTH_FAIL_THRESHOLD)"
        if [ "$HEALTH_FAILS" -ge "$HEALTH_FAIL_THRESHOLD" ]; then
            log "Tunnel unhealthy — killing cloudflared so launchd respawns us"
            kill "$CF_PID" 2>/dev/null || true
            break
        fi
    fi
done

wait "$CF_PID" 2>/dev/null
log "cloudflared exited — letting launchd respawn this script"
exit 1
