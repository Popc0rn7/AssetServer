"""Gateway-local retrieval orchestration using a remote embedding provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import numpy as np

from assetserver.config import BackendSpec, project_root

from .assets import AssetCatalog
from .sources import ArticulatedSource, MaterialsSource


class OpenCLIPClient:
    def __init__(self, base_url: str, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def text_embedding(self, text: str) -> np.ndarray:
        async with httpx.AsyncClient(timeout=self.timeout_s, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/v1/embeddings/text", json={"inputs": [text]}
            )
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings") or []
        if len(embeddings) != 1:
            raise RuntimeError("OpenCLIP response has no embedding")
        return np.asarray(embeddings[0], dtype=np.float32)


class RetrievalEngine:
    def __init__(self, *, sources: dict[str, Any], embedding_client: OpenCLIPClient, cache_root: str | Path) -> None:
        self.sources = sources
        self.embedding_client = embedding_client
        self._default_cache_root = Path(cache_root)
        self.catalogs: dict[str, AssetCatalog] = {}

    @classmethod
    def from_config(cls, config: Any) -> "RetrievalEngine | None":
        providers = _as_dict(config.get("embedding_providers", {}))
        openclip = providers.get("openclip")
        if not openclip:
            return None
        sources: dict[str, Any] = {}
        cache_roots: dict[str, Path] = {}
        for spec in _retrieve_specs(config):
            factory = _source_factory_from_spec(spec)
            if factory is not None:
                sources[spec.name] = factory
                raw = _as_dict(spec.config)
                delivery = _as_dict(raw.get("delivery", {}))
                cache_roots[spec.name] = _resolve(
                    delivery.get("cache_dir", f".cache/assetserver/retrieve/{spec.name}")
                )
        if not sources:
            return None
        cache_root = project_root() / ".cache/assetserver/retrieve"
        engine = cls(
            sources=sources,
            embedding_client=OpenCLIPClient(
                str(openclip.get("base_url", "http://127.0.0.1:7006")),
                float(openclip.get("timeout_s", 30)),
            ),
            cache_root=cache_root,
        )
        engine.catalogs = {name: AssetCatalog(path) for name, path in cache_roots.items()}
        return engine

    async def retrieve(self, source_name: str, request: dict) -> dict:
        source_or_factory = self.sources.get(source_name)
        if source_or_factory is None:
            raise KeyError(source_name)
        if callable(source_or_factory):
            source = source_or_factory()
            self.sources[source_name] = source
        else:
            source = source_or_factory
        description = request.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description is required")
        num_candidates = int(request.get("num_candidates", 1))
        if not 1 <= num_candidates <= 20:
            raise ValueError("num_candidates must be between 1 and 20")
        embedding = await self.embedding_client.text_embedding(description)
        candidates = source.search(
            embedding,
            num_candidates=num_candidates,
            object_type=str(request.get("object_type", "FURNITURE")),
            desired_dimensions=request.get("desired_dimensions"),
        )
        results = []
        for candidate in candidates:
            catalog = self.catalogs.setdefault(
                source_name, AssetCatalog(self._default_cache_root / source_name)
            )
            asset_id = catalog.register(source.describe_asset(candidate))
            results.append(
                {
                    "asset_id": asset_id,
                    "score": candidate.score,
                    "metadata": candidate.metadata,
                    "download_url": f"/v1/assets/{source_name}/{asset_id}",
                }
            )
        return {"source": source_name, "query": description, "results": results}

    def package(self, source_name: str, asset_id: str):
        catalog = self.catalogs.get(source_name)
        descriptor = catalog.descriptor(asset_id) if catalog else None
        if descriptor is None or descriptor.source != source_name:
            raise KeyError(asset_id)
        return catalog.package(asset_id)


def _retrieve_specs(config: Any) -> list[BackendSpec]:
    from assetserver.config import enabled_backend_specs

    return [item for item in enabled_backend_specs(config) if item.role == "retrieve"]


def _as_dict(value: Any) -> dict:
    from omegaconf import OmegaConf

    if isinstance(value, dict):
        return value
    return OmegaConf.to_container(value, resolve=True) or {}


def _source_factory_from_spec(spec: BackendSpec):
    raw = _as_dict(spec.config)
    dataset = _as_dict(raw.get("dataset", {}))
    if spec.type == "materials":
        return lambda: MaterialsSource(
            data_root=_resolve(dataset["root"]), embeddings_root=_resolve(dataset["embeddings"])
        )
    if spec.type == "articulated":
        source_configs = _as_dict(raw.get("sources", {}))
        sources = {
            name: {
                "data_root": _resolve(item["root"]),
                "embeddings_root": _resolve(item["embeddings"]),
            }
            for name, item in source_configs.items()
            if item.get("enabled", True)
        }
        search = _as_dict(raw.get("search", {}))
        return lambda: ArticulatedSource(
            sources=sources, clip_pool_size=int(search.get("clip_pool_size", 5))
        )
    return None


def _resolve(path: str | Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else project_root() / value
