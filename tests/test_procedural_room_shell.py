import json
import xml.etree.ElementTree as ET
import zipfile

import pytest
import yaml

from httpx import ASGITransport, AsyncClient

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.asset_store import AssetStore
from assetserver.jobs import SQLiteJobStore
from assetserver.procedural_room_shell import (
    ProceduralRoomShellStore,
    normalized_shell,
    shell_boxes,
)
from assetserver.scene_compilers import blender_recipe, compile_drake_directives
from assetserver.scene_ir import (
    ProceduralRoomShell,
    SceneIR,
    SceneIRValidationError,
    load_scene_yaml,
)
from assetserver.scene_ir_store import IRSceneStore
import assetserver.scene_job_handlers as handlers


def _shell(**changes):
    value = {
        "kind": "procedural",
        "dimensions": [3.2, 2.8, 2.7],
        "wall_thickness": 0.05,
        "floor_thickness": 0.1,
        "include_ceiling": False,
        "openings": [],
    }
    value.update(changes)
    return value


def _scene(shell=None):
    return {"rooms": [{"id": "office", "shell": shell or _shell()}]}


def test_schema_and_parser_support_legacy_and_procedural_shells():
    legacy = "asset://sha256/" + "a" * 64
    assert load_scene_yaml(
        yaml.safe_dump(_scene({"asset_ref": legacy}))
    ).asset_refs() == {legacy}
    procedural = load_scene_yaml(yaml.safe_dump(_scene()))
    assert procedural.asset_refs() == set()
    schema = SceneIR.model_json_schema()
    shell_schema = schema["$defs"]["Room"]["properties"]["shell"]
    assert "anyOf" in shell_schema
    serialized = json.dumps(schema)
    assert "ProceduralRoomShell" in serialized
    assert "dimensions" in serialized
    assert "openings" in serialized


@pytest.mark.parametrize(
    ("shell", "code"),
    [
        (_shell(dimensions=[3.0, -1.0, 2.5]), "invalid_room_dimensions"),
        (
            _shell(
                openings=[
                    {
                        "id": "door",
                        "opening_type": "door",
                        "wall": "south",
                        "offset_m": 2.8,
                        "width": 0.8,
                        "height": 2.0,
                    }
                ]
            ),
            "opening_out_of_bounds",
        ),
        (
            _shell(
                openings=[
                    {
                        "id": "a",
                        "opening_type": "door",
                        "wall": "north",
                        "offset_m": 0.5,
                        "width": 1.0,
                        "height": 2.0,
                    },
                    {
                        "id": "b",
                        "opening_type": "window",
                        "wall": "north",
                        "offset_m": 1.0,
                        "width": 1.0,
                        "height": 1.0,
                        "sill_height": 1.0,
                    },
                ]
            ),
            "opening_overlap",
        ),
        (
            _shell(
                openings=[
                    {
                        "id": "same",
                        "opening_type": "door",
                        "wall": "north",
                        "offset_m": 0.0,
                        "width": 0.8,
                        "height": 2.0,
                    },
                    {
                        "id": "same",
                        "opening_type": "door",
                        "wall": "south",
                        "offset_m": 0.0,
                        "width": 0.8,
                        "height": 2.0,
                    },
                ]
            ),
            "duplicate_opening_id",
        ),
        (
            _shell(
                openings=[
                    {
                        "id": "open",
                        "opening_type": "open",
                        "wall": "west",
                        "offset_m": 0.0,
                        "width": 1.0,
                        "height": 2.0,
                    }
                ]
            ),
            "invalid_opening_semantics",
        ),
        ({"kind": "remote", "dimensions": [3, 3, 3]}, "procedural_shell_unsupported"),
    ],
)
def test_procedural_input_errors_have_stable_codes(shell, code):
    with pytest.raises(SceneIRValidationError) as error:
        load_scene_yaml(yaml.safe_dump(_scene(shell)))
    assert error.value.code == code


def test_geometry_preserves_clear_dimensions_and_cuts_door_window_and_opening():
    empty = shell_boxes(ProceduralRoomShell.model_validate(_shell()))
    assert next(box for box in empty if box.name == "wall_north_000").extents == (
        3.3,
        0.05,
        2.7,
    )
    assert next(box for box in empty if box.name == "wall_east_000").extents == (
        0.05,
        2.8,
        2.7,
    )
    shell = ProceduralRoomShell.model_validate(
        _shell(
            include_ceiling=True,
            openings=[
                {
                    "id": "door",
                    "opening_type": "door",
                    "wall": "south",
                    "offset_m": 1.0,
                    "width": 0.9,
                    "height": 2.1,
                },
                {
                    "id": "window",
                    "opening_type": "window",
                    "wall": "north",
                    "offset_m": 0.4,
                    "width": 1.0,
                    "height": 1.0,
                    "sill_height": 0.9,
                },
                {
                    "id": "passage",
                    "opening_type": "open",
                    "wall": "east",
                    "offset_m": 0.5,
                    "width": 1.0,
                    "height": 2.7,
                },
            ],
        )
    )
    boxes = shell_boxes(shell)
    floor = next(box for box in boxes if box.name == "floor")
    assert floor.extents == pytest.approx((3.3, 2.9, 0.1))
    assert floor.center == pytest.approx((0, 0, -0.05))
    assert next(box for box in boxes if box.name == "ceiling").center[2] == 2.75

    # Door/open intervals contain no collision box; the window keeps sill and header.
    south = [box for box in boxes if box.name.startswith("wall_south")]
    for box in south:
        x0, x1 = box.center[0] - box.extents[0] / 2, box.center[0] + box.extents[0] / 2
        z0, z1 = box.center[2] - box.extents[2] / 2, box.center[2] + box.extents[2] / 2
        assert not (
            x0 < 0.3 - 1e-9 and -0.6 + 1e-9 < x1 and z0 < 2.1 - 1e-9 and 1e-9 < z1
        )
    north = [box for box in boxes if box.name.startswith("wall_north")]
    assert any(box.center[2] == pytest.approx(0.45) for box in north)
    assert any(box.center[2] == pytest.approx(2.3) for box in north)
    east = [box for box in boxes if box.name.startswith("wall_east")]
    assert len(east) == 2


def test_materialization_is_content_addressed_and_contains_glb_uv_and_sdf_boxes(
    tmp_path,
):
    first = ProceduralRoomShell.model_validate(
        _shell(
            openings=[
                {
                    "id": "b",
                    "opening_type": "window",
                    "wall": "north",
                    "offset_m": 1.5,
                    "width": 0.5,
                    "height": 0.8,
                    "sill_height": 1.0,
                },
                {
                    "id": "a",
                    "opening_type": "door",
                    "wall": "south",
                    "offset_m": 0.2,
                    "width": 0.8,
                    "height": 2.0,
                },
            ]
        )
    )
    second = first.model_copy(update={"openings": list(reversed(first.openings))})
    assert normalized_shell(first) == normalized_shell(second)
    store = ProceduralRoomShellStore(tmp_path / "procedural_room_shells")
    materialized = store.materialize(first)
    assert store.materialize(second).cache_key == materialized.cache_key
    assert materialized.root.parent.parent == tmp_path / "procedural_room_shells"
    assert materialized.visual_path.read_bytes()[:4] == b"glTF"
    sdf = ET.fromstring(materialized.simulation_path.read_bytes())
    assert len(list(sdf.iter("collision"))) == len(materialized.manifest["boxes"])
    assert all(
        item.find("geometry/box/size") is not None for item in sdf.iter("collision")
    )

    import trimesh

    loaded = trimesh.load(materialized.visual_path, force="scene")
    assert loaded.geometry
    assert all(geometry.visual.uv is not None for geometry in loaded.geometry.values())

    rounded_variant = first.model_copy(update={"dimensions": (3.2000001, 2.8, 2.7)})
    other = ProceduralRoomShellStore(tmp_path / "other-cache").materialize(
        rounded_variant
    )
    assert other.cache_key == materialized.cache_key
    assert other.visual_path.read_bytes() == materialized.visual_path.read_bytes()
    assert (
        other.simulation_path.read_bytes() == materialized.simulation_path.read_bytes()
    )


def test_compilers_use_generated_shell_without_creating_asset(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    scene = SceneIR.model_validate(_scene())
    recipe = json.loads(blender_recipe(scene, assets))
    room = recipe["instances"][0]
    assert room["procedural_shell"]["generator_version"] == ("procedural-room-shell/v1")
    assert "/procedural_room_shells/" in room["visual"]
    assert room["interior_bounds"] == {
        "min": [-1.6, -1.4, 0.0],
        "max": [1.6, 1.4, 2.7],
    }
    assert room["asset_transform"] == [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, -1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    assert recipe["observation_bounds"] == room["interior_bounds"]
    directives = compile_drake_directives(scene, assets).decode()
    assert "procedural_room_shells" in directives
    assert not assets.root.exists()


def test_same_shell_reuses_geometry_but_revision_provenance_remains_distinct(tmp_path):
    scenes = IRSceneStore(tmp_path / "scenes", AssetStore(tmp_path / "assets"))
    first = scenes.create(yaml.safe_dump(_scene()).encode())
    changed = _scene()
    changed["description"] = "same geometry, new scene revision"
    second = scenes.update(
        first.scene_id, yaml.safe_dump(changed).encode(), base_revision=1
    )
    first_binding = json.loads(
        (tmp_path / "scenes" / first.scene_id / "geometry" / "000001.json").read_text()
    )
    second_binding = json.loads(
        (tmp_path / "scenes" / first.scene_id / "geometry" / "000002.json").read_text()
    )
    assert first_binding["procedural_shells"] == second_binding["procedural_shells"]
    assert first_binding["scene_revision"] == 1
    assert second_binding["scene_revision"] == 2
    assert first_binding["scene_sha256"] != second_binding["scene_sha256"]
    assert second.sha256 == second_binding["scene_sha256"]


async def test_gateway_returns_stable_error_and_marks_old_observation_stale(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    scenes = IRSceneStore(tmp_path / "scenes", assets)
    jobs = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    gateway = AssetAcquisitionApp(ir_scene_store=scenes, job_store=jobs)
    gateway._scene_data_root = tmp_path
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        invalid = await client.post(
            "/v2/scenes",
            content=yaml.safe_dump(_scene(_shell(dimensions=[0, 2, 3]))),
            headers={"content-type": "application/yaml"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"] == "invalid_room_dimensions"

        created = await client.post(
            "/v2/scenes",
            content=yaml.safe_dump(_scene()),
            headers={"content-type": "application/yaml"},
        )
        assert created.status_code == 201
        scene_id = created.json()["scene_id"]
        first_geometry = json.loads(
            (tmp_path / "scenes" / scene_id / "geometry" / "000001.json").read_text()
        )
        assert first_geometry["scene_sha256"] == created.json()["sha256"]
        job, _ = jobs.submit("observe", scene_id, 1, {})
        jobs.claim("worker")
        observation = tmp_path / "scenes" / scene_id / "observations" / job.job_id
        observation.mkdir(parents=True)
        (observation / "top.webp").write_bytes(b"image")
        (observation / "manifest.json").write_text(
            json.dumps(
                {
                    "observation_id": job.job_id,
                    "scene_revision": 1,
                    "views": [{"view": "top", "path": "top.webp"}],
                }
            )
        )
        jobs.complete(
            job.job_id,
            "worker",
            {
                "manifest_path": (observation / "manifest.json")
                .relative_to(tmp_path)
                .as_posix(),
                "views": [
                    {
                        "view": "top",
                        "path": (observation / "top.webp")
                        .relative_to(tmp_path)
                        .as_posix(),
                    }
                ],
            },
        )
        changed = _scene(_shell(include_ceiling=True))
        updated = await client.put(
            f"/v2/scenes/{scene_id}",
            content=yaml.safe_dump(changed),
            headers={
                "content-type": "application/yaml",
                "x-base-revision": "1",
            },
        )
        assert updated.status_code == 201
        assert updated.json()["sha256"] != created.json()["sha256"]
        second_geometry = json.loads(
            (tmp_path / "scenes" / scene_id / "geometry" / "000002.json").read_text()
        )
        assert (
            second_geometry["procedural_shells"]["office"]["cache_key"]
            != first_geometry["procedural_shells"]["office"]["cache_key"]
        )
        old = await client.get(f"/v2/observations/{job.job_id}")
        assert old.json()["stale"] is True


def test_observe_and_export_include_procedural_visual_collision_and_materials(
    tmp_path, monkeypatch
):
    data = tmp_path / "data"
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("ASSETSERVER_DATA_ROOT", str(data))
    monkeypatch.setenv("ASSETSERVER_OUTPUT_ROOT", str(outputs))
    assets = AssetStore(data / "assets")
    scenes = IRSceneStore(data / "scenes", assets)
    scene = scenes.create(yaml.safe_dump(_scene()).encode())
    jobs = SQLiteJobStore(data / "jobs.sqlite3")
    captured = []

    def fake_render(
        recipe_path,
        output_dir,
        *,
        views,
        image_format,
        blend_path=None,
        **_,
    ):
        recipe = json.loads(recipe_path.read_text())
        captured.append(recipe)
        assert recipe["instances"][0]["procedural_shell"]["cache_key"]
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

    monkeypatch.setattr(handlers, "render_recipe", fake_render)
    observe_job, _ = jobs.submit("observe", scene.scene_id, 1, {"views": ["top"]})
    observation = handlers.observe(observe_job)
    manifest = json.loads((data / observation["manifest_path"]).read_text())
    assert manifest["provenance"]["scene_revision"] == 1
    assert captured[0]["instances"][0]["visual"].endswith("shell.glb")

    export_job, _ = jobs.submit("export", scene.scene_id, 1, {"views": ["top"]})
    exported = handlers.export(export_job)
    with zipfile.ZipFile(outputs / exported["zip_path"]) as archive:
        names = set(archive.namelist())
        shell_sdf = next(
            name
            for name in names
            if name.startswith("package/procedural_shells/")
            and name.endswith("/shell.sdf")
        )
        assert shell_sdf.replace("shell.sdf", "shell.glb") in names
        assert not any(name.startswith("package/assets/") for name in names)
        simulation = json.loads(archive.read("package/compiled/simulation/scene.json"))
        generated = next(iter(simulation["procedural_shells"].values()))
        assert generated["generator_version"] == "procedural-room-shell/v1"
        assert generated["material_version"] == "procedural-room-materials/v1"
        assert generated["collision_geometries"]
