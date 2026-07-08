#!/usr/bin/env python3
"""Send smoke requests to AssetServer generate/retrieve backends."""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

import requests


DEFAULT_PORTS = {
    "sam3d": 7000,
    "hunyuan3d": 7002,
    "hssd": 7001,
    "objaverse": 7007,
}


def parse_dimensions(value: str | None) -> tuple[float, float, float] | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("--dimensions must be WIDTH,DEPTH,HEIGHT")
    return tuple(float(part) for part in parts)  # type: ignore[return-value]


def build_parser(default_backend: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    if default_backend:
        parser.add_argument(
            "backend",
            nargs="?",
            choices=sorted(DEFAULT_PORTS),
            default=default_backend,
        )
    else:
        parser.add_argument("backend", choices=sorted(DEFAULT_PORTS))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--description", required=True)
    parser.add_argument("--image-path", default=None)
    parser.add_argument("--output-dir", default="outputs/request_backend")
    parser.add_argument("--object-type", default="FURNITURE")
    parser.add_argument("--dimensions", default=None)
    parser.add_argument("--num-candidates", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=3600)
    return parser


def stream_request(url: str, payload: list[dict], timeout: int) -> list[dict]:
    response = requests.post(url, json=payload, stream=True, timeout=(10, timeout))
    response.raise_for_status()
    results = []
    for line in response.iter_lines():
        if not line:
            continue
        decoded = json.loads(line.decode("utf-8"))
        print(json.dumps(decoded, indent=2))
        results.append(decoded)
    return results


def request_generation(args: argparse.Namespace) -> list[dict]:
    if not args.image_path:
        raise SystemExit(f"--image-path is required for {args.backend}")
    payload = [
        {
            "image_path": str(Path(args.image_path).expanduser()),
            "output_dir": args.output_dir,
            "prompt": args.description,
            "backend": args.backend,
        }
    ]
    return stream_request(
        f"http://{args.host}:{args.port}/generate_geometries",
        payload,
        args.timeout,
    )


def request_retrieval(args: argparse.Namespace) -> list[dict]:
    payload = [
        {
            "object_description": args.description,
            "object_type": args.object_type,
            "output_dir": args.output_dir,
            "desired_dimensions": parse_dimensions(args.dimensions),
            "num_candidates": args.num_candidates,
        }
    ]
    return stream_request(
        f"http://{args.host}:{args.port}/retrieve_objects",
        payload,
        args.timeout,
    )


def main(default_backend: str | None = None) -> int:
    args = build_parser(default_backend).parse_args()
    args.port = args.port or DEFAULT_PORTS[args.backend]
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    try:
        if args.backend in {"sam3d", "hunyuan3d"}:
            request_generation(args)
        else:
            request_retrieval(args)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        print(detail, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
