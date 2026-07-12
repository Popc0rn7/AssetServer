#!/usr/bin/env python3
"""Generate one SAM3D asset through the explicit backend API."""

import argparse
import json

from pathlib import Path

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:7000")
    parser.add_argument("--prompt")
    args = parser.parse_args()
    mode = "object_description" if args.prompt else "foreground"
    with args.image.open("rb") as stream:
        response = requests.post(
            f"{args.url.rstrip('/')}/v1/sam3d/generations",
            files={"image": (args.image.name, stream)},
            data={"mode": mode, "prompt": args.prompt or ""},
            timeout=(10, 3600),
        )
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
