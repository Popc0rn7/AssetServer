#!/usr/bin/env bash
set -euo pipefail

IMAGE="${ASSETSERVER_SAM3D_DOCKER_IMAGE:-assetserver-sam3d:latest}"
export DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}"
DOCKER=(docker)
BUILD_ARGS=()
DOCKER_ARGS=()
PROXY_URL=""
DEFAULT_TORCH_CUDA_ARCH_LIST="7.5;8.0;8.6;8.9;9.0"

usage() {
  cat <<'EOF'
Usage: scripts/build_sam3d_image.sh [--sudo] [--proxy URL] [docker build args...]

Environment:
  GITHUB              GitHub URL prefix, e.g. https://gh-proxy.com/https://github.com/
  PYPI                Python package index URL, e.g. https://pypi.tuna.tsinghua.edu.cn/simple
  UV_HTTP_TIMEOUT     uv network timeout in seconds. Default: 300.
  TORCH_CUDA_ARCH_LIST CUDA arch list for extension builds.
                      Default: auto-detect with nvidia-smi, then fallback to common archs.
  ASSETSERVER_SAM3D_DOCKER_IMAGE
EOF
}

detect_torch_cuda_arch_list() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    return 1
  fi

  nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null \
    | tr -d ' ' \
    | awk 'NF && !seen[$0]++ { print }' \
    | paste -sd ';' -
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --sudo)
      DOCKER=(sudo env "DOCKER_BUILDKIT=${DOCKER_BUILDKIT}" docker)
      shift
      ;;
    --proxy)
      if [[ "$#" -lt 2 ]]; then
        echo "Error: --proxy requires a value." >&2
        exit 1
      fi
      PROXY_URL="$2"
      shift 2
      ;;
    --proxy=*)
      PROXY_URL="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      DOCKER_ARGS+=("$1")
      shift
      ;;
  esac
done

GITHUB_PREFIX="${GITHUB:-}"
if [[ -n "$GITHUB_PREFIX" ]]; then
  BUILD_ARGS+=(--build-arg "GITHUB_URL_PREFIX=${GITHUB_PREFIX}")
fi

if [[ -n "${PYPI:-}" ]]; then
  BUILD_ARGS+=(--build-arg "PYPI_INDEX_URL=${PYPI}")
fi
BUILD_ARGS+=(--build-arg "UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT:-300}")

if [[ -z "${TORCH_CUDA_ARCH_LIST:-}" ]]; then
  TORCH_CUDA_ARCH_LIST="$(detect_torch_cuda_arch_list || true)"
fi
TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-$DEFAULT_TORCH_CUDA_ARCH_LIST}"
echo "Using TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST}"
BUILD_ARGS+=(--build-arg "TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST}")

if [[ -n "$PROXY_URL" ]]; then
  BUILD_ARGS+=(
    --build-arg "HTTP_PROXY=${PROXY_URL}"
    --build-arg "HTTPS_PROXY=${PROXY_URL}"
    --build-arg "ALL_PROXY=${PROXY_URL}"
    --build-arg "http_proxy=${PROXY_URL}"
    --build-arg "https_proxy=${PROXY_URL}"
    --build-arg "all_proxy=${PROXY_URL}"
    --build-arg "NO_PROXY=localhost,127.0.0.1"
    --build-arg "no_proxy=localhost,127.0.0.1"
  )
  DOCKER_ARGS+=(--add-host=host.docker.internal:host-gateway)
fi

"${DOCKER[@]}" build \
  -f Dockerfile.sam3d \
  -t "$IMAGE" \
  "${BUILD_ARGS[@]}" \
  "${DOCKER_ARGS[@]}" \
  .
