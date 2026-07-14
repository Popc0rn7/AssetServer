"""Isolated, bounded CoACD HTTP worker."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import threading
import time
import uuid

from pathlib import Path, PurePosixPath
from typing import Any

import coacd
import trimesh

from fastapi import FastAPI, HTTPException

from assetserver.postprocess.config import normalized_profile


class PostprocessServerApp:
    """Serve one CoACD task at a time without accepting arbitrary host paths."""

    def __init__(
        self,
        asset_root: str | Path | None = None,
        staging_root: str | Path | None = None,
        *,
        max_waiting: int = 8,
        max_input_bytes: int = 512 * 1024**2,
        max_input_faces: int = 5_000_000,
        max_output_bytes: int = 512 * 1024**2,
    ) -> None:
        data_root = Path(os.environ.get("ASSETSERVER_DATA_ROOT", "data"))
        self.asset_root = Path(asset_root or data_root / "assets").resolve()
        self.staging_root = Path(
            staging_root
            or os.environ.get(
                "ASSETSERVER_POSTPROCESS_STAGING", data_root / "postprocess/staging"
            )
        ).resolve()
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.max_waiting = max_waiting
        self.max_input_bytes = max_input_bytes
        self.max_input_faces = max_input_faces
        self.max_output_bytes = max_output_bytes
        self._slots = threading.BoundedSemaphore(max_waiting + 1)
        self._task = threading.Lock()
        coacd.set_log_level("error")
        self.app = FastAPI(title="AssetServer Postprocess Worker")
        self.app.add_api_route("/health/live", self._live, methods=["GET"])
        self.app.add_api_route("/health/ready", self._ready, methods=["GET"])
        self.app.add_api_route(
            "/v1/decompositions", self._decompose, methods=["POST"]
        )

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)

    def _live(self) -> dict[str, str]:
        return {"status": "live"}

    def _ready(self) -> dict[str, str]:
        if not self.asset_root.is_dir() or not os.access(self.staging_root, os.W_OK):
            raise HTTPException(status_code=503, detail="configured roots are not ready")
        return {"status": "ready"}

    async def _decompose(self, data: dict[str, Any]) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="postprocess queue is full")
        try:
            # CoACD/OpenMP must execute on the worker's main thread. Moving it to
            # an executor can deadlock interpreter shutdown on libgomp.
            return self._serialized_run(data)
        finally:
            self._slots.release()

    def _serialized_run(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._task:
            return self._run(data)

    def _run(self, data: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        request_id = _hex_digest(data.get("request_id"), "request_id")
        source = data.get("source")
        if not isinstance(source, dict):
            raise HTTPException(status_code=422, detail="source is required")
        digest = _hex_digest(source.get("asset_digest"), "asset_digest")
        entrypoint = _safe_relative(source.get("entrypoint"), "entrypoint")
        if entrypoint.suffix.lower() not in {".glb", ".gltf", ".obj"}:
            raise HTTPException(status_code=422, detail="unsupported input format")
        profile_data = data.get("profile")
        if not isinstance(profile_data, dict):
            raise HTTPException(status_code=422, detail="profile is required")
        supplied = {
            "name": profile_data.get("name"),
            "method": profile_data.get("method"),
            **dict(profile_data.get("parameters") or {}),
        }
        try:
            profile = normalized_profile(supplied)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        source_path = self._inside(
            self.asset_root / "sha256" / digest[:2] / digest / "files" / entrypoint
        )
        if not source_path.is_file():
            raise HTTPException(status_code=422, detail="source asset does not exist")
        if source_path.stat().st_size > self.max_input_bytes:
            raise HTTPException(status_code=413, detail="source asset is too large")
        _validate_external_references(
            source_path,
            self.asset_root / "sha256" / digest[:2] / digest / "files",
        )
        try:
            loaded = trimesh.load(source_path, force="mesh")
            if isinstance(loaded, trimesh.Scene):
                loaded = loaded.to_mesh()
            mesh = loaded
        except Exception as exc:
            raise HTTPException(status_code=422, detail="unable to load source mesh") from exc
        if len(mesh.faces) == 0 or len(mesh.faces) > self.max_input_faces:
            raise HTTPException(status_code=422, detail="invalid source face count")

        result = coacd.run_coacd(
            coacd.Mesh(mesh.vertices, mesh.faces),
            threshold=float(profile["threshold"]),
            max_convex_hull=int(profile["max_convex_hulls"]),
            preprocess_mode=str(profile["preprocess_mode"]),
            preprocess_resolution=int(profile["preprocess_resolution"]),
            resolution=int(profile["resolution"]),
            mcts_nodes=int(profile["mcts_nodes"]),
            mcts_iterations=int(profile["mcts_iterations"]),
            mcts_max_depth=int(profile["mcts_max_depth"]),
            max_ch_vertex=int(profile["max_ch_vertex"]),
            merge=bool(profile["merge"]),
            decimate=bool(profile["decimate"]),
            seed=int(profile["seed"]),
        )
        if not 1 <= len(result) <= int(profile["max_convex_hulls"]):
            raise HTTPException(status_code=422, detail="invalid convex hull count")

        temporary = self.staging_root / f".{request_id}-{uuid.uuid4().hex}.tmp"
        destination = self._inside(self.staging_root / request_id)
        pieces: list[dict[str, Any]] = []
        total = 0
        try:
            temporary.mkdir(parents=True)
            for index, (vertices, faces) in enumerate(result):
                name = f"hull_{index:03d}.obj"
                path = temporary / name
                hull = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
                hull.export(path, file_type="obj")
                content = path.read_bytes()
                total += len(content)
                if total > self.max_output_bytes:
                    raise HTTPException(status_code=413, detail="output is too large")
                pieces.append(
                    {
                        "path": name,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "vertices": int(len(hull.vertices)),
                        "faces": int(len(hull.faces)),
                    }
                )
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(temporary, destination)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return {
            "success": True,
            "pieces": pieces,
            "processing_time_s": time.monotonic() - started,
        }

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        root = self.asset_root if self.asset_root in resolved.parents else self.staging_root
        if resolved != root and root not in resolved.parents:
            raise HTTPException(status_code=422, detail="path escapes configured root")
        return resolved


def _hex_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise HTTPException(status_code=422, detail=f"invalid {field}")
    return value


def _safe_relative(value: Any, field: str) -> Path:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"invalid {field}")
    posix = PurePosixPath(value)
    if posix.is_absolute() or ".." in posix.parts or "\\" in value:
        raise HTTPException(status_code=422, detail=f"unsafe {field}")
    return Path(*posix.parts)


def _validate_external_references(source: Path, files_root: Path) -> None:
    """Reject glTF/OBJ references that escape the immutable asset directory."""
    suffix = source.suffix.lower()
    references: list[str] = []
    try:
        if suffix == ".gltf":
            document = json.loads(source.read_text())
            references = _gltf_references(document)
        elif suffix == ".glb":
            data = source.read_bytes()
            if len(data) < 20 or data[:4] != b"glTF":
                raise ValueError("invalid GLB header")
            offset = 12
            document = None
            while offset + 8 <= len(data):
                length, chunk_type = struct.unpack_from("<II", data, offset)
                offset += 8
                chunk = data[offset : offset + length]
                offset += length
                if chunk_type == 0x4E4F534A:
                    document = json.loads(chunk.rstrip(b" \t\r\n\0"))
                    break
            if document is None:
                raise ValueError("GLB has no JSON chunk")
            references = _gltf_references(document)
        elif suffix == ".obj":
            for line in source.read_text(errors="strict").splitlines():
                fields = line.strip().split()
                if fields and fields[0].lower() == "mtllib":
                    references.extend(fields[1:])
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid mesh container") from exc
    root = files_root.resolve()
    seen: set[Path] = set()
    pending = [(source.parent, value) for value in references]
    for reference_base, value in pending:
        if value.startswith("data:"):
            continue
        relative = PurePosixPath(value)
        if "://" in value or relative.is_absolute() or "\\" in value:
            raise HTTPException(status_code=422, detail="unsafe external mesh reference")
        resolved = (reference_base / Path(*relative.parts)).resolve()
        if root not in resolved.parents or not resolved.is_file():
            raise HTTPException(status_code=422, detail="unresolved external mesh reference")
        if resolved.suffix.lower() == ".mtl" and resolved not in seen:
            seen.add(resolved)
            try:
                for line in resolved.read_text(errors="strict").splitlines():
                    fields = line.strip().split()
                    if fields and (
                        fields[0].lower().startswith("map_")
                        or fields[0].lower() in {"bump", "disp", "decal", "refl"}
                    ):
                        pending.append((resolved.parent, fields[-1]))
            except (OSError, UnicodeError) as exc:
                raise HTTPException(status_code=422, detail="invalid material file") from exc


def _gltf_references(document: dict[str, Any]) -> list[str]:
    output = []
    for collection in (document.get("buffers") or [], document.get("images") or []):
        for item in collection:
            uri = item.get("uri") if isinstance(item, dict) else None
            if isinstance(uri, str):
                output.append(uri)
    return output
