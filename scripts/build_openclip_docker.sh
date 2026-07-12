#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source docker/versions.env
DOCKER=(docker)
PROXY_URL=""
EXTRA_BUILD_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sudo) DOCKER=(sudo docker); shift ;;
    --proxy)
      [[ $# -ge 2 ]] || { echo "--proxy requires a URL" >&2; exit 2; }
      PROXY_URL="$2"; shift 2 ;;
    --proxy=*) PROXY_URL="${1#*=}"; shift ;;
    *) EXTRA_BUILD_ARGS+=("$1"); shift ;;
  esac
done
args=("${DOCKER[@]}" build -f docker/3d/Dockerfile --target openclip-runtime
  --build-arg "CUDA_VERSION=$CUDA_VERSION"
  --build-arg "PYTHON_VERSION=$PYTHON_VERSION"
  --build-arg "TORCH_VERSION=$TORCH_VERSION"
  --build-arg "TORCHVISION_VERSION=$TORCHVISION_VERSION"
  --build-arg "PYPI_INDEX_URL=${PYPI:-}"
  -t "${OPENCLIP_IMAGE:-assetserver-openclip:dev}")
if [[ -n "$PROXY_URL" ]]; then
  args+=(--add-host host.docker.internal:host-gateway
    --build-arg "HTTP_PROXY=$PROXY_URL" --build-arg "HTTPS_PROXY=$PROXY_URL"
    --build-arg "http_proxy=$PROXY_URL" --build-arg "https_proxy=$PROXY_URL")
fi
args+=("${EXTRA_BUILD_ARGS[@]}" .)
"${args[@]}"
