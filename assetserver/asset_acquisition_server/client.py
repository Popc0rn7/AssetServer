import logging
import requests

console_logger = logging.getLogger(__name__)


class AssetAcquisitionClient:
    """Client for the unified asset acquisition HTTP backend."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7010) -> None:
        self.base_url = f"http://{host}:{port}"
        self.session = requests.Session()

    def health_check(self) -> bool:
        try:
            response = self.session.get(f"{self.base_url}/health", timeout=5)
            response.raise_for_status()
            return True
        except Exception as e:
            console_logger.warning(f"Health check failed: {e}")
            return False
