#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
python scripts/run_backend_docker.py sam3d --sudo --replace "$@"
