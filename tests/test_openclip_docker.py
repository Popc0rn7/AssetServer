from pathlib import Path


def test_openclip_target_reuses_sam3d_builder_base_without_sam3d_layers():
    dockerfile = Path("docker/3d/Dockerfile").read_text()

    assert "FROM builder-base AS openclip-runtime" in dockerfile
    target = dockerfile.split("FROM builder-base AS openclip-runtime", 1)[1]
    assert "open-clip-torch==" in target
    assert "assetserver/openclip_server" in target
    assert "install_sam3d_docker.sh" not in target
    assert "ENTRYPOINT [\"python\", \"-m\", \"assetserver.openclip_server.standalone\"]" in target


def test_openclip_scripts_use_stable_user_facing_entries():
    build = Path("scripts/build_openclip_docker.sh").read_text()
    run = Path("scripts/run_openclip_docker.sh").read_text()
    download = Path("scripts/download_openclip_ckpt.sh").read_text()

    assert "--target openclip-runtime" in build
    assert "--sudo" in build and "--proxy" in build
    assert "OPENCLIP_MODELS" in run
    assert "openclip-cache" in run
    assert "download_openclip_ckpt.py" in download
    assert "HF_ENDPOINT" in download
    assert "hf-mirror.com" not in download
    assert 'HF_ENDPOINT="${HF_ENDPOINT:-}"' in download


def test_openclip_downloader_creates_flat_offline_bundle_without_torch_validation():
    downloader = Path("scripts/download_openclip_ckpt.py").read_text()

    assert "create_manifest" in downloader
    assert "shutil.copy2" in downloader
    assert "verify_assetserver_load" not in downloader
