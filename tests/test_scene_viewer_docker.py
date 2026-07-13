from pathlib import Path

import yaml


def test_scene_viewer_branches_before_torch_and_installs_source_last():
    dockerfile = Path("docker/Dockerfile").read_text()
    target = dockerfile.split("FROM python-base AS scene-viewer", 1)[1].split(
        "FROM builder-base AS hunyuan3d-builder", 1
    )[0]

    assert '"bpy==${BLENDER_VERSION}"' in target
    assert '"drake==${DRAKE_VERSION}"' in target
    assert "import bpy, pydrake" in target
    assert target.index('"bpy==${BLENDER_VERSION}"') < target.index(
        "COPY assetserver/__init__.py"
    )
    assert "install_sam3d_docker.sh" not in target


def test_scene_viewer_has_read_only_asset_overlay_and_writable_job_storage():
    service = yaml.safe_load(Path("docker/services.yaml").read_text())["services"][
        "scene-viewer"
    ]
    manager = Path("scripts/docker_service.py").read_text()

    assert service["data"] == "read-write"
    assert service["assets_read_only"] is True
    assert service["outputs"] == "read-write"
    assert "f\"{data / 'assets'}:/data/assets:ro\"" in manager


def test_scene_viewer_runs_persistent_sqlite_worker():
    dockerfile = Path("docker/Dockerfile").read_text()

    assert '"assetserver.job_worker"' in dockerfile
    assert "observe=assetserver.scene_job_handlers:observe" in dockerfile
    assert "validate=assetserver.scene_job_handlers:validate" in dockerfile
    assert "export=assetserver.scene_job_handlers:export" in dockerfile
    assert "ASSETSERVER_DATA_ROOT=/data" in dockerfile
    assert "ASSETSERVER_OUTPUT_ROOT=/outputs" in dockerfile
