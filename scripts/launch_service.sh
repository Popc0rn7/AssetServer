#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${ASSETSERVER_SERVICE_PYTHON:-$PWD/.venv/bin/python}"
STATE_ROOT="${ASSETSERVER_SERVICE_STATE_ROOT:-$PWD/data/runtime/services}"
LOG_ROOT="${ASSETSERVER_SERVICE_LOG_ROOT:-$PWD/data/logs/services}"
OPENCLIP_GPU="${OPENCLIP_GPU:-0}"
SAM3D_GPU="${SAM3D_GPU:-1}"
SCENE_VIEWER_GPU="${SCENE_VIEWER_GPU:-0}"
SCENE_VIEWER_RENDER_DEVICE="${SCENE_VIEWER_RENDER_DEVICE:-gpu}"
OPENCLIP_READY_TIMEOUT="${OPENCLIP_READY_TIMEOUT:-300}"
SAM3D_READY_TIMEOUT="${SAM3D_READY_TIMEOUT:-900}"
POSTPROCESS_READY_TIMEOUT="${POSTPROCESS_READY_TIMEOUT:-30}"
SCENE_VIEWER_READY_TIMEOUT="${SCENE_VIEWER_READY_TIMEOUT:-30}"

mkdir -p "$STATE_ROOT" "$LOG_ROOT"

usage() {
    cat <<'EOF'
Usage: scripts/launch_service.sh COMMAND [SERVICE] [OPTIONS]

Commands:
  run SERVICE         Run one service in the foreground
  start SERVICE       Start one service or all services
  stop SERVICE        Stop one service or all services
  restart SERVICE     Restart one service or all services
  status [SERVICE]    Show managed process and readiness status
  check [SERVICE]     Fail unless the service is ready
  logs SERVICE        Follow the service log

Options for start/restart:
  --gpu INDEX         Override the service's GPU index
  --no-wait           Return without waiting for readiness

Services:
  openclip, sam3d, scene-viewer, postprocess, all

Environment defaults:
  OPENCLIP_GPU=0, SAM3D_GPU=1, SCENE_VIEWER_GPU=0
  SCENE_VIEWER_RENDER_DEVICE=gpu
EOF
}

validate_service() {
    case "$1" in
        openclip|sam3d|scene-viewer|postprocess|all) ;;
        *) echo "unknown service: $1" >&2; exit 2 ;;
    esac
}

pid_file() { echo "$STATE_ROOT/$1.pid"; }
log_file() { echo "$LOG_ROOT/$1.log"; }
port_for() {
    case "$1" in openclip) echo 7006 ;; sam3d) echo 7000 ;; postprocess) echo 7100 ;; esac
}
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
    if [ "$1" = scene-viewer ]; then
        managed_pid "$1" >/dev/null && [ -s "$PWD/data/runtime/scene-worker.json" ]
        return
    fi
    curl --fail --silent --max-time 3 "$(ready_url "$1")" \
        | grep -q '"status":"ready"'
}

wait_ready() {
    local service="$1"
    local timeout="$2"
    local started=$SECONDS
    while (( SECONDS - started < timeout )); do
        if is_ready "$service"; then
            if [ "$service" = scene-viewer ]; then
                echo "$service is ready (SQLite worker process is running)"
            else
                echo "$service is ready at $(ready_url "$service")"
            fi
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
        echo "run: uv sync --group retrieve --group scene-viewer --group postprocess --extra sam3d" >&2
        exit 1
    }
}

run_foreground() {
    local service="$1"
    local gpu="$2"
    require_runtime

    if is_ready "$service"; then
        echo "error: $service is already ready" >&2
        return 1
    fi

    if [ "$service" = openclip ]; then
        "$PYTHON" -c 'import open_clip, torch' >/dev/null || {
            echo "error: OpenCLIP dependencies are missing; run uv sync --group retrieve" >&2
            return 1
        }
        echo "running openclip in foreground on GPU $gpu; press Ctrl+C to stop"
        exec env \
            CUDA_VISIBLE_DEVICES="$gpu" \
            OPENCLIP_MODEL_ROOT="$PWD/checkpoints/open_clip" \
            OPENCLIP_PORT=7006 \
            OPENCLIP_PRELOAD_SYNC=1 \
            HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
            PYTHONPATH="$PWD" \
            "$PYTHON" -m assetserver.openclip_server.standalone
    fi

    if [ "$service" = postprocess ]; then
        "$PYTHON" -c 'import coacd' >/dev/null || {
            echo "error: postprocess dependencies are missing; run uv sync --group postprocess" >&2
            return 1
        }
        mkdir -p "$PWD/data/assets" "$PWD/data/postprocess/staging"
        echo "running postprocess in foreground on port 7100; press Ctrl+C to stop"
        exec env \
            OMP_NUM_THREADS=4 \
            ASSETSERVER_DATA_ROOT="$PWD/data" \
            ASSETSERVER_POSTPROCESS_STAGING="$PWD/data/postprocess/staging" \
            PYTHONPATH="$PWD" \
            "$PYTHON" -m assetserver.postprocess_server.standalone_server \
            --host 127.0.0.1 --port 7100 --omp-threads 4
    fi

    if [ "$service" = scene-viewer ]; then
        "$PYTHON" -c 'import bpy, pydrake' >/dev/null || {
            echo "error: scene-viewer dependencies are missing; run uv sync --group scene-viewer" >&2
            return 1
        }
        local cache="$PWD/data/cache/scene-viewer/xdg"
        mkdir -p "$cache" "$PWD/data/jobs" "$PWD/data/assets" \
            "$PWD/data/runtime" "$PWD/outputs"
        rm -f "$PWD/data/runtime/scene-worker.json"
        echo "running scene-viewer in foreground on GPU $gpu; press Ctrl+C to stop"
        exec env \
            CUDA_VISIBLE_DEVICES="$gpu" \
            ASSETSERVER_DATA_ROOT="$PWD/data" \
            ASSETSERVER_OUTPUT_ROOT="$PWD/outputs" \
            ASSETSERVER_BUILD_VERSION="${ASSETSERVER_BUILD_VERSION:-dev}" \
            ASSETSERVER_RENDER_DEVICE="$SCENE_VIEWER_RENDER_DEVICE" \
            XDG_CACHE_HOME="$cache" \
            PYTHONPATH="$PWD" \
            "$PYTHON" -m assetserver.job_worker \
            --database "$PWD/data/jobs/jobs.sqlite3" \
            --handler observe=assetserver.scene_job_handlers:observe \
            --handler validate=assetserver.scene_job_handlers:validate \
            --handler placement_proposal=assetserver.placement.engine:propose \
            --handler placement_repair=assetserver.placement.engine:repair \
            --handler export=assetserver.scene_job_handlers:export \
            --lease-seconds 300 --heartbeat-seconds 30
    fi

    "$PYTHON" -c 'import torch, nvdiffrast, pytorch3d, gsplat' >/dev/null || {
        echo "error: SAM3D dependencies are missing; run uv sync --extra sam3d" >&2
        return 1
    }
    echo "running sam3d in foreground on GPU $gpu; press Ctrl+C to stop"
    exec env \
        CUDA_VISIBLE_DEVICES="$gpu" \
        "$PYTHON" -m assetserver.generation_server.standalone \
        --config "$PWD/config/generate/sam3d.yaml" --host 0.0.0.0
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
        echo "error: $service is already ready but is not managed by this script" >&2
        return 1
    fi

    local logfile pidfile
    logfile="$(log_file "$service")"
    pidfile="$(pid_file "$service")"

    if [ "$service" = openclip ]; then
        "$PYTHON" -c 'import open_clip, torch' >/dev/null || {
            echo "error: OpenCLIP dependencies are missing; run uv sync --group retrieve" >&2
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
    elif [ "$service" = postprocess ]; then
        "$PYTHON" -c 'import coacd' >/dev/null || {
            echo "error: postprocess dependencies are missing; run uv sync --group postprocess" >&2
            return 1
        }
        mkdir -p "$PWD/data/assets" "$PWD/data/postprocess/staging"
        nohup env \
            OMP_NUM_THREADS=4 \
            ASSETSERVER_DATA_ROOT="$PWD/data" \
            ASSETSERVER_POSTPROCESS_STAGING="$PWD/data/postprocess/staging" \
            PYTHONPATH="$PWD" \
            "$PYTHON" -m assetserver.postprocess_server.standalone_server \
            --host 127.0.0.1 --port 7100 --omp-threads 4 \
            >"$logfile" 2>&1 &
        timeout="$POSTPROCESS_READY_TIMEOUT"
    elif [ "$service" = scene-viewer ]; then
        "$PYTHON" -c 'import bpy, pydrake' >/dev/null || {
            echo "error: scene-viewer dependencies are missing; run uv sync --group scene-viewer" >&2
            return 1
        }
        local cache="$PWD/data/cache/scene-viewer/xdg"
        mkdir -p "$cache" "$PWD/data/jobs" "$PWD/data/assets" \
            "$PWD/data/runtime" "$PWD/outputs"
        rm -f "$PWD/data/runtime/scene-worker.json"
        nohup env \
            CUDA_VISIBLE_DEVICES="$gpu" \
            ASSETSERVER_DATA_ROOT="$PWD/data" \
            ASSETSERVER_OUTPUT_ROOT="$PWD/outputs" \
            ASSETSERVER_BUILD_VERSION="${ASSETSERVER_BUILD_VERSION:-dev}" \
            ASSETSERVER_RENDER_DEVICE="$SCENE_VIEWER_RENDER_DEVICE" \
            XDG_CACHE_HOME="$cache" \
            PYTHONPATH="$PWD" \
            "$PYTHON" -m assetserver.job_worker \
            --database "$PWD/data/jobs/jobs.sqlite3" \
            --handler observe=assetserver.scene_job_handlers:observe \
            --handler validate=assetserver.scene_job_handlers:validate \
            --handler placement_proposal=assetserver.placement.engine:propose \
            --handler placement_repair=assetserver.placement.engine:repair \
            --handler export=assetserver.scene_job_handlers:export \
            --lease-seconds 300 --heartbeat-seconds 30 \
            >"$logfile" 2>&1 &
        timeout="$SCENE_VIEWER_READY_TIMEOUT"
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
    if [ "$service" = postprocess ]; then
        echo "started $service on port 7100 (pid $!, log $logfile)"
    else
        echo "started $service on GPU $gpu (pid $!, log $logfile)"
    fi
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
    printf '%-13s %-16s %s\n' "$service" "$process" "$health"
}

default_gpu() {
    case "$1" in
        openclip) echo "$OPENCLIP_GPU" ;;
        sam3d) echo "$SAM3D_GPU" ;;
        scene-viewer) echo "$SCENE_VIEWER_GPU" ;;
        postprocess) echo "" ;;
    esac
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

services=(openclip sam3d scene-viewer postprocess)
if [ "$service" != all ]; then services=("$service"); fi

case "$command" in
    run)
        [ "$service" != all ] || { echo "run requires one service" >&2; exit 2; }
        selected_gpu="$gpu"
        if [ -z "$selected_gpu" ]; then
            selected_gpu="$(default_gpu "$service")"
        fi
        run_foreground "$service" "$selected_gpu"
        ;;
    start)
        for item in "${services[@]}"; do
            selected_gpu="$gpu"
            if [ -z "$selected_gpu" ]; then
                selected_gpu="$(default_gpu "$item")"
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
                selected_gpu="$(default_gpu "$item")"
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
            if [ "$item" = scene-viewer ]; then
                echo "$item is ready (SQLite worker process is running)"
            else
                echo "$item is ready at $(ready_url "$item")"
            fi
        done
        ;;
    logs)
        [ "$service" != all ] || { echo "logs requires one service" >&2; exit 2; }
        touch "$(log_file "$service")"
        tail -n 100 -f "$(log_file "$service")"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
