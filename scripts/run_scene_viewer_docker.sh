#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER=(docker)
GPU=0
OUTPUT_DIR="${SCENE_VIEWER_OUTPUTS:-$PWD/outputs/scene-viewer-smoke}"
IMAGE="${SCENE_VIEWER_IMAGE:-assetserver-scene-viewer:dev}"

usage() {
  echo "Usage: $0 [--gpu N] [--output-dir PATH] [--sudo]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gpu)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      GPU="$2"; shift 2 ;;
    --output-dir)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      OUTPUT_DIR="$2"; shift 2 ;;
    --sudo) DOCKER=(sudo docker); shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

SMOKE_SCRIPT="$PWD/scripts/smoke_scene_viewer.py"
OUTPUT_DIR="$(mkdir -p "$OUTPUT_DIR" && readlink -f "$OUTPUT_DIR")"

"${DOCKER[@]}" run --rm --gpus "device=$GPU" \
  --user "$(id -u):$(id -g)" \
  --read-only \
  --tmpfs /tmp:rw,nosuid,size=1g \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics \
  -e HOME=/tmp \
  -e XDG_CACHE_HOME=/tmp/xdg-cache \
  -v "$SMOKE_SCRIPT:/app/smoke_scene_viewer.py:ro" \
  -v "$OUTPUT_DIR:/outputs" \
  "$IMAGE" python /app/smoke_scene_viewer.py --output-dir /outputs
