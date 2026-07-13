from pathlib import Path


def test_repository_fetches_are_shallow_retried_and_separate_layers():
    installer = Path("scripts/install_sam3d_docker.sh").read_text()
    dockerfile = Path("docker/Dockerfile").read_text()

    assert "fetch --depth 1" in installer
    assert "for attempt in 1 2 3" in installer
    assert "GIT_LFS_SKIP_SMUDGE=1" in installer
    assert "--stage repos-objects" in dockerfile
    assert "--stage repos-sam3" in dockerfile
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


def test_runtime_caches_are_routed_to_the_writable_cache_volume():
    dockerfile = Path("docker/Dockerfile").read_text()

    assert "XDG_CACHE_HOME=/var/cache/sam3d/xdg" in dockerfile
    assert "XDG_CONFIG_HOME=/var/cache/sam3d/config" in dockerfile
    assert "MPLCONFIGDIR=/var/cache/sam3d/matplotlib" in dockerfile
    assert "HF_HOME=/var/cache/sam3d/hf" in dockerfile
    assert "TORCH_HOME=/var/cache/sam3d/torch" in dockerfile
    assert "TORCH_EXTENSIONS_DIR=/var/cache/sam3d/torch-extensions" in dockerfile
    assert "mkdir -p /var/lib/sam3d/assets /var/cache/sam3d/xdg" in dockerfile
    assert "ln -s /var/cache/sam3d/xdg /home/sam3d/.cache" in dockerfile
