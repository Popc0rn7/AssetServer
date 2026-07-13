"""Blender-side executor for ``blender-recipe/v1`` documents.

This module imports bpy lazily so AssetServer's gateway remains Blender-free.
"""

from __future__ import annotations

import json
import math

from pathlib import Path


class BlenderRecipeError(ValueError):
    pass


def build_blend(recipe_path: str | Path, output_path: str | Path) -> None:
    import bpy

    recipe = json.loads(Path(recipe_path).read_text())
    _build_scene(recipe)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.file.pack_all()
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
    scene.render.image_settings.file_format = (
        "WEBP" if image_format == "webp" else "PNG"
    )
    scene.world = bpy.data.worlds.new("SceneIRWorld")
    scene.world.color = (0.055, 0.055, 0.055)

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
    }
    requested = views or ["top", "front", "side", "perspective"]
    unknown = sorted(set(requested) - set(offsets))
    if unknown:
        raise BlenderRecipeError(f"unsupported views: {unknown}")
    rendered = []
    for view in requested:
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
            }
        )
    if blend_path is not None:
        destination = Path(blend_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.file.pack_all()
        bpy.ops.wm.save_as_mainfile(filepath=str(destination))
    return rendered


def _build_scene(recipe: dict):
    import bpy

    if recipe.get("schema_version") != "blender-recipe/v1":
        raise BlenderRecipeError("unsupported Blender recipe")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    all_imported = []
    for instance in recipe.get("instances", []):
        before = set(bpy.data.objects)
        visual = Path(instance["visual"])
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
        root = bpy.data.objects.new(instance["name"], None)
        bpy.context.scene.collection.objects.link(root)
        root.location = instance["translation"]
        root.rotation_euler = instance["rotation_radians"]
        root.scale = (instance["scale"],) * 3
        for obj in imported:
            if obj.parent is None:
                obj.parent = root
        all_imported.extend(imported)
    bpy.context.view_layer.update()
    return all_imported


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
