#!/usr/bin/env bash
#
# Non-interactive Hunyuan3D installation for Docker builds.
# Source code is cloned into external/Hunyuan3D-2 during the image build.
# Model weights are mounted at runtime from checkpoints/Hunyuan3D-2.

set -euo pipefail

HUNYUAN3D_REPO="${HUNYUAN3D_REPO:-Tencent-Hunyuan/Hunyuan3D-2.git}"
HUNYUAN3D_COMMIT="${HUNYUAN3D_COMMIT:-}"
GITHUB_URL_PREFIX="${GITHUB_URL_PREFIX:-https://github.com/}"
GITHUB_URL_PREFIX="${GITHUB_URL_PREFIX%/}/"
PYPI_INDEX_URL="${PYPI_INDEX_URL:-}"
UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-300}"
export UV_HTTP_TIMEOUT
if [ -n "$PYPI_INDEX_URL" ]; then
    export UV_INDEX_URL="$PYPI_INDEX_URL"
    export PIP_INDEX_URL="$PYPI_INDEX_URL"
fi

github_url() {
    local path="$1"
    printf "%s%s" "$GITHUB_URL_PREFIX" "$path"
}

echo "========================================="
echo "Hunyuan3D Docker Installation"
echo "========================================="
echo ""

if [ "$GITHUB_URL_PREFIX" != "https://github.com/" ]; then
    echo "Using GitHub URL prefix: ${GITHUB_URL_PREFIX}"
    git config --global url."${GITHUB_URL_PREFIX}".insteadOf https://github.com/
fi
if [ -n "$PYPI_INDEX_URL" ]; then
    echo "Using PyPI index: ${PYPI_INDEX_URL}"
fi
echo "Using UV_HTTP_TIMEOUT: ${UV_HTTP_TIMEOUT}"

mkdir -p external

if [ ! -d external/Hunyuan3D-2 ]; then
    git clone "$(github_url "$HUNYUAN3D_REPO")" external/Hunyuan3D-2
    echo "Cloned Hunyuan3D-2"
else
    echo "external/Hunyuan3D-2 already exists"
fi

if [ -n "$HUNYUAN3D_COMMIT" ]; then
    echo "Checking out Hunyuan3D commit: ${HUNYUAN3D_COMMIT}"
    git -C external/Hunyuan3D-2 fetch origin
    git -C external/Hunyuan3D-2 checkout --detach "$HUNYUAN3D_COMMIT"
fi

bash scripts/install_hunyuan3d.sh

echo ""
echo "========================================="
echo "Hunyuan3D Docker Installation Complete!"
echo "========================================="
echo ""
echo "Model weights must be mounted at runtime:"
echo "  -v ./checkpoints:/app/checkpoints"
