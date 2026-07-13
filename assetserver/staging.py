"""Crash-recovery cleanup for producer job staging directories."""

from __future__ import annotations

import shutil
import time

from pathlib import Path


def cleanup_staging(root: str | Path, *, ttl_seconds: float = 24 * 60 * 60) -> int:
    directory = Path(root)
    if not directory.is_dir():
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    for child in directory.iterdir():
        try:
            if child.name.startswith(".") or child.stat().st_mtime >= cutoff:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    return removed
