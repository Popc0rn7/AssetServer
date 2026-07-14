"""Heavy postprocessing HTTP services (loaded lazily for OMP configuration)."""

from typing import Any

__all__ = ["PostprocessServerApp"]


def __getattr__(name: str) -> Any:
    if name == "PostprocessServerApp":
        from assetserver.postprocess_server.server_app import PostprocessServerApp

        return PostprocessServerApp
    raise AttributeError(name)
