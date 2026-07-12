"""Immutable OpenCLIP model bundle validation."""

from __future__ import annotations

import hashlib
import json

from dataclasses import dataclass
from pathlib import Path

MODEL_NAME = "ViT-H-14-378-quickgelu"
CHECKPOINT_NAME = "open_clip_pytorch_model.bin"
DIMENSION = 1024


@dataclass(frozen=True)
class OpenCLIPBundle:
    path: Path
    checkpoint: Path
    model: str
    revision: str
    dimension: int
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_manifest(path: str | Path, revision: str = "dfn5b") -> dict:
    root = Path(path).resolve()
    checkpoint = root / CHECKPOINT_NAME
    if not checkpoint.is_file() or checkpoint.stat().st_size == 0:
        raise RuntimeError(f"OpenCLIP checkpoint missing or empty: {checkpoint}")
    manifest = {
        "model": MODEL_NAME,
        "revision": revision,
        "dimension": DIMENSION,
        "checkpoint": CHECKPOINT_NAME,
        "size": checkpoint.stat().st_size,
        "sha256": _sha256(checkpoint),
    }
    (root / "model-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return manifest


def validate_bundle(path: str | Path) -> OpenCLIPBundle:
    root = Path(path).resolve()
    manifest_path = root / "model-manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"OpenCLIP manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    checkpoint = (root / str(manifest.get("checkpoint", ""))).resolve()
    if root not in checkpoint.parents or not checkpoint.is_file():
        raise RuntimeError("OpenCLIP checkpoint missing or unsafe")
    if checkpoint.stat().st_size != manifest.get("size"):
        raise RuntimeError("OpenCLIP checkpoint size mismatch")
    digest = _sha256(checkpoint)
    if digest != manifest.get("sha256"):
        raise RuntimeError("OpenCLIP checkpoint SHA256 mismatch")
    return OpenCLIPBundle(
        path=root,
        checkpoint=checkpoint,
        model=str(manifest["model"]),
        revision=str(manifest["revision"]),
        dimension=int(manifest["dimension"]),
        sha256=digest,
    )
