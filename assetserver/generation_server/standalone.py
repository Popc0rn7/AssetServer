"""Load one configured generation pipeline and serve it with Uvicorn."""

from __future__ import annotations

import argparse
import importlib

from pathlib import Path
from typing import Any

import uvicorn

from omegaconf import OmegaConf

from .app import create_app
from .protocol import GenerationPipeline
from .runtime import GenerationRuntime


def load_pipeline_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"generation config does not exist: {config_path}")
    loaded = OmegaConf.load(config_path)
    config = OmegaConf.to_container(loaded, resolve=True)
    if not isinstance(config, dict):
        raise ValueError("generation config must be a YAML mapping")
    if config.get("role") != "generate":
        raise ValueError("generation config role must be generate")
    name = config.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("generation config name is required")
    generation = config.get("generation")
    if not isinstance(generation, dict):
        raise ValueError("generation config section is required")
    module_name = generation.get("pipeline")
    if not isinstance(module_name, str) or not module_name:
        raise ValueError("generation.pipeline must be a module name")
    if not isinstance(generation.get("preload"), bool):
        raise ValueError("generation.preload must be a boolean")
    if not isinstance(generation.get("model"), dict):
        raise ValueError("generation.model must be a mapping")
    if not isinstance(generation.get("defaults", {}), dict):
        raise ValueError("generation.defaults must be a mapping")
    server = config.get("server")
    if not isinstance(server, dict) or not isinstance(server.get("port"), int):
        raise ValueError("server.port must be an integer")
    return config


def create_runtime(config: dict[str, Any]) -> GenerationRuntime:
    generation = config["generation"]
    module = importlib.import_module(generation["pipeline"])
    factory = getattr(module, "create_pipeline", None)
    if not callable(factory):
        raise ValueError(
            f"pipeline module {generation['pipeline']} has no create_pipeline factory"
        )
    pipeline = factory(config)
    if not isinstance(pipeline, GenerationPipeline):
        raise TypeError("create_pipeline returned an incompatible pipeline")
    if pipeline.name != config["name"]:
        raise ValueError(
            f"pipeline name {pipeline.name!r} does not match config name {config['name']!r}"
        )
    return GenerationRuntime(pipeline, preload=generation["preload"])


def build_app(config_path: str | Path):
    config = load_pipeline_config(config_path)
    runtime = create_runtime(config)
    storage = config.get("storage") or {}
    return create_app(
        runtime=runtime,
        asset_root=storage.get("asset_root"),
        staging_root=storage.get("staging_root"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    config = load_pipeline_config(args.config)
    runtime = create_runtime(config)
    storage = config.get("storage") or {}
    app = create_app(
        runtime=runtime,
        asset_root=storage.get("asset_root"),
        staging_root=storage.get("staging_root"),
    )
    uvicorn.run(
        app,
        host=args.host or config["server"].get("host", "127.0.0.1"),
        port=args.port or config["server"]["port"],
    )


if __name__ == "__main__":
    main()
