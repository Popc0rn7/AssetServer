"""Client for materials retrieval server."""

import json

from typing import Iterator

import requests

from .dataclasses import (
    MaterialsRetrievalServerRequest,
    MaterialsRetrievalServerResponse,
    StreamedResult,
)


class MaterialsRetrievalClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 7004):
        self.base_url = f"http://{host}:{port}"
        self.session = requests.Session()

    def retrieve_materials(
        self,
        retrieval_requests: list[MaterialsRetrievalServerRequest],
        timeout_s: int = 3600,
    ) -> Iterator[tuple[int, MaterialsRetrievalServerResponse]]:
        response = self.session.post(
            f"{self.base_url}/retrieve_objects",
            json=[req.to_dict() for req in retrieval_requests],
            stream=True,
            timeout=(10, timeout_s),
        )
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            streamed = StreamedResult(**json.loads(line.decode("utf-8")))
            if streamed.status == "error":
                raise RuntimeError(streamed.error)
            if streamed.data is None:
                raise RuntimeError("Server returned success without data")
            yield streamed.index, MaterialsRetrievalServerResponse.from_dict(
                streamed.data
            )
