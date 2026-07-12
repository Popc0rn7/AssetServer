#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
GPU=0
PORT=7000
FOREGROUND=false
DOCKER=(docker)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gpu) GPU="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --foreground) FOREGROUND=true; shift ;;
    --sudo) DOCKER=(sudo docker); shift ;;
    *) echo "Usage: scripts/run_sam3d_docker.sh [--gpu N] [--port N] [--foreground] [--sudo]" >&2; exit 2 ;;
  esac
done
CHECKPOINTS="$(readlink -f "${SAM3D_CHECKPOINTS:-$PWD/checkpoints}")"
ASSETS="${SAM3D_ASSETS:-$PWD/outputs/sam3d}"
IMAGE="${SAM3D_IMAGE:-assetserver-sam3d:dev}"
.venv/bin/python -m assetserver.sam3d_server.model_tool "$CHECKPOINTS"
mkdir -p "$ASSETS"
# The asset store is service-owned. The host supplies only the persistent bind
# mount; SAM3D (UID 10001) creates all asset contents inside it.
"${DOCKER[@]}" run --rm --user 0 --entrypoint /bin/sh \
  -v "$ASSETS:/assets" "$IMAGE" \
  -c 'chown 10001:10001 /assets && chmod u+rwx /assets'
"${DOCKER[@]}" network inspect assetserver >/dev/null 2>&1 || "${DOCKER[@]}" network create assetserver >/dev/null
"${DOCKER[@]}" rm -f assetserver-sam3d >/dev/null 2>&1 || true
args=("${DOCKER[@]}" run --name assetserver-sam3d --gpus "device=$GPU" --network assetserver
  -p "127.0.0.1:${PORT}:7000" --read-only --tmpfs /tmp:rw,noexec,nosuid,size=1g
  --cap-drop ALL --security-opt no-new-privileges
  -v "$CHECKPOINTS:/models:ro" -v "$ASSETS:/var/lib/sam3d/assets"
  -v sam3d-cache:/var/cache/sam3d)
if [[ "$FOREGROUND" == false ]]; then args+=(-d); fi
args+=("$IMAGE")
"${args[@]}"
if [[ "$FOREGROUND" == false ]]; then
  for _ in $(seq 1 180); do
    if curl --fail --silent "http://127.0.0.1:${PORT}/health/ready" >/dev/null; then
      echo "SAM3D ready at http://127.0.0.1:${PORT}"; exit 0
    fi
    sleep 5
  done
  "${DOCKER[@]}" logs --tail 200 assetserver-sam3d
  exit 1
fi
