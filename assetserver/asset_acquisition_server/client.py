import logging
import time

import requests

from .dataclasses import (
    AssetAcquisitionServerRequest,
    AssetAcquisitionServerResponse,
)

console_logger = logging.getLogger(__name__)


class AssetAcquisitionClient:
    """Client for the unified asset acquisition HTTP backend."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7010) -> None:
        self.base_url = f"http://{host}:{port}"
        self.session = requests.Session()

    def generate_assets(
        self,
        request: AssetAcquisitionServerRequest,
        max_retries: int = 3,
        timeout_s: int = 3600,
    ) -> AssetAcquisitionServerResponse:
        request.validate()

        for attempt in range(max_retries):
            try:
                response = self.session.post(
                    f"{self.base_url}/generate_assets",
                    json=request.to_dict(),
                    timeout=(10, timeout_s),
                )
                response.raise_for_status()
                return AssetAcquisitionServerResponse.from_dict(response.json())
            except requests.exceptions.ConnectionError as e:
                if attempt < max_retries - 1:
                    console_logger.warning(
                        "Asset acquisition server connection failed; retrying "
                        f"({attempt + 1}/{max_retries})"
                    )
                    time.sleep(min(2**attempt, 30))
                    continue
                raise ConnectionError(
                    f"Failed to connect to asset acquisition server at {self.base_url}"
                ) from e
            except requests.exceptions.HTTPError as e:
                try:
                    body = e.response.json()
                    detail = body.get("error", body.get("detail", str(e)))
                except ValueError:
                    detail = str(e)
                raise RuntimeError(f"Asset acquisition server error: {detail}") from e
            except requests.exceptions.Timeout as e:
                raise TimeoutError("Asset acquisition request timed out") from e

        raise RuntimeError("Asset acquisition request failed unexpectedly")

    def health_check(self) -> bool:
        try:
            response = self.session.get(f"{self.base_url}/health", timeout=5)
            response.raise_for_status()
            return True
        except Exception as e:
            console_logger.warning(f"Health check failed: {e}")
            return False
