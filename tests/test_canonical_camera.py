import math

import pytest

from assetserver.blender_scene_worker import canonical_perspective_distance


def test_canonical_distance_scales_with_asset_bounds():
    small = canonical_perspective_distance(
        (0.5, 0.6, 1.0),
        (1.5, -1.5, 1.1),
        vertical_fov_degrees=39.6,
        aspect_ratio=1.0,
        margin=1.15,
    )
    large = canonical_perspective_distance(
        (1.0, 1.2, 2.0),
        (1.5, -1.5, 1.1),
        vertical_fov_degrees=39.6,
        aspect_ratio=1.0,
        margin=1.15,
    )

    assert large == pytest.approx(small * 2)
    old_minimum_radius_distance = math.sqrt(3.0**2 + 3.0**2 + 2.2**2)
    assert small < old_minimum_radius_distance


def test_canonical_distance_fits_every_aabb_corner():
    extent = (0.5, 0.6, 1.0)
    direction = (1.5, -1.5, 1.1)
    margin = 1.15
    fov = 39.6
    distance = canonical_perspective_distance(
        extent,
        direction,
        vertical_fov_degrees=fov,
        aspect_ratio=1.0,
        margin=margin,
    )

    length = math.sqrt(sum(value * value for value in direction))
    outward = tuple(value / length for value in direction)
    right = (-outward[1], outward[0], 0.0)
    right_length = math.sqrt(sum(value * value for value in right))
    right = tuple(value / right_length for value in right)
    up = (
        outward[1] * right[2] - outward[2] * right[1],
        outward[2] * right[0] - outward[0] * right[2],
        outward[0] * right[1] - outward[1] * right[0],
    )
    tangent = math.tan(math.radians(fov) / 2)

    for x in (-extent[0] / 2, extent[0] / 2):
        for y in (-extent[1] / 2, extent[1] / 2):
            for z in (-extent[2] / 2, extent[2] / 2):
                corner = (x, y, z)
                depth = distance - sum(corner[i] * outward[i] for i in range(3))
                projected_x = abs(sum(corner[i] * right[i] for i in range(3))) / depth
                projected_y = abs(sum(corner[i] * up[i] for i in range(3))) / depth
                assert projected_x <= tangent / margin
                assert projected_y <= tangent / margin
