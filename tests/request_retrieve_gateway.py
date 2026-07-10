#!/usr/bin/env python3
"""Smoke request for gateway retrieve backends.

Run this after the gateway and retrieve backend(s) are running.
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path
from typing import Any

import requests


DEFAULT_GATEWAY_URL = "http://127.0.0.1:7010"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    parser.add_argument(
        "--backend",
        choices=["all", "articulated", "materials"],
        default="all",
    )
    parser.add_argument("--output-dir", default="outputs/retrieve_gateway_smoke")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--num-candidates", type=int, default=1)
    parser.add_argument("--articulated-query", default="wooden wardrobe cabinet")
    parser.add_argument("--material-query", default="warm wooden floor")
    return parser


def print_json_get(url: str, timeout: int = 5) -> None:
    print(f"\nGET {url}")
    try:
        response = requests.get(url, timeout=timeout)
        print(f"HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            print(json.dumps(response.json(), indent=2))
        else:
            print(response.text[:1000])
    except requests.RequestException as exc:
        print(f"unreachable: {exc}")


def request_gateway(
    gateway_url: str,
    backend: str,
    payload: list[dict[str, Any]],
    timeout: int,
    output_dir: Path,
) -> list[dict[str, Any]]:
    url = f"{gateway_url.rstrip('/')}/retrieve/{backend}"
    print(f"\nPOST {url}")
    print(json.dumps(payload, indent=2))

    response = requests.post(
        url,
        json=payload,
        stream=True,
        timeout=(10, timeout),
    )
    if response.status_code >= 400:
        print(response.text, file=sys.stderr)
        response.raise_for_status()

    rows: list[dict[str, Any]] = []
    for line in response.iter_lines():
        if not line:
            continue
        row = json.loads(line.decode("utf-8"))
        print(json.dumps(row, indent=2))
        rows.append(row)

    if not rows:
        raise RuntimeError(f"{backend} returned no NDJSON rows")

    for row in rows:
        if row.get("status") != "success":
            raise RuntimeError(f"{backend} failed: {row.get('error')}")
        data = row.get("data") or {}
        results = data.get("results") or []
        if not results:
            raise RuntimeError(f"{backend} returned success without results")

    response_path = output_dir / f"{backend}_response.json"
    response_path.write_text(json.dumps(rows, indent=2) + "\n")
    print(f"Wrote response: {response_path}")

    return rows


def articulated_payload(
    output_dir: Path,
    query: str,
    num_candidates: int,
) -> list[dict[str, Any]]:
    return [
        {
            "scene_id": "retrieve-gateway-smoke",
            "object_description": query,
            "object_type": "FURNITURE",
            "desired_dimensions": [1.2, 0.55, 1.8],
            "output_dir": str(output_dir / "articulated"),
            "num_candidates": num_candidates,
        }
    ]


def materials_payload(
    output_dir: Path,
    query: str,
    num_candidates: int,
) -> list[dict[str, Any]]:
    return [
        {
            "scene_id": "retrieve-gateway-smoke",
            "material_description": query,
            "output_dir": str(output_dir / "materials"),
            "num_candidates": num_candidates,
        }
    ]


def main() -> int:
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print_json_get(f"{args.gateway_url.rstrip('/')}/health")
    print_json_get(f"{args.gateway_url.rstrip('/')}/backends")

    backends = (
        ["articulated", "materials"] if args.backend == "all" else [args.backend]
    )
    try:
        for backend in backends:
            if backend == "articulated":
                payload = articulated_payload(
                    output_dir=output_dir,
                    query=args.articulated_query,
                    num_candidates=args.num_candidates,
                )
            else:
                payload = materials_payload(
                    output_dir=output_dir,
                    query=args.material_query,
                    num_candidates=args.num_candidates,
                )
            request_gateway(
                gateway_url=args.gateway_url,
                backend=backend,
                payload=payload,
                timeout=args.timeout,
                output_dir=output_dir,
            )
    except requests.RequestException as exc:
        print(f"HTTP request failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Retrieve smoke failed: {exc}", file=sys.stderr)
        return 1

    print("\nRetrieve gateway smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
