#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER=(docker)
GPU=0
USE_GPU=true
DATA_DIR="${ASSETSERVER_DATA_ROOT:-$PWD/data}"
OUTPUT_DIR="${ASSETSERVER_OUTPUT_ROOT:-$PWD/outputs}"
IMAGE="${SCENE_VIEWER_IMAGE:-assetserver-scene-viewer:dev}"
CONTAINER_NAME="${SCENE_VIEWER_CONTAINER:-assetserver-scene-viewer-worker}"
FOREGROUND=false
SMOKE=false

usage() {
  echo "Usage: $0 [--gpu N|--no-gpu] [--data-dir PATH] [--output-dir PATH]" >&2
  echo "          [--name NAME] [--foreground] [--smoke] [--sudo]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gpu)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      GPU="$2"; shift 2 ;;
    --no-gpu) USE_GPU=false; shift ;;
    --data-dir)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      DATA_DIR="$2"; shift 2 ;;
    --output-dir)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      OUTPUT_DIR="$2"; shift 2 ;;
    --name)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      CONTAINER_NAME="$2"; shift 2 ;;
    --foreground) FOREGROUND=true; shift ;;
    --smoke) SMOKE=true; FOREGROUND=true; shift ;;
    --sudo) DOCKER=(sudo docker); shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

DATA_DIR="$(mkdir -p "$DATA_DIR" && readlink -f "$DATA_DIR")"
OUTPUT_DIR="$(mkdir -p "$OUTPUT_DIR" && readlink -f "$OUTPUT_DIR")"
mkdir -p "$DATA_DIR/assets" "$DATA_DIR/scenes" "$DATA_DIR/jobs" "$DATA_DIR/cache"

if [[ "$SMOKE" == false && "$FOREGROUND" == false ]]; then
  "${DOCKER[@]}" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

args=("${DOCKER[@]}" run)
if [[ "$FOREGROUND" == true ]]; then
  args+=(--rm)
else
  args+=(-d --restart unless-stopped --name "$CONTAINER_NAME")
fi
if [[ "$USE_GPU" == true ]]; then
  args+=(--gpus "device=$GPU")
fi
args+=(--user "$(id -u):$(id -g)"
  --read-only
  --tmpfs /tmp:rw,nosuid,size=2g
  --cap-drop ALL
  --security-opt no-new-privileges
  -e HOME=/tmp
  -e XDG_CACHE_HOME=/tmp/xdg-cache
  -e ASSETSERVER_DATA_ROOT=/data
  -e ASSETSERVER_OUTPUT_ROOT=/outputs
  -v "$DATA_DIR:/data"
  -v "$OUTPUT_DIR:/outputs")

if [[ "$USE_GPU" == true ]]; then
  args+=(-e NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics)
fi

args+=("$IMAGE")
if [[ "$SMOKE" == true ]]; then
  args+=(python /app/scripts/smoke_scene_viewer.py --output-dir /data/cache/scene-viewer-smoke)
fi
"${args[@]}"
if [[ "$FOREGROUND" == false ]]; then
  echo "Scene viewer worker started: $CONTAINER_NAME"
  echo "Follow logs with: ${DOCKER[*]} logs -f $CONTAINER_NAME"
fi
