from pathlib import Path


def test_scene_viewer_target_reuses_shared_python_base_without_torch_layer():
    dockerfile = Path("docker/3d/Dockerfile").read_text()

    assert "FROM python-base AS scene-viewer" in dockerfile
    target = dockerfile.split("FROM python-base AS scene-viewer", 1)[1]
    assert '"bpy==${BLENDER_VERSION}"' in target
    assert '"drake==${DRAKE_VERSION}"' in target
    assert "import bpy, pydrake" in target
    assert "install_sam3d_docker.sh" not in target


def test_scene_viewer_build_script_targets_viewer_image():
    script = Path("scripts/build_scene_viewer_docker.sh").read_text()

    assert "--target scene-viewer" in script
    assert "BLENDER_VERSION" in script
    assert "DRAKE_VERSION" in script
    assert "SCENE_VIEWER_IMAGE:-assetserver-scene-viewer:dev" in script
    assert "--sudo" in script
    assert "--proxy" in script


def test_scene_viewer_includes_renderable_smoke_scene():
    dockerfile = Path("docker/3d/Dockerfile").read_text()
    smoke = Path("scripts/smoke_scene_viewer.py").read_text()
    run = Path("scripts/run_scene_viewer_docker.sh").read_text()

    assert (
        "COPY scripts/smoke_scene_viewer.py /app/scripts/smoke_scene_viewer.py"
        in dockerfile
    )
    assert "BLENDER_EEVEE_NEXT" in smoke
    assert "bpy.ops.render.render(write_still=True)" in smoke
    assert 'bpy.data.worlds.new("SmokeWorld")' in smoke
    assert "smoke_scene.blend" in smoke
    assert "view_" in smoke
    assert "--smoke" in run
    assert "/data/cache/scene-viewer-smoke" in run
    assert "--gpu" in run
    assert "--output-dir" in run
    assert "--sudo" in run
    assert "SCENE_VIEWER_IMAGE:-assetserver-scene-viewer:dev" in run
    assert "-e HOME=/tmp" in run


def test_scene_viewer_runs_persistent_sqlite_worker_with_shared_storage():
    dockerfile = Path("docker/3d/Dockerfile").read_text()
    run = Path("scripts/run_scene_viewer_docker.sh").read_text()

    assert '"assetserver.job_worker"' in dockerfile
    assert "observe=assetserver.scene_job_handlers:observe" in dockerfile
    assert "validate=assetserver.scene_job_handlers:validate" in dockerfile
    assert "export=assetserver.scene_job_handlers:export" in dockerfile
    assert "ASSETSERVER_DATA_ROOT=/data" in dockerfile
    assert "ASSETSERVER_OUTPUT_ROOT=/outputs" in dockerfile
    assert '-v "$DATA_DIR:/data"' in run
    assert '-v "$OUTPUT_DIR:/outputs"' in run
    assert "--restart unless-stopped" in run
    assert "--foreground" in run
    assert "--no-gpu" in run
