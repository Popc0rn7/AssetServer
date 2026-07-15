#!/usr/bin/env python3
"""Pixel and world-AABB golden canary for the procedural room worker image."""

from __future__ import annotations

import argparse
import json
import tempfile

from pathlib import Path

import numpy as np
import trimesh

from PIL import Image

from assetserver.blender_scene_worker import _build_scene, render_recipe
from assetserver.procedural_room_shell import ProceduralRoomShellStore
from assetserver.scene_compilers import TRIMESH_GLTF_TO_SCENE_IR
from assetserver.scene_ir import ProceduralRoomShell


def _green_cube(path: Path) -> None:
    mesh = trimesh.creation.box(extents=(0.5, 0.5, 0.5))
    mesh.apply_translation((0.0, 0.0, 0.25))
    mesh.visual = trimesh.visual.TextureVisuals(
        uv=np.zeros((len(mesh.vertices), 2)),
        material=trimesh.visual.material.PBRMaterial(
            name="canary_green",
            baseColorFactor=(0.0, 1.0, 0.0, 1.0),
            emissiveFactor=(0.0, 1.0, 0.0),
            roughnessFactor=0.7,
        ),
    )
    scene = trimesh.Scene()
    scene.add_geometry(mesh, node_name="canary_cube", geom_name="canary_cube")
    path.write_bytes(scene.export(file_type="glb"))


def _instance_world_aabb(
    objects, instance_name: str
) -> tuple[list[float], list[float]]:
    from mathutils import Vector

    points = [
        obj.matrix_world @ Vector(corner)
        for obj in objects
        if obj.get("assetserver_scene_instance") == instance_name
        for corner in obj.bound_box
    ]
    return (
        [min(point[axis] for point in points) for axis in range(3)],
        [max(point[axis] for point in points) for axis in range(3)],
    )


def _world_aabb(objects) -> tuple[list[float], list[float]]:
    from mathutils import Vector

    points = [
        obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box
    ]
    return (
        [min(point[axis] for point in points) for axis in range(3)],
        [max(point[axis] for point in points) for axis in range(3)],
    )


def _run(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    shell = ProceduralRoomShellStore(root / "shells").materialize(
        ProceduralRoomShell(
            kind="procedural",
            dimensions=(3.2, 2.8, 2.7),
            include_ceiling=True,
        )
    )
    cube = root / "canary.glb"
    _green_cube(cube)
    recipe = {
        "schema_version": "blender-recipe/v1",
        "observation_bounds": {
            "min": [-1.6, -1.4, 0.0],
            "max": [1.6, 1.4, 2.7],
        },
        "instances": [
            {
                "name": "room_canary",
                "visual": str(shell.visual_path),
                "translation": [0, 0, 0],
                "rotation_radians": [0, 0, 0],
                "scale": 1,
                "asset_transform": TRIMESH_GLTF_TO_SCENE_IR,
                "procedural_shell": {"cache_key": shell.cache_key},
            },
            {
                "name": "canary",
                "visual": str(cube),
                "translation": [0, 0, 0],
                "rotation_radians": [0, 0, 0],
                "scale": 1,
                "asset_transform": TRIMESH_GLTF_TO_SCENE_IR,
            },
            {
                "name": "boundary_canary",
                "visual": str(cube),
                "translation": [1.25, 1.05, 0],
                "rotation_radians": [0, 0, 0],
                "scale": 1,
                "asset_transform": TRIMESH_GLTF_TO_SCENE_IR,
            },
        ],
    }
    recipe_path = root / "recipe.json"
    recipe_path.write_text(json.dumps(recipe))

    imported = _build_scene(recipe)

    minimum, maximum = _instance_world_aabb(imported, "canary")
    if not np.allclose(minimum, [-0.25, -0.25, 0.0], atol=1e-5):
        scene_minimum, scene_maximum = _world_aabb(imported)
        raise RuntimeError(
            f"incorrect canary world AABB minimum: {minimum}; "
            f"full_scene={scene_minimum}..{scene_maximum}"
        )
    if not np.allclose(maximum, [0.25, 0.25, 0.5], atol=1e-5):
        raise RuntimeError(f"incorrect canary world AABB maximum: {maximum}")
    boundary_minimum, boundary_maximum = _instance_world_aabb(
        imported, "boundary_canary"
    )
    if not np.allclose(boundary_minimum, [1.0, 0.8, 0.0], atol=1e-5):
        raise RuntimeError(
            f"incorrect boundary canary AABB minimum: {boundary_minimum}"
        )
    if not np.allclose(boundary_maximum, [1.5, 1.3, 0.5], atol=1e-5):
        raise RuntimeError(
            f"incorrect boundary canary AABB maximum: {boundary_maximum}"
        )

    rendered = render_recipe(
        recipe_path,
        root / "renders",
        views=["top", "front", "side", "perspective"],
        width=256,
        height=256,
        image_format="png",
    )
    visible = []
    for view in rendered:
        with Image.open(view["path"]) as image:
            pixels = np.asarray(image.convert("RGB"))
        green = (
            (pixels[:, :, 1] > 80)
            & (pixels[:, :, 1] > pixels[:, :, 0] * 1.35)
            & (pixels[:, :, 1] > pixels[:, :, 2] * 1.35)
        )
        if int(green.sum()) >= 16:
            visible.append(view["view"])
    if len(visible) < 3:
        raise RuntimeError(
            f"canary object visible in only {len(visible)}/4 views: {visible}"
        )
    print(f"procedural room canary: PASS; visible_views={','.join(visible)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        help="Keep the recipe, generated geometry, and rendered PNGs here",
    )
    args = parser.parse_args()
    if args.output_dir:
        _run(Path(args.output_dir))
        return
    with tempfile.TemporaryDirectory(prefix="procedural-room-canary-") as temporary:
        _run(Path(temporary))


if __name__ == "__main__":
    main()
