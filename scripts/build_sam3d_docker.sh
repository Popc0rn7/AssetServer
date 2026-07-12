#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source docker/versions.env
CACHE=true
PROXY_URL=""
DOCKER=(docker)
EXTRA_BUILD_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean) CACHE=false; shift ;;
    --sudo) DOCKER=(sudo docker); shift ;;
    --proxy)
      [[ $# -ge 2 ]] || { echo "--proxy requires a URL" >&2; exit 2; }
      PROXY_URL="$2"; shift 2 ;;
    --proxy=*) PROXY_URL="${1#*=}"; shift ;;
    *) EXTRA_BUILD_ARGS+=("$1"); shift ;;
  esac
done
if [[ -z "${TORCH_CUDA_ARCH_LIST:-}" ]]; then
  TORCH_CUDA_ARCH_LIST="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | tr -d ' ' | sort -u | paste -sd ';' -)"
fi
if [[ -z "$TORCH_CUDA_ARCH_LIST" ]]; then
  echo "Cannot detect GPU architecture; set TORCH_CUDA_ARCH_LIST." >&2; exit 1
fi
IMAGE_VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
SAM3D_TAG="${SAM3D_IMAGE:-assetserver-sam3d:${IMAGE_VERSION}}"
args=("${DOCKER[@]}" build -f docker/3d/Dockerfile --target sam3d-runtime
  --build-arg "CUDA_VERSION=$CUDA_VERSION"
  --build-arg "PYTHON_VERSION=$PYTHON_VERSION"
  --build-arg "TORCH_VERSION=$TORCH_VERSION"
  --build-arg "TORCHVISION_VERSION=$TORCHVISION_VERSION"
  --build-arg "TORCH_CUDA_ARCH_LIST=$TORCH_CUDA_ARCH_LIST"
  --build-arg "SAM3_REVISION=$SAM3_REVISION"
  --build-arg "SAM3D_OBJECTS_REVISION=$SAM3D_OBJECTS_REVISION"
  --build-arg "NVDIFFRAST_REVISION=$NVDIFFRAST_REVISION"
  --build-arg "PYTORCH3D_REVISION=$PYTORCH3D_REVISION"
  --build-arg "MOGE_REVISION=$MOGE_REVISION"
  --build-arg "DINOV2_REVISION=$DINOV2_REVISION"
  --build-arg "IMAGE_VERSION=$IMAGE_VERSION"
  --build-arg "GITHUB_URL_PREFIX=${GITHUB:-}"
  --build-arg "PYPI_INDEX_URL=${PYPI:-}"
  -t "$SAM3D_TAG" -t assetserver-sam3d:dev)
if [[ -n "$PROXY_URL" ]]; then
  args+=(
    --add-host host.docker.internal:host-gateway
    --build-arg "HTTP_PROXY=$PROXY_URL"
    --build-arg "HTTPS_PROXY=$PROXY_URL"
    --build-arg "ALL_PROXY=$PROXY_URL"
    --build-arg "http_proxy=$PROXY_URL"
    --build-arg "https_proxy=$PROXY_URL"
    --build-arg "all_proxy=$PROXY_URL"
    --build-arg "NO_PROXY=localhost,127.0.0.1"
    --build-arg "no_proxy=localhost,127.0.0.1"
  )
fi
if [[ "$CACHE" == false ]]; then args+=(--no-cache); fi
args+=("${EXTRA_BUILD_ARGS[@]}")
args+=(.)
"${args[@]}"
echo "Built ${SAM3D_TAG} for CUDA architectures ${TORCH_CUDA_ARCH_LIST}"
