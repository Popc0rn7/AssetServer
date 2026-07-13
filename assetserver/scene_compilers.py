"""Pure Scene IR compilers for Blender and Drake worker runtimes."""

from __future__ import annotations

import json
import math

from collections.abc import Callable

import yaml

from assetserver.asset_store import ContentAddressedAssetStore, IDENTITY_MATRIX
from assetserver.scene_ir import SceneIR, Transform


class SceneCompileError(ValueError):
    pass


class _Rpy(tuple):
    pass


class _DrakeDumper(yaml.SafeDumper):
    pass


_DrakeDumper.add_representer(
    _Rpy,
    lambda dumper, value: dumper.represent_mapping("!Rpy", {"deg": list(value)}),
)


def compile_drake_directives(
    scene: SceneIR,
    assets: ContentAddressedAssetStore,
    *,
    uri_resolver: Callable[[str], str] | None = None,
) -> bytes:
    directives: list[dict] = []
    for room in scene.rooms:
        model_name = f"room_{room.id}"
        uri, base_link, asset_transform = _simulation_spec(
            room.shell.asset_ref, assets, uri_resolver
        )
        directives.extend(
            _drake_instance(
                model_name,
                uri,
                base_link,
                room.transform,
                "static",
                {},
                asset_transform,
            )
        )
    for obj in scene.objects:
        uri, base_link, asset_transform = _simulation_spec(
            obj.asset_ref, assets, uri_resolver
        )
        _validate_initial_joints(obj.id, obj.asset_ref, obj.initial_joints, assets)
        directives.extend(
            _drake_instance(
                obj.id,
                uri,
                base_link,
                obj.transform,
                obj.mobility,
                obj.initial_joints,
                asset_transform,
            )
        )
    return yaml.dump(
        {"directives": directives}, Dumper=_DrakeDumper, sort_keys=False
    ).encode()


def _simulation_spec(
    asset_ref: str,
    assets: ContentAddressedAssetStore,
    resolver: Callable[[str], str] | None,
) -> tuple[str, str, list[list[float]]]:
    stored = assets.resolve(asset_ref)
    simulation = stored.manifest.get("simulation") or {}
    base_link = simulation.get("base_link")
    if not isinstance(base_link, str) or not base_link:
        raise SceneCompileError(f"asset has no declared simulation base link: {asset_ref}")
    uri = (
        resolver(asset_ref)
        if resolver is not None
        else f"file://{assets.entrypoint(asset_ref, 'simulation').resolve()}"
    )
    return uri, base_link, simulation.get("transform_to_asset", IDENTITY_MATRIX)


def _drake_instance(
    name: str,
    uri: str,
    base_link: str,
    transform: Transform,
    mobility: str,
    initial_joints: dict[str, float],
    asset_transform: list[list[float]],
) -> list[dict]:
    model = {"name": name, "file": uri}
    if initial_joints:
        model["default_joint_positions"] = {
            joint: [position] for joint, position in initial_joints.items()
        }
    values: list[dict] = [{"add_model": model}]
    translation, rotation = _composed_pose(transform, asset_transform)
    pose = {"translation": translation, "rotation": _Rpy(rotation)}
    if mobility == "static":
        values.append(
            {
                "add_weld": {
                    "parent": "world",
                    "child": f"{name}::{base_link}",
                    "X_PC": pose,
                }
            }
        )
    else:
        values[0]["add_model"]["default_free_body_pose"] = {
            base_link: {"base_frame": "world", **pose}
        }
    return values


def blender_recipe(scene: SceneIR, assets: ContentAddressedAssetStore) -> bytes:
    """Create a transport-neutral recipe consumed by the bpy viewer worker."""
    instances = []
    for room in scene.rooms:
        instances.append(
            _blender_instance(
                f"room_{room.id}",
                room.shell.asset_ref,
                room.transform,
                assets,
                1.0,
                {},
            )
        )
    for obj in scene.objects:
        instances.append(
            _blender_instance(
                obj.id,
                obj.asset_ref,
                obj.transform,
                assets,
                obj.scale,
                obj.initial_joints,
            )
        )
    return (
        json.dumps(
            {"schema_version": "blender-recipe/v1", "instances": instances}, indent=2
        )
        + "\n"
    ).encode()


def _blender_instance(
    name: str,
    asset_ref: str,
    transform: Transform,
    assets: ContentAddressedAssetStore,
    scale: float,
    initial_joints: dict[str, float],
) -> dict:
    stored = assets.resolve(asset_ref)
    visual = assets.entrypoint(asset_ref, "visual")
    visual_spec = stored.manifest["visual"]
    _validate_initial_joints(name, asset_ref, initial_joints, assets)
    parts = []
    for part in visual_spec.get("parts") or []:
        parts.append(
            {
                "link": part["link"],
                "visual": str(
                    assets.file_path(stored.root, part["entrypoint"]).resolve()
                ),
            }
        )
    instance = {
        "name": name,
        "visual": str(visual.resolve()),
        "translation": list(transform.translation),
        "rotation_radians": [math.radians(v) for v in transform.rotation_rpy_degrees],
        "scale": scale,
        "asset_transform": visual_spec.get("transform_to_asset", IDENTITY_MATRIX),
        "initial_joints": initial_joints,
        "bounds": stored.manifest.get("bounds"),
        "materials": visual_spec.get("materials", []),
    }
    if parts:
        simulation = stored.manifest.get("simulation") or {}
        instance["visual_parts"] = parts
        instance["simulation"] = str(
            assets.entrypoint(asset_ref, "simulation").resolve()
        )
        instance["base_link"] = simulation["base_link"]
    return instance


def _validate_initial_joints(
    object_id: str,
    asset_ref: str,
    positions: dict[str, float],
    assets: ContentAddressedAssetStore,
) -> None:
    if not positions:
        return
    declared = {
        item.get("name"): item for item in assets.resolve(asset_ref).manifest.get("joints", [])
    }
    for name, position in positions.items():
        field = f"objects[{object_id}].initial_joints.{name}"
        if name not in declared:
            raise SceneCompileError(
                f"{field}: joint is not declared by asset {asset_ref}"
            )
        limits = declared[name].get("limits")
        if limits and not limits["lower"] <= position <= limits["upper"]:
            raise SceneCompileError(
                f"{field}: {position} is outside [{limits['lower']}, {limits['upper']}]"
            )


def _composed_pose(
    transform: Transform, asset_transform: list[list[float]]
) -> tuple[list[float], list[float]]:
    scene_matrix = _pose_matrix(transform)
    matrix = _matmul(scene_matrix, asset_transform)
    translation = [matrix[index][3] for index in range(3)]
    # XYZ fixed-axis RPY extraction, matching Drake's RollPitchYaw convention.
    pitch = math.asin(max(-1.0, min(1.0, -matrix[2][0])))
    if abs(math.cos(pitch)) > 1e-8:
        roll = math.atan2(matrix[2][1], matrix[2][2])
        yaw = math.atan2(matrix[1][0], matrix[0][0])
    else:
        roll = math.atan2(-matrix[1][2], matrix[1][1])
        yaw = 0.0
    return translation, [math.degrees(value) for value in (roll, pitch, yaw)]


def _pose_matrix(transform: Transform) -> list[list[float]]:
    roll, pitch, yaw = [math.radians(v) for v in transform.rotation_rpy_degrees]
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    matrix = [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, transform.translation[0]],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, transform.translation[1]],
        [-sp, cp * sr, cp * cr, transform.translation[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]
    return matrix


def _matmul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [
        [sum(a[row][k] * b[k][column] for k in range(4)) for column in range(4)]
        for row in range(4)
    ]
