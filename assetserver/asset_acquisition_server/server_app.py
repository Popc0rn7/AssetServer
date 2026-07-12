import logging
import os
import time
import uuid

from asyncio import to_thread
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from omegaconf import OmegaConf

from assetserver.artifacts import GLOBAL_ARTIFACTS, artifact_media_type
from assetserver.config import BackendSpec, backend_specs, enabled_backend_specs

from .dataclasses import (
    AssetAcquisitionServerRequest,
    AssetAcquisitionServerResponse,
)
from .docker_manager import DockerBackendManager

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


class AssetAcquisitionApp:
    """FastAPI gateway for routing model traffic to backend services."""

    def __init__(
        self,
        generate_assets_handler: GenerateAssetsHandler | None = None,
        config: Any | None = None,
        retrieval_engine: Any | None = None,
    ) -> None:
        self._generate_assets_handler = generate_assets_handler
        self._config = config
        if retrieval_engine is None and config is not None:
            from assetserver.retrieval import RetrievalEngine

            retrieval_engine = RetrievalEngine.from_config(config)
        self._retrieval_engine = retrieval_engine
        self._docker_manager = DockerBackendManager(config)
        self._history: deque[GatewayHistoryEntry] = deque(
            maxlen=self._gateway_int("history_max_entries", 500)
        )
        self._rate_window: dict[str, deque[float]] = {}
        self.app = FastAPI(title="AssetServer Gateway")
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

    def _health_endpoint(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "mode": "gateway",
            "handler_configured": self._generate_assets_handler is not None,
            "enabled_backends": len(self._enabled_backend_specs()),
            "gateway": self._gateway_config(),
            "runtime": self._runtime_config(),
            "docker": {
                "launch_backend": self._docker_manager.launch_backend,
            },
        }

    def _tools_endpoint(self) -> dict[str, Any]:
        return {
            "enabled": [backend.to_dict() for backend in self._enabled_backend_specs()],
            "all": [backend.to_dict() for backend in self._backend_specs()],
            "routes": {
                "generate": "/generate/{backend}",
                "retrieve": "/v1/retrieve/{source}",
                "assets": "/v1/assets/{source}/{asset_id}",
            },
        }

    def _backends_endpoint(self) -> dict[str, Any]:
        backends = self._enabled_backend_specs()
        return {
            "enabled": [backend.to_dict() for backend in backends],
            "docker": {
                "launch_backend": self._docker_manager.launch_backend,
                "services": self._docker_manager.service_statuses(backends),
            },
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

    async def _proxy_sam3d_generate_endpoint(
        self, request: Request
    ) -> JSONResponse:
        backend = self._require_backend("sam3d", expected_role="generate")
        self._authorize(request)
        self._check_rate_limit(self._client_id(request))
        try:
            await to_thread(self._docker_manager.ensure_backend_running, backend)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        upstream_url = self._upstream_url(backend, "/v1/sam3d/generations")
        headers = self._forward_headers(request, str(uuid.uuid4()))
        timeout = httpx.Timeout(self._gateway_float("request_timeout_s", 3600.0))
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                upstream_url, content=await request.body(), headers=headers
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="invalid SAM3D response") from exc
        if response.status_code >= 400:
            return JSONResponse(data, status_code=response.status_code)
        return JSONResponse(rewrite_sam3d_download_url(data))

    async def _proxy_sam3d_asset_endpoint(
        self, asset_id: str, request: Request
    ) -> Response:
        backend = self._require_backend("sam3d", expected_role="generate")
        return await self._proxy_request(
            request=request,
            backend=backend,
            upstream_path=f"/v1/sam3d/assets/{asset_id}",
        )

    async def _retrieve_v1_endpoint(self, source: str, request: Request) -> Any:
        if self._retrieval_engine is None:
            raise HTTPException(status_code=404, detail="Retrieval is not configured")
        self._authorize(request)
        self._check_rate_limit(self._client_id(request))
        try:
            self._docker_manager.ensure_named_service_running("openclip")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="OpenCLIP is unavailable") from exc
        try:
            data = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Expected a JSON object") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        try:
            num_candidates = int(data.get("num_candidates", 1))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid num_candidates") from exc
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
            raise HTTPException(status_code=404, detail=f"Unknown source: {source}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not download:
            return JSONResponse(result)
        results = result.get("results") or []
        if not results:
            raise HTTPException(status_code=404, detail="No retrieval candidates")
        return await self._retrieval_file_response(source, results[0]["asset_id"])

    async def _retrieve_asset_v1_endpoint(
        self, source: str, asset_id: str, request: Request
    ) -> Response:
        if self._retrieval_engine is None:
            raise HTTPException(status_code=404, detail="Retrieval is not configured")
        self._authorize(request)
        return await self._retrieval_file_response(source, asset_id)

    async def _retrieval_file_response(
        self, source: str, asset_id: str
    ) -> Response:
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
            },
        )

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
        ensure_backend: bool = True,
    ) -> StreamingResponse:
        start = time.time()
        request_id = str(uuid.uuid4())
        client_id = self._client_id(request)

        self._authorize(request)
        self._check_rate_limit(client_id)
        if ensure_backend:
            try:
                await to_thread(self._docker_manager.ensure_backend_running, backend)
            except Exception as e:
                console_logger.exception(
                    "Failed to ensure Docker backend '%s' is running", backend.name
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Failed to start backend '{backend.name}' via Docker: {e}"
                    ),
                ) from e

        upstream_url = (
            f"{upstream_base_url}{upstream_path}"
            if upstream_base_url
            else self._upstream_url(backend, upstream_path)
        )

        headers = self._forward_headers(request, request_id)
        body = await request.body()
        timeout = httpx.Timeout(self._gateway_float("request_timeout_s", 3600.0))
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

    def _authorize(self, request: Request) -> None:
        api_key = self._gateway_value("api_key")
        if not api_key:
            return
        bearer = request.headers.get("authorization", "")
        token = request.headers.get("x-assetserver-key")
        if bearer.startswith("Bearer "):
            token = bearer.removeprefix("Bearer ").strip()
        if token != api_key:
            raise HTTPException(status_code=401, detail="Invalid gateway API key")

    def _check_rate_limit(self, client_id: str) -> None:
        rate_cfg = self._gateway_config().get("rate_limit", {})
        if not rate_cfg.get("enabled", False):
            return
        limit = int(rate_cfg.get("max_requests_per_minute", 60))
        now = time.time()
        window = self._rate_window.setdefault(client_id, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            raise HTTPException(status_code=429, detail="Gateway rate limit exceeded")
        window.append(now)

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

    def _gateway_config(self) -> dict[str, Any]:
        if self._config is None or "gateway" not in self._config:
            return {}
        gateway = OmegaConf.to_container(self._config.gateway, resolve=True)
        assert isinstance(gateway, dict)
        return gateway

    def _gateway_value(self, key: str) -> Any:
        if key == "api_key":
            return self._gateway_config().get(key) or os.environ.get(
                "ASSETSERVER_API_KEY"
            )
        return self._gateway_config().get(key)

    def _gateway_int(self, key: str, default: int) -> int:
        value = self._gateway_value(key)
        return int(value) if value is not None else default

    def _gateway_float(self, key: str, default: float) -> float:
        value = self._gateway_value(key)
        return float(value) if value is not None else default
