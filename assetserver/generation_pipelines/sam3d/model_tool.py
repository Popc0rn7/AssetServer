"""Create or verify a SAM3D offline model bundle manifest."""

from __future__ import annotations

import argparse

from pathlib import Path

from .model_bundle import ModelBundleError, create_manifest, validate_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--bundle-version", default="sam3d-v1")
    args = parser.parse_args()
    try:
        bundle = (
            create_manifest(args.path, args.bundle_version)
            if args.create
            else validate_bundle(args.path).manifest
        )
    except ModelBundleError as exc:
        parser.error(str(exc))
    print(f"SAM3D model bundle valid: {args.path} " f"({len(bundle['files'])} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
