#!/usr/bin/env bash
set -euo pipefail

IMAGE="${ASSETSERVER_HUNYUAN3D_DOCKER_IMAGE:-assetserver-hunyuan3d:latest}"
DOCKER=(docker)
BUILD_ARGS=()
DOCKER_ARGS=()
PROXY_URL=""

usage() {
  cat <<'EOF'
Usage: scripts/build_hunyuan3d_image.sh [--sudo] [--proxy URL] [docker build args...]

Environment:
  GITHUB              GitHub URL prefix, e.g. https://gh-proxy.com/https://github.com/
  PYPI                Python package index URL, e.g. https://pypi.tuna.tsinghua.edu.cn/simple
  UV_HTTP_TIMEOUT     uv network timeout in seconds. Default: 300.
  HUNYUAN3D_COMMIT    Optional commit/tag to checkout during Docker build.
  ASSETSERVER_HUNYUAN3D_DOCKER_IMAGE
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --sudo)
      DOCKER=(sudo docker)
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

if [[ -n "${GITHUB:-}" ]]; then
  BUILD_ARGS+=(--build-arg "GITHUB_URL_PREFIX=${GITHUB}")
fi

if [[ -n "${HUNYUAN3D_COMMIT:-}" ]]; then
  BUILD_ARGS+=(--build-arg "HUNYUAN3D_COMMIT=${HUNYUAN3D_COMMIT}")
fi

if [[ -n "${PYPI:-}" ]]; then
  BUILD_ARGS+=(--build-arg "PYPI_INDEX_URL=${PYPI}")
fi
BUILD_ARGS+=(--build-arg "UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT:-300}")

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
  -f Dockerfile.hunyuan3d \
  -t "$IMAGE" \
  "${BUILD_ARGS[@]}" \
  "${DOCKER_ARGS[@]}" \
  .
