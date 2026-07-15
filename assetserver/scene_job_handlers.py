"""Concrete Blender, Drake, and export handlers for scene SQLite jobs."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
import zipfile

from pathlib import Path
from typing import Any

from assetserver.asset_store import ContentAddressedAssetStore
from assetserver.blender_scene_worker import BlenderRecipeError, render_recipe
from assetserver.jobs import Job, JobExecutionError
from assetserver.procedural_room_shell import ProceduralRoomShellStore
from assetserver.scene_compilers import (
    SceneCompileError,
    blender_recipe,
    compile_drake_directives,
)
from assetserver.scene_ir import (
    AssetRoomShell,
    ProceduralRoomShell,
    SceneIR,
    load_scene_yaml,
    scene_sha256,
)
from assetserver.simulation_assets import (
    SimulationAssetError,
    simulation_asset_payload,
)


RENDERER_VERSION = "scene-ir-eevee/v2"
EXPORT_VERSION = "scene-export/v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def observe(job: Job) -> dict[str, Any]:
    data_root, _ = _roots()
    scene, assets = _load(job, data_root)
    # The worker explicitly resolves server-owned procedural geometry before
    # compiling a renderer recipe. In deployed workers this cache is the shared
    # /data materialization boundary populated during Scene IR publication.
    procedural_shells = _procedural_provenance(scene, assets)
    destination = data_root / "scenes" / job.scene_id / "observations" / job.job_id
    temporary = _fresh_temporary(destination)
    try:
        recipe_path = temporary / "recipe.json"
        try:
            recipe_path.write_bytes(blender_recipe(scene, assets))
        except SceneCompileError as exc:
            raise JobExecutionError(
                str(exc), code="invalid_initial_joint", retryable=False
            ) from exc
        options = job.request
        views, width, height, image_format = _render_options(options)
        asset_digests = sorted(assets.resolve(ref).digest for ref in scene.asset_refs())
        visual_scene = _blender_cache_input(scene, assets)
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "visual_scene": visual_scene,
                    "renderer_version": RENDERER_VERSION,
                    "options": {
                        "views": views,
                        "width": width,
                        "height": height,
                        "format": image_format,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        cache = data_root / "observations" / "cache" / cache_key
        if (cache / "rendered.json").is_file():
            rendered = json.loads((cache / "rendered.json").read_text())
            for item in rendered:
                source = cache / Path(item["path"]).name
                target = temporary / source.name
                shutil.copy2(source, target)
                item["path"] = str(target)
        else:
            try:
                rendered = render_recipe(
                    recipe_path,
                    temporary,
                    views=views,
                    width=width,
                    height=height,
                    image_format=image_format,
                )
            except BlenderRecipeError as exc:
                code = (
                    "render_device_unavailable"
                    if "render device" in str(exc).lower()
                    else "blender_recipe_error"
                )
                raise JobExecutionError(str(exc), code=code, retryable=False) from exc
            cache_tmp = _fresh_temporary(cache)
            cached = []
            for item in rendered:
                source = Path(item["path"])
                shutil.copy2(source, cache_tmp / source.name)
                cached.append({**item, "path": source.name})
            (cache_tmp / "rendered.json").write_text(
                json.dumps(cached, sort_keys=True) + "\n"
            )
            _publish(cache_tmp, cache)
        recipe_path.unlink()
        public_views = []
        for item in rendered:
            image_path = Path(item["path"])
            public_views.append(
                {
                    **item,
                    "path": image_path.name,
                    "media_type": (
                        "image/webp" if image_format == "webp" else "image/png"
                    ),
                    "sha256": _sha256(image_path),
                    "size_bytes": image_path.stat().st_size,
                    "width": width,
                    "height": height,
                }
            )
        provenance = {
            "job_id": job.job_id,
            "scene_id": job.scene_id,
            "scene_revision": job.scene_revision,
            "scene_sha256": scene_sha256(scene),
            "producer_version": RENDERER_VERSION,
            "render_options": {
                "views": views,
                "width": width,
                "height": height,
                "format": image_format,
            },
            "procedural_shells": procedural_shells,
        }
        manifest = {
            "schema_version": "observation/v2",
            "scene_id": job.scene_id,
            "scene_revision": job.scene_revision,
            "scene_sha256": scene_sha256(scene),
            "observation_id": job.job_id,
            "cache_key": cache_key,
            "asset_digests": asset_digests,
            "renderer_version": RENDERER_VERSION,
            "blender_version": _blender_version(),
            "render_device": next(
                (item.get("device") for item in rendered if item.get("device")),
                "unknown",
            ),
            "options": {
                "views": views,
                "width": width,
                "height": height,
                "format": image_format,
            },
            "provenance": provenance,
            "views": public_views,
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


def _blender_cache_input(
    scene: SceneIR, assets: ContentAddressedAssetStore
) -> dict[str, Any]:
    """Describe only inputs Blender consumes, excluding collision derivations."""
    value = scene.model_dump(mode="json", exclude_none=True)
    fingerprints: dict[str, str] = {}
    for ref in scene.asset_refs():
        stored = assets.resolve(ref)
        visual = stored.manifest["visual"]
        names = [visual["entrypoint"]] + [
            part["entrypoint"] for part in visual.get("parts") or []
        ]
        records = {item["path"]: item["sha256"] for item in stored.manifest["files"]}
        payload = {
            "visual": visual,
            "files": {name: records[name] for name in sorted(names)},
            "bounds": stored.manifest.get("bounds"),
        }
        fingerprints[ref] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    for room in value["rooms"]:
        if "asset_ref" in room["shell"]:
            room["shell"]["asset_ref"] = fingerprints[room["shell"]["asset_ref"]]
    for item in value["objects"]:
        item["asset_ref"] = fingerprints[item["asset_ref"]]
    return value


def _validate_result(job: Job) -> dict[str, Any]:
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
    issues: list[dict[str, Any]] = []
    shell_store = _procedural_store(assets)
    specs = []
    for room in scene.rooms:
        if isinstance(room.shell, AssetRoomShell):
            specs.append(
                (
                    f"room_{room.id}",
                    room.id,
                    room.shell.asset_ref,
                    None,
                    room.transform,
                    "static",
                    {},
                )
            )
        else:
            generated = shell_store.materialize(room.shell)
            specs.append(
                (
                    f"room_{room.id}",
                    room.id,
                    None,
                    generated.simulation_path,
                    room.transform,
                    "static",
                    {},
                )
            )
    specs.extend(
        (
            obj.id,
            obj.id,
            obj.asset_ref,
            None,
            obj.transform,
            obj.mobility,
            obj.initial_joints,
        )
        for obj in scene.objects
    )
    try:
        for (
            name,
            object_id,
            asset_ref,
            procedural_path,
            transform,
            mobility,
            joints,
        ) in specs:
            if asset_ref is not None:
                stored = assets.resolve(asset_ref)
                simulation = stored.manifest.get("simulation") or {}
                path = assets.entrypoint(asset_ref, "simulation")
                base_link = simulation.get("base_link")
                asset_transform = simulation.get("transform_to_asset")
            else:
                stored = None
                simulation = {}
                path = procedural_path
                base_link = "shell"
                asset_transform = None
            try:
                loaded = parser.AddModels(str(path))
            except Exception as exc:
                issue_type = (
                    "unsupported_collision"
                    if "collision" in str(exc).lower()
                    else "model_load"
                )
                issues.append(_issue(issue_type, "error", [object_id], str(exc), False))
                return {"valid": False, "issues": issues, "model_count": len(instances)}
            if len(loaded) != 1:
                issues.append(
                    _issue(
                        "model_load",
                        "error",
                        [object_id],
                        "simulation entrypoint must contain exactly one model",
                        False,
                    )
                )
                return {"valid": False, "issues": issues, "model_count": len(instances)}
            model = loaded[0]
            try:
                body = plant.GetBodyByName(base_link, model)
            except Exception:
                issues.append(
                    _issue(
                        "missing_base_link",
                        "error",
                        [object_id],
                        f"declared base link does not exist: {base_link}",
                        False,
                    )
                )
                return {"valid": False, "issues": issues, "model_count": len(instances)}
            pose = _drake_pose(transform, RigidTransform, RollPitchYaw).multiply(
                RigidTransform(asset_transform) if asset_transform else RigidTransform()
            )
            if mobility == "static":
                plant.WeldFrames(plant.world_frame(), body.body_frame(), pose)
            for joint_name, position in joints.items():
                assert stored is not None
                declared = next(
                    (
                        item
                        for item in stored.manifest.get("joints", [])
                        if item.get("name") == joint_name
                    ),
                    None,
                )
                if declared is None:
                    issues.append(
                        _issue(
                            "unknown_joint",
                            "error",
                            [object_id],
                            f"objects[{name}].initial_joints.{joint_name}: unknown joint",
                            False,
                            metric=position,
                        )
                    )
                    continue
                limits = declared.get("limits")
                if limits and not limits["lower"] <= position <= limits["upper"]:
                    issues.append(
                        _issue(
                            "limited_joint",
                            "error",
                            [object_id],
                            f"objects[{name}].initial_joints.{joint_name}: outside declared limits",
                            False,
                            metric=position,
                        )
                    )
                    continue
                joint = plant.GetJointByName(joint_name, model)
                if joint.num_positions() != 1:
                    issues.append(
                        _issue(
                            "unsupported_joint",
                            "error",
                            [object_id],
                            f"joint {joint_name} does not have one position",
                            False,
                        )
                    )
                    continue
                joint.set_default_positions([position])
            instances.append((name, object_id, model, body, pose, mobility))
        if issues:
            return {"valid": False, "issues": issues, "model_count": len(instances)}
        plant.Finalize()
        diagram = builder.Build()
        context = diagram.CreateDefaultContext()
        plant_context = plant.GetMyContextFromRoot(context)
        for name, _, _, body, pose, mobility in instances:
            if mobility == "dynamic":
                try:
                    plant.SetFreeBodyPose(plant_context, body, pose)
                except Exception as exc:
                    issues.append(
                        _issue(
                            "free_body_initialization", "error", [name], str(exc), False
                        )
                    )
        query = scene_graph.get_query_output_port().Eval(
            scene_graph.GetMyContextFromRoot(context)
        )
        penetrations = query.ComputePointPairPenetration()
    except JobExecutionError:
        raise
    except Exception as exc:
        raise JobExecutionError(str(exc), code="drake_validation_failed") from exc
    inspector = query.inspector()
    model_objects = {
        model: (object_id, mobility)
        for _, object_id, model, _, _, mobility in instances
    }
    epsilon = float(job.request.get("penetration_epsilon", 1e-6))
    include_static_static = bool(job.request.get("static_static", True))
    support_tolerance = float(
        job.request.get("support_contact_tolerance", max(epsilon, 1e-4))
    )
    placements = {
        obj.id: obj.placement.parent_object_id for obj in scene.objects if obj.placement
    }
    penetration_contacts: list[tuple[str, str, float]] = []
    for item in penetrations:
        depth = float(item.depth)
        if depth <= epsilon:
            continue
        try:
            body_a = plant.GetBodyFromFrameId(inspector.GetFrameId(item.id_A))
            body_b = plant.GetBodyFromFrameId(inspector.GetFrameId(item.id_B))
            owner_a = model_objects.get(body_a.model_instance())
            owner_b = model_objects.get(body_b.model_instance())
        except Exception:
            owner_a = owner_b = None
        if owner_a is None or owner_b is None:
            logging.getLogger(__name__).warning("unmapped Drake penetration geometry")
            continue
        name_a, mobility_a = owner_a
        name_b, mobility_b = owner_b
        if name_a == name_b:
            # Link-level articulated checks require their own semantics and issue
            # code. A normal object-pair penetration must never be a self-pair.
            continue
        if not include_static_static and mobility_a == mobility_b == "static":
            continue
        if (
            placements.get(name_a) == name_b or placements.get(name_b) == name_a
        ) and depth <= support_tolerance:
            continue
        if (
            name_a in {room.id for room in scene.rooms}
            or name_b in {room.id for room in scene.rooms}
        ) and depth <= support_tolerance:
            continue
        penetration_contacts.append((name_a, name_b, depth))
    issues.extend(_aggregate_penetration_contacts(penetration_contacts))
    issues.sort(
        key=lambda issue: (
            issue["code"],
            tuple(issue.get("object_ids") or []),
            issue["message"],
        )
    )
    return {"valid": not issues, "issues": issues, "model_count": len(instances)}


def _aggregate_penetration_contacts(
    contacts: list[tuple[str, str, float]],
) -> list[dict[str, Any]]:
    """Collapse narrow-phase contacts to one stable issue per object pair."""
    aggregates: dict[tuple[str, str], tuple[float, int]] = {}
    for object_a, object_b, depth in contacts:
        if object_a == object_b:
            continue
        pair = tuple(sorted((object_a, object_b)))
        maximum, count = aggregates.get(pair, (0.0, 0))
        aggregates[pair] = (max(maximum, float(depth)), count + 1)
    output = []
    for pair in sorted(aggregates):
        maximum, count = aggregates[pair]
        output.append(
            _issue(
                "penetration",
                "error",
                list(pair),
                f"objects penetrate by {maximum:.6g} m",
                False,
                depth=maximum,
                metric=maximum,
                metadata={"contact_count": count},
            )
        )
    return output


def validate(job: Job) -> dict[str, Any]:
    """Run physical validation and publish its canonical JSON report bytes."""
    result = _validate_result(job)
    data_root, _ = _roots()
    scene, assets = _load(job, data_root)
    provenance = {
        "job_id": job.job_id,
        "scene_id": job.scene_id,
        "scene_revision": job.scene_revision,
        "scene_sha256": scene_sha256(scene),
        "producer_version": "validation-report/v1",
        "procedural_shells": _procedural_provenance(scene, assets),
    }
    report = {
        "schema_version": "validation-report/v1",
        "job_id": job.job_id,
        "scene_id": job.scene_id,
        "scene_revision": job.scene_revision,
        "scene_sha256": provenance["scene_sha256"],
        "provenance": provenance,
        **result,
    }
    destination = data_root / "scenes" / job.scene_id / "validations" / job.job_id
    destination.mkdir(parents=True, exist_ok=True)
    report_path = destination / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return {
        **result,
        "scene_revision": job.scene_revision,
        "scene_sha256": report["scene_sha256"],
        "report_path": _relative(report_path, data_root),
    }


def _issue(
    issue_type: str,
    severity: str,
    object_ids: list[str],
    message: str,
    retryable: bool,
    *,
    depth: float | None = None,
    metric: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue = {
        "code": issue_type,
        "type": issue_type,
        "severity": severity,
        "object_ids": object_ids,
        "depth": depth,
        "metric": metric,
        "message": message,
        "retryable": retryable,
    }
    if metadata is not None:
        issue["metadata"] = metadata
    return issue


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
        simulation_payload = _simulation_scene_payload(scene, assets)
        for asset_ref in sorted(scene.asset_refs()):
            stored = assets.resolve(asset_ref)
            target = package / "assets" / "sha256" / stored.digest[:2] / stored.digest
            shutil.copytree(stored.root, target)
        shell_store = _procedural_store(assets)
        procedural_shells = {}
        for room in scene.rooms:
            if isinstance(room.shell, ProceduralRoomShell):
                generated = shell_store.materialize(room.shell)
                target = package / "procedural_shells" / generated.cache_key
                if not target.exists():
                    shutil.copytree(generated.root, target)
                procedural_shells[room.id] = {
                    "cache_key": generated.cache_key,
                    "generator_version": generated.manifest["generator_version"],
                    "material_version": generated.manifest["material_version"],
                    "visual": f"procedural_shells/{generated.cache_key}/shell.glb",
                    "simulation": f"procedural_shells/{generated.cache_key}/shell.sdf",
                }

        compiled = package / "compiled"
        (compiled / "drake").mkdir(parents=True)
        (compiled / "blender").mkdir(parents=True)
        (compiled / "simulation").mkdir(parents=True)
        (compiled / "simulation" / "scene.json").write_text(
            json.dumps(simulation_payload, indent=2, sort_keys=True) + "\n"
        )

        def portable_uri(asset_ref: str) -> str:
            stored = assets.resolve(asset_ref)
            entry = stored.manifest.get("simulation")
            if not entry:
                raise JobExecutionError(
                    f"asset has no simulation entrypoint: {asset_ref}",
                    code="invalid_simulation_asset",
                )
            return f"package://scene/assets/sha256/{stored.digest[:2]}/{stored.digest}/files/{entry}"

        def procedural_portable_uri(room_id: str, cache_key: str) -> str:
            return f"package://scene/procedural_shells/{cache_key}/shell.sdf"

        (compiled / "drake" / "scene.dmd.yaml").write_bytes(
            compile_drake_directives(
                scene,
                assets,
                uri_resolver=portable_uri,
                procedural_uri_resolver=procedural_portable_uri,
            )
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
            "schema_version": EXPORT_VERSION,
            "scene_id": job.scene_id,
            "scene_revision": job.scene_revision,
            "scene_sha256": scene_sha256(scene),
            "asset_digests": sorted(
                assets.resolve(ref).digest for ref in scene.asset_refs()
            ),
            "assets": sorted(scene.asset_refs()),
            "procedural_shells": procedural_shells,
            "simulation_manifest": "compiled/simulation/scene.json",
            "versions": {
                "exporter": EXPORT_VERSION,
                "renderer": RENDERER_VERSION,
                "blender": _blender_version(),
                "drake": _drake_version(),
            },
            "asset_tool_versions": {
                assets.resolve(ref).digest: assets.resolve(ref).manifest.get(
                    "tool_versions", {}
                )
                for ref in sorted(scene.asset_refs())
            },
            "parameters": {
                "views": views,
                "width": width,
                "height": height,
                "format": image_format,
                "procedural_room_generator": "procedural-room-shell/v1",
                "procedural_room_materials": "procedural-room-materials/v1",
            },
        }
        (package / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        checksum_lines = []
        for path in sorted(package.rglob("*")):
            if path.is_file():
                checksum_lines.append(
                    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(package).as_posix()}"
                )
        (package / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n")
        _verify_package_checksums(package)
        _scan_for_absolute_paths(package)
        _validate_blend(package / "compiled" / "blender" / "scene.blend")
        _validate_drake_package(package)
        archive = temporary / f"{job.scene_id}-r{job.scene_revision}.zip"
        _write_zip(package, archive)
        _publish(temporary, export_root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    archive = export_root / f"{job.scene_id}-r{job.scene_revision}.zip"
    return {
        "export_id": job.job_id,
        "scene_sha256": scene_sha256(scene),
        "package_path": _relative(export_root / "package", output_root),
        "zip_path": _relative(archive, output_root),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "size_bytes": archive.stat().st_size,
    }


def _simulation_scene_payload(
    scene: SceneIR, assets: ContentAddressedAssetStore
) -> dict[str, Any]:
    payloads: dict[str, dict[str, Any]] = {}
    for ref in sorted(scene.asset_refs()):
        stored = assets.resolve(ref)
        try:
            payloads[stored.digest] = simulation_asset_payload(stored)
        except SimulationAssetError as exc:
            raise JobExecutionError(
                f"invalid simulation collision asset {ref}: {exc}",
                code="invalid_collision_asset",
                retryable=False,
            ) from exc
    procedural_payloads: dict[str, dict[str, Any]] = {}
    instances = []
    shell_store = _procedural_store(assets)
    for room in scene.rooms:
        if isinstance(room.shell, AssetRoomShell):
            stored = assets.resolve(room.shell.asset_ref)
            instances.append(
                {
                    "name": f"room_{room.id}",
                    "asset_digest": stored.digest,
                    "mobility": "static",
                    "scale": 1.0,
                    "transform": room.transform.model_dump(mode="json"),
                }
            )
        else:
            generated = shell_store.materialize(room.shell)
            procedural_payloads[generated.cache_key] = {
                "kind": "procedural_room_shell",
                "cache_key": generated.cache_key,
                "generator_version": generated.manifest["generator_version"],
                "material_version": generated.manifest["material_version"],
                "visual": f"procedural_shells/{generated.cache_key}/shell.glb",
                "simulation": f"procedural_shells/{generated.cache_key}/shell.sdf",
                "collision_geometries": [
                    {
                        "link": "shell",
                        "name": box["name"],
                        "representation": "primitive",
                        "shape": "box",
                        "parameters": {"size": box["extents"]},
                        "pose": box["center"] + [0.0, 0.0, 0.0],
                    }
                    for box in generated.manifest["boxes"]
                ],
            }
            instances.append(
                {
                    "name": f"room_{room.id}",
                    "procedural_shell_key": generated.cache_key,
                    "mobility": "static",
                    "scale": 1.0,
                    "transform": room.transform.model_dump(mode="json"),
                }
            )
    for item in scene.objects:
        stored = assets.resolve(item.asset_ref)
        instances.append(
            {
                "name": item.id,
                "asset_digest": stored.digest,
                "mobility": item.mobility,
                "scale": item.scale,
                "transform": item.transform.model_dump(mode="json"),
                "initial_joints": item.initial_joints,
            }
        )
    return {
        "schema_version": "simulation-scene/v1",
        "canonical_frame": {
            "units": "m",
            "handedness": "right",
            "up_axis": "+Z",
        },
        "assets": payloads,
        "procedural_shells": procedural_payloads,
        "instances": instances,
    }


def _load(job: Job, data_root: Path) -> tuple[SceneIR, ContentAddressedAssetStore]:
    scene_path = (
        data_root
        / "scenes"
        / job.scene_id
        / "revisions"
        / f"{job.scene_revision:06d}.yaml"
    )
    if not scene_path.is_file():
        raise JobExecutionError("scene revision not found", code="scene_not_found")
    return load_scene_yaml(scene_path.read_bytes()), ContentAddressedAssetStore(
        data_root / "assets"
    )


def _roots() -> tuple[Path, Path]:
    return Path(os.environ.get("ASSETSERVER_DATA_ROOT", "/data")), Path(
        os.environ.get("ASSETSERVER_OUTPUT_ROOT", "/outputs")
    )


def _procedural_store(
    assets: ContentAddressedAssetStore,
) -> ProceduralRoomShellStore:
    return ProceduralRoomShellStore(assets.root.parent / "procedural_room_shells")


def _procedural_provenance(
    scene: SceneIR, assets: ContentAddressedAssetStore
) -> dict[str, dict[str, str]]:
    store = _procedural_store(assets)
    output = {}
    for room in scene.rooms:
        if isinstance(room.shell, ProceduralRoomShell):
            generated = store.materialize(room.shell)
            output[room.id] = {
                "cache_key": generated.cache_key,
                "generator_version": generated.manifest["generator_version"],
                "material_version": generated.manifest["material_version"],
            }
    return output


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


def _blender_version() -> str:
    try:
        import bpy

        return str(bpy.app.version_string)
    except ImportError:
        return "unavailable"


def _drake_version() -> str:
    try:
        import pydrake

        return str(getattr(pydrake, "__version__", "installed"))
    except ImportError:
        return "unavailable"


def _verify_package_checksums(package: Path) -> None:
    for line in (package / "checksums.sha256").read_text().splitlines():
        digest, relative = line.split("  ", 1)
        path = package / relative
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise JobExecutionError(
                f"export checksum verification failed: {relative}",
                code="export_verification_failed",
                retryable=False,
            )


def _scan_for_absolute_paths(package: Path) -> None:
    forbidden = (b"file://", b"/home/", b"/app/", b"/data/", b"/outputs/")
    for path in sorted(item for item in package.rglob("*") if item.is_file()):
        content = path.read_bytes()
        if any(value in content for value in forbidden):
            raise JobExecutionError(
                f"export contains an absolute path: {path.relative_to(package)}",
                code="export_path_leak",
                retryable=False,
            )


def _validate_blend(path: Path) -> None:
    try:
        import bpy
    except ImportError:
        return
    try:
        bpy.ops.wm.open_mainfile(filepath=str(path))
    except Exception as exc:
        raise JobExecutionError(
            f"Blender reopen validation failed: {exc}",
            code="export_blender_validation_failed",
            retryable=False,
        ) from exc


def _validate_drake_package(package: Path) -> None:
    try:
        from pydrake.all import (
            AddMultibodyPlantSceneGraph,
            DiagramBuilder,
            LoadModelDirectives,
            Parser,
            ProcessModelDirectives,
        )
    except ImportError:
        return
    try:
        builder = DiagramBuilder()
        plant, _ = AddMultibodyPlantSceneGraph(builder, time_step=0.0)
        parser = Parser(plant)
        parser.package_map().Add("scene", str(package))
        directives = package / "compiled" / "drake" / "scene.dmd.yaml"
        loaded = LoadModelDirectives(str(directives))
        ProcessModelDirectives(loaded, plant, parser)
        plant.Finalize()
    except Exception as exc:
        raise JobExecutionError(
            f"Drake package validation failed: {exc}",
            code="export_drake_validation_failed",
            retryable=False,
        ) from exc


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
                name = (Path("package") / path.relative_to(package)).as_posix()
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                output.writestr(info, path.read_bytes())
