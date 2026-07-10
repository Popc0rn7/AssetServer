#!/usr/bin/env bash
#
# Download Hunyuan3D model weights into the layout expected by AssetServer.
# Hunyuan3D source code lives in external/Hunyuan3D-2; model weights live under
# checkpoints/.
#
# Usage:
#   scripts/download_hunyuan3d_checkpoints.sh
#   scripts/download_hunyuan3d_checkpoints.sh --include-mini
#   scripts/download_hunyuan3d_checkpoints.sh --checkpoint-dir /mnt/data/checkpoints
#
# Optional:
#   HF_ENDPOINT=https://hf-mirror.com scripts/download_hunyuan3d_checkpoints.sh

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/download_hunyuan3d_checkpoints.sh [--checkpoint-dir PATH] [--include-mini]

Downloads Hunyuan3D model weights into the layout expected by AssetServer.
The default full-model download is enough for config/generate/hunyuan3d.yaml
when params.use_mini is false.

Options:
  --checkpoint-dir PATH  Checkpoint root. Default: checkpoints
  --include-mini         Also download tencent/Hunyuan3D-2mini
  -h, --help             Show this help.

Environment:
  HF_ENDPOINT            Optional HuggingFace endpoint, e.g. https://hf-mirror.com

Resulting layout:
  PATH/Hunyuan3D-2/
  PATH/Hunyuan3D-2mini/  (only with --include-mini)
EOF
}

CHECKPOINT_DIR="checkpoints"
INCLUDE_MINI=false

while [ "$#" -gt 0 ]; do
    case "$1" in
        --checkpoint-dir)
            if [ "$#" -lt 2 ]; then
                echo "Error: --checkpoint-dir requires a value." >&2
                exit 1
            fi
            CHECKPOINT_DIR="$2"
            shift 2
            ;;
        --checkpoint-dir=*)
            CHECKPOINT_DIR="${1#*=}"
            shift
            ;;
        --include-mini)
            INCLUDE_MINI=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'." >&2
            usage
            exit 1
            ;;
    esac
done

if command -v hf >/dev/null 2>&1; then
    HF_CLI=(hf download)
elif command -v huggingface-cli >/dev/null 2>&1; then
    HF_CLI=(huggingface-cli download)
else
    echo "Error: neither 'hf' nor 'huggingface-cli' is available." >&2
    echo "Install huggingface_hub or run this inside the project environment." >&2
    exit 1
fi

FULL_MODEL_DIR="${CHECKPOINT_DIR}/Hunyuan3D-2"
MINI_MODEL_DIR="${CHECKPOINT_DIR}/Hunyuan3D-2mini"

mkdir -p "$CHECKPOINT_DIR"

echo "Checkpoint directory: $CHECKPOINT_DIR"
if [ -n "${HF_ENDPOINT:-}" ]; then
    echo "Using HF_ENDPOINT=$HF_ENDPOINT"
fi
echo

download_repo() {
    local repo_id="$1"
    local local_dir="$2"
    echo "Downloading $repo_id into $local_dir..."
    "${HF_CLI[@]}" "$repo_id" \
        --repo-type model \
        --local-dir "$local_dir"
    echo "Downloaded $repo_id"
}

download_repo tencent/Hunyuan3D-2 "$FULL_MODEL_DIR"

if [ "$INCLUDE_MINI" = true ]; then
    download_repo tencent/Hunyuan3D-2mini "$MINI_MODEL_DIR"
fi

echo
echo "Hunyuan3D checkpoint layout:"
echo "  $FULL_MODEL_DIR"
if [ "$INCLUDE_MINI" = true ]; then
    echo "  $MINI_MODEL_DIR"
fi
echo
echo "Use these environment variables for local non-Docker runs:"
echo "  export HUNYUAN3D_MODEL_DIR=$FULL_MODEL_DIR"
if [ "$INCLUDE_MINI" = true ]; then
    echo "  export HUNYUAN3D_MINI_MODEL_DIR=$MINI_MODEL_DIR"
fi
