"""Mesh postprocessing helpers, imported lazily by lightweight runtimes."""

from typing import Any

__all__ = [
    "convert_gltf_to_glb",
    "load_mesh_as_trimesh",
    "remove_mesh_floaters",
    "scale_mesh_uniformly_to_dimensions",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from assetserver import mesh_utils

        return getattr(mesh_utils, name)
    raise AttributeError(name)
