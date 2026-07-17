#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
    echo "Usage: scripts/fetch_model_sources.sh [sam3|sam3d|dinov2|all]"
    echo "Initializes the pinned inference submodules under thirdparty/."
}

case "${1:-all}" in
    sam3) paths=(thirdparty/SAM3) ;;
    sam3d) paths=(thirdparty/sam-3d-objects) ;;
    dinov2) paths=(thirdparty/dinov2) ;;
    all)
        paths=(thirdparty/SAM3 thirdparty/sam-3d-objects thirdparty/dinov2)
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

git submodule update --init --depth 1 "${paths[@]}"
