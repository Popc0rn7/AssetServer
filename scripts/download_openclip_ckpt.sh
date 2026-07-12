#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
HF_ENDPOINT="${HF_ENDPOINT:-}"
if [[ -n "$HF_ENDPOINT" ]]; then
  echo "Hugging Face route: ${HF_ENDPOINT} (proxy disabled for HF only)"
  env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    -u http_proxy -u https_proxy -u all_proxy \
    HF_ENDPOINT="$HF_ENDPOINT" \
    uv run python scripts/download_openclip_ckpt.py "$@"
else
  echo "Hugging Face route: default endpoint (proxy disabled for HF only)"
  env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    -u http_proxy -u https_proxy -u all_proxy -u HF_ENDPOINT \
    uv run python scripts/download_openclip_ckpt.py "$@"
fi
