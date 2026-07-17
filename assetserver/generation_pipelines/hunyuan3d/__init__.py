"""Hunyuan3D generation pipeline."""

from .pipeline import Hunyuan3DPipeline


def create_pipeline(config: dict) -> Hunyuan3DPipeline:
    return Hunyuan3DPipeline(config)


__all__ = ["Hunyuan3DPipeline", "create_pipeline"]
