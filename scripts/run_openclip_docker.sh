#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=(docker)
GPU=0
PORT=7006
FOREGROUND=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sudo) DOCKER=(sudo docker); shift ;;
    --gpu) GPU="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --foreground) FOREGROUND=true; shift ;;
    *) echo "Usage: $0 [--sudo] [--gpu N] [--port N] [--foreground]" >&2; exit 2 ;;
  esac
done
MODELS="$(readlink -f "${OPENCLIP_MODELS:-$PWD/checkpoints/open_clip}")"
IMAGE="${OPENCLIP_IMAGE:-assetserver-openclip:dev}"
.venv/bin/python -m assetserver.openclip_server.model_tool "$MODELS"
"${DOCKER[@]}" rm -f assetserver-openclip >/dev/null 2>&1 || true
args=("${DOCKER[@]}" run --name assetserver-openclip --gpus "device=$GPU"
  -p "127.0.0.1:${PORT}:7006" --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m
  --cap-drop ALL --security-opt no-new-privileges
  -v "$MODELS:/models:ro" -v openclip-cache:/var/cache/openclip)
if [[ "$FOREGROUND" == false ]]; then args+=(-d); fi
args+=("$IMAGE")
"${args[@]}"
if [[ "$FOREGROUND" == false ]]; then
  for _ in $(seq 1 120); do
    if curl --noproxy '*' --fail --silent "http://127.0.0.1:${PORT}/health/ready" >/dev/null; then
      echo "OpenCLIP ready at http://127.0.0.1:${PORT}"
      exit 0
    fi
    sleep 2
  done
  "${DOCKER[@]}" logs --tail 200 assetserver-openclip
  exit 1
fi
