#!/usr/bin/env bash
# Start / stop / inspect the local reef service that runs the sar smoke loop.
#
#   reef_sar_adapter/reef-service.sh start     # export the key, serve, wait for /healthz
#   reef_sar_adapter/reef-service.sh stop      # SIGTERM the stack, wait for the port to free
#   reef_sar_adapter/reef-service.sh status    # pid + health
#   reef_sar_adapter/reef-service.sh ping      # 1-token check of the frozen provider binding
#
# Everything the service owns lives in the state directory below (stack config
# state, stack logs, campaign releases, step records); stopping and re-starting
# the service keeps the scenarios and their releases. The upstream API key is
# read from the main repo's .env here, exported only, never echoed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK="$HERE/stack.yaml"
REEF_PY="/home/wyh/daily_work/reef/.venv/bin/python"
MAIN_ENV="/home/wyh/daily_work/LLaMAR/.env"
# reef's artifact backend shells out to `git lfs`; the user-space install lives
# in ~/.local/bin, which a non-login shell may not carry.
export PATH="$HOME/.local/bin:$PATH"
# Keep in sync with stack.yaml's absolute state paths.
WORK="${REEF_SAR_WORK:-/home/wyh/.cache/reef-sar-smoke/reef-service}"
PID_FILE="$WORK/serve.pid"
STDOUT_LOG="$WORK/serve.stdout"

port() {
    sed -n 's/^[[:space:]]*port:[[:space:]]*//p' "$STACK" | head -n 1
}

export_key() {
    local key
    key="$(sed -n 's/^[[:space:]]*api_key[[:space:]]*=[[:space:]]*//p' "$MAIN_ENV" | head -n 1)"
    key="${key%$'\r'}"
    key="${key%\"}"; key="${key#\"}"; key="${key%\'}"; key="${key#\'}"
    if [ -z "$key" ]; then
        echo "reef-service: no api_key in $MAIN_ENV" >&2
        exit 1
    fi
    export REEF_SAR_UPSTREAM_API_KEY="$key"
}

running_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    kill -0 "$pid" 2>/dev/null || return 1
    printf '%s\n' "$pid"
}

wait_ready() {
    local seconds="${1:-120}" started
    started="$(date +%s)"
    while true; do
        if curl -sf "http://127.0.0.1:$(port)/healthz" >/dev/null 2>&1; then
            return 0
        fi
        if ! running_pid >/dev/null; then
            echo "reef-service: the stack exited during startup; tail of $STDOUT_LOG:" >&2
            tail -n 40 "$STDOUT_LOG" >&2 || true
            return 1
        fi
        if [ "$(( $(date +%s) - started ))" -ge "$seconds" ]; then
            echo "reef-service: not ready after ${seconds}s; tail of $STDOUT_LOG:" >&2
            tail -n 40 "$STDOUT_LOG" >&2 || true
            return 1
        fi
        sleep 1
    done
}

case "${1:-}" in
    ping)
        # Freeze-check: one 1-token call to the binding stack.yaml declares.
        export_key
        env -u PYTHONPATH "$REEF_PY" -m reef_sar_adapter.ping_upstream
        ;;
    start)
        if running_pid >/dev/null; then
            echo "reef-service: already running (pid $(running_pid)), port $(port)"
            exit 0
        fi
        mkdir -p "$WORK"
        command -v git-lfs >/dev/null || {
            echo "reef-service: git-lfs not on PATH; reef's artifact backend requires it" >&2
            exit 1
        }
        export_key
        cd "$WORK"
        # `env -u PYTHONPATH`: the ROS environment must not leak into the stack.
        nohup env -u PYTHONPATH "$REEF_PY" -m reef.service.deploy -c "$STACK" \
            >>"$STDOUT_LOG" 2>&1 &
        echo $! >"$PID_FILE"
        if wait_ready 120; then
            echo "reef-service: up (pid $(running_pid), port $(port), logs $WORK/stack/reef.log)"
        else
            rm -f "$PID_FILE"
            exit 1
        fi
        ;;
    stop)
        if ! running_pid >/dev/null; then
            rm -f "$PID_FILE"
            echo "reef-service: not running"
            exit 0
        fi
        pid="$(running_pid)"
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 60); do
            if ! kill -0 "$pid" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "reef-service: pid $pid still alive after 60s; sending SIGKILL" >&2
            kill -9 "$pid" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$PID_FILE"
        if ss -ltn 2>/dev/null | grep -q ":$(port) "; then
            echo "reef-service: port $(port) is still listening" >&2
            exit 1
        fi
        echo "reef-service: stopped, port $(port) released"
        ;;
    status)
        if running_pid >/dev/null; then
            echo "reef-service: running (pid $(running_pid), port $(port))"
        else
            echo "reef-service: not running (port $(port) $(ss -ltn 2>/dev/null | grep -q ":$(port) " && echo 'busy' || echo 'free'))"
        fi
        curl -sf "http://127.0.0.1:$(port)/healthz" && echo || true
        ;;
    *)
        echo "usage: reef-service.sh {start|stop|status}" >&2
        exit 2
        ;;
esac
