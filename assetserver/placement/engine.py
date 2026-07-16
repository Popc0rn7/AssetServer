"""Deterministic room-level placement evaluation and bounded SE(2) search."""

from __future__ import annotations

import json
import math
import os

from pathlib import Path
from typing import Any

from assetserver.asset_store import ContentAddressedAssetStore
from assetserver.jobs import Job, JobExecutionError
from assetserver.scene_ir import ProceduralRoomShell, SceneIR, Transform, load_scene_yaml, scene_sha256
from assetserver.placement.models import issue


VALIDATOR_VERSION = "room-placement-validator/1"
SOLVER_VERSION = "placement-solver/1"
REPAIR_VERSION = "placement-repair/1"


def _load(job: Job) -> tuple[SceneIR, ContentAddressedAssetStore, Path]:
    root = Path(os.environ.get("ASSETSERVER_DATA_ROOT", "data"))
    path = root / "scenes" / job.scene_id / "revisions" / f"{job.scene_revision:06d}.yaml"
    scene = load_scene_yaml(path.read_bytes())
    actual = scene_sha256(scene)
    expected = job.request.get("scene_sha256")
    if expected and expected != actual:
        raise JobExecutionError(
            "scene revision SHA no longer matches the submitted request",
            code="scene_revision_conflict",
            retryable=False,
        )
    return scene, ContentAddressedAssetStore(root / "assets"), root


def _profile(assets: ContentAddressedAssetStore, asset_ref: str) -> dict[str, Any]:
    profile = assets.placement_profile(asset_ref)
    if profile is None:
        raise JobExecutionError(
            "asset does not have a usable placement profile",
            code="placement_profile_missing",
            retryable=False,
        )
    return profile


def _world_aabb(obj: Any, profile: dict[str, Any], transform: Transform | None = None):
    transform = transform or obj.transform
    bounds = profile["bounds"]["aabb"]
    lo, hi = bounds["min"], bounds["max"]
    yaw = math.radians(transform.rotation_rpy_degrees[2])
    corners = [
        (x, y)
        for x in (float(lo[0]), float(hi[0]))
        for y in (float(lo[1]), float(hi[1]))
    ]
    rotated = [
        (x * math.cos(yaw) - y * math.sin(yaw), x * math.sin(yaw) + y * math.cos(yaw))
        for x, y in corners
    ]
    tx, ty, tz = transform.translation
    return {
        "min": [min(x for x, _ in rotated) + tx, min(y for _, y in rotated) + ty, float(lo[2]) + tz],
        "max": [max(x for x, _ in rotated) + tx, max(y for _, y in rotated) + ty, float(hi[2]) + tz],
    }


def _overlap(a: dict[str, list[float]], b: dict[str, list[float]]) -> list[float]:
    return [max(0.0, min(a["max"][i], b["max"][i]) - max(a["min"][i], b["min"][i])) for i in range(3)]


def evaluate(scene: SceneIR, assets: ContentAddressedAssetStore, request: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    profiles: dict[str, dict[str, Any]] = {}
    boxes: dict[str, dict[str, list[float]]] = {}
    rooms = {room.id: room for room in scene.rooms}
    for obj in scene.objects:
        try:
            profiles[obj.id] = _profile(assets, obj.asset_ref)
        except JobExecutionError:
            issues.append(issue("placement_profile_missing", [obj.id], metric=1, threshold=0, units="count", message="asset does not have a usable placement profile"))
            continue
        boxes[obj.id] = _world_aabb(obj, profiles[obj.id])
        room = rooms[obj.room_id]
        tolerance = float(request.get("room_boundary_tolerance", 0.001))
        if isinstance(room.shell, ProceduralRoomShell):
            width, depth, height = room.shell.dimensions
            room_min = [room.transform.translation[0] - width / 2, room.transform.translation[1] - depth / 2, room.transform.translation[2]]
            room_max = [room.transform.translation[0] + width / 2, room.transform.translation[1] + depth / 2, room.transform.translation[2] + height]
            violation = max([room_min[i] - boxes[obj.id]["min"][i] for i in range(3)] + [boxes[obj.id]["max"][i] - room_max[i] for i in range(3)] + [0.0])
            if violation > tolerance:
                issues.append(issue("outside_room", [obj.id], metric=violation, threshold=tolerance, units="m", message=f"{obj.id} lies outside room {room.id}", evidence={"scene_frame_aabb": boxes[obj.id]}))
        contact_z = boxes[obj.id]["min"][2]
        support_tolerance = float(request.get("support_contact_tolerance", 0.001))
        if obj.placement is None and abs(contact_z - room.transform.translation[2]) > support_tolerance:
            code = "floating" if contact_z > room.transform.translation[2] else "unsupported"
            issues.append(issue(code, [obj.id], metric=abs(contact_z - room.transform.translation[2]), threshold=support_tolerance, units="m", message=f"{obj.id} is not in floor contact", repair_hint={"preferred_action": "snap_to_support", "movable_object_id": obj.id, "direction_hint": [0, 0, -1], "minimum_distance": abs(contact_z - room.transform.translation[2])}))
    ids = sorted(boxes)
    epsilon = float(request.get("penetration_epsilon", 0.000001))
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            overlap = _overlap(boxes[left], boxes[right])
            volume = overlap[0] * overlap[1] * overlap[2]
            depth = min(overlap)
            if volume > 0 and depth > epsilon:
                issues.append(issue("penetration", [left, right], metric=depth, threshold=epsilon, units="m", message=f"{left} penetrates {right}", evidence={"overlap_volume": volume, "contact_count": 1}))
    by_object = {obj.id: obj for obj in scene.objects}
    for intent in request.get("intents") or []:
        obj = by_object.get(intent["object_id"])
        if obj is None:
            continue
        for constraint in intent.get("constraints") or []:
            if not constraint.get("required", True):
                continue
            if constraint["type"] == "inside_room" and any(item["code"] == "outside_room" and obj.id in item["object_ids"] for item in issues):
                issues.append(issue("required_relation_unsatisfied", [obj.id], metric=1, threshold=0, units="boolean", message="inside_room constraint is not satisfied", constraint_id=constraint["id"]))
            if constraint["type"] == "facing" and constraint.get("target", {}).get("type") == "room_center":
                room = rooms[obj.room_id]
                dx = room.transform.translation[0] - obj.transform.translation[0]
                dy = room.transform.translation[1] - obj.transform.translation[1]
                desired = math.degrees(math.atan2(dx, dy)) % 360
                actual = obj.transform.rotation_rpy_degrees[2] % 360
                delta = abs((actual - desired + 180) % 360 - 180)
                threshold = float(constraint.get("angular_tolerance_degrees") or 0)
                if delta > threshold:
                    issues.append(issue("wrong_orientation", [obj.id], metric=delta, threshold=threshold, units="degrees", message="required facing constraint is not satisfied", constraint_id=constraint["id"]))
    unique = {item["issue_id"]: item for item in issues}
    ordered = [unique[key] for key in sorted(unique)]
    return {"valid": not ordered, "issues": ordered, "model_count": len(scene.objects)}


def validate(job: Job) -> dict[str, Any]:
    scene, assets, root = _load(job)
    result = evaluate(scene, assets, job.request)
    report = {"schema_version": "validation-report/v1", "job_id": job.job_id, "scene_id": job.scene_id, "scene_revision": job.scene_revision, "scene_sha256": scene_sha256(scene), "producer": {"name": "room-placement-validator", "version": "1"}, **result}
    path = root / "scenes" / job.scene_id / "validations" / job.job_id / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return {**result, "scene_revision": job.scene_revision, "scene_sha256": report["scene_sha256"], "report_path": path.relative_to(root).as_posix()}


def _candidate_transforms(scene: SceneIR, assets: ContentAddressedAssetStore, intent: dict[str, Any]):
    obj = next(item for item in scene.objects if item.id == intent["object_id"])
    room = next(item for item in scene.rooms if item.id == obj.room_id)
    profile = _profile(assets, obj.asset_ref)
    aabb = profile["bounds"]["aabb"]
    if not isinstance(room.shell, ProceduralRoomShell):
        return []
    width, depth, _ = room.shell.dimensions
    cx, cy, floor = room.transform.translation
    half_x = (aabb["max"][0] - aabb["min"][0]) / 2
    half_y = (aabb["max"][1] - aabb["min"][1]) / 2
    z = floor - aabb["min"][2]
    anchors = [(cx, cy, 0.0), (cx, cy + depth / 2 - half_y, 180.0), (cx, cy - depth / 2 + half_y, 0.0), (cx + width / 2 - half_x, cy, 270.0), (cx - width / 2 + half_x, cy, 90.0)]
    required_wall = next((c.get("wall") for c in intent.get("constraints", []) if c["type"] == "against_wall" and c.get("required", True)), None)
    wall_index = {"north": 1, "south": 2, "east": 3, "west": 4}
    if required_wall:
        anchors = [anchors[wall_index[required_wall]]]
    return [Transform(translation=(x, y, z), rotation_rpy_degrees=(0, 0, yaw)) for x, y, yaw in anchors]


def propose(job: Job) -> dict[str, Any]:
    scene, assets, root = _load(job)
    candidates = []
    intents = job.request.get("intents") or []
    if len(intents) != 1:
        raise JobExecutionError("P0 solver accepts one placement intent per job", code="invalid_placement_intent", retryable=False)
    intent = intents[0]
    obj = next((item for item in scene.objects if item.id == intent["object_id"]), None)
    if obj is None:
        raise JobExecutionError("placement intent references an unknown object", code="invalid_placement_intent", retryable=False)
    if obj.id in set(intent.get("locked_object_ids") or []):
        transforms = []
    else:
        transforms = _candidate_transforms(scene, assets, intent)
    maximum = int((job.request.get("options") or {}).get("max_candidates", 5))
    for index, transform in enumerate(transforms):
        changed = scene.model_copy(deep=True)
        target = next(item for item in changed.objects if item.id == obj.id)
        target.transform = transform
        validation = evaluate(changed, assets, {**job.request, "intents": [intent]})
        if validation["valid"]:
            candidates.append({"candidate_id": f"pose_{index + 1}", "object_updates": [{"object_id": obj.id, "transform": transform.model_dump(mode="json")}], "hard_constraints_satisfied": True, "constraint_results": [{"constraint_id": item["id"], "satisfied": True} for item in intent.get("constraints") or []], "scores": {"total": 1.0, "orientation": 1.0, "clearance": 1.0, "composition": 1.0}})
        if len(candidates) >= maximum:
            break
    seed = int((job.request.get("options") or {}).get("seed", 0))
    report = {"schema_version": "placement-proposals/v1", "scene_id": job.scene_id, "scene_revision": job.scene_revision, "scene_sha256": scene_sha256(scene), "candidates": candidates, "infeasibility_issues": [] if candidates else [{"code": "no_feasible_pose", "message": "no candidate satisfies every hard constraint"}], "solver": {"name": "placement-solver", "version": "1", "seed": seed}}
    return _write_result(root, job, "proposals", report)


def repair(job: Job) -> dict[str, Any]:
    scene, assets, root = _load(job)
    before = evaluate(scene, assets, job.request)
    requested = set(job.request.get("issue_ids") or [])
    locked = set(job.request.get("locked_object_ids") or [])
    changed = scene.model_copy(deep=True)
    updates = []
    for current in before["issues"]:
        if current["issue_id"] not in requested or current["code"] not in {"floating", "penetration"}:
            continue
        movable = next((value for value in current["object_ids"] if value not in locked), None)
        if movable is None:
            continue
        obj = next(item for item in changed.objects if item.id == movable)
        x, y, z = obj.transform.translation
        if current["code"] == "floating" and "snap_to_support" in job.request.get("allowed_operations", []):
            profile = _profile(assets, obj.asset_ref)
            z = -float(profile["bounds"]["aabb"]["min"][2])
        elif current["code"] == "penetration" and "translate" in job.request.get("allowed_operations", []):
            x += float(current["metric"]) + 0.001
        else:
            continue
        obj.transform = Transform(translation=(x, y, z), rotation_rpy_degrees=obj.transform.rotation_rpy_degrees)
        updates.append({"object_id": obj.id, "transform": obj.transform.model_dump(mode="json")})
    after = evaluate(changed, assets, job.request)
    remaining_ids = {item["issue_id"] for item in after["issues"]}
    before_ids = {item["issue_id"] for item in before["issues"]}
    introduced = remaining_ids - before_ids
    if introduced:
        updates = []
        after = before
        remaining_ids = before_ids
    resolved = sorted(requested - remaining_ids)
    report = {"schema_version": "scene-patch/v1", "base": {"scene_id": job.scene_id, "scene_revision": job.scene_revision, "scene_sha256": scene_sha256(scene)}, "object_updates": updates if resolved else [], "resolved_issue_ids": resolved, "remaining_issues": after["issues"], "solver": {"name": "placement-repair", "version": "1", "seed": int((job.request.get("options") or {}).get("seed", 0))}}
    return _write_result(root, job, "repairs", report)


def _write_result(root: Path, job: Job, kind: str, report: dict[str, Any]) -> dict[str, Any]:
    path = root / "scenes" / job.scene_id / kind / job.job_id / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return {**report, "result_path": path.relative_to(root).as_posix()}
