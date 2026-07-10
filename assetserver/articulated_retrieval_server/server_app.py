"""FastAPI application for articulated object retrieval."""

from __future__ import annotations

import logging
import time
import uuid

from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from assetserver.articulated_retrieval.config import (
    ArticulatedConfig,
    ArticulatedSourceConfig,
)
from assetserver.articulated_retrieval.retrieval import ArticulatedRetriever
from assetserver.scheduler import StrictRoundRobinScheduler

from .dataclasses import (
    ArticulatedRetrievalResult,
    ArticulatedRetrievalServerRequest,
    ArticulatedRetrievalServerResponse,
    StreamedResult,
)

console_logger = logging.getLogger(__name__)


class ArticulatedRetrievalApp:
    """ASGI app for CLIP retrieval over ArtVIP/PartNet-style SDF libraries."""

    def __init__(
        self,
        preload_retriever: bool = True,
        articulated_config: ArticulatedConfig | None = None,
        clip_device: str | None = None,
    ) -> None:
        self._retriever: ArticulatedRetriever | None = None
        self._articulated_config = articulated_config
        self._clip_device = clip_device
        self._scheduler = StrictRoundRobinScheduler()
        self._processing_thread: Thread | None = None
        self._processing_active = False
        self._current_processing: str | None = None
        self._total_requests = 0
        self._completed_requests = 0
        self._failed_requests = 0
        self._request_times: list[float] = []
        self.app = FastAPI(title="AssetServer Articulated Retrieval")
        self._register_routes()

        if preload_retriever:
            self._get_retriever()

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)

    def _register_routes(self) -> None:
        self.app.add_api_route("/health", self._health_endpoint, methods=["GET"])
        self.app.add_api_route("/shutdown", self._shutdown_endpoint, methods=["POST"])
        self.app.add_api_route(
            "/retrieve_objects", self._retrieve_objects_endpoint, methods=["POST"]
        )

    def _get_retriever(self) -> ArticulatedRetriever:
        if self._retriever is None:
            config = self._articulated_config
            if config is None:
                config = ArticulatedConfig(
                    sources={
                        "artvip": ArticulatedSourceConfig(
                            name="artvip",
                            enabled=True,
                            data_path=Path("data/artvip_sdf"),
                            embeddings_path=Path("data/artvip_sdf/embeddings"),
                        )
                    },
                    use_top_k=5,
                )
            self._retriever = ArticulatedRetriever(
                config=config, clip_device=self._clip_device
            )
            if not self._retriever.initialize():
                raise RuntimeError("Articulated retriever initialization failed")
        return self._retriever

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
        self._retriever = None

    def _process_queue(self) -> None:
        while self._processing_active:
            request = self._scheduler.get_next_request()
            if request is None:
                time.sleep(0.1)
                continue
            try:
                self._current_processing = (
                    f"{request.client_id}[{request.request_index}]: "
                    f"{request.request.object_description}"
                )
                start_time = time.time()
                result = self._retrieve_internal(request.request)
                request.callback(request.request_index, ("success", result.to_dict()))
                self._completed_requests += 1
                self._request_times.append(time.time() - start_time)
                if len(self._request_times) > 100:
                    self._request_times.pop(0)
            except Exception as exc:
                request.callback(request.request_index, ("error", str(exc)))
                self._failed_requests += 1
            finally:
                self._current_processing = None

    def _retrieve_internal(
        self, request: ArticulatedRetrievalServerRequest
    ) -> ArticulatedRetrievalServerResponse:
        retriever = self._get_retriever()
        candidates = retriever.retrieve(
            description=request.object_description,
            object_type=request.object_type,
            desired_dimensions=list(request.desired_dimensions)
            if request.desired_dimensions
            else None,
            top_k=request.num_candidates,
        )
        if not candidates:
            raise ValueError(f"No candidates found for '{request.object_description}'")
        Path(request.output_dir).mkdir(parents=True, exist_ok=True)

        results = [
            ArticulatedRetrievalResult(
                mesh_path=str(candidate.sdf_path),
                sdf_path=str(candidate.sdf_path),
                object_id=candidate.object_id,
                source=candidate.source,
                description=candidate.description,
                clip_score=float(candidate.clip_score),
                bbox_score=float(candidate.bbox_score),
                bounding_box_min=candidate.bounding_box_min,
                bounding_box_max=candidate.bounding_box_max,
            )
            for candidate in candidates
        ]
        return ArticulatedRetrievalServerResponse(
            results=results, query_description=request.object_description
        )

    def _health_endpoint(self) -> dict[str, Any]:
        avg_processing_time = (
            sum(self._request_times) / len(self._request_times)
            if self._request_times
            else None
        )
        return {
            "status": "healthy",
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
            "retriever_loaded": self._retriever is not None,
        }

    def _shutdown_endpoint(self) -> dict[str, str]:
        return {"status": "shutdown_requested"}

    def _retrieve_objects_endpoint(
        self, data: list[dict[str, Any]]
    ) -> StreamingResponse:
        batch_requests = [ArticulatedRetrievalServerRequest(**item) for item in data]
        batch_id = batch_requests[0].scene_id if batch_requests[0].scene_id else str(uuid.uuid4())
        client_result_queue: Queue = Queue()
        results_received = 0
        batch_size = len(batch_requests)

        def result_callback(index: int, result: tuple[str, dict]) -> None:
            client_result_queue.put((index, result))

        self._scheduler.add_batch(
            client_id=batch_id,
            requests=batch_requests,
            callback=result_callback,
            received_timestamp=time.time(),
        )
        self._total_requests += batch_size

        def generate():
            nonlocal results_received
            while results_received < batch_size:
                index, (status, result_data) = client_result_queue.get()
                if status == "success":
                    streamed = StreamedResult(index=index, status=status, data=result_data)
                else:
                    streamed = StreamedResult(index=index, status=status, error=result_data)
                yield streamed.to_json() + "\n"
                results_received += 1

        return StreamingResponse(generate(), media_type="application/x-ndjson")
