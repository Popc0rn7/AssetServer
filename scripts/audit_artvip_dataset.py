#!/usr/bin/env python3
"""Audit the supported subset of a SceneSmith-preprocessed ArtVIP tree."""

from __future__ import annotations

import argparse
import json

from assetserver.retrieval.artvip import audit_artvip_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default="data/artvip_sdf")
    args = parser.parse_args()
    print(json.dumps(audit_artvip_dataset(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
