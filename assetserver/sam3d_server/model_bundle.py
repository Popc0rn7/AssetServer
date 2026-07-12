"""Validation for immutable, offline SAM3D model bundles."""

from __future__ import annotations

import hashlib
import json

from dataclasses import dataclass
from pathlib import Path

import yaml


class ModelBundleError(RuntimeError):
    """Raised when a model bundle is incomplete or corrupt."""


@dataclass(frozen=True)
class ModelBundle:
    path: Path
    bundle_version: str
    manifest: dict
    sam3_checkpoint: Path
    pipeline_config: Path
    moge_model: Path
    dino_weights: Path


def create_manifest(path: str | Path, bundle_version: str) -> dict:
    root = Path(path).resolve()
    if (root / "sam3.pt").is_file() and (root / "pipeline.yaml").is_file():
        candidates = _legacy_model_files(root)
        files = [_manifest_entry(root, candidate) for candidate in candidates]
        return _write_manifest(root, bundle_version, files, layout="checkpoints-v1")

    required = [
        root / "sam3" / "sam3.pt",
        root / "sam3d-objects" / "pipeline.yaml",
        root / "dependencies" / "moge-vitl" / "model.pt",
    ]
    for candidate in required:
        if not candidate.is_file():
            raise ModelBundleError(
                f"required model file missing: {candidate.relative_to(root)}"
            )
    dino_weights = root / "dependencies" / "dinov2" / "dinov2_vitl14_reg4_pretrain.pth"
    if not dino_weights.is_file():
        raise ModelBundleError(
            "required model file missing: "
            "dependencies/dinov2/dinov2_vitl14_reg4_pretrain.pth"
        )
    excluded_roots = {"hf-cache", "torch-cache", "cache"}
    files = []
    for candidate in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = candidate.relative_to(root)
        if (
            relative.name == "model-manifest.json"
            or relative.parts[0] in excluded_roots
            or ".cache" in relative.parts
        ):
            continue
        files.append(_manifest_entry(root, candidate))
    if not files:
        raise ModelBundleError(f"no model files found under {root}")
    return _write_manifest(root, bundle_version, files, layout="bundle-v1")


def _write_manifest(
    root: Path, bundle_version: str, files: list[dict], layout: str
) -> dict:
    manifest = {"bundle_version": bundle_version, "layout": layout, "files": files}
    (root / "model-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return manifest


def _manifest_entry(root: Path, candidate: Path) -> dict:
    return {
        "path": candidate.relative_to(root).as_posix(),
        "size": candidate.stat().st_size,
        "sha256": _sha256(candidate),
    }


def _legacy_model_files(root: Path) -> list[Path]:
    pipeline_path = root / "pipeline.yaml"
    pipeline = yaml.safe_load(pipeline_path.read_text()) or {}
    required = [root / "sam3.pt", pipeline_path]
    for key, value in pipeline.items():
        if key.endswith(("_path", "_ckpt_path", "_config_path")) and value:
            required.append(root / str(value))

    moge_snapshots = sorted(
        list(
            (root / "hf-cache/models--Ruicheng--moge-vitl/snapshots").glob(
                "*/model.pt"
            )
        )
        + list(
            (root / "hf-cache/hub/models--Ruicheng--moge-vitl/snapshots").glob(
                "*/model.pt"
            )
        )
    )
    if not moge_snapshots:
        raise ModelBundleError("required model file missing: MoGe model.pt")
    required.append(moge_snapshots[-1])
    required.append(
        root / "torch-cache" / "hub" / "checkpoints" / "dinov2_vitl14_reg4_pretrain.pth"
    )
    unique = sorted(set(required), key=str)
    for candidate in unique:
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise ModelBundleError(
                f"required model file missing or empty: {candidate.relative_to(root)}"
            )
    return unique


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_bundle(path: str | Path) -> ModelBundle:
    root = Path(path).resolve()
    manifest_path = root / "model-manifest.json"
    if not manifest_path.is_file():
        raise ModelBundleError(f"model manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelBundleError(f"invalid model manifest: {exc}") from exc

    bundle_version = manifest.get("bundle_version")
    files = manifest.get("files")
    if not isinstance(bundle_version, str) or not bundle_version:
        raise ModelBundleError("model manifest is missing bundle_version")
    if not isinstance(files, list) or not files:
        raise ModelBundleError("model manifest has no files")

    for item in files:
        relative = Path(str(item.get("path", "")))
        candidate = (root / relative).resolve()
        if not relative.parts or root not in candidate.parents:
            raise ModelBundleError(f"unsafe model path: {relative}")
        if not candidate.is_file():
            raise ModelBundleError(f"model file missing: {relative}")
        expected_size = item.get("size")
        if (
            not isinstance(expected_size, int)
            or candidate.stat().st_size != expected_size
        ):
            raise ModelBundleError(f"size mismatch for model file: {relative}")
        expected_hash = item.get("sha256")
        if not isinstance(expected_hash, str) or _sha256(candidate) != expected_hash:
            raise ModelBundleError(f"SHA256 mismatch for model file: {relative}")

    layout = manifest.get("layout", "bundle-v1")
    if layout == "checkpoints-v1":
        files = _legacy_model_files(root)
        moge_model = next(
            path for path in files if "models--Ruicheng--moge-vitl" in str(path)
        )
        dino_weights = (
            root / "torch-cache/hub/checkpoints/dinov2_vitl14_reg4_pretrain.pth"
        )
        sam3_checkpoint = root / "sam3.pt"
        pipeline_config = root / "pipeline.yaml"
    else:
        sam3_checkpoint = root / "sam3" / "sam3.pt"
        pipeline_config = root / "sam3d-objects" / "pipeline.yaml"
        moge_model = root / "dependencies" / "moge-vitl" / "model.pt"
        dino_weights = (
            root / "dependencies" / "dinov2" / "dinov2_vitl14_reg4_pretrain.pth"
        )
    return ModelBundle(
        path=root,
        bundle_version=bundle_version,
        manifest=manifest,
        sam3_checkpoint=sam3_checkpoint,
        pipeline_config=pipeline_config,
        moge_model=moge_model,
        dino_weights=dino_weights,
    )
