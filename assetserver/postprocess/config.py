"""Canonical configuration and cache keys for collision postprocessing."""

from __future__ import annotations

import hashlib
import json
import os
import warnings

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


PROFILE_DEFAULTS: dict[str, Any] = {
    "name": "rigid-object-v1",
    "method": "coacd",
    "max_convex_hulls": 32,
    "threshold": 0.05,
    "preprocess_mode": "auto",
    "preprocess_resolution": 50,
    "resolution": 2000,
    "mcts_nodes": 20,
    "mcts_iterations": 150,
    "mcts_max_depth": 3,
    "max_ch_vertex": 128,
    "merge": True,
    "decimate": False,
    "seed": 0,
}

POSTPROCESS_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "policy": "required",
    "url": "http://127.0.0.1:7100",
    "timeout_s": 300,
    "concurrency": 1,
    "omp_threads": 4,
    "database": "data/postprocess/postprocess.sqlite3",
    "staging_root": "data/postprocess/staging",
    "profile": PROFILE_DEFAULTS,
}


def normalized_profile(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    profile = {**PROFILE_DEFAULTS, **dict(value or {})}
    if profile["method"] != "coacd":
        raise ValueError("only the coacd postprocess method is supported")
    if not 1 <= int(profile["max_convex_hulls"]) <= 32:
        raise ValueError("profile.max_convex_hulls must be between 1 and 32")
    profile["max_convex_hulls"] = int(profile["max_convex_hulls"])
    return profile


def canonical_profile_json(value: Mapping[str, Any] | None = None) -> str:
    return json.dumps(
        normalized_profile(value), sort_keys=True, separators=(",", ":")
    )


def profile_sha256(value: Mapping[str, Any] | None = None) -> str:
    return hashlib.sha256(canonical_profile_json(value).encode()).hexdigest()


def artifact_key(
    visual_sha256: str,
    canonical_frame: Mapping[str, Any],
    profile: Mapping[str, Any],
    coacd_version: str,
) -> str:
    return _key(visual_sha256, canonical_frame, normalized_profile(profile), coacd_version)


def derivation_key(
    parent_asset_digest: str, artifact_digest: str, sdf_generator_version: str
) -> str:
    return _key(parent_asset_digest, artifact_digest, sdf_generator_version)


def _key(*values: Any) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PostprocessConfig:
    enabled: bool
    policy: str
    url: str
    timeout_s: float
    concurrency: int
    omp_threads: int
    database: Path
    staging_root: Path
    profile: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "PostprocessConfig":
        data = {**POSTPROCESS_DEFAULTS, **dict(value or {})}
        host = os.environ.get("ASSETSERVER_POSTPROCESS_HOST")
        port = os.environ.get("ASSETSERVER_POSTPROCESS_PORT")
        if host or port:
            warnings.warn(
                "ASSETSERVER_POSTPROCESS_HOST/PORT are deprecated; use runtime.postprocess.url",
                DeprecationWarning,
                stacklevel=2,
            )
            data["url"] = f"http://{host or '127.0.0.1'}:{port or '7100'}"
        if data["policy"] not in {"required", "disabled"}:
            raise ValueError("runtime.postprocess.policy must be required or disabled")
        return cls(
            enabled=bool(data["enabled"]),
            policy=str(data["policy"]),
            url=str(data["url"]).rstrip("/"),
            timeout_s=float(data["timeout_s"]),
            concurrency=int(data["concurrency"]),
            omp_threads=int(data["omp_threads"]),
            database=Path(str(data["database"])),
            staging_root=Path(str(data["staging_root"])),
            profile=normalized_profile(data.get("profile")),
        )
