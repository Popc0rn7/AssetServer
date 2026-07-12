#!/usr/bin/env python3
"""Download OpenCLIP weights used by AssetServer retrieval.

The retrieval embeddings are built with OpenCLIP ViT-H-14-378-quickgelu dfn5b,
so the runtime text encoder must use the same model.
"""

from __future__ import annotations

import os
import shutil

from pathlib import Path


MODEL_NAME = "ViT-H-14-378-quickgelu"
PRETRAINED = "dfn5b"
HF_REPO_ID = "apple/DFN5B-CLIP-ViT-H-14-378"
HF_FILENAME = "open_clip_pytorch_model.bin"


def resolve_cache_dir() -> Path:
    env_cache_dir = os.environ.get("ASSETSERVER_OPENCLIP_CACHE_DIR")
    if env_cache_dir:
        return Path(env_cache_dir).expanduser()

    return Path("checkpoints") / "open_clip"


def checkpoint_looks_valid(path: Path) -> bool:
    with open(path, "rb") as f:
        header = f.read(4)
    return header.startswith((b"PK\x03\x04", b"\x80"))


def download_openclip(cache_dir: Path, force_download: bool = False) -> Path:
    # Avoid sparse/corrupt Xet-backed downloads on some HF mirrors unless the user
    # explicitly configured this variable before launching the script.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    from huggingface_hub import hf_hub_download

    path = hf_hub_download(
        repo_id=HF_REPO_ID,
        filename=HF_FILENAME,
        cache_dir=str(cache_dir / "hf-cache"),
        force_download=force_download,
    )
    return Path(path)


def main() -> int:
    cache_dir = resolve_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if os.environ.get("HF_ENDPOINT"):
        print(f"Using HF_ENDPOINT={os.environ['HF_ENDPOINT']}")
    print(f"OpenCLIP cache directory: {cache_dir}")
    print(f"Model: {MODEL_NAME}")
    print(f"Pretrained: {PRETRAINED}")
    print(f"HuggingFace repo: {HF_REPO_ID}")
    print(f"HF_HUB_DISABLE_XET={os.environ.get('HF_HUB_DISABLE_XET', '1')}")

    checkpoint_path = download_openclip(cache_dir)
    if not checkpoint_looks_valid(checkpoint_path):
        print("Downloaded checkpoint appears invalid; forcing a fresh download...")
        checkpoint_path = download_openclip(cache_dir, force_download=True)
    if not checkpoint_looks_valid(checkpoint_path):
        raise RuntimeError(
            "Downloaded checkpoint is still invalid. Remove the OpenCLIP cache and "
            "retry with the official HuggingFace endpoint, for example: "
            "unset HF_ENDPOINT; export HF_HUB_DISABLE_XET=1"
        )
    bundle_checkpoint = cache_dir / HF_FILENAME
    if checkpoint_path.resolve() != bundle_checkpoint.resolve():
        shutil.copy2(checkpoint_path, bundle_checkpoint)
    from assetserver.openclip_server.model_bundle import create_manifest

    create_manifest(cache_dir, revision=PRETRAINED)
    print(f"OpenCLIP offline bundle ready: {cache_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
