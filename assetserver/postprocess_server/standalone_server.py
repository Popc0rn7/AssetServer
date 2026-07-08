#!/usr/bin/env python3
"""Standalone heavy postprocess server.

Currently this exposes mandatory convex decomposition at /generate_collision.
Additional Blender, SDF, and physics postprocessing endpoints should be added
under this package rather than to generation or retrieval backends.
"""

from assetserver.convex_decomposition_server.standalone_server import main


if __name__ == "__main__":
    main()
