"""Create and render a small furnished scene to smoke-test Scene Viewer."""

from __future__ import annotations

import argparse
import math

from pathlib import Path

import bpy
from mathutils import Vector


def material(name: str, color: tuple[float, float, float, float]):
    value = bpy.data.materials.new(name)
    value.diffuse_color = color
    return value


def cube(
    name: str,
    location: tuple[float, float, float],
    scale: tuple[float, float, float],
    surface,
):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    obj.data.materials.append(surface)
    return obj


def point_camera(camera, target: tuple[float, float, float]) -> None:
    camera.rotation_euler = (Vector(target) - camera.location).to_track_quat(
        "-Z", "Y"
    ).to_euler()


def build_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.world = bpy.data.worlds.new("SmokeWorld")

    floor_mat = material("Floor", (0.32, 0.36, 0.42, 1.0))
    wood_mat = material("Wood", (0.36, 0.13, 0.045, 1.0))
    chair_mat = material("Chair", (0.06, 0.22, 0.48, 1.0))
    cabinet_mat = material("Cabinet", (0.12, 0.45, 0.24, 1.0))

    cube("Floor", (0, 0, -0.1), (3.5, 3.0, 0.1), floor_mat)
    cube("TableTop", (0, 0, 1.0), (1.25, 0.75, 0.08), wood_mat)
    for x in (-1.05, 1.05):
        for y in (-0.55, 0.55):
            cube(f"TableLeg_{x}_{y}", (x, y, 0.48), (0.08, 0.08, 0.48), wood_mat)

    for index, y in enumerate((-1.35, 1.35), start=1):
        cube(f"ChairSeat_{index}", (0, y, 0.52), (0.52, 0.48, 0.08), chair_mat)
        cube(
            f"ChairBack_{index}",
            (0, y + math.copysign(0.42, y), 0.98),
            (0.52, 0.08, 0.52),
            chair_mat,
        )
        for x in (-0.4, 0.4):
            for offset in (-0.34, 0.34):
                cube(
                    f"ChairLeg_{index}_{x}_{offset}",
                    (x, y + offset, 0.24),
                    (0.06, 0.06, 0.24),
                    chair_mat,
                )

    cube("Cabinet", (2.35, 1.65, 0.8), (0.55, 0.35, 0.8), cabinet_mat)

    bpy.ops.object.light_add(type="AREA", location=(0, 0, 5.0))
    key = bpy.context.object
    key.name = "KeyLight"
    key.data.energy = 1100
    key.data.shape = "DISK"
    key.data.size = 5.0

    bpy.ops.object.light_add(type="AREA", location=(-3.0, -2.0, 2.5))
    fill = bpy.context.object
    fill.name = "FillLight"
    fill.data.energy = 550
    fill.data.size = 3.0
    point_camera(fill, (0, 0, 0.7))

    bpy.ops.object.camera_add()
    camera = bpy.context.object
    camera.name = "PreviewCamera"
    camera.data.lens = 48
    bpy.context.scene.camera = camera


def render(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 640
    scene.render.resolution_y = 480
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.world.color = (0.055, 0.055, 0.055)

    camera = scene.camera
    views = (
        (5.5, -5.5, 4.0),
        (-5.5, -5.5, 3.5),
        (-5.5, 5.5, 4.0),
        (5.5, 5.5, 3.5),
    )
    for index, location in enumerate(views, start=1):
        camera.location = location
        point_camera(camera, (0, 0, 0.75))
        scene.render.filepath = str(output_dir / f"view_{index:02d}.png")
        bpy.ops.render.render(write_still=True)

    bpy.ops.wm.save_as_mainfile(filepath=str(output_dir / "smoke_scene.blend"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("/outputs"))
    args = parser.parse_args()
    build_scene()
    render(args.output_dir)
    print(f"Rendered smoke scene to {args.output_dir}")


if __name__ == "__main__":
    main()
