#!/usr/bin/env python3
"""Real Gateway smoke test for Materials and Articulated retrieval.

Run after OpenCLIP and Gateway are ready. This script issues real HTTP requests,
checks candidate metadata, and optionally downloads/validates one ZIP per source.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile

from pathlib import Path

import requests


DEFAULT_GATEWAY_URL = "http://127.0.0.1:7010"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    value.add_argument("--source", choices=["all", "materials", "articulated"], default="all")
    value.add_argument("--timeout", type=int, default=3600)
    value.add_argument("--download", action="store_true", help="Download and inspect ZIP assets")
    value.add_argument("--output-dir", default="outputs/retrieve_gateway_smoke")
    value.add_argument("--material-query", default="warm wooden floor")
    value.add_argument("--articulated-query", default="wooden wardrobe cabinet")
    return value


def request_json(url: str, payload: dict, timeout: int) -> dict:
    response = requests.post(url, json=payload, timeout=(10, timeout))
    response.raise_for_status()
    data = response.json()
    if not data.get("results"):
        raise RuntimeError(f"no candidates returned by {url}")
    return data


def verify_download(url: str, payload: dict, output: Path, timeout: int) -> None:
    response = requests.post(url, json={**payload, "download": True}, timeout=(10, timeout))
    response.raise_for_status()
    output.write_bytes(response.content)
    with zipfile.ZipFile(output) as archive:
        if "manifest.json" not in archive.namelist():
            raise RuntimeError(f"ZIP has no manifest: {output}")
    print(f"validated ZIP: {output}")


def main() -> int:
    args = parser().parse_args()
    root = args.gateway_url.rstrip("/")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        health = requests.get(f"{root}/health", timeout=10)
        health.raise_for_status()
        print(json.dumps(health.json(), indent=2))
        sources = ["materials", "articulated"] if args.source == "all" else [args.source]
        for source in sources:
            payload = (
                {"description": args.material_query, "num_candidates": 1}
                if source == "materials"
                else {
                    "description": args.articulated_query,
                    "object_type": "FURNITURE",
                    "desired_dimensions": [1.2, 0.55, 1.8],
                    "num_candidates": 1,
                }
            )
            url = f"{root}/v1/retrieve/{source}"
            result = request_json(url, payload, args.timeout)
            (output_dir / f"{source}.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            if args.download:
                verify_download(url, payload, output_dir / f"{source}.zip", args.timeout)
    except (requests.RequestException, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"Retrieve smoke failed: {exc}", file=sys.stderr)
        return 1
    print("Retrieve Gateway smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
