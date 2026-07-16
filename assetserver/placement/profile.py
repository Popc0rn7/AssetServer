"""Deterministic baseline placement-profile production and validation."""

from __future__ import annotations

import hashlib
import json
import math

from typing import Any


PROFILE_SCHEMA_VERSION = "asset-placement/v1"
ANALYZER_VERSION = "asset-placement-analyzer/1"
PROFILE_ENTRYPOINT = "placement/profile.json"


def baseline_profile(
    bounds: dict[str, Any],
    *,
    support_surfaces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a conservative geometry-only profile from canonical asset bounds."""
    minimum = [float(value) for value in bounds["min"]]
    maximum = [float(value) for value in bounds["max"]]
    extents = [maximum[i] - minimum[i] for i in range(3)]
    center = [(maximum[i] + minimum[i]) / 2 for i in range(3)]
    ground = [
        [minimum[0], minimum[1], minimum[2]],
        [maximum[0], minimum[1], minimum[2]],
        [maximum[0], maximum[1], minimum[2]],
        [minimum[0], maximum[1], minimum[2]],
    ]
    surfaces = [_normalize_support_surface(item) for item in support_surfaces or []]
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "canonical_frame": {
            "units": "m",
            "handedness": "right",
            "up_axis": "+Z",
            "origin": "ground-center",
        },
        "bounds": {
            "aabb": {"min": minimum, "max": maximum},
            "obb": {
                "center": center,
                "extents": extents,
                "rotation": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            },
        },
        "semantic_axes": {
            "front": {
                "status": "unavailable",
                "axis": None,
                "confidence": 0.0,
                "method": "none",
                "reason": "no_reliable_semantic_source",
            }
        },
        "contact_surfaces": [
            {
                "id": "ground_contact",
                "type": "ground",
                "frame": "asset",
                "normal": [0.0, 0.0, 1.0],
                "polygon": ground,
                "confidence": 1.0,
                "method": "geometry",
            }
        ],
        "support_surfaces": surfaces,
        "interaction_zones": [],
        "articulation_sweeps": [],
        "producer": {"name": "asset-placement-analyzer", "version": "1"},
    }


def profile_bytes(profile: dict[str, Any]) -> bytes:
    validate_profile(profile)
    return json.dumps(
        profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode() + b"\n"


def placement_manifest(profile: dict[str, Any]) -> tuple[dict[str, str], bytes]:
    content = profile_bytes(profile)
    return (
        {
            "entrypoint": PROFILE_ENTRYPOINT,
            "sha256": hashlib.sha256(content).hexdigest(),
            "schema_version": PROFILE_SCHEMA_VERSION,
        },
        content,
    )


def validate_profile(profile: dict[str, Any]) -> None:
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported placement profile schema")
    if "asset_ref" in profile:
        raise ValueError("stored placement profile must not contain derived asset_ref")
    bounds = profile.get("bounds") or {}
    aabb, obb = bounds.get("aabb"), bounds.get("obb")
    if not isinstance(aabb, dict) or not isinstance(obb, dict):
        raise ValueError("placement profile requires AABB and OBB")
    values = [*(aabb.get("min") or []), *(aabb.get("max") or [])]
    if len(values) != 6 or not all(math.isfinite(float(value)) for value in values):
        raise ValueError("placement profile bounds are invalid")
    front = ((profile.get("semantic_axes") or {}).get("front") or {})
    if front.get("status") not in {"available", "unavailable"}:
        raise ValueError("placement profile requires semantic_axes.front.status")
    if not profile.get("producer"):
        raise ValueError("placement profile requires producer")


def public_profile(profile: dict[str, Any], asset_ref: str) -> dict[str, Any]:
    validate_profile(profile)
    return {**profile, "asset_ref": asset_ref}


def _normalize_support_surface(item: dict[str, Any]) -> dict[str, Any]:
    value = dict(item)
    identifier = value.pop("name", None) or value.get("id")
    return {
        **value,
        "id": identifier,
        "type": value.get("type", "horizontal_support"),
        "frame": value.get("frame", "asset"),
        "confidence": float(value.get("confidence", 1.0)),
        "method": value.get("method", "geometry"),
    }
