"""Geometry generation server components.

This module contains the complete geometry generation server implementation,
including server infrastructure and both Hunyuan3D and SAM3D backends.

For multi-GPU support, the server automatically detects available GPUs and
spawns one worker process per GPU. Use CUDA_VISIBLE_DEVICES to control which
GPUs are used.

The package root retains only the historical client, DTO, and server exports. They are
loaded lazily so importing a focused submodule does not pull legacy server dependencies
into standalone model images. Pipeline managers and generation functions are internal
implementation details and must be imported from their defining modules.
"""


def __getattr__(name: str):
    """Load compatibility exports only when explicitly requested.

    Besides preventing CUDA initialization in parent processes, this keeps focused
    runtime images such as the standalone SAM3D service from importing the legacy
    HTTP server and its unrelated postprocessing dependencies.
    """
    if name == "GeometryGenerationClient":
        from .client import GeometryGenerationClient

        return GeometryGenerationClient

    if name in {
        "GeometryGenerationServerRequest",
        "GeometryGenerationServerResponse",
    }:
        from . import dataclasses

        return getattr(dataclasses, name)

    if name == "GeometryGenerationServer":
        from .server_manager import GeometryGenerationServer

        return GeometryGenerationServer

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GeometryGenerationClient",
    "GeometryGenerationServer",
    "GeometryGenerationServerRequest",
    "GeometryGenerationServerResponse",
]
