from pathlib import Path


def test_repository_sources_are_copied_from_pinned_submodules():
    dockerfile = Path("docker/Dockerfile").read_text()
    modules = Path(".gitmodules").read_text()

    assert "thirdparty/SAM3" in modules
    assert "thirdparty/sam-3d-objects" in modules
    assert "thirdparty/dinov2" in modules
    assert "COPY thirdparty/SAM3 thirdparty/SAM3" in dockerfile
    assert "COPY thirdparty/sam-3d-objects thirdparty/sam-3d-objects" in dockerfile
    assert "COPY thirdparty/dinov2 thirdparty/dinov2" in dockerfile
    assert "--stage repos" not in dockerfile
    assert "--stage sam3" in dockerfile
    assert "--stage inference" in dockerfile


def test_pytorch3d_uses_existing_immutable_revision():
    versions = Path("docker/versions.env").read_text()

    assert (
        "PYTORCH3D_REVISION="
        "33824be3cbc87a7dd1db0f6a9a9de9ac81b2d0ba"
    ) in versions
    assert "PYTORCH3D_REVISION=v0.7.8" not in versions


def test_sam3d_image_uses_one_cuda_devel_base_and_installs_uv_from_pypi():
    dockerfile = Path("docker/Dockerfile").read_text()

    assert dockerfile.count("FROM nvidia/cuda:") == 1
    assert "-devel-ubuntu22.04 AS python-base" in dockerfile
    assert "FROM python-base AS builder-base" in dockerfile
    assert "-runtime-ubuntu22.04" not in dockerfile
    assert "ghcr.io/astral-sh/uv" not in dockerfile
    assert 'pip install --no-cache-dir "uv==' in dockerfile
    assert "/opt/venv/bin/uv pip install --python /opt/venv/bin/python" in dockerfile
    assert "FROM sam3d-builder AS sam3d-runtime" in dockerfile


def test_runtime_paths_are_owned_by_backend_config():
    dockerfile = Path("docker/Dockerfile").read_text()
    runtime = dockerfile.split("FROM sam3d-builder AS sam3d-runtime", 1)[1].split(
        "FROM builder-base AS openclip-runtime", 1
    )[0]
    config = Path("config/generate/sam3d.yaml").read_text()

    assert "root: data/cache/sam3d" in config
    assert "asset_root: data/assets" in config
    assert "staging_root: data/jobs/staging" in config
    assert "XDG_CACHE_HOME=" not in runtime
    assert "SAM3D_MODEL_ROOT=" not in runtime
