from pathlib import Path

import yaml


def test_sam3d_uses_host_owned_shared_data_without_recursive_chown():
    manager = Path("scripts/docker_service.py").read_text()
    service = yaml.safe_load(Path("docker/services.yaml").read_text())["services"][
        "sam3d"
    ]

    assert service["data"] == "read-write"
    assert service["run_as_host"] is True
    assert service["environment"]["ASSETSERVER_ASSET_ROOT"] == "/data/assets"
    assert "os.getuid()" in manager and "os.getgid()" in manager
    assert "chown" not in manager


def test_container_manager_is_the_only_build_run_interface():
    assert Path("scripts/docker_service.sh").is_file()
    assert Path("scripts/docker_service.py").is_file()
    assert not list(Path("scripts").glob("build_*_docker.sh"))
    assert not list(Path("scripts").glob("run_*_docker.sh"))
    assert not Path("scripts/build_sam3d_image.sh").exists()
