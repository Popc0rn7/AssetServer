"""FastAPI application for geometry generation with multi-GPU workers."""

import logging
import time
import uuid

from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from assetserver.artifacts import GLOBAL_ARTIFACTS, artifact_media_type
from assetserver.geometry_generation_server.worker_pool import GPUWorkerPool
from assetserver.postprocess.collision import generate_collision_artifacts
from assetserver.scheduler import StrictRoundRobinScheduler

from .dataclasses import GeometryGenerationServerRequest, StreamedResult

console_logger = logging.getLogger(__name__)


class GeometryGenerationApp:
    """ASGI app that routes generation requests to a GPU worker pool."""

    def __init__(
        self,
        use_mini: bool = False,
        backend: str = "sam3d",
        sam3d_config: dict | None = None,
        preload_pipeline: bool = True,
        log_file: Path | None = None,
    ) -> None:
        self._use_mini = use_mini
        self._backend = backend
        self._sam3d_config = sam3d_config
        self._preload_pipeline = preload_pipeline
        self._scheduler = StrictRoundRobinScheduler()
        self._worker_pool = GPUWorkerPool(
            use_mini=use_mini,
            backend=backend,
            sam3d_config=sam3d_config,
            preload_pipeline=preload_pipeline,
            log_file=log_file,
        )
        self._processing_thread: Thread | None = None
        self._processing_active = False
        self.app = FastAPI(title="AssetServer Geometry Generation")
        self._register_routes()

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)

    def _register_routes(self) -> None:
        self.app.add_api_route("/health", self._health_endpoint, methods=["GET"])
        self.app.add_api_route("/shutdown", self._shutdown_endpoint, methods=["POST"])
        self.app.add_api_route(
            "/generate_geometries",
            self._generate_geometries_endpoint,
            methods=["POST"],
        )
        self.app.add_api_route(
            "/assets/{asset_id}", self._asset_endpoint, methods=["GET"]
        )

    def start_processing(self) -> None:
        if self._processing_active:
            console_logger.warning("Processing already active")
            return

        console_logger.info("Starting geometry generation processing...")
        self._worker_pool.start()
        console_logger.info(
            "Started worker pool with %s GPU(s)", self._worker_pool.num_workers
        )
        self._processing_active = True
        self._processing_thread = Thread(target=self._process_queue, daemon=False)
        self._processing_thread.start()

    def stop_processing(self) -> None:
        if not self._processing_active:
            return

        console_logger.info("Stopping geometry generation processing...")
        self._processing_active = False
        if self._processing_thread and self._processing_thread.is_alive():
            self._processing_thread.join(timeout=5)
            if self._processing_thread.is_alive():
                console_logger.warning("Coordinator thread did not stop gracefully")
        self._worker_pool.stop()

    def _process_queue(self) -> None:
        try:
            while self._processing_active:
                queued_request = self._scheduler.get_next_request()
                if queued_request:
                    self._worker_pool.submit_request(
                        request=queued_request.request,
                        callback=queued_request.callback,
                        request_index=queued_request.request_index,
                        received_timestamp=queued_request.received_timestamp,
                    )
                else:
                    time.sleep(0.1)
        except Exception as e:
            console_logger.error("Coordinator thread failed: %s", e)

    def _health_endpoint(self) -> dict[str, Any]:
        pool_stats = self._worker_pool.get_stats()
        return {
            "status": "healthy",
            "num_workers": pool_stats.num_workers,
            "scheduler_queue_size": self._scheduler.get_queue_size(),
            "active_clients": self._scheduler.get_client_count(),
            "processing_active": self._processing_active,
            "total_requests": pool_stats.total_requests,
            "completed_requests": pool_stats.completed_requests,
            "failed_requests": pool_stats.failed_requests,
            "avg_processing_time_seconds": pool_stats.avg_processing_time_s,
            "avg_end_to_end_latency_seconds": pool_stats.avg_end_to_end_latency_s,
            "avg_queue_wait_seconds": pool_stats.avg_queue_wait_s,
            "max_queue_wait_seconds": pool_stats.max_queue_wait_s,
            "workers": pool_stats.worker_details,
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

    def _generate_geometries_endpoint(self, data: list[dict[str, Any]]):
        if not isinstance(data, list):
            raise HTTPException(status_code=400, detail="Expected a list of requests")
        if not data:
            raise HTTPException(status_code=400, detail="Empty request list")

        required_fields = ["image_path", "output_dir", "prompt"]
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

        batch_requests = [GeometryGenerationServerRequest(**req) for req in data]
        first_scene_id = batch_requests[0].scene_id if batch_requests else None
        batch_id = first_scene_id if first_scene_id else str(uuid.uuid4())
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

        def generate():
            nonlocal results_received
            while results_received < batch_size:
                index, (status, result_data) = client_result_queue.get()
                if status == "success":
                    try:
                        result_data = dict(result_data)
                        artifact = GLOBAL_ARTIFACTS.register(
                            result_data["geometry_path"]
                        )
                        result_data.update(
                            {
                                "asset_id": artifact["asset_id"],
                                "download_url": artifact["download_url"],
                                "collision": generate_collision_artifacts(
                                    result_data["geometry_path"]
                                ),
                            }
                        )
                        streamed_result = StreamedResult(
                            index=index, status="success", data=result_data
                        )
                    except Exception as e:
                        console_logger.exception("Mandatory postprocess failed")
                        streamed_result = StreamedResult(
                            index=index, status="error", error=str(e)
                        )
                else:
                    streamed_result = StreamedResult(
                        index=index, status="error", error=result_data
                    )
                yield streamed_result.to_json() + "\n"
                results_received += 1

        return StreamingResponse(generate(), media_type="application/x-ndjson")
