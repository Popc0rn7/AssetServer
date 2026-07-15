"""Blender-side executor for ``blender-recipe/v1`` documents.

This module imports bpy lazily so AssetServer's gateway remains Blender-free.
"""

from __future__ import annotations

import json
import math
import os
import subprocess

from pathlib import Path


class BlenderRecipeError(ValueError):
    pass


_OBSERVATION_CUTAWAY = {
    "top": frozenset({"ceiling"}),
    "front": frozenset({"wall_south"}),
    "side": frozenset({"wall_east"}),
    "perspective": frozenset({"ceiling", "wall_south", "wall_east"}),
    "room_corner": frozenset({"ceiling", "wall_south", "wall_west"}),
    "agent_view": frozenset({"wall_south"}),
}


def observation_hidden_shell_roles(view: str) -> frozenset[str]:
    """Deterministic observation-only shell visibility policy."""
    try:
        return _OBSERVATION_CUTAWAY[view]
    except KeyError as exc:
        raise BlenderRecipeError(f"unsupported view: {view}") from exc


def build_blend(recipe_path: str | Path, output_path: str | Path) -> None:
    import bpy

    recipe = json.loads(Path(recipe_path).read_text())
    _build_scene(recipe)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.file.pack_all()
    bpy.ops.file.make_paths_relative()
    bpy.ops.wm.save_as_mainfile(filepath=str(destination))


def render_recipe(
    recipe_path: str | Path,
    output_dir: str | Path,
    *,
    views: list[str] | None = None,
    width: int = 512,
    height: int = 512,
    image_format: str = "webp",
    blend_path: str | Path | None = None,
) -> list[dict]:
    import bpy
    from mathutils import Vector

    recipe = json.loads(Path(recipe_path).read_text())
    imported = _build_scene(recipe)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    scene.render.image_settings.file_format = (
        "WEBP" if image_format == "webp" else "PNG"
    )
    scene.world = bpy.data.worlds.new("SceneIRWorld")
    scene.world.color = (0.055, 0.055, 0.055)

    if recipe.get("observation_bounds"):
        minimum = Vector(recipe["observation_bounds"]["min"])
        maximum = Vector(recipe["observation_bounds"]["max"])
    else:
        minimum, maximum = _world_bounds(imported)
    center = (minimum + maximum) / 2
    extent = maximum - minimum
    radius = max(float(extent.length) * 0.8, 2.0)

    bpy.ops.object.light_add(
        type="AREA", location=center + Vector((0, 0, radius * 1.5))
    )
    bpy.context.object.data.energy = 1200
    bpy.context.object.data.shape = "DISK"
    bpy.context.object.data.size = radius
    bpy.ops.object.light_add(
        type="AREA", location=center + Vector((-radius, -radius, radius))
    )
    fill = bpy.context.object
    fill.data.energy = 600
    fill.data.size = radius
    fill.rotation_euler = (center - fill.location).to_track_quat("-Z", "Y").to_euler()

    bpy.ops.object.camera_add()
    camera = bpy.context.object
    camera.data.lens = 50
    scene.camera = camera
    offsets = {
        "top": (0, 0, radius * 2.0),
        "front": (0, -radius * 2.0, radius * 0.45),
        "side": (radius * 2.0, 0, radius * 0.45),
        "perspective": (radius * 1.5, -radius * 1.5, radius * 1.1),
        "room_corner": (-radius * 1.35, -radius * 1.35, radius * 0.85),
        "agent_view": (0, -radius * 1.7, max(radius * 0.55, 1.6)),
    }
    requested = views or ["top", "front", "side", "perspective"]
    unknown = sorted(set(requested) - set(offsets))
    if unknown:
        raise BlenderRecipeError(f"unsupported views: {unknown}")
    rendered = []
    render_device = _render_device()
    for view in requested:
        hidden_roles = observation_hidden_shell_roles(view)
        for obj in imported:
            role = obj.get("assetserver_procedural_shell_role")
            obj.hide_render = role in hidden_roles
        camera.location = center + Vector(offsets[view])
        camera.rotation_euler = (
            (center - camera.location).to_track_quat("-Z", "Y").to_euler()
        )
        path = output / f"{view}.{image_format}"
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        rendered.append(
            {
                "view": view,
                "path": str(path),
                "camera_location": list(camera.location),
                "target": list(center),
                "extrinsics": [list(row) for row in camera.matrix_world.inverted()],
                "intrinsics": _camera_intrinsics(camera.data, width, height),
                "renderer": "BLENDER_EEVEE_NEXT",
                "device": render_device,
            }
        )
    if blend_path is not None:
        destination = Path(blend_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.file.pack_all()
        bpy.ops.file.make_paths_relative()
        bpy.ops.wm.save_as_mainfile(filepath=str(destination))
    return rendered


def _build_scene(recipe: dict):
    import bpy

    if recipe.get("schema_version") != "blender-recipe/v1":
        raise BlenderRecipeError("unsupported Blender recipe")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    all_imported = []
    for instance in recipe.get("instances", []):
        root = bpy.data.objects.new(instance["name"], None)
        bpy.context.scene.collection.objects.link(root)
        root.location = instance["translation"]
        root.rotation_euler = instance["rotation_radians"]
        root.scale = (instance["scale"],) * 3
        asset_frame = bpy.data.objects.new(f"{instance['name']}__asset_frame", None)
        bpy.context.scene.collection.objects.link(asset_frame)
        asset_frame.parent = root
        from mathutils import Matrix

        asset_frame.matrix_local = Matrix(instance.get("asset_transform") or _identity())
        parts = instance.get("visual_parts") or []
        if parts:
            deltas = _articulated_link_deltas(instance)
            for index, part in enumerate(parts):
                link = part["link"]
                if link not in deltas:
                    raise BlenderRecipeError(f"Drake did not resolve visual link: {link}")
                imported = _import_visual(Path(part["visual"]))
                part_frame = bpy.data.objects.new(
                    f"{instance['name']}__{link}__{index}", None
                )
                bpy.context.scene.collection.objects.link(part_frame)
                part_frame.parent = asset_frame
                part_frame.matrix_local = Matrix(deltas[link])
                for obj in imported:
                    if obj.parent is None:
                        obj.parent = part_frame
                all_imported.extend(imported)
        else:
            imported = _import_visual(Path(instance["visual"]))
            if instance.get("procedural_shell"):
                for obj in imported:
                    role = _procedural_shell_role(obj.name)
                    if role is not None:
                        obj["assetserver_procedural_shell_role"] = role
                    obj["assetserver_scene_instance"] = instance["name"]
            for obj in imported:
                if obj.parent is None:
                    obj.parent = asset_frame
                obj["assetserver_scene_instance"] = instance["name"]
            all_imported.extend(imported)
    bpy.context.view_layer.update()
    return all_imported


def _procedural_shell_role(name: str) -> str | None:
    normalized = name.lower()
    if "ceiling" in normalized:
        return "ceiling"
    if "floor" in normalized:
        return "floor"
    for wall in ("north", "south", "east", "west"):
        if f"wall_{wall}" in normalized:
            return f"wall_{wall}"
    return None


def _import_visual(visual: Path):
    import bpy

    before = set(bpy.data.objects)
    suffix = visual.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(visual))
    elif suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=str(visual))
    else:
        raise BlenderRecipeError(f"unsupported visual format: {suffix}")
    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not imported:
        raise BlenderRecipeError(f"asset imported no objects: {visual}")
    return imported


def _articulated_link_deltas(instance: dict) -> dict[str, list[list[float]]]:
    """Use Drake FK; Blender only consumes the resulting snapshot transforms."""
    try:
        import numpy as np
        from pydrake.multibody.parsing import Parser
        from pydrake.multibody.plant import MultibodyPlant

        plant = MultibodyPlant(time_step=0.0)
        models = Parser(plant).AddModels(str(instance["simulation"]))
        if len(models) != 1:
            raise BlenderRecipeError("articulated simulation must contain one model")
        plant.Finalize()
        default_context = plant.CreateDefaultContext()
        posed_context = plant.CreateDefaultContext()
        positions = plant.GetPositions(posed_context).copy()
        for name, value in (instance.get("initial_joints") or {}).items():
            joint = plant.GetJointByName(name, models[0])
            if joint.num_positions() != 1:
                raise BlenderRecipeError(f"unsupported joint position count: {name}")
            positions[joint.position_start()] = float(value)
        plant.SetPositions(posed_context, positions)
        output = {}
        for part in instance.get("visual_parts") or []:
            link = part["link"]
            body = plant.GetBodyByName(link, models[0])
            default = plant.EvalBodyPoseInWorld(default_context, body).GetAsMatrix4()
            posed = plant.EvalBodyPoseInWorld(posed_context, body).GetAsMatrix4()
            output[link] = (posed @ np.linalg.inv(default)).tolist()
        return output
    except BlenderRecipeError:
        raise
    except Exception as exc:
        raise BlenderRecipeError(f"Drake FK failed: {exc}") from exc


def _world_bounds(objects):
    from mathutils import Vector

    points = [
        obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box
    ]
    if not points:
        raise BlenderRecipeError("scene has no renderable bounds")
    minimum = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    maximum = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    if not all(math.isfinite(value) for value in (*minimum, *maximum)):
        raise BlenderRecipeError("scene bounds are not finite")
    return minimum, maximum


def _identity():
    return (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _camera_intrinsics(camera, width: int, height: int) -> list[list[float]]:
    focal = camera.lens / camera.sensor_width * width
    return [
        [float(focal), 0.0, width / 2.0],
        [0.0, float(focal), height / 2.0],
        [0.0, 0.0, 1.0],
    ]


def _render_device() -> str:
    policy = os.environ.get("ASSETSERVER_RENDER_DEVICE", "gpu").lower()
    if policy == "disabled":
        raise BlenderRecipeError("render device unavailable: GPU rendering is disabled")
    if policy != "gpu":
        raise BlenderRecipeError(f"unsupported render device policy: {policy}")
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BlenderRecipeError(f"render device unavailable: {exc}") from exc
    devices = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not devices:
        raise BlenderRecipeError("render device unavailable: nvidia-smi returned no GPU")
    visible = os.environ.get("NVIDIA_VISIBLE_DEVICES") or os.environ.get(
        "CUDA_VISIBLE_DEVICES", "all"
    )
    return f"NVIDIA[{visible}]/{devices[0]}"
