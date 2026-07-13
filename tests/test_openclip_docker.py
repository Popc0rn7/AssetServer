from pathlib import Path

import yaml


def test_openclip_target_reuses_torch_base_without_sam3d_layers():
    dockerfile = Path("docker/Dockerfile").read_text()
    target = dockerfile.split("FROM builder-base AS openclip-runtime", 1)[1].split(
        "FROM python-base AS scene-viewer", 1
    )[0]

    assert "open-clip-torch==" in target
    assert target.index("open-clip-torch==") < target.index(
        "COPY assetserver/openclip_server"
    )
    assert "install_sam3d_docker.sh" not in target
    assert "assetserver.openclip_server.standalone" in target


def test_openclip_runtime_has_no_shared_data_mount():
    service = yaml.safe_load(Path("docker/services.yaml").read_text())["services"][
        "openclip"
    ]

    assert "data" not in service
    assert service["model_host"] == "checkpoints/open_clip"
    assert service["model_container"] == "/models"
    assert service["cache_volume"] == "openclip-cache"
    assert "cache_host" not in service


def test_openclip_downloader_creates_flat_offline_bundle_without_torch_validation():
    downloader = Path("scripts/download_openclip_ckpt.py").read_text()

    assert "create_manifest" in downloader
    assert "shutil.copy2" in downloader
    assert "verify_assetserver_load" not in downloader
