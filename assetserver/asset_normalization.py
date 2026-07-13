"""Explicit dataset coordinate conversions used by source adapters."""

from __future__ import annotations

import numpy as np


def normalize_y_up_mesh(mesh):
    """Convert a metre/right-handed/Y-up dataset mesh to ground-centred Z-up."""
    rotation = np.array(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, -1.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )
    output = mesh.copy()
    output.apply_transform(rotation)
    minimum, maximum = output.bounds
    translation = np.eye(4)
    translation[:3, 3] = [
        -(minimum[0] + maximum[0]) / 2,
        -(minimum[1] + maximum[1]) / 2,
        -minimum[2],
    ]
    output.apply_transform(translation)
    return output, (translation @ rotation).tolist()


def y_up_source_frame(transform_to_asset: list[list[float]]) -> dict:
    return {
        "units": "m",
        "handedness": "right",
        "up_axis": "+Y",
        "origin": "dataset-defined",
        "transform_to_asset": transform_to_asset,
    }


def inspect_y_up_glb(path):
    """Return canonical bounds and the declared conversion without rewriting GLB."""
    import trimesh

    source = trimesh.load(path, force="scene")
    canonical, transform = normalize_y_up_mesh(source)
    return (
        {
            "min": [float(value) for value in canonical.bounds[0]],
            "max": [float(value) for value in canonical.bounds[1]],
        },
        y_up_source_frame(transform),
    )
