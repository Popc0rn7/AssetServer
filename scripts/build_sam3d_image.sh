#!/usr/bin/env bash
set -euo pipefail
echo "Deprecated: use scripts/build_sam3d_docker.sh" >&2
exec "$(dirname "$0")/build_sam3d_docker.sh" "$@"
