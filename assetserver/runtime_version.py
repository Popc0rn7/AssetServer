"""Shared API/worker runtime identity and schema compatibility guard."""

from __future__ import annotations

import json
import logging
import os
import tempfile

from datetime import UTC, datetime
from pathlib import Path

from assetserver.procedural_room_shell import GENERATOR_VERSION
from assetserver.scene_ir import SCHEMA_VERSION


SCENE_IR_MODEL_VERSION = f"{SCHEMA_VERSION}+{GENERATOR_VERSION}"
SCENE_JOB_IMPLEMENTATION_VERSION = (
    f"{SCENE_IR_MODEL_VERSION}+renderer=scene-ir-eevee/v2"
    "+validator=validation-report/v1+exporter=scene-export/v2"
)


def scene_job_cache_version() -> str:
    """Identity all code that can change a scene job's durable result."""
    return (
        f"{SCENE_JOB_IMPLEMENTATION_VERSION}"
        f"+build={os.environ.get('ASSETSERVER_BUILD_VERSION', 'dev')}"
    )


def runtime_identity(role: str, instance_id: str) -> dict[str, str]:
    return {
        "role": role,
        "instance_id": instance_id,
        "scene_ir_schema_version": SCHEMA_VERSION,
        "scene_ir_model_version": SCENE_IR_MODEL_VERSION,
        "build_version": os.environ.get("ASSETSERVER_BUILD_VERSION", "dev"),
        "started_at": datetime.now(UTC).isoformat(),
    }


def register_runtime(
    root: str | Path,
    *,
    role: str,
    instance_id: str,
    logger: logging.Logger | None = None,
) -> dict[str, str]:
    """Record one runtime and reject API/worker SceneIR model drift."""
    log = logger or logging.getLogger(__name__)
    identity = runtime_identity(role, instance_id)
    directory = Path(root) / "runtime"
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob("*.json"):
        try:
            peer = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if peer.get("role") == role:
            continue
        peer_model = peer.get("scene_ir_model_version")
        if peer_model and peer_model != SCENE_IR_MODEL_VERSION:
            message = (
                "SceneIR model version drift: "
                f"{role}={SCENE_IR_MODEL_VERSION}, "
                f"{peer.get('role')}={peer_model}"
            )
            if role != "api":
                raise RuntimeError(message)
            log.error(message)
        peer_build = peer.get("build_version")
        if peer_build and peer_build != identity["build_version"]:
            log.warning(
                "AssetServer build versions differ but SceneIR model is compatible: "
                "%s=%s, %s=%s",
                role,
                identity["build_version"],
                peer.get("role"),
                peer_build,
            )
    filename = f"{_safe_instance_id(role)}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{filename}-", dir=directory)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n")
        temporary.replace(directory / filename)
    finally:
        temporary.unlink(missing_ok=True)
    log.info(
        "AssetServer runtime role=%s instance=%s scene_ir_schema=%s "
        "scene_ir_model=%s build=%s",
        role,
        instance_id,
        identity["scene_ir_schema_version"],
        identity["scene_ir_model_version"],
        identity["build_version"],
    )
    return identity


def worker_model_version(root: str | Path) -> str | None:
    value = _worker_identity(root)
    version = value.get("scene_ir_model_version") if value else None
    return str(version) if version else None


def deployed_scene_job_cache_version(root: str | Path) -> str:
    """Use the deployed worker build, not the API process build, for dedup."""
    value = _worker_identity(root)
    if value and value.get("build_version"):
        return f"{SCENE_JOB_IMPLEMENTATION_VERSION}+build={value['build_version']}"
    return scene_job_cache_version()


def _worker_identity(root: str | Path) -> dict[str, str] | None:
    path = Path(root) / "runtime" / "scene-worker.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _safe_instance_id(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() else "-" for character in value
    )
    return normalized[:128] or "unknown"
