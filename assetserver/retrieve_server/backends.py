"""Backend adapters for the unified retrieve server."""

from __future__ import annotations

import logging
import shutil

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

from omegaconf import DictConfig, OmegaConf

from assetserver.artifacts import GLOBAL_ARTIFACTS
from assetserver.articulated_retrieval.config import (
    ArticulatedConfig,
    ArticulatedSourceConfig,
)
from assetserver.articulated_retrieval.retrieval import ArticulatedRetriever
from assetserver.articulated_retrieval_server.dataclasses import (
    ArticulatedRetrievalResult,
    ArticulatedRetrievalServerRequest,
    ArticulatedRetrievalServerResponse,
)
from assetserver.config import BackendSpec, project_root
from assetserver.hssd_retrieval.config import HssdConfig
from assetserver.hssd_retrieval.retrieval import HssdRetriever
from assetserver.hssd_retrieval_server.dataclasses import (
    HssdRetrievalResult,
    HssdRetrievalServerRequest,
    HssdRetrievalServerResponse,
)
from assetserver.materials_retrieval.config import MaterialsConfig
from assetserver.materials_retrieval.retrieval import MaterialsRetriever
from assetserver.materials_retrieval_server.dataclasses import (
    MaterialRetrievalResult,
    MaterialsRetrievalServerRequest,
    MaterialsRetrievalServerResponse,
)
from assetserver.postprocess.collision import generate_collision_artifacts

console_logger = logging.getLogger(__name__)


class BaseRetrieveBackend(ABC):
    """Adapter interface for a single retrieval source."""

    def __init__(self, spec: BackendSpec, clip_device: str | None = None) -> None:
        self.spec = spec
        self.name = spec.name
        self.clip_device = clip_device or _params(spec).get("clip_device")

    @abstractmethod
    def load(self) -> None:
        """Load source metadata/retriever state."""

    @abstractmethod
    def is_loaded(self) -> bool:
        """Return whether this backend has loaded its retriever."""

    @abstractmethod
    def parse_request(self, data: dict[str, Any]) -> Any:
        """Convert raw JSON request data to the source-specific request DTO."""

    @abstractmethod
    def retrieve(self, request: Any) -> dict[str, Any]:
        """Retrieve assets/materials and return a serializable response dict."""

    def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.spec.type,
            "enabled": self.spec.enabled,
            "loaded": self.is_loaded(),
        }


class HssdRetrieveBackend(BaseRetrieveBackend):
    """HSSD source adapter."""

    def __init__(self, spec: BackendSpec, clip_device: str | None = None) -> None:
        super().__init__(spec=spec, clip_device=clip_device)
        self._retriever: HssdRetriever | None = None

    def load(self) -> None:
        if self._retriever is not None:
            return
        params = _params(self.spec)
        data_path = _resolve_path(params.get("hssd_data_path", "data/hssd-models"))
        preprocessed_path = _resolve_path(
            params.get("hssd_preprocessed_path", "data/preprocessed")
        )
        config = HssdConfig(
            data_path=data_path,
            preprocessed_path=preprocessed_path,
            use_top_k=int(params.get("hssd_top_k", 5)),
            object_type_mapping=None,
        )
        self._retriever = HssdRetriever(config=config, clip_device=self.clip_device)

    def is_loaded(self) -> bool:
        return self._retriever is not None

    def parse_request(self, data: dict[str, Any]) -> HssdRetrievalServerRequest:
        return HssdRetrievalServerRequest(**data)

    def retrieve(self, request: HssdRetrievalServerRequest) -> dict[str, Any]:
        self.load()
        assert self._retriever is not None
        desired_dimensions = (
            np.array(request.desired_dimensions) if request.desired_dimensions else None
        )
        candidates = self._retriever.retrieve_multiple(
            description=request.object_description,
            object_type=request.object_type,
            desired_dimensions=desired_dimensions,
            max_candidates=request.num_candidates,
        )
        if not candidates:
            raise ValueError(f"No candidates found for '{request.object_description}'")
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        category = self._retriever.config.object_type_mapping.get(
            request.object_type.upper(), "unknown"
        )
        results: list[HssdRetrievalResult] = []
        for candidate in candidates:
            mesh_path = output_dir / f"{candidate.mesh_id}.glb"
            candidate.mesh.export(str(mesh_path))
            artifact = GLOBAL_ARTIFACTS.register(mesh_path)
            collision = generate_collision_artifacts(mesh_path)
            results.append(
                HssdRetrievalResult(
                    mesh_path=str(mesh_path),
                    hssd_id=candidate.mesh_id,
                    object_name=request.object_description,
                    similarity_score=float(candidate.clip_score),
                    size=tuple(candidate.mesh.extents.tolist()),
                    category=category,
                    asset_id=artifact["asset_id"],
                    download_url=artifact["download_url"],
                    collision=collision,
                )
            )
        return HssdRetrievalServerResponse(
            results=results,
            query_description=request.object_description,
        ).to_dict()


class MaterialsRetrieveBackend(BaseRetrieveBackend):
    """Materials source adapter."""

    def __init__(self, spec: BackendSpec, clip_device: str | None = None) -> None:
        super().__init__(spec=spec, clip_device=clip_device)
        self._retriever: MaterialsRetriever | None = None

    def load(self) -> None:
        if self._retriever is not None:
            return
        params = _params(self.spec)
        config = MaterialsConfig(
            data_path=_resolve_path(
                params.get("materials_data_path", "data/materials")
            ),
            embeddings_path=_resolve_path(
                params.get("materials_embeddings_path", "data/materials/embeddings")
            ),
            use_top_k=int(params.get("materials_top_k", 5)),
            enabled=True,
        )
        self._retriever = MaterialsRetriever(
            config=config, clip_device=self.clip_device
        )
        if not self._retriever.initialize():
            raise RuntimeError("Materials retriever initialization failed")

    def is_loaded(self) -> bool:
        return self._retriever is not None

    def parse_request(self, data: dict[str, Any]) -> MaterialsRetrievalServerRequest:
        if "material_description" not in data and "object_description" in data:
            data = {**data, "material_description": data["object_description"]}
        return MaterialsRetrievalServerRequest(**data)

    def retrieve(self, request: MaterialsRetrievalServerRequest) -> dict[str, Any]:
        self.load()
        assert self._retriever is not None
        candidates = self._retriever.retrieve(
            description=request.material_description,
            top_k=request.num_candidates,
        )
        if not candidates:
            raise ValueError(
                f"No candidates found for '{request.material_description}'"
            )

        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[MaterialRetrievalResult] = []
        for candidate in candidates:
            source_dir = self._retriever.config.data_path / candidate.material_id
            textures = _find_pbr_textures(source_dir)
            if textures is None:
                console_logger.warning(
                    "Skipping material with missing textures: %s", source_dir
                )
                continue
            material_output_dir = output_dir / candidate.material_id
            material_output_dir.mkdir(parents=True, exist_ok=True)
            copied = {}
            for key, src in textures.items():
                dst = material_output_dir / src.name
                shutil.copy2(src, dst)
                copied[key] = dst
            results.append(
                MaterialRetrievalResult(
                    material_path=str(material_output_dir),
                    material_id=candidate.material_id,
                    similarity_score=float(candidate.clip_score),
                    category=candidate.category,
                    color_texture=str(copied["color"]),
                    normal_texture=str(copied["normal"]),
                    roughness_texture=str(copied["roughness"]),
                )
            )
        if not results:
            raise ValueError(
                f"Failed to copy textures for '{request.material_description}'"
            )
        return MaterialsRetrievalServerResponse(
            results=results,
            query_description=request.material_description,
        ).to_dict()


class ArticulatedRetrieveBackend(BaseRetrieveBackend):
    """Articulated object source adapter."""

    def __init__(self, spec: BackendSpec, clip_device: str | None = None) -> None:
        super().__init__(spec=spec, clip_device=clip_device)
        self._retriever: ArticulatedRetriever | None = None

    def load(self) -> None:
        if self._retriever is not None:
            return
        params = _params(self.spec)
        config = _articulated_config_from_params(params)
        self._retriever = ArticulatedRetriever(
            config=config, clip_device=self.clip_device
        )
        if not self._retriever.initialize():
            raise RuntimeError("Articulated retriever initialization failed")

    def is_loaded(self) -> bool:
        return self._retriever is not None

    def parse_request(self, data: dict[str, Any]) -> ArticulatedRetrievalServerRequest:
        return ArticulatedRetrievalServerRequest(**data)

    def retrieve(self, request: ArticulatedRetrievalServerRequest) -> dict[str, Any]:
        self.load()
        assert self._retriever is not None
        candidates = self._retriever.retrieve(
            description=request.object_description,
            object_type=request.object_type,
            desired_dimensions=(
                list(request.desired_dimensions) if request.desired_dimensions else None
            ),
            top_k=request.num_candidates,
        )
        if not candidates:
            raise ValueError(f"No candidates found for '{request.object_description}'")
        Path(request.output_dir).mkdir(parents=True, exist_ok=True)
        results = [
            ArticulatedRetrievalResult(
                mesh_path=str(candidate.sdf_path),
                sdf_path=str(candidate.sdf_path),
                object_id=candidate.object_id,
                source=candidate.source,
                description=candidate.description,
                clip_score=float(candidate.clip_score),
                bbox_score=float(candidate.bbox_score),
                bounding_box_min=candidate.bounding_box_min,
                bounding_box_max=candidate.bounding_box_max,
            )
            for candidate in candidates
        ]
        return ArticulatedRetrievalServerResponse(
            results=results,
            query_description=request.object_description,
        ).to_dict()


def create_backend(
    spec: BackendSpec, clip_device: str | None = None
) -> BaseRetrieveBackend | None:
    """Create a backend adapter for supported retrieve backend types."""
    if spec.type == "hssd_retrieval":
        return HssdRetrieveBackend(spec=spec, clip_device=clip_device)
    if spec.type == "materials_retrieval":
        return MaterialsRetrieveBackend(spec=spec, clip_device=clip_device)
    if spec.type == "articulated_retrieval":
        return ArticulatedRetrieveBackend(spec=spec, clip_device=clip_device)
    if spec.type == "objaverse_retrieval":
        console_logger.info(
            "Skipping unsupported retrieve backend for v1: %s", spec.name
        )
        return None
    console_logger.warning(
        "Skipping unknown retrieve backend type %s for %s", spec.type, spec.name
    )
    return None


def _params(spec: BackendSpec) -> dict[str, Any]:
    params = spec.config.get("params", {})
    if isinstance(params, DictConfig):
        params = OmegaConf.to_container(params, resolve=True)
    assert isinstance(params, dict)
    return params


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_root() / path


def _articulated_config_from_params(params: dict[str, Any]) -> ArticulatedConfig:
    sources: dict[str, ArticulatedSourceConfig] = {}
    if "sources" in params:
        source_configs = params["sources"]
        assert isinstance(source_configs, dict)
        for name, cfg in source_configs.items():
            sources[str(name)] = ArticulatedSourceConfig(
                name=str(name),
                enabled=bool(cfg.get("enabled", True)),
                data_path=_resolve_path(cfg["data_path"]),
                embeddings_path=_resolve_path(cfg["embeddings_path"]),
            )
    else:
        sources["artvip"] = ArticulatedSourceConfig(
            name="artvip",
            enabled=True,
            data_path=_resolve_path(params.get("artvip_data_path", "data/artvip_sdf")),
            embeddings_path=_resolve_path(
                params.get("artvip_embeddings_path", "data/artvip_sdf/embeddings")
            ),
        )
    return ArticulatedConfig(
        sources=sources,
        use_top_k=int(params.get("articulated_top_k", 5)),
    )


def _find_pbr_textures(material_dir: Path) -> dict[str, Path] | None:
    files = [path for path in material_dir.iterdir() if path.is_file()]
    color = _find_texture(files, ["color", "albedo", "diffuse"])
    normal = _find_texture(files, ["normalgl", "normal"])
    roughness = _find_texture(files, ["roughness", "rough"])
    if color is None or normal is None or roughness is None:
        return None
    return {"color": color, "normal": normal, "roughness": roughness}


def _find_texture(files: list[Path], needles: list[str]) -> Path | None:
    for path in files:
        lower = path.name.lower()
        if any(needle in lower for needle in needles):
            return path
    return None
