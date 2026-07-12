#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
CHECKPOINTS="${SAM3D_CHECKPOINTS:-$PWD/checkpoints}"
HF_MIRROR_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

without_proxy() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
      -u http_proxy -u https_proxy -u all_proxy \
      HF_ENDPOINT="$HF_MIRROR_ENDPOINT" "$@"
}
if [[ "${1:-}" == "--verify" ]]; then
  exec .venv/bin/python -m assetserver.sam3d_server.model_tool "$CHECKPOINTS"
fi
if [[ $# -ne 0 ]]; then
  echo "Usage: scripts/download_sam3d_ckpt.sh [--verify]" >&2; exit 2
fi

echo "Hugging Face route: $HF_MIRROR_ENDPOINT (proxy disabled for HF only)"
source docker/versions.env
export SAM3_MODEL_REVISION SAM3D_MODEL_REVISION
without_proxy scripts/download_sam3d_ckpt_impl.sh --checkpoint-dir "$CHECKPOINTS"
if command -v hf >/dev/null 2>&1; then
  HF=(hf download)
elif command -v huggingface-cli >/dev/null 2>&1; then
  HF=(huggingface-cli download)
else
  echo "Missing Hugging Face CLI. Install huggingface_hub first." >&2; exit 1
fi

MOGE_CACHE="$CHECKPOINTS/hf-cache"
MOGE_MODEL="$MOGE_CACHE/models--Ruicheng--moge-vitl/snapshots/$MOGE_MODEL_REVISION/model.pt"
MOGE_MODEL_LEGACY="$MOGE_CACHE/hub/models--Ruicheng--moge-vitl/snapshots/$MOGE_MODEL_REVISION/model.pt"
if [[ ! -s "$MOGE_MODEL" && ! -s "$MOGE_MODEL_LEGACY" ]]; then
  without_proxy "${HF[@]}" Ruicheng/moge-vitl --revision "$MOGE_MODEL_REVISION" \
    --cache-dir "$MOGE_CACHE"
fi

DINO_DIR="$CHECKPOINTS/torch-cache/hub/checkpoints"
DINO_MODEL="$DINO_DIR/dinov2_vitl14_reg4_pretrain.pth"
mkdir -p "$DINO_DIR"
if [[ ! -s "$DINO_MODEL" ]]; then
  echo "DINOv2 route: official fbaipublicfiles URL (current proxy preserved)"
  curl --fail --location --continue-at - --output "$DINO_MODEL" \
    https://dl.fbaipublicfiles.com/dinov2/dinov2_vitl14/dinov2_vitl14_reg4_pretrain.pth
fi
echo "$DINO_MODEL_SHA256  $DINO_MODEL" | sha256sum --check --status || {
  echo "DINOv2 checksum mismatch: $DINO_MODEL" >&2; exit 1;
}
.venv/bin/python -m assetserver.sam3d_server.model_tool \
  --create --bundle-version checkpoints-v1 "$CHECKPOINTS"
