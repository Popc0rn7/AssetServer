"""Pure Scene IR compilers for Blender and Drake worker runtimes."""

from __future__ import annotations

import json
import math

from collections.abc import Callable

import yaml

from assetserver.asset_store import AssetStore
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
    assets: AssetStore,
    *,
    uri_resolver: Callable[[str], str] | None = None,
) -> bytes:
    directives: list[dict] = []
    for room in scene.rooms:
        model_name = f"room_{room.id}"
        uri, base_link = _simulation_spec(room.shell.asset_ref, assets, uri_resolver)
        directives.extend(
            _drake_instance(model_name, uri, base_link, room.transform, "static", {})
        )
    for obj in scene.objects:
        uri, base_link = _simulation_spec(obj.asset_ref, assets, uri_resolver)
        directives.extend(
            _drake_instance(
                obj.id,
                uri,
                base_link,
                obj.transform,
                obj.mobility,
                obj.initial_joints,
            )
        )
    return yaml.dump(
        {"directives": directives}, Dumper=_DrakeDumper, sort_keys=False
    ).encode()


def _simulation_spec(
    asset_ref: str, assets: AssetStore, resolver: Callable[[str], str] | None
) -> tuple[str, str]:
    stored = assets.resolve(asset_ref)
    base_link = str(stored.manifest.get("metadata", {}).get("base_link", "base"))
    uri = (
        resolver(asset_ref)
        if resolver is not None
        else f"file://{assets.entrypoint(asset_ref, 'simulation').resolve()}"
    )
    return uri, base_link


def _drake_instance(
    name: str,
    uri: str,
    base_link: str,
    transform: Transform,
    mobility: str,
    initial_joints: dict[str, float],
) -> list[dict]:
    model = {"name": name, "file": uri}
    if initial_joints:
        model["default_joint_positions"] = {
            joint: [position] for joint, position in initial_joints.items()
        }
    values: list[dict] = [{"add_model": model}]
    pose = {
        "translation": list(transform.translation),
        "rotation": _Rpy(transform.rotation_rpy_degrees),
    }
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


def blender_recipe(scene: SceneIR, assets: AssetStore) -> bytes:
    """Create a transport-neutral recipe consumed by the bpy viewer worker."""
    instances = []
    for room in scene.rooms:
        instances.append(
            _blender_instance(
                f"room_{room.id}", room.shell.asset_ref, room.transform, assets, 1.0
            )
        )
    for obj in scene.objects:
        instances.append(
            _blender_instance(obj.id, obj.asset_ref, obj.transform, assets, obj.scale)
        )
    return (
        json.dumps(
            {"schema_version": "blender-recipe/v1", "instances": instances}, indent=2
        )
        + "\n"
    ).encode()


def _blender_instance(
    name: str, asset_ref: str, transform: Transform, assets: AssetStore, scale: float
) -> dict:
    visual = assets.entrypoint(asset_ref, "visual")
    return {
        "name": name,
        "visual": str(visual.resolve()),
        "translation": list(transform.translation),
        "rotation_radians": [math.radians(v) for v in transform.rotation_rpy_degrees],
        "scale": scale,
    }
