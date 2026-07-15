"""Deterministic server-side materialization of procedural room shells."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assetserver.scene_ir import ProceduralRoomShell


GENERATOR_VERSION = "procedural-room-shell/v1"
DEFAULT_MATERIAL_VERSION = "procedural-room-materials/v1"


@dataclass(frozen=True)
class ShellBox:
    name: str
    role: str
    extents: tuple[float, float, float]
    center: tuple[float, float, float]


@dataclass(frozen=True)
class MaterializedRoomShell:
    cache_key: str
    root: Path
    visual_path: Path
    simulation_path: Path
    manifest: dict[str, Any]


class ProceduralRoomShellStore:
    """Content-addressed geometry cache separate from the asset repository."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def materialize(self, shell: ProceduralRoomShell) -> MaterializedRoomShell:
        normalized = normalized_shell(shell)
        cache_key = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        destination = self.root / cache_key[:2] / cache_key
        manifest_path = destination / "manifest.json"
        if manifest_path.is_file():
            return self._load(cache_key, destination)

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{cache_key}-", dir=destination.parent)
        )
        try:
            geometry_shell = ProceduralRoomShell.model_validate(
                {
                    "kind": "procedural",
                    "dimensions": normalized["dimensions"],
                    "wall_thickness": normalized["wall_thickness"],
                    "floor_thickness": normalized["floor_thickness"],
                    "include_ceiling": normalized["include_ceiling"],
                    "openings": normalized["openings"],
                }
            )
            boxes = shell_boxes(geometry_shell)
            _write_visual_glb(boxes, temporary / "shell.glb")
            _write_simulation_sdf(boxes, temporary / "shell.sdf")
            files = []
            for name in ("shell.glb", "shell.sdf"):
                path = temporary / name
                files.append(
                    {
                        "path": name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size_bytes": path.stat().st_size,
                    }
                )
            manifest = {
                "schema_version": "procedural-room-shell-cache/v1",
                "cache_key": cache_key,
                "generator_version": GENERATOR_VERSION,
                "material_version": DEFAULT_MATERIAL_VERSION,
                "normalized": normalized,
                "visual": "shell.glb",
                "simulation": "shell.sdf",
                "base_link": "shell",
                "boxes": [
                    {
                        "name": box.name,
                        "role": box.role,
                        "extents": list(box.extents),
                        "center": list(box.center),
                    }
                    for box in boxes
                ],
                "files": files,
            }
            manifest_path_tmp = temporary / "manifest.json"
            manifest_path_tmp.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
            try:
                temporary.rename(destination)
            except FileExistsError:
                shutil.rmtree(temporary, ignore_errors=True)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return self._load(cache_key, destination)

    @staticmethod
    def _load(cache_key: str, root: Path) -> MaterializedRoomShell:
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest.get("cache_key") != cache_key:
            raise RuntimeError("procedural room shell cache key mismatch")
        return MaterializedRoomShell(
            cache_key=cache_key,
            root=root,
            visual_path=root / manifest["visual"],
            simulation_path=root / manifest["simulation"],
            manifest=manifest,
        )


def normalized_shell(shell: ProceduralRoomShell) -> dict[str, Any]:
    def rounded(value: float) -> float:
        result = round(float(value), 6)
        return 0.0 if result == 0 else result

    return {
        "dimensions": [rounded(value) for value in shell.dimensions],
        "wall_thickness": rounded(shell.wall_thickness),
        "floor_thickness": rounded(shell.floor_thickness),
        "include_ceiling": shell.include_ceiling,
        "openings": sorted(
            [
                {
                    "id": item.id,
                    "opening_type": item.opening_type,
                    "wall": item.wall,
                    "offset_m": rounded(item.offset_m),
                    "width": rounded(item.width),
                    "height": rounded(item.height),
                    "sill_height": rounded(item.sill_height),
                }
                for item in shell.openings
            ],
            key=lambda item: (item["wall"], item["offset_m"], item["id"]),
        ),
        "generator_version": GENERATOR_VERSION,
        "material_version": DEFAULT_MATERIAL_VERSION,
    }


def shell_boxes(shell: ProceduralRoomShell) -> list[ShellBox]:
    x, y, z = shell.dimensions
    wall_t = shell.wall_thickness
    floor_t = shell.floor_thickness
    boxes = [
        ShellBox(
            "floor",
            "floor",
            (x + 2 * wall_t, y + 2 * wall_t, floor_t),
            (0.0, 0.0, -floor_t / 2),
        )
    ]
    by_wall = {
        wall: sorted(
            (item for item in shell.openings if item.wall == wall),
            key=lambda item: (item.offset_m, item.id),
        )
        for wall in ("north", "south", "east", "west")
    }
    for wall in ("north", "south", "east", "west"):
        length = x if wall in {"north", "south"} else y
        boxes.extend(
            _wall_boxes(
                wall,
                length,
                z,
                wall_t,
                x,
                y,
                by_wall[wall],
            )
        )
    if shell.include_ceiling:
        boxes.append(
            ShellBox(
                "ceiling",
                "ceiling",
                (x + 2 * wall_t, y + 2 * wall_t, floor_t),
                (0.0, 0.0, z + floor_t / 2),
            )
        )
    return boxes


def _wall_boxes(wall, length, height, thickness, x, y, openings) -> list[ShellBox]:
    corner_extension = thickness if wall in {"north", "south"} else 0.0
    if not openings:
        return [
            _wall_box(
                wall,
                -corner_extension,
                length + corner_extension,
                0.0,
                height,
                thickness,
                x,
                y,
                0,
            )
        ]
    intervals = [(item.offset_m, item.offset_m + item.width, item) for item in openings]
    output: list[ShellBox] = []
    cursor = 0.0
    index = 0
    if corner_extension:
        output.append(
            _wall_box(wall, -corner_extension, 0.0, 0.0, height, thickness, x, y, index)
        )
        index += 1
    for start, end, opening in intervals:
        if start > cursor:
            output.append(
                _wall_box(wall, cursor, start, 0.0, height, thickness, x, y, index)
            )
            index += 1
        sill = opening.sill_height
        top = sill + opening.height
        if sill > 0:
            output.append(
                _wall_box(wall, start, end, 0.0, sill, thickness, x, y, index)
            )
            index += 1
        if top < height:
            output.append(
                _wall_box(wall, start, end, top, height, thickness, x, y, index)
            )
            index += 1
        cursor = end
    if cursor < length:
        output.append(
            _wall_box(wall, cursor, length, 0.0, height, thickness, x, y, index)
        )
        index += 1
    if corner_extension:
        output.append(
            _wall_box(
                wall,
                length,
                length + corner_extension,
                0.0,
                height,
                thickness,
                x,
                y,
                index,
            )
        )
    return output


def _wall_box(wall, start, end, bottom, top, thickness, x, y, index) -> ShellBox:
    span = end - start
    vertical = top - bottom
    longitudinal_center = (
        -((x if wall in {"north", "south"} else y) / 2) + (start + end) / 2
    )
    if wall == "north":
        extents = (span, thickness, vertical)
        center = (longitudinal_center, y / 2 + thickness / 2, (bottom + top) / 2)
    elif wall == "south":
        extents = (span, thickness, vertical)
        center = (longitudinal_center, -y / 2 - thickness / 2, (bottom + top) / 2)
    elif wall == "east":
        extents = (thickness, span, vertical)
        center = (x / 2 + thickness / 2, longitudinal_center, (bottom + top) / 2)
    else:
        extents = (thickness, span, vertical)
        center = (-x / 2 - thickness / 2, longitudinal_center, (bottom + top) / 2)
    return ShellBox(f"wall_{wall}_{index:03d}", "wall", extents, center)


def _write_visual_glb(boxes: list[ShellBox], path: Path) -> None:
    import numpy as np
    import trimesh

    colors = {
        "wall": [0.72, 0.72, 0.69, 1.0],
        "floor": [0.42, 0.28, 0.16, 1.0],
        "ceiling": [0.9, 0.9, 0.88, 1.0],
    }
    scene = trimesh.Scene()
    for box in boxes:
        mesh = trimesh.creation.box(extents=box.extents)
        mesh.apply_translation(box.center)
        axes = np.argsort(np.asarray(box.extents))[-2:]
        coordinates = mesh.vertices[:, axes]
        span = np.ptp(coordinates, axis=0)
        span[span == 0] = 1.0
        uv = (coordinates - coordinates.min(axis=0)) / span
        mesh.visual = trimesh.visual.TextureVisuals(
            uv=uv,
            material=trimesh.visual.material.PBRMaterial(
                name=f"{box.role}_{DEFAULT_MATERIAL_VERSION}",
                baseColorFactor=colors[box.role],
                roughnessFactor=0.9,
                metallicFactor=0.0,
            ),
        )
        scene.add_geometry(mesh, node_name=box.name, geom_name=box.name)
    path.write_bytes(scene.export(file_type="glb"))


def _write_simulation_sdf(boxes: list[ShellBox], path: Path) -> None:
    parts = [
        "<sdf version='1.10'>",
        "  <model name='procedural_room_shell'>",
        "    <link name='shell'>",
    ]
    colors = {
        "wall": "0.75 0.75 0.72 1",
        "floor": "0.42 0.28 0.16 1",
        "ceiling": "0.9 0.9 0.88 1",
    }
    for box in boxes:
        size = " ".join(f"{value:.9g}" for value in box.extents)
        pose = " ".join(f"{value:.9g}" for value in (*box.center, 0, 0, 0))
        parts.extend(
            [
                f"      <collision name='{box.name}'>",
                f"        <pose>{pose}</pose>",
                f"        <geometry><box><size>{size}</size></box></geometry>",
                "      </collision>",
                f"      <visual name='{box.name}'>",
                f"        <pose>{pose}</pose>",
                f"        <geometry><box><size>{size}</size></box></geometry>",
                f"        <material><diffuse>{colors[box.role]}</diffuse></material>",
                "      </visual>",
            ]
        )
    parts.extend(["    </link>", "  </model>", "</sdf>"])
    path.write_text("\n".join(parts) + "\n")
