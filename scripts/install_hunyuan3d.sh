#!/bin/bash

# Install Hunyuan3D-2 into the current uv environment.
# Expected layout:
#   external/Hunyuan3D-2/
#
# This mirrors the SceneSmith installer because assetserver imports hy3dgen
# directly from the Hunyuan3D-2 package.

set -euo pipefail

cd external/Hunyuan3D-2

uv pip install -e .
cd hy3dgen/texgen/custom_rasterizer
uv run --active python setup.py install
cd ../../..
cd hy3dgen/texgen/differentiable_renderer
uv run --active python setup.py install
