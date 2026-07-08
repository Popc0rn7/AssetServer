"""FastAPI application for heavy mesh postprocessing."""

from assetserver.convex_decomposition_server.server_app import (
    ConvexDecompositionServerApp as PostprocessServerApp,
)

__all__ = ["PostprocessServerApp"]
