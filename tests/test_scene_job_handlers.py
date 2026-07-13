import json
import zipfile

from dataclasses import replace

import yaml
import pytest

from assetserver.asset_store import AssetStore
from assetserver.jobs import SQLiteJobStore
from assetserver.scene_ir_store import IRSceneStore
import assetserver.scene_job_handlers as handlers
from assetserver.blender_scene_worker import BlenderRecipeError
from assetserver.jobs import JobExecutionError


def _scene_and_job(tmp_path, monkeypatch, job_type, request=None):
    data = tmp_path / "data"
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("ASSETSERVER_DATA_ROOT", str(data))
    monkeypatch.setenv("ASSETSERVER_OUTPUT_ROOT", str(outputs))
    assets = AssetStore(data / "assets")
    asset = assets.ingest(
        {"room.glb": b"visual", "room.sdf": b"<sdf version='1.10'/>"},
        visual="room.glb",
        simulation="room.sdf",
    )
    scenes = IRSceneStore(data / "scenes", assets)
    scene = scenes.create(
        yaml.safe_dump(
            {"rooms": [{"id": "main", "shell": {"asset_ref": asset.asset_ref}}]}
        ).encode()
    )
    store = SQLiteJobStore(data / "jobs/jobs.sqlite3")
    job, _ = store.submit(job_type, scene.scene_id, 1, request or {})
    return data, outputs, asset, scene, job


def _fake_render(recipe_path, output_dir, *, views, image_format, blend_path=None, **_):
    assert json.loads(recipe_path.read_text())["schema_version"] == "blender-recipe/v1"
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for view in views:
        path = output_dir / f"{view}.{image_format}"
        path.write_bytes(b"image")
        rendered.append(
            {
                "view": view,
                "path": str(path),
                "camera_location": [1, 2, 3],
                "target": [0, 0, 0],
            }
        )
    if blend_path:
        blend_path.write_bytes(b"blend")
    return rendered


def test_observe_handler_publishes_revision_scoped_images(tmp_path, monkeypatch):
    data, _, _, scene, job = _scene_and_job(
        tmp_path, monkeypatch, "observe", {"views": ["top"], "format": "png"}
    )
    monkeypatch.setattr(handlers, "render_recipe", _fake_render)
    result = handlers.observe(job)
    assert result["observation_id"] == job.job_id
    assert (data / result["views"][0]["path"]).read_bytes() == b"image"
    manifest = json.loads((data / result["manifest_path"]).read_text())
    assert manifest["scene_id"] == scene.scene_id
    assert manifest["scene_revision"] == 1


def test_observe_device_failure_is_non_retryable(tmp_path, monkeypatch):
    _, _, _, _, job = _scene_and_job(tmp_path, monkeypatch, "observe")
    monkeypatch.setattr(
        handlers,
        "render_recipe",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            BlenderRecipeError("render device unavailable: no GPU")
        ),
    )

    with pytest.raises(JobExecutionError) as error:
        handlers.observe(job)

    assert error.value.code == "render_device_unavailable"
    assert error.value.retryable is False


def test_export_handler_builds_package_assets_blend_drake_and_zip(
    tmp_path, monkeypatch
):
    _, outputs, asset, scene, job = _scene_and_job(tmp_path, monkeypatch, "export")
    monkeypatch.setattr(handlers, "render_recipe", _fake_render)
    result = handlers.export(job)
    package = outputs / result["package_path"]
    assert (package / "scene.yaml").is_file()
    assert (package / "compiled/blender/scene.blend").read_bytes() == b"blend"
    directive = (package / "compiled/drake/scene.dmd.yaml").read_text()
    assert (
        f"package://scene/assets/sha256/{asset.digest[:2]}/{asset.digest}" in directive
    )
    assert (package / "checksums.sha256").is_file()
    archive = outputs / result["zip_path"]
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        assert "package/scene.yaml" in names
        assert "package/compiled/blender/scene.blend" in names
        assert "package/compiled/drake/scene.dmd.yaml" in names
    assert result["size_bytes"] == archive.stat().st_size


def test_export_zip_is_reproducible_across_job_ids(tmp_path, monkeypatch):
    _, _, _, _, job = _scene_and_job(tmp_path, monkeypatch, "export")
    monkeypatch.setattr(handlers, "render_recipe", _fake_render)
    first = handlers.export(job)
    second = handlers.export(replace(job, job_id="00000000-0000-0000-0000-000000000002"))
    assert first["sha256"] == second["sha256"]
