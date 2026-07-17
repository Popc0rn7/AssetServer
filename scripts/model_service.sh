#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${ASSETSERVER_MODEL_PYTHON:-$PWD/.venv/bin/python}"
STATE_ROOT="${ASSETSERVER_MODEL_STATE_ROOT:-$PWD/data/runtime/model-services}"
LOG_ROOT="${ASSETSERVER_MODEL_LOG_ROOT:-$PWD/data/logs/model-services}"
OPENCLIP_GPU="${OPENCLIP_GPU:-0}"
SAM3D_GPU="${SAM3D_GPU:-1}"
OPENCLIP_READY_TIMEOUT="${OPENCLIP_READY_TIMEOUT:-300}"
SAM3D_READY_TIMEOUT="${SAM3D_READY_TIMEOUT:-900}"

mkdir -p "$STATE_ROOT" "$LOG_ROOT"

usage() {
    cat <<'EOF'
Usage: scripts/model_service.sh COMMAND [SERVICE] [OPTIONS]

Commands:
  start SERVICE       Start openclip, sam3d, or all
  stop SERVICE        Stop openclip, sam3d, or all
  restart SERVICE     Restart openclip, sam3d, or all
  status [SERVICE]    Show managed process and readiness status
  check [SERVICE]     Fail unless the service is ready
  logs SERVICE        Follow the service log

Options for start/restart:
  --gpu INDEX         Override the service's GPU index
  --no-wait           Return without waiting for readiness

Environment defaults:
  OPENCLIP_GPU=0, SAM3D_GPU=1
  OPENCLIP_READY_TIMEOUT=300, SAM3D_READY_TIMEOUT=900
EOF
}

validate_service() {
    case "$1" in openclip|sam3d|all) ;; *) echo "unknown service: $1" >&2; exit 2 ;; esac
}

pid_file() { echo "$STATE_ROOT/$1.pid"; }
log_file() { echo "$LOG_ROOT/$1.log"; }
port_for() { if [ "$1" = openclip ]; then echo 7006; else echo 7000; fi; }
ready_url() { echo "http://127.0.0.1:$(port_for "$1")/health/ready"; }

managed_pid() {
    local file
    file="$(pid_file "$1")"
    [ -s "$file" ] || return 1
    local pid
    pid="$(cat "$file")"
    if kill -0 "$pid" 2>/dev/null; then
        echo "$pid"
        return 0
    fi
    rm -f "$file"
    return 1
}

is_ready() {
    curl --fail --silent --max-time 3 "$(ready_url "$1")" \
        | grep -q '"status":"ready"'
}

wait_ready() {
    local service="$1"
    local timeout="$2"
    local started=$SECONDS
    while (( SECONDS - started < timeout )); do
        if is_ready "$service"; then
            echo "$service is ready at $(ready_url "$service")"
            return 0
        fi
        if ! managed_pid "$service" >/dev/null; then
            echo "error: $service exited during startup; see $(log_file "$service")" >&2
            tail -n 40 "$(log_file "$service")" >&2 || true
            return 1
        fi
        sleep 2
    done
    echo "error: $service was not ready after ${timeout}s" >&2
    return 1
}

require_runtime() {
    [ -x "$PYTHON" ] || {
        echo "error: current project environment is missing: $PYTHON" >&2
        echo "run: uv sync --extra openclip --extra sam3d" >&2
        exit 1
    }
}

start_one() {
    local service="$1"
    local gpu="$2"
    local wait="$3"
    local timeout
    require_runtime

    if managed_pid "$service" >/dev/null; then
        echo "$service is already managed (pid $(managed_pid "$service"))"
        return 0
    fi
    if is_ready "$service"; then
        echo "error: $service is already ready on port $(port_for "$service") but is not managed by this script" >&2
        return 1
    fi

    local logfile pidfile
    logfile="$(log_file "$service")"
    pidfile="$(pid_file "$service")"

    if [ "$service" = openclip ]; then
        "$PYTHON" -c 'import open_clip, torch' >/dev/null || {
            echo "error: OpenCLIP dependencies are missing; run uv sync --extra openclip" >&2
            return 1
        }
        nohup env \
            CUDA_VISIBLE_DEVICES="$gpu" \
            OPENCLIP_MODEL_ROOT="$PWD/checkpoints/open_clip" \
            OPENCLIP_PORT=7006 \
            HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
            PYTHONPATH="$PWD" \
            "$PYTHON" -m assetserver.openclip_server.standalone \
            >"$logfile" 2>&1 &
        timeout="$OPENCLIP_READY_TIMEOUT"
    else
        "$PYTHON" -c 'import torch, nvdiffrast, pytorch3d, gsplat' >/dev/null || {
            echo "error: SAM3D dependencies are missing; run uv sync --extra sam3d" >&2
            return 1
        }
        nohup env \
            CUDA_VISIBLE_DEVICES="$gpu" \
            "$PYTHON" -m assetserver.generation_server.standalone \
            --config "$PWD/config/generate/sam3d.yaml" --host 0.0.0.0 \
            >"$logfile" 2>&1 &
        timeout="$SAM3D_READY_TIMEOUT"
    fi
    echo "$!" >"$pidfile"
    echo "started $service on GPU $gpu (pid $!, log $logfile)"
    [ "$wait" = false ] || wait_ready "$service" "$timeout"
}

stop_one() {
    local service="$1"
    local pid
    if ! pid="$(managed_pid "$service")"; then
        echo "$service is not managed by this script"
        return 0
    fi
    kill "$pid"
    for _ in $(seq 1 30); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "error: $service did not stop within 30s (pid $pid)" >&2
        return 1
    fi
    rm -f "$(pid_file "$service")"
    echo "stopped $service"
}

status_one() {
    local service="$1"
    local process="not-managed"
    local health="not-ready"
    if managed_pid "$service" >/dev/null; then process="pid=$(managed_pid "$service")"; fi
    if is_ready "$service"; then health="ready"; fi
    printf '%-8s %-16s %s\n' "$service" "$process" "$health"
}

command="${1:-status}"
service="${2:-all}"
case "$command" in -h|--help|help) usage; exit 0 ;; esac
validate_service "$service"
shift $(( $# >= 2 ? 2 : $# ))

gpu=""
wait=true
while [ "$#" -gt 0 ]; do
    case "$1" in
        --gpu) gpu="${2:?--gpu requires an index}"; shift 2 ;;
        --no-wait) wait=false; shift ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

services=(openclip sam3d)
if [ "$service" != all ]; then services=("$service"); fi

case "$command" in
    start)
        for item in "${services[@]}"; do
            selected_gpu="$gpu"
            if [ -z "$selected_gpu" ]; then
                if [ "$item" = openclip ]; then selected_gpu="$OPENCLIP_GPU"; else selected_gpu="$SAM3D_GPU"; fi
            fi
            start_one "$item" "$selected_gpu" "$wait"
        done
        ;;
    stop)
        for item in "${services[@]}"; do stop_one "$item"; done
        ;;
    restart)
        for item in "${services[@]}"; do stop_one "$item"; done
        for item in "${services[@]}"; do
            selected_gpu="$gpu"
            if [ -z "$selected_gpu" ]; then
                if [ "$item" = openclip ]; then selected_gpu="$OPENCLIP_GPU"; else selected_gpu="$SAM3D_GPU"; fi
            fi
            start_one "$item" "$selected_gpu" "$wait"
        done
        ;;
    status)
        for item in "${services[@]}"; do status_one "$item"; done
        ;;
    check)
        for item in "${services[@]}"; do
            is_ready "$item" || { echo "$item is not ready" >&2; exit 1; }
            echo "$item is ready at $(ready_url "$item")"
        done
        ;;
    logs)
        [ "$service" != all ] || { echo "logs requires openclip or sam3d" >&2; exit 2; }
        touch "$(log_file "$service")"
        tail -n 100 -f "$(log_file "$service")"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
