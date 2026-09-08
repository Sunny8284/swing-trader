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
# --protocol http2 forces TCP instead of the default QUIC/UDP, which some
# networks block or throttle (symptom: tunnel registers but all requests
# return HTTP 000 with "QUIC stream: timeout: no recent network activity").
"$CLOUDFLARED" tunnel --protocol http2 --url http://localhost:8000 > "$TUNNEL_LOG" 2>&1 &
CF_PID=$!

NEW_URL=""
for _ in $(seq 1 30); do
    # Only the banner URL counts. When tunnel creation FAILS, cloudflared logs
    # its own control-plane endpoint (https://api.trycloudflare.com) in the
    # error, and a bare grep happily captured that and shipped it to Vercel as
    # the public API URL — a guaranteed-dead dashboard. Exclude that host.
    NEW_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" \
              | grep -v '^https://api\.trycloudflare\.com$' | head -1)
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

# ── 3b. Wait for the URL to actually be reachable ────────────────────────────
# A fresh quick-tunnel hostname is NOT live the moment cloudflared prints it —
# cloudflared says so itself ("it may take some time to be reachable"), and in
# practice it takes 1-5 minutes. The old code checked once after 3 seconds,
# logged a warning, and published to Vercel regardless. That guaranteed a
# window where production pointed at a hostname that did not resolve yet, and
# it is what put the dashboard offline this morning.
#
# Publish only what we have proven works. If it never comes up, do NOT touch
# Vercel — leave production pointing at the last URL that did work, and exit so
# launchd respawns us with a fresh tunnel.
URL_WAIT_SECONDS=360
URL_POLL_INTERVAL=10
url_live=0
waited=0
while [ "$waited" -lt "$URL_WAIT_SECONDS" ]; do
    if [ "$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$NEW_URL/api/health" 2>/dev/null)" = "200" ]; then
        url_live=1
        log "Tunnel reachable after ${waited}s"
        break
    fi
    if ! kill -0 "$CF_PID" 2>/dev/null; then
        log "ERROR: cloudflared died while waiting for URL to go live"
        exit 1
    fi
    sleep "$URL_POLL_INTERVAL"
    waited=$((waited + URL_POLL_INTERVAL))
done

if [ "$url_live" -ne 1 ]; then
    log "ERROR: $NEW_URL never returned 200 after ${URL_WAIT_SECONDS}s — not publishing it"
    kill "$CF_PID" 2>/dev/null || true
    exit 1
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

# No DNS grace period is needed any more: step 3b already proved this URL
# serves 200 before we published it, so a failure here is a real regression
# rather than a hostname that has not warmed up yet.
#
# But this Mac sleeps, and `sleep 120` sleeps with it — the logs show "2 minute"
# checks landing 15-40 minutes apart. A check straight out of suspend runs
# before Wi-Fi has reassociated, fails for reasons that say nothing about the
# tunnel, and used to count toward tearing it down. Detect the time warp and
# re-test after the network settles instead of counting it.
SLEEP_SKEW_TOLERANCE=60
NETWORK_SETTLE_SECONDS=20

check_health() {
    local code
    code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$NEW_URL/api/health" 2>/dev/null || echo 000)
    code=$(echo "$code" | tr -d '[:space:]')
    [ -z "$code" ] && code="000"
    echo "$code"
}

while kill -0 "$CF_PID" 2>/dev/null; do
    before=$(date +%s)
    sleep "$HEALTH_CHECK_INTERVAL"
    elapsed=$(( $(date +%s) - before ))
    code=$(check_health)

    # Woke from suspend: give the network a moment, then re-test once. Only the
    # retry's verdict counts.
    if [ "$code" != "200" ] && [ "$elapsed" -gt $((HEALTH_CHECK_INTERVAL + SLEEP_SKEW_TOLERANCE)) ]; then
        log "Woke after ${elapsed}s asleep (expected ${HEALTH_CHECK_INTERVAL}s) — re-testing in ${NETWORK_SETTLE_SECONDS}s"
        sleep "$NETWORK_SETTLE_SECONDS"
        code=$(check_health)
    fi

    if [ "$code" = "200" ]; then
        HEALTH_FAILS=0
    else
        # The tunnel is only half the story: FastAPI can die underneath it
        # (e.g. launchd kills the previous job's process group after a new
        # instance already passed the step-1 port check). Revive the origin
        # before blaming — and churning — the tunnel.
        if ! lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
            log "Origin :8000 is down — restarting FastAPI"
            cd "$PROJECT_DIR"
            nohup "$VENV_PYTHON" main.py api >> "$API_LOG" 2>&1 &
            disown
            sleep 4
            if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
                log "FastAPI back up; tunnel left intact"
                HEALTH_FAILS=0
                continue
            fi
            log "ERROR: FastAPI restart failed; see $API_LOG"
        fi
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
