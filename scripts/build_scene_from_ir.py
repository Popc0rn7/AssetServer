#!/usr/bin/env python3
"""Execute a compiled Blender recipe inside the scene-viewer image."""

import argparse

from assetserver.blender_scene_worker import build_blend


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe")
    parser.add_argument("output")
    args = parser.parse_args()
    build_blend(args.recipe, args.output)


if __name__ == "__main__":
    main()
