"""Config-driven Materials and Articulated dataset adapters."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .assets import AssetDescriptor
from .artvip import ArtVipContractError, inspect_artvip_sdf


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    source: str
    resource_key: str
    score: float
    metadata: dict[str, Any]
    asset_path: Path


def _similarities(query: np.ndarray, embeddings: np.ndarray) -> np.ndarray:
    query = query / np.linalg.norm(query)
    norms = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)
    return norms @ query


def _texture_files(folder: Path) -> dict[str, Path] | None:
    files = [path for path in folder.iterdir() if path.is_file()]

    def find(names: tuple[str, ...]) -> Path | None:
        return next(
            (path for path in files if any(name in path.name.lower() for name in names)),
            None,
        )

    textures = {
        "color": find(("color", "albedo", "diffuse")),
        "normal": find(("normalgl", "normal")),
        "roughness": find(("roughness", "rough")),
    }
    return None if any(path is None for path in textures.values()) else textures  # type: ignore[return-value]


class MaterialsSource:
    name = "materials"

    def __init__(self, *, data_root: str | Path, embeddings_root: str | Path) -> None:
        self.data_root = Path(data_root).resolve()
        embeddings_root = Path(embeddings_root)
        self.embeddings = np.load(embeddings_root / "clip_embeddings.npy")
        self.index = yaml.safe_load((embeddings_root / "embedding_index.yaml").read_text())
        self.metadata = yaml.safe_load((embeddings_root / "metadata_index.yaml").read_text())

    def search(self, embedding: np.ndarray, *, num_candidates: int, **_: Any) -> list[Candidate]:
        scores = _similarities(embedding, self.embeddings)
        results = []
        for index in np.argsort(scores)[::-1]:
            material_id = str(self.index[int(index)])
            folder = self.data_root / material_id
            textures = _texture_files(folder) if folder.is_dir() else None
            if textures is None:
                continue
            metadata = dict(self.metadata.get(material_id) or {})
            metadata["channels"] = {key: path.name for key, path in textures.items()}
            results.append(Candidate(self.name, material_id, float(scores[index]), metadata, folder))
            if len(results) >= num_candidates:
                break
        return results

    def describe_asset(self, candidate: Candidate) -> AssetDescriptor:
        textures = _texture_files(candidate.asset_path)
        if textures is None:
            raise RuntimeError(f"material textures missing: {candidate.resource_key}")
        return AssetDescriptor(
            source=self.name,
            resource_key=candidate.resource_key,
            root=candidate.asset_path,
            files=tuple(sorted(textures.values())),
            metadata=candidate.metadata,
        )


class ArticulatedSource:
    name = "articulated"

    def __init__(self, *, sources: dict[str, dict], clip_pool_size: int = 5) -> None:
        self.clip_pool_size = clip_pool_size
        self.records: list[dict] = []
        embeddings = []
        for source_name, config in sources.items():
            data_root = Path(config["data_root"]).resolve()
            embeddings_root = Path(config["embeddings_root"])
            vectors = np.load(embeddings_root / "clip_embeddings.npy")
            index = yaml.safe_load((embeddings_root / "embedding_index.yaml").read_text())
            metadata = yaml.unsafe_load((embeddings_root / "metadata_index.yaml").read_text())
            for offset, object_id in enumerate(index):
                item = dict(metadata[object_id])
                item.update(
                    source=source_name,
                    object_id=str(object_id),
                    data_root=data_root,
                    sdf_path=(data_root / item["sdf_path"]).resolve(),
                    dataset_version=str(config.get("dataset_version", "unknown")),
                    conversion_version=str(config.get("conversion_version", "assetserver-p1")),
                    frame=config.get("frame"),
                )
                self.records.append(item)
                embeddings.append(vectors[offset])
        self.embeddings = np.asarray(embeddings)

    @staticmethod
    def _matches_type(item: dict, object_type: str) -> bool:
        placement = item.get("placement_type") or _placement(item.get("placement_options", {}))
        if object_type == "FURNITURE":
            return not item.get("is_manipuland", False) and placement == "floor"
        if object_type == "MANIPULAND":
            return bool(item.get("is_manipuland", False))
        if object_type == "WALL_MOUNTED":
            return placement == "wall"
        if object_type == "CEILING_MOUNTED":
            return placement == "ceiling"
        return True

    def search(
        self,
        embedding: np.ndarray,
        *,
        num_candidates: int,
        object_type: str = "FURNITURE",
        desired_dimensions: list[float] | None = None,
        **_: Any,
    ) -> list[Candidate]:
        scores = _similarities(embedding, self.embeddings)
        eligible = [index for index, item in enumerate(self.records) if self._matches_type(item, object_type)]
        ranked = sorted(eligible, key=lambda index: scores[index], reverse=True)
        pool = []
        layouts = {}
        for index in ranked:
            item = self.records[index]
            try:
                layout = inspect_artvip_sdf(item["sdf_path"])
            except ArtVipContractError as exc:
                LOGGER.warning(
                    "skipping unsupported ArtVIP candidate %s: %s",
                    item["object_id"],
                    exc.reason,
                )
                continue
            pool.append(index)
            layouts[index] = layout
            if len(pool) >= max(self.clip_pool_size, num_candidates):
                break
        if desired_dimensions is not None:
            pool.sort(key=lambda index: _bbox_distance(self.records[index], desired_dimensions))
        results = []
        for index in pool[:num_candidates]:
            item = self.records[index]
            layout = layouts[index]
            metadata = {
                "category": item.get("category", "articulated"),
                "description": item.get("description", ""),
                "bounding_box_min": item["bounding_box_min"],
                "bounding_box_max": item["bounding_box_max"],
                "base_link": layout.base_link,
                "model_name": layout.model_name,
                "links": list(layout.links),
                "joints": list(layout.joints),
                "visual_parts": list(layout.visual_parts),
                "articulation": {
                    "articulated": layout.articulated,
                    "joint_count": len(layout.joints),
                    "joints": [joint["name"] for joint in layout.joints],
                },
                "dataset_version": item["dataset_version"],
                "conversion_version": item["conversion_version"],
                "frame": item["frame"],
            }
            results.append(
                Candidate(
                    self.name,
                    f"{item['source']}:{item['object_id']}",
                    float(scores[index]),
                    metadata,
                    item["sdf_path"],
                )
            )
        return results

    def describe_asset(self, candidate: Candidate) -> AssetDescriptor:
        layout = inspect_artvip_sdf(candidate.asset_path)
        return AssetDescriptor(
            self.name,
            candidate.resource_key,
            layout.root,
            layout.dependency_files,
            candidate.metadata,
            dataset_version=candidate.metadata["dataset_version"],
            conversion_version=candidate.metadata["conversion_version"],
            frame=candidate.metadata.get("frame"),
            file_aliases=layout.dependency_aliases,
        )


def _placement(options: dict) -> str:
    for key, value in (("on_floor", "floor"), ("on_wall", "wall"), ("on_ceiling", "ceiling"), ("on_object", "on_object")):
        if options.get(key):
            return value
    return "floor"


def _bbox_distance(item: dict, desired: list[float]) -> float:
    actual = np.asarray(item["bounding_box_max"]) - np.asarray(item["bounding_box_min"])
    return float(np.abs(actual - np.asarray(desired)).sum())
