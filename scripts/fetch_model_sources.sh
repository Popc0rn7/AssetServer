#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

THIRDPARTY_ROOT="${THIRDPARTY_ROOT:-$PWD/thirdparty}"
GITHUB_URL_PREFIX="${GITHUB_URL_PREFIX:-https://github.com/}"
GITHUB_URL_PREFIX="${GITHUB_URL_PREFIX%/}/"

SAM3_REVISION="${SAM3_REVISION:-11dec2936de97f2857c1f76b66d982d5a001155d}"
SAM3D_OBJECTS_REVISION="${SAM3D_OBJECTS_REVISION:-81a82373a3a7f4cbb00bd5b32aaf6b4d0f659ddd}"
DINOV2_REVISION="${DINOV2_REVISION:-7764ea0f912e53c92e82eb78a2a1631e92725fc8}"

usage() {
    echo "Usage: scripts/fetch_model_sources.sh [sam3|sam3d|dinov2|all]"
    echo "Fetches pinned inference sources into thirdparty/. Default: all"
}

fetch_repo() {
    local name="$1"
    local repository="$2"
    local revision="$3"
    local destination="$THIRDPARTY_ROOT/$name"

    mkdir -p "$THIRDPARTY_ROOT"
    if [ -e "$destination" ] && [ ! -d "$destination/.git" ]; then
        echo "error: $destination exists but is not a Git checkout" >&2
        return 1
    fi

    if [ ! -d "$destination/.git" ]; then
        mkdir -p "$destination"
        git -C "$destination" init --quiet
        git -C "$destination" remote add origin "${GITHUB_URL_PREFIX}${repository}"
    else
        if [ -n "$(git -C "$destination" status --porcelain)" ]; then
            echo "error: refusing to update dirty checkout: $destination" >&2
            return 1
        fi
        git -C "$destination" remote set-url origin "${GITHUB_URL_PREFIX}${repository}"
    fi

    if [ "$(git -C "$destination" rev-parse HEAD 2>/dev/null || true)" != "$revision" ]; then
        echo "Fetching $name at $revision"
        GIT_LFS_SKIP_SMUDGE=1 git -C "$destination" \
            -c http.version=HTTP/1.1 fetch --depth 1 origin "$revision"
        git -C "$destination" checkout --detach --quiet FETCH_HEAD
    else
        echo "$name is already at $revision"
    fi
}

case "${1:-all}" in
    sam3)
        fetch_repo SAM3 facebookresearch/sam3.git "$SAM3_REVISION"
        ;;
    sam3d)
        fetch_repo sam-3d-objects facebookresearch/sam-3d-objects.git \
            "$SAM3D_OBJECTS_REVISION"
        ;;
    dinov2)
        fetch_repo dinov2 facebookresearch/dinov2.git "$DINOV2_REVISION"
        ;;
    all)
        fetch_repo SAM3 facebookresearch/sam3.git "$SAM3_REVISION"
        fetch_repo sam-3d-objects facebookresearch/sam-3d-objects.git \
            "$SAM3D_OBJECTS_REVISION"
        fetch_repo dinov2 facebookresearch/dinov2.git "$DINOV2_REVISION"
        ;;
    -h|--help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
