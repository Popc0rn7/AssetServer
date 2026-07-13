"""Concrete Blender, Drake, and export handlers for scene SQLite jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import zipfile

from pathlib import Path
from typing import Any

from assetserver.asset_store import AssetStore
from assetserver.blender_scene_worker import render_recipe
from assetserver.jobs import Job, JobExecutionError
from assetserver.scene_compilers import blender_recipe, compile_drake_directives
from assetserver.scene_ir import SceneIR, load_scene_yaml


def observe(job: Job) -> dict[str, Any]:
    data_root, _ = _roots()
    scene, assets = _load(job, data_root)
    destination = data_root / "scenes" / job.scene_id / "observations" / job.job_id
    temporary = _fresh_temporary(destination)
    try:
        recipe_path = temporary / "recipe.json"
        recipe_path.write_bytes(blender_recipe(scene, assets))
        options = job.request
        views, width, height, image_format = _render_options(options)
        rendered = render_recipe(
            recipe_path,
            temporary,
            views=views,
            width=width,
            height=height,
            image_format=image_format,
        )
        recipe_path.unlink()
        manifest = {
            "scene_id": job.scene_id,
            "scene_revision": job.scene_revision,
            "observation_id": job.job_id,
            "views": [{**item, "path": Path(item["path"]).name} for item in rendered],
        }
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        _publish(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "observation_id": job.job_id,
        "manifest_path": _relative(destination / "manifest.json", data_root),
        "views": [
            {
                **item,
                "path": _relative(destination / Path(item["path"]).name, data_root),
            }
            for item in rendered
        ],
    }


def validate(job: Job) -> dict[str, Any]:
    try:
        from pydrake.all import (
            AddMultibodyPlantSceneGraph,
            DiagramBuilder,
            Parser,
            RigidTransform,
            RollPitchYaw,
        )
    except ImportError as exc:
        raise JobExecutionError(
            "Drake is unavailable", code="drake_unavailable", retryable=True
        ) from exc

    data_root, _ = _roots()
    scene, assets = _load(job, data_root)
    builder = DiagramBuilder()
    plant, scene_graph = AddMultibodyPlantSceneGraph(builder, time_step=0.0)
    parser = Parser(plant)
    parser.SetAutoRenaming(True)
    instances = []
    specs = [
        (f"room_{room.id}", room.shell.asset_ref, room.transform, "static", {})
        for room in scene.rooms
    ] + [
        (obj.id, obj.asset_ref, obj.transform, obj.mobility, obj.initial_joints)
        for obj in scene.objects
    ]
    try:
        for name, asset_ref, transform, mobility, joints in specs:
            path = assets.entrypoint(asset_ref, "simulation")
            loaded = parser.AddModels(str(path))
            if len(loaded) != 1:
                raise JobExecutionError(
                    f"{name} simulation entrypoint must contain exactly one model",
                    code="invalid_simulation_asset",
                )
            model = loaded[0]
            body_indices = plant.GetBodyIndices(model)
            if not body_indices:
                raise JobExecutionError(
                    f"{name} contains no bodies", code="invalid_simulation_asset"
                )
            pose = _drake_pose(transform, RigidTransform, RollPitchYaw)
            body = plant.get_body(body_indices[0])
            if mobility == "static":
                plant.WeldFrames(plant.world_frame(), body.body_frame(), pose)
            for joint_name, position in joints.items():
                joint = plant.GetJointByName(joint_name, model)
                if joint.num_positions() != 1:
                    raise JobExecutionError(
                        f"joint {joint_name} does not have one position",
                        code="invalid_initial_joint",
                    )
                joint.set_default_positions([position])
            instances.append((name, model, body, pose, mobility))
        plant.Finalize()
        diagram = builder.Build()
        context = diagram.CreateDefaultContext()
        plant_context = plant.GetMyContextFromRoot(context)
        for _, _, body, pose, mobility in instances:
            if mobility == "dynamic":
                plant.SetFreeBodyPose(plant_context, body, pose)
        query = scene_graph.get_query_output_port().Eval(
            scene_graph.GetMyContextFromRoot(context)
        )
        penetrations = query.ComputePointPairPenetration()
    except JobExecutionError:
        raise
    except Exception as exc:
        raise JobExecutionError(str(exc), code="drake_validation_failed") from exc
    issues = [
        {
            "type": "penetration",
            "depth": float(item.depth),
            "geometry_a": str(item.id_A),
            "geometry_b": str(item.id_B),
        }
        for item in penetrations
        if float(item.depth) > 1e-6
    ]
    return {"valid": not issues, "issues": issues, "model_count": len(instances)}


def export(job: Job) -> dict[str, Any]:
    data_root, output_root = _roots()
    scene, assets = _load(job, data_root)
    export_root = output_root / job.scene_id / job.job_id
    temporary = _fresh_temporary(export_root)
    package = temporary / "package"
    package.mkdir()
    try:
        (package / "scene.yaml").write_bytes(
            (
                data_root
                / "scenes"
                / job.scene_id
                / "revisions"
                / f"{job.scene_revision:06d}.yaml"
            ).read_bytes()
        )
        for asset_ref in sorted(scene.asset_refs()):
            stored = assets.resolve(asset_ref)
            target = package / "assets" / "sha256" / stored.digest[:2] / stored.digest
            shutil.copytree(stored.root, target)

        compiled = package / "compiled"
        (compiled / "drake").mkdir(parents=True)
        (compiled / "blender").mkdir(parents=True)

        def portable_uri(asset_ref: str) -> str:
            stored = assets.resolve(asset_ref)
            entry = stored.manifest.get("simulation")
            if not entry:
                raise JobExecutionError(
                    f"asset has no simulation entrypoint: {asset_ref}",
                    code="invalid_simulation_asset",
                )
            return f"package://scene/assets/sha256/{stored.digest[:2]}/{stored.digest}/files/{entry}"

        (compiled / "drake" / "scene.dmd.yaml").write_bytes(
            compile_drake_directives(scene, assets, uri_resolver=portable_uri)
        )
        recipe_path = temporary / "recipe.json"
        recipe_path.write_bytes(blender_recipe(scene, assets))
        previews = package / "previews"
        views, width, height, image_format = _render_options(job.request)
        render_recipe(
            recipe_path,
            previews,
            views=views,
            width=width,
            height=height,
            image_format=image_format,
            blend_path=compiled / "blender" / "scene.blend",
        )
        recipe_path.unlink()
        manifest = {
            "schema_version": "scene-export/v1",
            "scene_id": job.scene_id,
            "scene_revision": job.scene_revision,
            "assets": sorted(scene.asset_refs()),
        }
        (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        checksum_lines = []
        for path in sorted(package.rglob("*")):
            if path.is_file():
                checksum_lines.append(
                    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(package).as_posix()}"
                )
        (package / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n")
        archive = temporary / f"{job.scene_id}-r{job.scene_revision}.zip"
        _write_zip(package, archive)
        _publish(temporary, export_root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    archive = export_root / f"{job.scene_id}-r{job.scene_revision}.zip"
    return {
        "export_id": job.job_id,
        "package_path": _relative(export_root / "package", output_root),
        "zip_path": _relative(archive, output_root),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "size_bytes": archive.stat().st_size,
    }


def _load(job: Job, data_root: Path) -> tuple[SceneIR, AssetStore]:
    scene_path = (
        data_root
        / "scenes"
        / job.scene_id
        / "revisions"
        / f"{job.scene_revision:06d}.yaml"
    )
    if not scene_path.is_file():
        raise JobExecutionError("scene revision not found", code="scene_not_found")
    return load_scene_yaml(scene_path.read_bytes()), AssetStore(data_root / "assets")


def _roots() -> tuple[Path, Path]:
    return Path(os.environ.get("ASSETSERVER_DATA_ROOT", "/data")), Path(
        os.environ.get("ASSETSERVER_OUTPUT_ROOT", "/outputs")
    )


def _integer_option(
    options: dict, name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(options.get(name, default))
    except (TypeError, ValueError) as exc:
        raise JobExecutionError(
            f"{name} must be an integer", code="invalid_render_options"
        ) from exc
    if not minimum <= value <= maximum:
        raise JobExecutionError(
            f"{name} must be between {minimum} and {maximum}",
            code="invalid_render_options",
        )
    return value


def _render_options(options: dict) -> tuple[list[str], int, int, str]:
    views = options.get("views") or ["top", "front", "side", "perspective"]
    if (
        not isinstance(views, list)
        or not views
        or not all(isinstance(view, str) for view in views)
    ):
        raise JobExecutionError(
            "views must be a non-empty string list", code="invalid_render_options"
        )
    width = _integer_option(options, "width", 512, 1, 4096)
    height = _integer_option(options, "height", 512, 1, 4096)
    image_format = options.get("format", "webp")
    if image_format not in {"webp", "png"}:
        raise JobExecutionError(
            "format must be webp or png", code="invalid_render_options"
        )
    return views, width, height, image_format


def _drake_pose(transform, rigid_transform, roll_pitch_yaw):
    angles = [math.radians(value) for value in transform.rotation_rpy_degrees]
    return rigid_transform(roll_pitch_yaw(*angles), transform.translation)


def _fresh_temporary(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )


def _publish(temporary: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    temporary.replace(destination)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _write_zip(package: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                output.write(path, Path("package") / path.relative_to(package))
