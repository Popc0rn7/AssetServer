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
    assert 'SCENE_VIEWER_IMAGE:-assetserver-scene-viewer:dev' in script
    assert "--sudo" in script
    assert "--proxy" in script


def test_scene_viewer_includes_renderable_smoke_scene():
    dockerfile = Path("docker/3d/Dockerfile").read_text()
    smoke = Path("scripts/smoke_scene_viewer.py").read_text()
    run = Path("scripts/run_scene_viewer_docker.sh").read_text()

    assert "COPY scripts/smoke_scene_viewer.py" not in dockerfile
    assert "BLENDER_EEVEE_NEXT" in smoke
    assert "bpy.ops.render.render(write_still=True)" in smoke
    assert 'bpy.data.worlds.new("SmokeWorld")' in smoke
    assert "smoke_scene.blend" in smoke
    assert "view_" in smoke
    assert 'SMOKE_SCRIPT="$PWD/scripts/smoke_scene_viewer.py"' in run
    assert '-v "$SMOKE_SCRIPT:/app/smoke_scene_viewer.py:ro"' in run
    assert "--gpu" in run
    assert "--output-dir" in run
    assert "--sudo" in run
    assert 'SCENE_VIEWER_IMAGE:-assetserver-scene-viewer:dev' in run
    assert "-e HOME=/tmp" in run
