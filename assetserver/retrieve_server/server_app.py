"""FastAPI application for unified retrieval."""

from __future__ import annotations

import logging
import time
import uuid

from queue import Queue
from threading import Thread
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from assetserver.artifacts import GLOBAL_ARTIFACTS, artifact_media_type
from assetserver.clip_embeddings import get_text_embedding
from assetserver.config import BackendSpec
from assetserver.scheduler import QueuedRequest, StrictRoundRobinScheduler

from .backends import BaseRetrieveBackend, create_backend
from .dataclasses import StreamedResult

console_logger = logging.getLogger(__name__)


class RetrieveServerApp:
    """ASGI app that routes retrieval requests to configured source backends."""

    def __init__(
        self,
        backend_specs: list[BackendSpec],
        preload_retrievers: bool = True,
        clip_device: str | None = None,
        warmup_openclip: bool = True,
    ) -> None:
        self._clip_device = clip_device
        self._scheduler = StrictRoundRobinScheduler()
        self._processing_thread: Thread | None = None
        self._processing_active = False
        self._current_processing: str | None = None
        self._total_requests = 0
        self._completed_requests = 0
        self._failed_requests = 0
        self._request_times: list[float] = []
        self._backends = self._build_backends(backend_specs)
        self._openclip_warmed = False
        self.app = FastAPI(title="AssetServer Retrieve Server")
        self._register_routes()

        if warmup_openclip:
            self._warmup_openclip()
        if preload_retrievers:
            self.preload_backends()

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)

    def _register_routes(self) -> None:
        self.app.add_api_route("/health", self._health_endpoint, methods=["GET"])
        self.app.add_api_route("/sources", self._sources_endpoint, methods=["GET"])
        self.app.add_api_route("/shutdown", self._shutdown_endpoint, methods=["POST"])
        self.app.add_api_route(
            "/retrieve/{source}", self._retrieve_source_endpoint, methods=["POST"]
        )
        self.app.add_api_route(
            "/retrieve_objects", self._retrieve_objects_endpoint, methods=["POST"]
        )
        self.app.add_api_route(
            "/retrieve_materials", self._retrieve_materials_endpoint, methods=["POST"]
        )
        self.app.add_api_route(
            "/assets/{asset_id}", self._asset_endpoint, methods=["GET"]
        )

    def _build_backends(
        self, backend_specs: list[BackendSpec]
    ) -> dict[str, BaseRetrieveBackend]:
        backends = {}
        for spec in backend_specs:
            if spec.role != "retrieve" or not spec.enabled:
                continue
            backend = create_backend(spec=spec, clip_device=self._clip_device)
            if backend is not None:
                backends[backend.name] = backend
        return backends

    def preload_backends(self) -> None:
        for backend in self._backends.values():
            backend.load()

    def start_processing(self) -> None:
        if self._processing_active:
            return
        self._processing_active = True
        self._processing_thread = Thread(target=self._process_queue, daemon=False)
        self._processing_thread.start()

    def stop_processing(self) -> None:
        self._processing_active = False
        if self._processing_thread and self._processing_thread.is_alive():
            self._processing_thread.join(timeout=5)

    def _process_queue(self) -> None:
        while self._processing_active:
            request = self._scheduler.get_next_request()
            if request is None:
                time.sleep(0.1)
                continue
            self._process_request(request)

    def _process_request(self, queued_request: QueuedRequest) -> None:
        try:
            backend_name, source_request = queued_request.request
            backend = self._require_backend(backend_name)
            description = getattr(
                source_request,
                "object_description",
                getattr(source_request, "material_description", backend_name),
            )
            self._current_processing = (
                f"{backend_name}:{queued_request.client_id}"
                f"[{queued_request.request_index}]: {description}"
            )
            start_time = time.time()
            result = backend.retrieve(source_request)
            queued_request.callback(queued_request.request_index, ("success", result))
            self._completed_requests += 1
            self._request_times.append(time.time() - start_time)
            if len(self._request_times) > 100:
                self._request_times.pop(0)
        except Exception as exc:
            queued_request.callback(queued_request.request_index, ("error", str(exc)))
            self._failed_requests += 1
        finally:
            self._current_processing = None

    def _health_endpoint(self) -> dict[str, Any]:
        avg_processing_time = (
            sum(self._request_times) / len(self._request_times)
            if self._request_times
            else None
        )
        return {
            "status": "healthy",
            "sources": {
                name: backend.health() for name, backend in self._backends.items()
            },
            "openclip_loaded": self._openclip_warmed,
            "scheduler_queue_size": self._scheduler.get_queue_size(),
            "active_clients": self._scheduler.get_client_count(),
            "pending_requests": self._scheduler.get_queue_size()
            + (1 if self._current_processing else 0),
            "currently_processing": self._current_processing,
            "total_requests": self._total_requests,
            "completed_requests": self._completed_requests,
            "failed_requests": self._failed_requests,
            "processing_active": self._processing_active,
            "avg_processing_time_seconds": avg_processing_time,
        }

    def _sources_endpoint(self) -> dict[str, Any]:
        return {"sources": [backend.health() for backend in self._backends.values()]}

    def _shutdown_endpoint(self) -> dict[str, str]:
        return {"status": "shutdown_requested"}

    def _asset_endpoint(self, asset_id: str) -> FileResponse:
        path = GLOBAL_ARTIFACTS.get(asset_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(
            path, media_type=artifact_media_type(path), filename=path.name
        )

    def _retrieve_source_endpoint(
        self, source: str, data: list[dict[str, Any]]
    ) -> StreamingResponse:
        return self._stream_batch(source=source, data=data)

    def _retrieve_objects_endpoint(
        self,
        data: list[dict[str, Any]],
        source: str = Query(default="hssd"),
    ) -> StreamingResponse:
        return self._stream_batch(source=source, data=data)

    def _retrieve_materials_endpoint(
        self, data: list[dict[str, Any]]
    ) -> StreamingResponse:
        return self._stream_batch(source="materials", data=data)

    def _stream_batch(
        self, source: str, data: list[dict[str, Any]]
    ) -> StreamingResponse:
        backend = self._require_backend(source)
        if not isinstance(data, list):
            raise HTTPException(status_code=400, detail="Expected a list of requests")
        if not data:
            raise HTTPException(status_code=400, detail="Empty request list")

        try:
            batch_requests = [backend.parse_request(item) for item in data]
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        first_request = batch_requests[0]
        batch_id = getattr(first_request, "scene_id", None) or str(uuid.uuid4())
        client_result_queue: Queue = Queue()
        results_received = 0
        batch_size = len(batch_requests)

        def result_callback(index: int, result: tuple[str, dict]) -> None:
            client_result_queue.put((index, result))

        self._scheduler.add_batch(
            client_id=f"{source}:{batch_id}",
            requests=[(source, request) for request in batch_requests],
            callback=result_callback,
            received_timestamp=time.time(),
        )
        self._total_requests += batch_size

        def generate():
            nonlocal results_received
            while results_received < batch_size:
                index, (status, result_data) = client_result_queue.get()
                if status == "success":
                    streamed = StreamedResult(
                        index=index, status=status, data=result_data
                    )
                else:
                    streamed = StreamedResult(
                        index=index, status=status, error=result_data
                    )
                yield streamed.to_json() + "\n"
                results_received += 1

        return StreamingResponse(generate(), media_type="application/x-ndjson")

    def _require_backend(self, source: str) -> BaseRetrieveBackend:
        backend = self._backends.get(source)
        if backend is None:
            raise HTTPException(
                status_code=404, detail=f"Retrieve source not enabled: {source}"
            )
        return backend

    def _warmup_openclip(self) -> None:
        get_text_embedding(
            "__assetserver_retrieve_server_warmup__", device=self._clip_device
        )
        self._openclip_warmed = True
