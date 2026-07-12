#!/usr/bin/env bash
#
# Download SAM3D checkpoints into the layout expected by AssetServer.
#
# Usage:
#   scripts/download_sam3d_ckpt_impl.sh
#   scripts/download_sam3d_ckpt_impl.sh --checkpoint-dir /mnt/data/checkpoints
#
# Optional:
#   HF_ENDPOINT=https://hf-mirror.com scripts/download_sam3d_ckpt_impl.sh

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/download_sam3d_ckpt_impl.sh [--checkpoint-dir PATH]

Downloads SAM3D checkpoints into the layout expected by AssetServer.

Options:
  --checkpoint-dir PATH  Destination directory. Default: checkpoints
  -h, --help             Show this help.

Environment:
  HF_ENDPOINT            Optional HuggingFace endpoint, e.g. https://hf-mirror.com
EOF
}

CHECKPOINT_DIR="checkpoints"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --checkpoint-dir)
            if [ "$#" -lt 2 ]; then
                echo "Error: --checkpoint-dir requires a value."
                exit 1
            fi
            CHECKPOINT_DIR="$2"
            shift 2
            ;;
        --checkpoint-dir=*)
            CHECKPOINT_DIR="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'."
            usage
            exit 1
            ;;
    esac
done

TMP_DIR="${CHECKPOINT_DIR}/.sam-3d-objects-download"
LEGACY_TMP_DIR="${CHECKPOINT_DIR}/sam-3d-objects-download"

if command -v hf >/dev/null 2>&1; then
    HF_CLI=(hf download)
elif command -v huggingface-cli >/dev/null 2>&1; then
    HF_CLI=(huggingface-cli download)
else
    echo "Error: neither 'hf' nor 'huggingface-cli' is available."
    echo "Install huggingface_hub or run this inside the project environment."
    exit 1
fi

mkdir -p "$CHECKPOINT_DIR"

echo "Checkpoint directory: $CHECKPOINT_DIR"
if [ -n "${HF_ENDPOINT:-}" ]; then
    echo "Using HF_ENDPOINT=$HF_ENDPOINT"
fi
echo

if [ -f "$CHECKPOINT_DIR/sam3.pt" ]; then
    echo "✓ sam3.pt already exists"
else
    echo "Downloading SAM3 checkpoint..."
    "${HF_CLI[@]}" facebook/sam3 sam3.pt \
        --revision "${SAM3_MODEL_REVISION:-main}" \
        --local-dir "$CHECKPOINT_DIR"
    echo "✓ Downloaded sam3.pt"
fi

install_sam3d_objects_from() {
    local staged_dir="$1"

    if [ ! -d "$staged_dir/checkpoints" ]; then
        echo "Error: expected '$staged_dir/checkpoints'."
        exit 1
    fi

    cp -a "$staged_dir/checkpoints/." "$CHECKPOINT_DIR/"
    rm -rf "$staged_dir"
    echo "✓ Installed SAM 3D Objects checkpoints"
}

if [ -f "$CHECKPOINT_DIR/pipeline.yaml" ]; then
    echo "✓ SAM 3D Objects checkpoints already appear to be installed"
elif [ -f "$TMP_DIR/checkpoints/pipeline.yaml" ]; then
    echo "Installing SAM 3D Objects checkpoints from existing staging directory..."
    install_sam3d_objects_from "$TMP_DIR"
elif [ -f "$LEGACY_TMP_DIR/checkpoints/pipeline.yaml" ]; then
    echo "Installing SAM 3D Objects checkpoints from existing staging directory..."
    install_sam3d_objects_from "$LEGACY_TMP_DIR"
else
    echo "Downloading SAM 3D Objects checkpoints..."
    "${HF_CLI[@]}" facebook/sam-3d-objects \
        --repo-type model \
        --revision "${SAM3D_MODEL_REVISION:-main}" \
        --local-dir "$TMP_DIR" \
        --include "checkpoints/*"

    install_sam3d_objects_from "$TMP_DIR"
fi

echo
echo "SAM3D checkpoint layout:"
echo "  $CHECKPOINT_DIR/sam3.pt"
echo "  $CHECKPOINT_DIR/pipeline.yaml"
