import hashlib
import json

import pytest

from assetserver.generation_pipelines.sam3d.model_bundle import (
    ModelBundleError,
    create_manifest,
    validate_bundle,
)


def test_validate_bundle_accepts_locked_files(tmp_path):
    model = tmp_path / "sam3" / "sam3.pt"
    model.parent.mkdir()
    model.write_bytes(b"weights")
    manifest = {
        "bundle_version": "sam3d-test",
        "files": [
            {
                "path": "sam3/sam3.pt",
                "size": 7,
                "sha256": hashlib.sha256(b"weights").hexdigest(),
            }
        ],
    }
    (tmp_path / "model-manifest.json").write_text(json.dumps(manifest))

    result = validate_bundle(tmp_path)

    assert result.bundle_version == "sam3d-test"
    assert result.path == tmp_path.resolve()


def test_validate_bundle_rejects_hash_mismatch(tmp_path):
    model = tmp_path / "sam3" / "sam3.pt"
    model.parent.mkdir()
    model.write_bytes(b"wrong")
    manifest = {
        "bundle_version": "sam3d-test",
        "files": [{"path": "sam3/sam3.pt", "size": 5, "sha256": "0" * 64}],
    }
    (tmp_path / "model-manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ModelBundleError, match="SHA256 mismatch"):
        validate_bundle(tmp_path)


def test_validate_bundle_rejects_path_escape(tmp_path):
    manifest = {
        "bundle_version": "sam3d-test",
        "files": [{"path": "../secret", "size": 1, "sha256": "0" * 64}],
    }
    (tmp_path / "model-manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ModelBundleError, match="unsafe model path"):
        validate_bundle(tmp_path)


def test_create_manifest_excludes_runtime_caches(tmp_path):
    (tmp_path / "sam3").mkdir()
    (tmp_path / "sam3" / "sam3.pt").write_bytes(b"sam")
    (tmp_path / "sam3d-objects").mkdir()
    (tmp_path / "sam3d-objects" / "pipeline.yaml").write_text("pipeline")
    (tmp_path / "dependencies" / "moge-vitl").mkdir(parents=True)
    (tmp_path / "dependencies" / "moge-vitl" / "model.pt").write_bytes(b"moge")
    (tmp_path / "dependencies" / "dinov2").mkdir(parents=True)
    (
        tmp_path / "dependencies" / "dinov2" / "dinov2_vitl14_reg4_pretrain.pth"
    ).write_bytes(b"dino")
    (tmp_path / "hf-cache").mkdir()
    (tmp_path / "hf-cache" / "download.tmp").write_bytes(b"cache")
    (tmp_path / "sam3" / ".cache" / "huggingface").mkdir(parents=True)
    (tmp_path / "sam3" / ".cache" / "huggingface" / "download.lock").write_bytes(
        b"cache"
    )

    manifest = create_manifest(tmp_path, bundle_version="test-v1")

    paths = [item["path"] for item in manifest["files"]]
    assert paths == [
        "dependencies/dinov2/dinov2_vitl14_reg4_pretrain.pth",
        "dependencies/moge-vitl/model.pt",
        "sam3/sam3.pt",
        "sam3d-objects/pipeline.yaml",
    ]
    assert (tmp_path / "model-manifest.json").is_file()
    assert validate_bundle(tmp_path).bundle_version == "test-v1"


def test_create_manifest_rejects_incomplete_offline_bundle(tmp_path):
    (tmp_path / "sam3").mkdir()
    (tmp_path / "sam3" / "sam3.pt").write_bytes(b"sam")

    with pytest.raises(ModelBundleError, match="required model file missing"):
        create_manifest(tmp_path, bundle_version="test-v1")


def test_legacy_checkpoint_manifest_resolves_cached_dependencies(tmp_path):
    (tmp_path / "sam3.pt").write_bytes(b"sam")
    (tmp_path / "pipeline.yaml").write_text(
        "ss_generator_config_path: ss_generator.yaml\n"
        "ss_generator_ckpt_path: ss_generator.ckpt\n"
    )
    (tmp_path / "ss_generator.yaml").write_text("model: test\n")
    (tmp_path / "ss_generator.ckpt").write_bytes(b"generator")
    moge = (
        tmp_path
        / "hf-cache/hub/models--Ruicheng--moge-vitl/snapshots/revision/model.pt"
    )
    moge.parent.mkdir(parents=True)
    moge.write_bytes(b"moge")
    dino = tmp_path / "torch-cache/hub/checkpoints/dinov2_vitl14_reg4_pretrain.pth"
    dino.parent.mkdir(parents=True)
    dino.write_bytes(b"dino")

    create_manifest(tmp_path, bundle_version="legacy-test")
    bundle = validate_bundle(tmp_path)

    assert bundle.sam3_checkpoint == (tmp_path / "sam3.pt").resolve()
    assert bundle.pipeline_config == (tmp_path / "pipeline.yaml").resolve()
    assert bundle.moge_model == moge.resolve()
    assert bundle.dino_weights == dino.resolve()
    paths = {item["path"] for item in bundle.manifest["files"]}
    assert (
        "hf-cache/hub/models--Ruicheng--moge-vitl/snapshots/revision/model.pt" in paths
    )
    assert "torch-cache/hub/checkpoints/dinov2_vitl14_reg4_pretrain.pth" in paths


def test_checkpoint_manifest_accepts_direct_hf_cache_layout(tmp_path):
    (tmp_path / "sam3.pt").write_bytes(b"sam")
    (tmp_path / "pipeline.yaml").write_text("{}\n")
    moge = (
        tmp_path
        / "hf-cache/models--Ruicheng--moge-vitl/snapshots/revision/model.pt"
    )
    moge.parent.mkdir(parents=True)
    moge.write_bytes(b"moge")
    dino = tmp_path / "torch-cache/hub/checkpoints/dinov2_vitl14_reg4_pretrain.pth"
    dino.parent.mkdir(parents=True)
    dino.write_bytes(b"dino")

    create_manifest(tmp_path, bundle_version="direct-cache-test")
    bundle = validate_bundle(tmp_path)

    assert bundle.moge_model == moge.resolve()
