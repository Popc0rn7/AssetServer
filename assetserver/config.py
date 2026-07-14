"""Configuration loading for AssetServer.

The main server config lives in ``config/server.yaml``. Backend configs live
under the directories listed by ``backend`` and are discovered automatically.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omegaconf import DictConfig, OmegaConf

from assetserver.utils.omegaconf import register_resolvers


DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 7010,
        "storage": {
            "data_root": "data",
            "output_root": "outputs",
        },
        "scenes": {
            "legacy_sdf_api_enabled": False,
            "scene_ir_api_enabled": True,
            "renderer_url": None,
        },
        "jobs": {
            "max_attempts": 3,
        },
    },
    "openclip": "config/openclip.yaml",
    "runtime": {
        "convex_decomposition": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 7100,
            "port_range": [7100, 7150],
            "omp_threads": None,
            "server_startup_delay": 1.0,
            "port_cleanup_delay": 1.0,
        },
        "postprocess_server": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 7100,
            "collision_method": "coacd",
            "timeout_s": 300,
        },
    },
    "backend": [
        "config/generate",
        "config/retrieve",
    ],
    "backends": {},
}


@dataclass(frozen=True)
class BackendSpec:
    """Resolved backend/tool declaration from YAML."""

    name: str
    type: str
    role: str
    enabled: bool
    config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "role": self.role,
            "enabled": self.enabled,
            "config": self.config,
        }


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    return project_root() / "config" / "server.yaml"


def load_assetserver_config(
    config_path: str | Path | None = None,
) -> DictConfig:
    """Load server config and merge discovered backend YAML declarations.

    Merge order:
    1. Built-in defaults.
    2. ``config/server.yaml``.
    3. YAML entries discovered from the directories in ``backend``.
    """
    register_resolvers()

    path = Path(config_path) if config_path is not None else default_config_path()
    if not path.is_absolute():
        path = project_root() / path

    base_cfg = OmegaConf.create(DEFAULT_CONFIG)
    file_cfg = _load_yaml_or_empty(path)
    cfg = OmegaConf.merge(base_cfg, file_cfg)
    cfg.openclip = _load_referenced_config(cfg.openclip)

    backend_paths = _resolve_backend_paths(cfg)
    discovered_backends = OmegaConf.create({})
    for backend_path in backend_paths:
        discovered_backends = OmegaConf.merge(
            discovered_backends, _load_backend_configs(backend_path)
        )
    cfg.backends = discovered_backends
    cfg.backend = [str(path) for path in backend_paths]
    cfg.config_path = str(path)
    return cfg


def enabled_backend_specs(cfg: DictConfig) -> list[BackendSpec]:
    """Return enabled backend declarations as plain dataclasses."""
    specs: list[BackendSpec] = []
    for name, backend_cfg in cfg.get("backends", {}).items():
        enabled = bool(backend_cfg.get("enabled", False))
        if not enabled:
            continue
        specs.append(_backend_spec_from_config(name=name, backend_cfg=backend_cfg))
    return specs


def backend_specs(cfg: DictConfig) -> list[BackendSpec]:
    """Return all backend declarations as plain dataclasses."""
    return [
        _backend_spec_from_config(name=name, backend_cfg=backend_cfg)
        for name, backend_cfg in cfg.get("backends", {}).items()
    ]


def config_to_container(cfg: DictConfig) -> dict[str, Any]:
    return OmegaConf.to_container(cfg, resolve=True)  # type: ignore[return-value]


def _load_yaml_or_empty(path: Path) -> DictConfig:
    if not path.exists() or path.stat().st_size == 0:
        return OmegaConf.create({})
    loaded = OmegaConf.load(path)
    if loaded is None:
        return OmegaConf.create({})
    return loaded


def _load_referenced_config(value: str | Path) -> DictConfig:
    path = Path(str(value))
    if not path.is_absolute():
        path = project_root() / path
    return _load_yaml_or_empty(path)


def _load_backend_configs(config_dir: Path) -> DictConfig:
    discovered = OmegaConf.create({})
    if not config_dir.exists():
        return discovered

    for backend_file in sorted(config_dir.glob("*.yaml")):
        backend_cfg = _load_yaml_or_empty(backend_file)
        if not backend_cfg:
            continue
        name = str(backend_cfg.get("name") or backend_file.stem)
        backend_cfg.name = name
        backend_cfg.source_path = str(backend_file)
        discovered[name] = backend_cfg
    return discovered


def _resolve_backend_paths(cfg: DictConfig) -> list[Path]:
    paths: list[Path] = []
    for value in cfg.backend:
        path = Path(str(value))
        if not path.is_absolute():
            path = project_root() / path
        paths.append(path)
    return paths


def _backend_spec_from_config(name: str, backend_cfg: DictConfig) -> BackendSpec:
    container = OmegaConf.to_container(backend_cfg, resolve=True)
    assert isinstance(container, dict)
    return BackendSpec(
        name=name,
        type=str(backend_cfg.get("type", "unknown")),
        role=str(backend_cfg.get("role", "tool")),
        enabled=bool(backend_cfg.get("enabled", False)),
        config=container,
    )
