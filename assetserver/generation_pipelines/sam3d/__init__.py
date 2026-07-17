"""SAM3D generation pipeline."""

from .pipeline import SAM3DPipeline


def create_pipeline(config: dict) -> SAM3DPipeline:
    return SAM3DPipeline(config)


__all__ = ["SAM3DPipeline", "create_pipeline"]
