"""FastAPI application for Objaverse/ObjectThor semantic retrieval."""

import logging
import time
import uuid

from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any

import numpy as np

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from assetserver.artifacts import GLOBAL_ARTIFACTS, artifact_media_type
from assetserver.objaverse_retrieval.retrieval import ObjaverseRetriever
from assetserver.postprocess.collision import generate_collision_artifacts
from assetserver.scheduler import QueuedRequest, StrictRoundRobinScheduler

from .dataclasses import (
    ObjaverseRetrievalResult,
    ObjaverseRetrievalServerRequest,
    ObjaverseRetrievalServerResponse,
    StreamedResult,
)

console_logger = logging.getLogger(__name__)


class ObjaverseRetrievalApp:
    """ASGI app for Objaverse retrieval with round-robin scheduling."""

    def __init__(
        self,
        preload_retriever: bool = True,
        objaverse_data_path: str | None = None,
        objaverse_preprocessed_path: str | None = None,
        objaverse_top_k: int = 5,
        clip_device: str | None = None,
    ) -> None:
        self._retriever: ObjaverseRetriever | None = None
        self._objaverse_data_path = objaverse_data_path
        self._objaverse_preprocessed_path = objaverse_preprocessed_path
        self._objaverse_top_k = objaverse_top_k
        self._clip_device = clip_device
        self._scheduler = StrictRoundRobinScheduler()
        self._processing_thread: Thread | None = None
        self._processing_active = False
        self._current_processing: str | None = None
        self._total_requests = 0
        self._completed_requests = 0
        self._failed_requests = 0
        self._request_times: list[float] = []
        self.app = FastAPI(title="AssetServer Objaverse Retrieval")
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
        self.app.add_api_route(
            "/assets/{asset_id}", self._asset_endpoint, methods=["GET"]
        )

    def _get_retriever(self) -> ObjaverseRetriever:
        if self._retriever is None:
            import os

            from assetserver.objaverse_retrieval.config import ObjaverseConfig

            data_path = Path(
                self._objaverse_data_path
                or os.environ.get("OBJAVERSE_DATA_PATH", "data/objathor-assets")
            )
            preprocessed_path = Path(
                self._objaverse_preprocessed_path
                or os.environ.get(
                    "OBJAVERSE_PREPROCESSED_PATH",
                    "data/objathor-assets/preprocessed",
                )
            )
            project_root = Path(__file__).parent.parent.parent.parent
            if not data_path.is_absolute():
                data_path = project_root / data_path
            if not preprocessed_path.is_absolute():
                preprocessed_path = project_root / preprocessed_path

            config = ObjaverseConfig(
                data_path=data_path,
                preprocessed_path=preprocessed_path,
                use_top_k=self._objaverse_top_k,
                object_type_mapping=None,
            )
            self._retriever = ObjaverseRetriever(
                config=config, clip_device=self._clip_device
            )
        return self._retriever

    def start_processing(self) -> None:
        if self._processing_active:
            return
        self._processing_active = True
        self._processing_thread = Thread(target=self._process_queue, daemon=False)
        self._processing_thread.start()

    def stop_processing(self) -> None:
        if not self._processing_active:
            return
        self._processing_active = False
        if self._processing_thread and self._processing_thread.is_alive():
            self._processing_thread.join(timeout=5)
        self._retriever = None

    def _process_queue(self) -> None:
        try:
            while self._processing_active:
                request = self._scheduler.get_next_request()
                if request:
                    self._process_round_robin_request(request)
                else:
                    time.sleep(0.1)
        except Exception as e:
            console_logger.error("Processing queue failed: %s", e)
        finally:
            self._current_processing = None

    def _process_round_robin_request(self, queued_request: QueuedRequest) -> None:
        try:
            self._current_processing = (
                f"{queued_request.client_id}[{queued_request.request_index}]: "
                f"{queued_request.request.object_description}"
            )
            start_time = time.time()
            result = self._retrieve_internal(queued_request.request)
            queued_request.callback(
                queued_request.request_index, ("success", result.to_dict())
            )
            self._completed_requests += 1
            self._request_times.append(time.time() - start_time)
            if len(self._request_times) > 100:
                self._request_times.pop(0)
        except Exception as e:
            queued_request.callback(queued_request.request_index, ("error", str(e)))
            self._failed_requests += 1
        finally:
            self._current_processing = None

    def _retrieve_internal(
        self, request: ObjaverseRetrievalServerRequest
    ) -> ObjaverseRetrievalServerResponse:
        retriever = self._get_retriever()
        desired_dimensions = (
            np.array(request.desired_dimensions) if request.desired_dimensions else None
        )
        candidates = retriever.retrieve_multiple(
            description=request.object_description,
            object_type=request.object_type,
            desired_dimensions=desired_dimensions,
            max_candidates=request.num_candidates,
        )
        if not candidates:
            raise ValueError(f"No candidates found for '{request.object_description}'")
        if not request.output_dir:
            raise ValueError("output_dir must be specified in request")

        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        category = retriever.config.object_type_mapping.get(
            request.object_type.upper(), "unknown"
        )
        results: list[ObjaverseRetrievalResult] = []
        for candidate in candidates:
            mesh_path = output_dir / f"{candidate.uid}.glb"
            candidate.mesh.export(str(mesh_path))
            artifact = GLOBAL_ARTIFACTS.register(mesh_path)
            collision = generate_collision_artifacts(mesh_path)
            results.append(
                ObjaverseRetrievalResult(
                    mesh_path=str(mesh_path),
                    objaverse_uid=candidate.uid,
                    object_name=request.object_description,
                    similarity_score=float(candidate.clip_score),
                    size=tuple(candidate.mesh.extents.tolist()),
                    category=category,
                    asset_id=artifact["asset_id"],
                    download_url=artifact["download_url"],
                    collision=collision,
                )
            )
        return ObjaverseRetrievalServerResponse(
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

    def _asset_endpoint(self, asset_id: str) -> FileResponse:
        path = GLOBAL_ARTIFACTS.get(asset_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(
            path, media_type=artifact_media_type(path), filename=path.name
        )

    def _retrieve_objects_endpoint(self, data: list[dict[str, Any]]):
        if not isinstance(data, list):
            raise HTTPException(status_code=400, detail="Expected a list of requests")
        if not data:
            raise HTTPException(status_code=400, detail="Empty request list")
        required_fields = ["object_description", "object_type", "output_dir"]
        for index, request_data in enumerate(data):
            if not isinstance(request_data, dict):
                raise HTTPException(
                    status_code=400, detail=f"Request {index} is not an object"
                )
            missing = [field for field in required_fields if field not in request_data]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Request {index} missing field: {missing[0]}",
                )

        batch_requests = [ObjaverseRetrievalServerRequest(**req) for req in data]
        batch_id = batch_requests[0].scene_id or str(uuid.uuid4())
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
                streamed_result = (
                    StreamedResult(index=index, status="success", data=result_data)
                    if status == "success"
                    else StreamedResult(index=index, status="error", error=result_data)
                )
                yield streamed_result.to_json() + "\n"
                results_received += 1

        return StreamingResponse(generate(), media_type="application/x-ndjson")
