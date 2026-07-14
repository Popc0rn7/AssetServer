from pathlib import Path

import yaml

from scripts.docker_service import _resolve_service


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


def test_sam3d_docker_endpoint_is_derived_from_backend_config(tmp_path, monkeypatch):
    backend = tmp_path / "sam3d.yaml"
    backend.write_text("server:\n  host: 127.0.0.1\n  port: 7012\n")
    monkeypatch.setattr("scripts.docker_service.ROOT", tmp_path)

    service = _resolve_service(
        "sam3d",
        {
            "backend_config": "sam3d.yaml",
            "container_port": 7000,
            "ready_path": "/health/ready",
        },
    )

    assert service["port"] == "127.0.0.1:7012:7000"
    assert service["ready_url"] == "http://127.0.0.1:7012/health/ready"


def test_sam3d_registry_has_no_duplicate_host_port():
    service = yaml.safe_load(Path("docker/services.yaml").read_text())["services"][
        "sam3d"
    ]

    assert service["backend_config"] == "config/generate/sam3d.yaml"
    assert service["container_port"] == 7000
    assert service["ready_path"] == "/health/ready"
    assert "port" not in service
    assert "ready_url" not in service
