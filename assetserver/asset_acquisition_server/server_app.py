import logging
import time
import uuid
import hashlib
import json

from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from typing import Literal

import httpx

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from omegaconf import OmegaConf
from pydantic import BaseModel, Field

from assetserver.artifacts import GLOBAL_ARTIFACTS, artifact_media_type
from assetserver.asset_store import (
    AssetStoreError,
    ContentAddressedAssetStore,
    StoredAsset,
)
from assetserver.config import BackendSpec, backend_specs, enabled_backend_specs
from assetserver.jobs import JobNotFoundError, SQLiteJobStore
from assetserver.scene_renderer import SceneRendererClient, SceneRendererError
from assetserver.scenes import (
    SceneConflictError,
    SceneNotFoundError,
    ScenePackageError,
    SceneStore,
)
from assetserver.scene_ir import SceneIR, SceneIRValidationError
from assetserver.scene_ir_store import (
    IRSceneAssetError,
    IRSceneConflictError,
    IRSceneNotFoundError,
    IRSceneStore,
)

from .dataclasses import (
    AssetAcquisitionServerRequest,
    AssetAcquisitionServerResponse,
)

console_logger = logging.getLogger(__name__)

GenerateAssetsHandler = Callable[
    [AssetAcquisitionServerRequest], AssetAcquisitionServerResponse
]

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

HISTORY_MAX_ENTRIES = 500
UPSTREAM_TIMEOUT_S = 3600.0
SCENE_RENDER_TIMEOUT_S = 300.0
SCENE_MAX_SDF_BYTES = 10 * 1024**2
SCENE_MAX_PACKAGE_BYTES = 2 * 1024**3


def _dimensions(metadata: dict[str, Any]) -> list[float] | None:
    value = metadata.get("dimensions")
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return [float(item) for item in value]
    return _dimensions_from_bounds(
        {
            "min": metadata.get("bounding_box_min"),
            "max": metadata.get("bounding_box_max"),
        }
    )


def _dimensions_from_bounds(bounds: dict[str, Any]) -> list[float] | None:
    minimum, maximum = bounds.get("min"), bounds.get("max")
    if not (
        isinstance(minimum, (list, tuple))
        and isinstance(maximum, (list, tuple))
        and len(minimum) == len(maximum) == 3
    ):
        return None
    return [float(hi) - float(lo) for lo, hi in zip(minimum, maximum, strict=True)]


def _articulation(joints: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "articulated": bool(joints),
        "joint_count": len(joints),
        "joints": [item.get("name") for item in joints],
    }


def rewrite_sam3d_download_url(data: dict[str, Any]) -> dict[str, Any]:
    rewritten = dict(data)
    asset = dict(rewritten.get("asset") or {})
    asset_id = asset.get("asset_id")
    if asset_id:
        asset["download_url"] = f"/v1/assets/sam3d/{asset_id}"
        rewritten["asset"] = asset
    return rewritten


@dataclass
class GatewayHistoryEntry:
    request_id: str
    method: str
    path: str
    backend: str | None
    upstream_url: str | None
    status_code: int | None
    duration_ms: float
    client: str | None
    error: str | None = None


class SceneRenderRequest(BaseModel):
    revision: int | None = Field(default=None, ge=1)
    views: list[str] = Field(
        default_factory=lambda: ["top", "front", "side", "perspective"],
        min_length=1,
    )
    width: int = Field(default=512, ge=1, le=4096)
    height: int = Field(default=512, ge=1, le=4096)
    format: Literal["webp", "png"] = "webp"


class AssetAcquisitionApp:
    """FastAPI gateway for routing model traffic to backend services."""

    def __init__(
        self,
        generate_assets_handler: GenerateAssetsHandler | None = None,
        config: Any | None = None,
        retrieval_engine: Any | None = None,
        scene_store: SceneStore | None = None,
        scene_renderer: Any | None = None,
        ir_scene_store: IRSceneStore | None = None,
        job_store: SQLiteJobStore | None = None,
        asset_store: ContentAddressedAssetStore | None = None,
    ) -> None:
        self._generate_assets_handler = generate_assets_handler
        self._config = config
        if retrieval_engine is None and config is not None:
            from assetserver.retrieval import RetrievalEngine

            retrieval_engine = RetrievalEngine.from_config(config)
        self._retrieval_engine = retrieval_engine
        scene_config = self._scene_config()
        storage_config = self._storage_config()
        data_root = Path(storage_config.get("data_root", "data"))
        output_root = Path(storage_config.get("output_root", "outputs"))
        if scene_store is None and scene_config.get("legacy_sdf_api_enabled", False):
            scene_store = SceneStore(
                data_root / "scenes",
                max_package_bytes=SCENE_MAX_PACKAGE_BYTES,
                max_sdf_bytes=SCENE_MAX_SDF_BYTES,
            )
        if scene_renderer is None and scene_store is not None:
            renderer_url = scene_config.get("renderer_url")
            if renderer_url:
                scene_renderer = SceneRendererClient(
                    renderer_url,
                    timeout_s=SCENE_RENDER_TIMEOUT_S,
                )
        self._scene_store = scene_store
        self._scene_renderer = scene_renderer
        self._scene_data_root = data_root
        self._scene_output_root = output_root
        self._advertise_v2_assets = bool(
            asset_store is not None
            or ir_scene_store is not None
            or scene_config.get("scene_ir_api_enabled", False)
        )
        self._asset_store = (
            asset_store
            or (ir_scene_store.asset_store if ir_scene_store is not None else None)
            or ContentAddressedAssetStore(data_root / "assets")
        )
        if ir_scene_store is None and scene_config.get("scene_ir_api_enabled", False):
            ir_scene_store = IRSceneStore(
                data_root / "scenes", ContentAddressedAssetStore(data_root / "assets")
            )
        self._ir_scene_store = ir_scene_store
        if job_store is None and ir_scene_store is not None and config is not None:
            job_store = SQLiteJobStore(data_root / "jobs" / "jobs.sqlite3")
        self._job_store = job_store
        self._history: deque[GatewayHistoryEntry] = deque(maxlen=HISTORY_MAX_ENTRIES)
        self.app = FastAPI(title="AssetServer")
        self._register_routes()

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)

    def _register_routes(self) -> None:
        self.app.add_api_route("/health", self._health_endpoint, methods=["GET"])
        self.app.add_api_route("/tools", self._tools_endpoint, methods=["GET"])
        self.app.add_api_route("/backends", self._backends_endpoint, methods=["GET"])
        self.app.add_api_route("/history", self._history_endpoint, methods=["GET"])
        self.app.add_api_route("/shutdown", self._shutdown_endpoint, methods=["POST"])
        self.app.add_api_route(
            "/generate/{backend_name}",
            self._proxy_generate_endpoint,
            methods=["POST"],
        )
        self.app.add_api_route(
            "/v1/generate/sam3d",
            self._proxy_sam3d_generate_endpoint,
            methods=["POST"],
        )
        self.app.add_api_route(
            "/v1/assets/sam3d/{asset_id}",
            self._proxy_sam3d_asset_endpoint,
            methods=["GET"],
        )
        self.app.add_api_route(
            "/v1/retrieve/{source}",
            self._retrieve_v1_endpoint,
            methods=["POST"],
        )
        self.app.add_api_route(
            "/v2/generate/{backend}", self._generate_v2_endpoint, methods=["POST"]
        )
        self.app.add_api_route(
            "/v2/retrieve/{source}", self._retrieve_v2_endpoint, methods=["POST"]
        )
        self.app.add_api_route(
            "/v2/retrieve/{source}/{candidate_id}/materialize",
            self._materialize_v2_endpoint,
            methods=["POST"],
        )
        self.app.add_api_route(
            "/v2/assets/{digest}", self._asset_v2_endpoint, methods=["GET"]
        )
        self.app.add_api_route(
            "/v2/assets/{digest}/preview",
            self._asset_preview_v2_endpoint,
            methods=["GET"],
        )
        self.app.add_api_route(
            "/v1/assets/{source}/{asset_id}",
            self._retrieve_asset_v1_endpoint,
            methods=["GET"],
        )
        self.app.add_api_route(
            "/generate_assets", self._generate_assets_endpoint, methods=["POST"]
        )
        self.app.add_api_route(
            "/assets/{asset_id}", self._asset_endpoint, methods=["GET"]
        )
        if self._scene_store is not None:
            self.app.add_api_route(
                "/v1/scenes", self._create_scene_endpoint, methods=["POST"]
            )
            self.app.add_api_route(
                "/v1/scenes/{scene_id}/sdf", self._scene_sdf_endpoint, methods=["GET"]
            )
            self.app.add_api_route(
                "/v1/scenes/{scene_id}/sdf",
                self._update_scene_sdf_endpoint,
                methods=["PUT"],
            )
            self.app.add_api_route(
                "/v1/scenes/{scene_id}/render",
                self._render_scene_endpoint,
                methods=["POST"],
            )
            self.app.add_api_route(
                "/v1/scenes/{scene_id}/final",
                self._final_scene_endpoint,
                methods=["GET"],
            )
        if self._ir_scene_store is not None:
            self.app.add_api_route(
                "/v2/scene-schema", self._ir_scene_schema_endpoint, methods=["GET"]
            )
            self.app.add_api_route(
                "/v2/scenes", self._create_ir_scene_endpoint, methods=["POST"]
            )
            self.app.add_api_route(
                "/v2/scenes/{scene_id}", self._get_ir_scene_endpoint, methods=["GET"]
            )
            self.app.add_api_route(
                "/v2/scenes/{scene_id}", self._update_ir_scene_endpoint, methods=["PUT"]
            )
        if self._ir_scene_store is not None and self._job_store is not None:
            self.app.add_api_route(
                "/v2/scenes/{scene_id}/observe",
                self._submit_observe_job_endpoint,
                methods=["POST"],
            )
            self.app.add_api_route(
                "/v2/scenes/{scene_id}/validate",
                self._submit_validate_job_endpoint,
                methods=["POST"],
            )
            self.app.add_api_route(
                "/v2/scenes/{scene_id}/exports",
                self._submit_export_job_endpoint,
                methods=["POST"],
            )
            self.app.add_api_route(
                "/v2/jobs/{job_id}", self._get_job_endpoint, methods=["GET"]
            )
            self.app.add_api_route(
                "/v2/jobs/{job_id}/cancel", self._cancel_job_endpoint, methods=["POST"]
            )
            self.app.add_api_route(
                "/v2/observations/{observation_id}",
                self._get_observation_endpoint,
                methods=["GET"],
            )
            self.app.add_api_route(
                "/v2/observations/{observation_id}/views/{view}",
                self._get_observation_view_endpoint,
                methods=["GET"],
            )
            self.app.add_api_route(
                "/v2/exports/{export_id}", self._get_export_endpoint, methods=["GET"]
            )

    def _health_endpoint(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "mode": "server",
            "handler_configured": self._generate_assets_handler is not None,
            "enabled_backends": len(self._enabled_backend_specs()),
            "server": self._server_config(),
            "runtime": self._runtime_config(),
        }

    async def _create_ir_scene_endpoint(self, request: Request) -> JSONResponse:
        if request.headers.get("content-type", "").split(";", 1)[0] not in {
            "application/yaml",
            "application/x-yaml",
            "text/yaml",
        }:
            return self._scene_error(
                415,
                "unsupported_scene_media_type",
                "Scene IR requires application/yaml",
            )
        try:
            info = self._ir_scene_store.create(await request.body())
        except (SceneIRValidationError, IRSceneAssetError) as exc:
            return self._scene_error(422, "invalid_scene_ir", str(exc))
        return JSONResponse(
            {
                "scene_id": info.scene_id,
                "revision": info.revision,
                "sha256": info.sha256,
                "scene_url": f"/v2/scenes/{info.scene_id}",
            },
            status_code=201,
        )

    @staticmethod
    def _ir_scene_schema_endpoint() -> dict[str, Any]:
        return SceneIR.model_json_schema()

    async def _get_ir_scene_endpoint(
        self, scene_id: str, revision: int | None = Query(default=None, ge=1)
    ) -> Response:
        try:
            info = self._ir_scene_store.revision(scene_id, revision)
            content = self._ir_scene_store.read(scene_id, info.revision)
        except IRSceneNotFoundError as exc:
            return self._scene_error(404, "scene_not_found", str(exc))
        return Response(
            content,
            media_type="application/yaml",
            headers={
                "X-Scene-ID": scene_id,
                "X-Scene-Revision": str(info.revision),
                "ETag": f'"{info.sha256}"',
            },
        )

    async def _update_ir_scene_endpoint(
        self,
        scene_id: str,
        request: Request,
        x_base_revision: int = Header(alias="X-Base-Revision", ge=1),
    ) -> JSONResponse:
        if request.headers.get("content-type", "").split(";", 1)[0] not in {
            "application/yaml",
            "application/x-yaml",
            "text/yaml",
        }:
            return self._scene_error(
                415,
                "unsupported_scene_media_type",
                "Scene IR requires application/yaml",
            )
        try:
            info = self._ir_scene_store.update(
                scene_id, await request.body(), base_revision=x_base_revision
            )
        except IRSceneConflictError as exc:
            return self._scene_error(409, "scene_revision_conflict", str(exc))
        except IRSceneNotFoundError as exc:
            return self._scene_error(404, "scene_not_found", str(exc))
        except (SceneIRValidationError, IRSceneAssetError) as exc:
            return self._scene_error(422, "invalid_scene_ir", str(exc))
        return JSONResponse(
            {
                "scene_id": info.scene_id,
                "revision": info.revision,
                "sha256": info.sha256,
                "size_bytes": info.size_bytes,
            },
            status_code=201,
        )

    async def _submit_ir_job_endpoint(
        self, scene_id: str, request: Request, job_type: str
    ) -> JSONResponse:
        try:
            options = await request.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Expected a JSON object"
            ) from exc
        if not isinstance(options, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        requested_revision = options.pop("revision", None)
        try:
            revision = self._ir_scene_store.revision(scene_id, requested_revision)
        except IRSceneNotFoundError as exc:
            return self._scene_error(404, "scene_not_found", str(exc))
        job, created = self._job_store.submit(
            job_type,
            scene_id,
            revision.revision,
            options,
            max_attempts=int(self._job_config().get("max_attempts", 3)),
        )
        return JSONResponse(
            {
                "job_id": job.job_id,
                "job_type": job.job_type,
                "scene_id": job.scene_id,
                "scene_revision": job.scene_revision,
                "status": job.status,
                "status_url": f"/v2/jobs/{job.job_id}",
                "deduplicated": not created,
            },
            status_code=202,
        )

    async def _submit_observe_job_endpoint(
        self, scene_id: str, request: Request
    ) -> JSONResponse:
        return await self._submit_ir_job_endpoint(scene_id, request, "observe")

    async def _submit_validate_job_endpoint(
        self, scene_id: str, request: Request
    ) -> JSONResponse:
        return await self._submit_ir_job_endpoint(scene_id, request, "validate")

    async def _submit_export_job_endpoint(
        self, scene_id: str, request: Request
    ) -> JSONResponse:
        return await self._submit_ir_job_endpoint(scene_id, request, "export")

    async def _get_job_endpoint(self, job_id: str, request: Request) -> JSONResponse:
        try:
            job = self._job_store.get(job_id)
        except JobNotFoundError as exc:
            return self._scene_error(404, "job_not_found", str(exc))
        return JSONResponse(self._public_job(job))

    async def _cancel_job_endpoint(self, job_id: str, request: Request) -> JSONResponse:
        try:
            job = self._job_store.cancel(job_id)
        except JobNotFoundError as exc:
            return self._scene_error(404, "job_not_found", str(exc))
        except ValueError as exc:
            return self._scene_error(409, "job_not_cancellable", str(exc))
        return JSONResponse(self._public_job(job))

    async def _get_observation_endpoint(
        self, observation_id: str, request: Request
    ) -> JSONResponse:
        job = self._completed_result_job(observation_id, "observe")
        manifest_path = self._safe_result_path(
            self._scene_data_root, job.result.get("manifest_path")
        )
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError) as exc:
            return self._scene_error(500, "observation_result_invalid", str(exc))
        for item in manifest.get("views", []):
            item.pop("path", None)
            item["url"] = f"/v2/observations/{observation_id}/views/{item['view']}"
        self._assert_path_free(manifest)
        return JSONResponse(manifest)

    async def _get_observation_view_endpoint(
        self, observation_id: str, view: str, request: Request
    ) -> Response:
        job = self._completed_result_job(observation_id, "observe")
        selected = next(
            (item for item in job.result.get("views", []) if item.get("view") == view),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=404, detail="Observation view not found")
        path = self._safe_result_path(self._scene_data_root, selected.get("path"))
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Observation file not found")
        media_type = "image/webp" if path.suffix.lower() == ".webp" else "image/png"
        return Response(
            path.read_bytes(),
            media_type=media_type,
            headers={"Content-Disposition": f'inline; filename="{path.name}"'},
        )

    async def _get_export_endpoint(
        self, export_id: str, request: Request
    ) -> StreamingResponse:
        job = self._completed_result_job(export_id, "export")
        path = self._safe_result_path(
            self._scene_output_root, job.result.get("zip_path")
        )
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Export file not found")

        async def chunks():
            with path.open("rb") as source:
                while content := source.read(1024 * 1024):
                    yield content

        return StreamingResponse(
            chunks(),
            media_type="application/zip",
            headers={
                "X-Scene-ID": job.scene_id,
                "X-Scene-Revision": str(job.scene_revision),
                "X-Export-SHA256": str(job.result.get("sha256", "")),
                "Content-Length": str(path.stat().st_size),
                "Content-Disposition": f'attachment; filename="{path.name}"',
            },
        )

    def _completed_result_job(self, job_id: str, expected_type: str):
        try:
            job = self._job_store.get(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Result not found") from exc
        if job.job_type != expected_type:
            raise HTTPException(status_code=404, detail="Result not found")
        if job.status != "completed" or job.result is None:
            raise HTTPException(status_code=409, detail="Result is not ready")
        return job

    @staticmethod
    def _safe_result_path(root: Path, relative: Any) -> Path:
        if not isinstance(relative, str):
            raise HTTPException(status_code=500, detail="Invalid result path")
        resolved_root = root.resolve()
        path = (root / relative).resolve()
        if path != resolved_root and resolved_root not in path.parents:
            raise HTTPException(status_code=500, detail="Invalid result path")
        return path

    @staticmethod
    def _public_job(job) -> dict[str, Any]:
        allowed_request = {
            key: value
            for key, value in job.request.items()
            if key
            in {
                "views",
                "width",
                "height",
                "format",
                "penetration_epsilon",
                "static_static",
                "support_contact_tolerance",
            }
        }
        result = None
        if job.result is not None:
            if job.job_type == "observe":
                result = {
                    "observation_id": job.job_id,
                    "manifest_url": f"/v2/observations/{job.job_id}",
                    "views": [
                        {
                            "view": item.get("view"),
                            "url": f"/v2/observations/{job.job_id}/views/{item.get('view')}",
                        }
                        for item in job.result.get("views", [])
                    ],
                }
            elif job.job_type == "export":
                result = {
                    "export_id": job.job_id,
                    "download_url": f"/v2/exports/{job.job_id}",
                    "sha256": job.result.get("sha256"),
                    "size_bytes": job.result.get("size_bytes"),
                }
            else:
                result = job.result
        payload = {
            "job_id": job.job_id,
            "job_type": job.job_type,
            "scene_id": job.scene_id,
            "scene_revision": job.scene_revision,
            "request": allowed_request,
            "status": job.status,
            "progress": job.progress,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
            "result": result,
            "error": (
                {
                    "code": job.error_code,
                    "message": job.error_message,
                    "retryable": job.retryable,
                }
                if job.error_code
                else None
            ),
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "updated_at": job.updated_at,
        }
        AssetAcquisitionApp._assert_path_free(payload)
        return payload

    def _tools_endpoint(self) -> dict[str, Any]:
        acquisition_routes = (
            {
                "generate": "/v2/generate/{backend}",
                "retrieve": "/v2/retrieve/{source}",
                "materialize": "/v2/retrieve/{source}/{candidate_id}/materialize",
                "assets": "/v2/assets/{digest}",
            }
            if self._advertise_v2_assets
            else {
                "generate": "/generate/{backend}",
                "retrieve": "/v1/retrieve/{source}",
                "assets": "/v1/assets/{source}/{asset_id}",
            }
        )
        result = {
            "enabled": [backend.to_dict() for backend in self._enabled_backend_specs()],
            "all": [backend.to_dict() for backend in self._backend_specs()],
            "routes": acquisition_routes,
            "deprecated_routes": {
                "generate": "/generate/{backend}",
                "retrieve": "/v1/retrieve/{source}",
                "assets": "/v1/assets/{source}/{asset_id}",
            },
        }
        if self._scene_store is not None:
            result["routes"]["scenes"] = "/v1/scenes"
        if self._ir_scene_store is not None:
            result["routes"]["scene_ir"] = "/v2/scenes"
            result["routes"]["scene_ir_schema"] = "/v2/scene-schema"
        if self._job_store is not None:
            result["routes"]["scene_jobs"] = "/v2/jobs/{job_id}"
            result["routes"]["observations"] = "/v2/observations/{observation_id}"
            result["routes"]["exports"] = "/v2/exports/{export_id}"
        return result

    async def _create_scene_endpoint(
        self, package: UploadFile = File(...)
    ) -> JSONResponse:
        if package.content_type not in {
            "application/zip",
            "application/octet-stream",
            None,
        }:
            return self._scene_error(
                415, "unsupported_scene_media_type", "package must be a ZIP file"
            )
        try:
            scene = self._scene_store.create(package.file)
        except ScenePackageError as exc:
            return self._scene_error(422, "invalid_scene_package", str(exc))
        return JSONResponse(
            {
                "scene_id": scene.scene_id,
                "revision": scene.revision,
                "sdf_url": f"/v1/scenes/{scene.scene_id}/sdf",
                "render_url": f"/v1/scenes/{scene.scene_id}/render",
            },
            status_code=201,
        )

    async def _scene_sdf_endpoint(
        self, scene_id: str, revision: int | None = Query(default=None, ge=1)
    ) -> Response:
        try:
            info = self._scene_store.revision(scene_id, revision)
            sdf = self._scene_store.read_sdf(scene_id, info.revision)
        except SceneNotFoundError as exc:
            return self._scene_error(404, "scene_not_found", str(exc))
        return Response(
            sdf,
            media_type="application/xml",
            headers={
                "X-Scene-ID": scene_id,
                "X-Scene-Revision": str(info.revision),
                "ETag": f'"{info.sha256}"',
            },
        )

    async def _update_scene_sdf_endpoint(
        self,
        scene_id: str,
        request: Request,
        x_base_revision: int = Header(alias="X-Base-Revision", ge=1),
    ) -> JSONResponse:
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/xml"
        ):
            return self._scene_error(
                415,
                "unsupported_scene_media_type",
                "SDF updates require application/xml",
            )
        try:
            info = self._scene_store.update_sdf(
                scene_id, await request.body(), base_revision=x_base_revision
            )
        except SceneConflictError as exc:
            return self._scene_error(409, "scene_revision_conflict", str(exc))
        except SceneNotFoundError as exc:
            return self._scene_error(404, "scene_not_found", str(exc))
        except ScenePackageError as exc:
            return self._scene_error(422, "invalid_sdf", str(exc))
        return JSONResponse(
            {
                "scene_id": scene_id,
                "revision": info.revision,
                "sha256": info.sha256,
                "size_bytes": info.size_bytes,
                "validation": {
                    "xml_valid": True,
                    "assets_resolved": True,
                    "warnings": [],
                },
            },
            status_code=201,
        )

    async def _render_scene_endpoint(
        self, scene_id: str, render_request: SceneRenderRequest
    ) -> Response:
        if self._scene_renderer is None:
            return self._scene_error(
                503,
                "render_backend_unavailable",
                "renderer is not configured",
                retryable=True,
            )
        revision = render_request.revision
        options = render_request.model_dump(exclude={"revision"})
        try:
            info = self._scene_store.revision(scene_id, revision)
            package = self._scene_store.build_package(scene_id, info.revision)
            rendered = await self._scene_renderer.render(package, options)
        except SceneNotFoundError as exc:
            return self._scene_error(404, "scene_not_found", str(exc))
        except SceneRendererError as exc:
            return self._scene_error(
                exc.status, exc.error, str(exc), retryable=exc.status >= 500
            )
        return Response(
            rendered,
            media_type="application/zip",
            headers={
                "X-Scene-ID": scene_id,
                "X-Scene-Revision": str(info.revision),
                "Content-Disposition": f'attachment; filename="{scene_id}-r{info.revision}-preview.zip"',
            },
        )

    async def _final_scene_endpoint(
        self, scene_id: str, revision: int | None = Query(default=None, ge=1)
    ) -> Response:
        try:
            info = self._scene_store.revision(scene_id, revision)
            package = self._scene_store.build_package(scene_id, info.revision)
        except SceneNotFoundError as exc:
            return self._scene_error(404, "scene_not_found", str(exc))
        digest = hashlib.sha256(package).hexdigest()
        return Response(
            package,
            media_type="application/zip",
            headers={
                "X-Scene-ID": scene_id,
                "X-Scene-Revision": str(info.revision),
                "X-Scene-SHA256": digest,
                "Content-Disposition": f'attachment; filename="{scene_id}-r{info.revision}.zip"',
            },
        )

    @staticmethod
    def _scene_error(
        status: int, error: str, message: str, *, retryable: bool = False
    ) -> JSONResponse:
        return JSONResponse(
            {"error": error, "message": message, "retryable": retryable},
            status_code=status,
        )

    def _backends_endpoint(self) -> dict[str, Any]:
        backends = self._enabled_backend_specs()
        return {
            "enabled": [backend.to_dict() for backend in backends],
        }

    def _history_endpoint(self) -> dict[str, Any]:
        return {"requests": [asdict(entry) for entry in self._history]}

    def _shutdown_endpoint(self) -> dict[str, str]:
        return {"status": "shutdown_requested"}

    async def _proxy_generate_endpoint(
        self, backend_name: str, request: Request
    ) -> Response:
        backend = self._require_backend(
            backend_name=backend_name,
            expected_role="generate",
        )
        return await self._proxy_request(
            request=request,
            backend=backend,
            upstream_path="/generate_geometries",
        )

    async def _proxy_sam3d_generate_endpoint(self, request: Request) -> JSONResponse:
        backend = self._require_backend("sam3d", expected_role="generate")
        upstream_url = self._upstream_url(backend, "/v1/sam3d/generations")
        headers = self._forward_headers(request, str(uuid.uuid4()))
        timeout = httpx.Timeout(UPSTREAM_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                upstream_url, content=await request.body(), headers=headers
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502, detail="invalid SAM3D response"
            ) from exc
        if response.status_code >= 400:
            return JSONResponse(data, status_code=response.status_code)
        return JSONResponse(
            rewrite_sam3d_download_url(data), headers=self._v1_deprecation_headers()
        )

    async def _proxy_sam3d_asset_endpoint(
        self, asset_id: str, request: Request
    ) -> Response:
        backend = self._require_backend("sam3d", expected_role="generate")
        return await self._proxy_request(
            request=request,
            backend=backend,
            upstream_path=f"/v1/sam3d/assets/{asset_id}",
        )

    async def _generate_v2_endpoint(
        self, backend: str, request: Request
    ) -> JSONResponse:
        """Forward conditioning data only; the producer publishes shared assets."""
        spec = self._require_backend(backend, expected_role="generate")
        try:
            upstream = self._upstream_url(spec, "/v2/generations")
            headers = self._forward_headers(request, str(uuid.uuid4()))
            async with httpx.AsyncClient(
                timeout=UPSTREAM_TIMEOUT_S,
                trust_env=False,
            ) as client:
                response = await client.post(
                    upstream, content=await request.body(), headers=headers
                )
            payload = response.json()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if response.status_code >= 400:
            return JSONResponse(payload, status_code=response.status_code)
        ref = payload.get("asset_ref") or (payload.get("asset") or {}).get("asset_ref")
        if not isinstance(ref, str):
            return self._scene_error(
                502,
                "invalid_generation_result",
                "producer did not publish an asset_ref",
                retryable=True,
            )
        try:
            public = self._public_asset(self._asset_store.resolve(ref))
        except AssetStoreError as exc:
            return self._scene_error(
                502, "unresolved_generation_asset", str(exc), retryable=True
            )
        public["generation_id"] = payload.get("generation_id")
        self._assert_path_free(public)
        return JSONResponse(public, status_code=201)

    async def _retrieve_v2_endpoint(
        self, source: str, request: Request
    ) -> JSONResponse:
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            data = {**data, "download": False}
            if self._retrieval_engine is None:
                return await self._remote_retrieve_v2(source, data, request)
            result = await self._retrieval_engine.retrieve(source, data)
        except KeyError:
            return await self._remote_retrieve_v2(source, data, request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        candidates = []
        for item in result.get("results") or []:
            metadata = dict(item.get("metadata") or {})
            candidate_id = str(item.get("candidate_id") or item.get("asset_id"))
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "category": metadata.get("category", source),
                    "description": metadata.get("description", result.get("query", "")),
                    "dimensions": _dimensions(metadata),
                    "preview_url": metadata.get("preview_url"),
                    "source": source,
                    "score": item.get("score"),
                    "articulation": _articulation(metadata.get("joints") or []),
                    "materialize_url": f"/v2/retrieve/{source}/{candidate_id}/materialize",
                }
            )
        payload = {
            "source": source,
            "query": result.get("query"),
            "candidates": candidates,
        }
        self._assert_path_free(payload)
        return JSONResponse(payload)

    async def _materialize_v2_endpoint(
        self, source: str, candidate_id: str, request: Request
    ) -> JSONResponse:
        try:
            if self._retrieval_engine is None:
                return await self._remote_materialize_v2(source, candidate_id, request)
            stored = self._retrieval_engine.materialize(
                source, candidate_id, self._asset_store
            )
        except KeyError:
            return await self._remote_materialize_v2(source, candidate_id, request)
        except (ValueError, AssetStoreError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = self._public_asset(stored)
        self._assert_path_free(payload)
        return JSONResponse(payload, status_code=201)

    async def _remote_retrieve_v2(
        self, source: str, data: dict[str, Any], request: Request
    ) -> JSONResponse:
        try:
            backend = self._require_backend(source, expected_role="retrieve")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        async with httpx.AsyncClient(
            timeout=UPSTREAM_TIMEOUT_S, trust_env=False
        ) as client:
            response = await client.post(
                self._upstream_url(backend, "/v2/candidates"), json=data
            )
        payload = response.json()
        if response.status_code >= 400:
            return JSONResponse(payload, status_code=response.status_code)
        for candidate in payload.get("candidates", []):
            candidate["materialize_url"] = (
                f"/v2/retrieve/{source}/{candidate['candidate_id']}/materialize"
            )
        self._assert_path_free(payload)
        return JSONResponse(payload)

    async def _remote_materialize_v2(
        self, source: str, candidate_id: str, request: Request
    ) -> JSONResponse:
        backend = self._require_backend(source, expected_role="retrieve")
        try:
            async with httpx.AsyncClient(
                timeout=UPSTREAM_TIMEOUT_S, trust_env=False
            ) as client:
                response = await client.post(
                    self._upstream_url(
                        backend, f"/v2/candidates/{candidate_id}/materialize"
                    )
                )
            payload = response.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if response.status_code >= 400:
            return JSONResponse(payload, status_code=response.status_code)
        try:
            stored = self._asset_store.resolve(payload["asset_ref"])
        except (KeyError, AssetStoreError) as exc:
            raise HTTPException(
                status_code=502, detail="unresolved materialized asset"
            ) from exc
        public = self._public_asset(stored)
        self._assert_path_free(public)
        return JSONResponse(public, status_code=201)

    async def _asset_v2_endpoint(self, digest: str, request: Request) -> JSONResponse:
        try:
            stored = self._asset_store.resolve(f"asset://sha256/{digest}")
        except AssetStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = self._public_asset(stored)
        self._assert_path_free(payload)
        return JSONResponse(payload)

    async def _asset_preview_v2_endpoint(
        self, digest: str, request: Request
    ) -> Response:
        ref = f"asset://sha256/{digest}"
        try:
            preview = self._asset_store.preview_path(ref)
        except AssetStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if preview is None:
            raise HTTPException(status_code=404, detail="Asset preview not found")
        media_type = {
            ".png": "image/png",
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(preview.suffix.lower(), "application/octet-stream")
        return Response(preview.read_bytes(), media_type=media_type)

    @staticmethod
    def _public_asset(stored: StoredAsset) -> dict[str, Any]:
        manifest = stored.manifest
        metadata = manifest.get("metadata") or {}
        source = manifest.get("source") or {}
        return {
            "asset_ref": stored.asset_ref,
            "kind": manifest.get("kind", "object"),
            "category": metadata.get("category", "unknown"),
            "description": metadata.get("description", ""),
            "dimensions": _dimensions_from_bounds(manifest.get("bounds") or {}),
            "preview_url": (
                f"/v2/assets/{stored.digest}/preview"
                if manifest.get("preview") or metadata.get("preview")
                else None
            ),
            "source": {
                key: value
                for key, value in source.items()
                if key
                in {
                    "type",
                    "name",
                    "resource_id",
                    "dataset_version",
                    "conversion_version",
                }
            },
            "articulation": _articulation(manifest.get("joints") or []),
            "license": manifest.get("license") or {},
        }

    @staticmethod
    def _assert_path_free(value: Any, field: str = "response") -> None:
        forbidden_keys = {"mesh_path", "output_dir", "geometry_path"}
        if isinstance(value, dict):
            for key, item in value.items():
                if key in forbidden_keys:
                    raise RuntimeError(f"internal path field leaked at {field}.{key}")
                if key == "download_url" and (
                    not isinstance(item, str) or not item.startswith("/v2/exports/")
                ):
                    raise RuntimeError(f"model download URL leaked at {field}.{key}")
                AssetAcquisitionApp._assert_path_free(item, f"{field}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                AssetAcquisitionApp._assert_path_free(item, f"{field}[{index}]")
        elif isinstance(value, str) and (
            value.startswith("file://")
            or value.startswith(("/home/", "/app/", "/data/", "/outputs/"))
        ):
            raise RuntimeError(f"internal path leaked at {field}")

    async def _retrieve_v1_endpoint(self, source: str, request: Request) -> Any:
        if self._retrieval_engine is None:
            raise HTTPException(status_code=404, detail="Retrieval is not configured")
        try:
            data = await request.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Expected a JSON object"
            ) from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        try:
            num_candidates = int(data.get("num_candidates", 1))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="invalid num_candidates"
            ) from exc
        download_value = data.get("download", False)
        if not isinstance(download_value, bool):
            raise HTTPException(status_code=422, detail="download must be a boolean")
        download = download_value
        if download and num_candidates != 1:
            raise HTTPException(
                status_code=400,
                detail="download=true requires num_candidates=1",
            )
        try:
            result = await self._retrieval_engine.retrieve(source, data)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Unknown source: {source}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not download:
            return JSONResponse(result, headers=self._v1_deprecation_headers())
        results = result.get("results") or []
        if not results:
            raise HTTPException(status_code=404, detail="No retrieval candidates")
        return await self._retrieval_file_response(source, results[0]["asset_id"])

    async def _retrieve_asset_v1_endpoint(
        self, source: str, asset_id: str, request: Request
    ) -> Response:
        if self._retrieval_engine is None:
            raise HTTPException(status_code=404, detail="Retrieval is not configured")
        return await self._retrieval_file_response(source, asset_id)

    async def _retrieval_file_response(self, source: str, asset_id: str) -> Response:
        try:
            asset = self._retrieval_engine.package(source, asset_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Asset not found") from exc
        return Response(
            content=asset.path.read_bytes(),
            media_type="application/zip",
            headers={
                "X-Asset-ID": asset_id,
                "X-Asset-SHA256": asset.sha256,
                "Content-Length": str(asset.size_bytes),
                "Content-Disposition": f'attachment; filename="{asset_id}.zip"',
                **self._v1_deprecation_headers(),
            },
        )

    @staticmethod
    def _v1_deprecation_headers() -> dict[str, str]:
        return {
            "Deprecation": "true",
            "Link": '</tools>; rel="successor-version"',
        }

    async def _generate_assets_endpoint(self, request: Request) -> Any:
        if self._generate_assets_handler is None:
            backend = await self._select_generate_assets_backend(request)
            return await self._proxy_request(
                request=request,
                backend=backend,
                upstream_path="/generate_geometries",
            )

        try:
            data = await request.json()
            request = AssetAcquisitionServerRequest.from_dict(data)
            request.validate()
            response = self._generate_assets_handler(request)
            return response.to_dict()
        except HTTPException:
            raise
        except Exception as e:
            console_logger.exception("Asset acquisition request failed")
            raise HTTPException(status_code=500, detail=str(e)) from e

    async def _select_generate_assets_backend(self, request: Request) -> BackendSpec:
        """Pick the generate backend for the legacy /generate_assets route.

        In gateway-only mode this route is a compatibility alias for the lower-level
        generation API. If exactly one generate backend is enabled, use it. If the
        caller includes "backend" in the JSON body, honor that selection.
        """
        requested_backend = await self._requested_backend_from_body(request)
        if requested_backend:
            return self._require_backend(
                backend_name=requested_backend,
                expected_role="generate",
            )

        generate_backends = [
            backend
            for backend in self._enabled_backend_specs()
            if backend.role == "generate"
        ]
        if len(generate_backends) == 1:
            return generate_backends[0]
        if not generate_backends:
            raise HTTPException(
                status_code=404,
                detail="No enabled generate backend is available for /generate_assets",
            )
        raise HTTPException(
            status_code=400,
            detail=(
                "Multiple generate backends are enabled; include a 'backend' field "
                "or use /generate/{backend}."
            ),
        )

    async def _requested_backend_from_body(self, request: Request) -> str | None:
        try:
            data = await request.json()
        except Exception:
            return None

        if isinstance(data, dict):
            value = data.get("backend") or data.get("backend_name")
            return str(value) if value else None
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                value = item.get("backend") or item.get("backend_name")
                if value:
                    return str(value)
        return None

    def _asset_endpoint(self, asset_id: str) -> FileResponse:
        path = GLOBAL_ARTIFACTS.get(asset_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(
            path, media_type=artifact_media_type(path), filename=path.name
        )

    async def _proxy_request(
        self,
        request: Request,
        backend: BackendSpec,
        upstream_path: str,
        upstream_base_url: str | None = None,
    ) -> StreamingResponse:
        start = time.time()
        request_id = str(uuid.uuid4())
        upstream_url = (
            f"{upstream_base_url}{upstream_path}"
            if upstream_base_url
            else self._upstream_url(backend, upstream_path)
        )

        headers = self._forward_headers(request, request_id)
        body = await request.body()
        timeout = httpx.Timeout(UPSTREAM_TIMEOUT_S)
        http_client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        status_code: int | None = None

        try:
            upstream_request = http_client.build_request(
                request.method,
                upstream_url,
                content=body,
                headers=headers,
                params=request.query_params,
            )
            upstream_response = await http_client.send(upstream_request, stream=True)
            status_code = upstream_response.status_code
            response_headers = self._response_headers(upstream_response)
            media_type = upstream_response.headers.get("content-type")

            self._record_history(
                request=request,
                request_id=request_id,
                backend=backend.name,
                upstream_url=upstream_url,
                status_code=status_code,
                started_at=start,
            )

            async def stream():
                try:
                    async for chunk in upstream_response.aiter_bytes():
                        yield chunk
                finally:
                    await upstream_response.aclose()
                    await http_client.aclose()

            console_logger.info(
                "gateway %s %s -> %s %s",
                request.method,
                request.url.path,
                upstream_url,
                status_code,
            )
            return StreamingResponse(
                stream(),
                status_code=status_code,
                media_type=media_type,
                headers=response_headers,
            )
        except Exception as e:
            await http_client.aclose()
            self._record_history(
                request=request,
                request_id=request_id,
                backend=backend.name,
                upstream_url=upstream_url,
                status_code=status_code,
                started_at=start,
                error=str(e),
            )
            console_logger.exception("Gateway proxy request failed")
            raise HTTPException(status_code=502, detail=str(e)) from e

    def _require_backend(
        self, backend_name: str, expected_role: str | None = None
    ) -> BackendSpec:
        for backend in self._enabled_backend_specs():
            if backend.name != backend_name:
                continue
            if expected_role is not None and backend.role != expected_role:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Backend '{backend_name}' has role '{backend.role}', "
                        f"expected '{expected_role}'"
                    ),
                )
            return backend
        raise HTTPException(
            status_code=404, detail=f"Backend not enabled: {backend_name}"
        )

    def _upstream_url(self, backend: BackendSpec, upstream_path: str) -> str:
        server = backend.config.get("server", {})
        host = server.get("host")
        port = server.get("port")
        if not host or not port:
            raise HTTPException(
                status_code=500,
                detail=f"Backend '{backend.name}' is missing server.host/server.port",
            )
        return f"http://{host}:{port}{upstream_path}"

    def _client_id(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    def _forward_headers(self, request: Request, request_id: str) -> dict[str, str]:
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }
        headers["x-assetserver-request-id"] = request_id
        return headers

    def _response_headers(self, response: httpx.Response) -> dict[str, str]:
        return {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }

    def _record_history(
        self,
        request: Request,
        request_id: str,
        backend: str | None,
        upstream_url: str | None,
        status_code: int | None,
        started_at: float,
        error: str | None = None,
    ) -> None:
        self._history.append(
            GatewayHistoryEntry(
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                backend=backend,
                upstream_url=upstream_url,
                status_code=status_code,
                duration_ms=(time.time() - started_at) * 1000,
                client=self._client_id(request),
                error=error,
            )
        )

    def _enabled_backend_specs(self) -> list[BackendSpec]:
        if self._config is None:
            return []
        return enabled_backend_specs(self._config)

    def _backend_specs(self) -> list[BackendSpec]:
        if self._config is None:
            return []
        return backend_specs(self._config)

    def _runtime_config(self) -> dict[str, Any]:
        if self._config is None or "runtime" not in self._config:
            return {}
        runtime = OmegaConf.to_container(self._config.runtime, resolve=True)
        assert isinstance(runtime, dict)
        return runtime

    def _server_config(self) -> dict[str, Any]:
        if self._config is None or "server" not in self._config:
            return {}
        server = OmegaConf.to_container(self._config.server, resolve=True)
        assert isinstance(server, dict)
        return server

    def _scene_config(self) -> dict[str, Any]:
        return self._server_config().get("scenes", {})

    def _storage_config(self) -> dict[str, Any]:
        return self._server_config().get("storage", {})

    def _job_config(self) -> dict[str, Any]:
        return self._server_config().get("jobs", {})
