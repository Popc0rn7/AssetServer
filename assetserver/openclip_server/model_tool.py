from __future__ import annotations

import argparse

from .model_bundle import create_manifest, validate_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    if args.create:
        create_manifest(args.path)
    bundle = validate_bundle(args.path)
    print(f"OpenCLIP model bundle valid: {bundle.model} ({bundle.revision})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
