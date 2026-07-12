"""Docker lifecycle management for gateway-managed backend services."""

from __future__ import annotations

import logging
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from assetserver.config import BackendSpec

console_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DockerServiceStatus:
    name: str
    configured: bool
    enabled: bool
    container_name: str | None
    image: str | None
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "enabled": self.enabled,
            "container_name": self.container_name,
            "image": self.image,
            "status": self.status,
            "error": self.error,
        }


class DockerBackendManager:
    """Starts configured backend containers before gateway proxying."""

    def __init__(self, config: Any | None) -> None:
        self._config = config
        self._docker = None
        self._client = None

    @property
    def enabled(self) -> bool:
        return self.launch_backend

    @property
    def launch_backend(self) -> bool:
        return _as_bool(self._docker_config().get("launch_backend", False))

    def ensure_backend_running(self, backend: BackendSpec) -> None:
        """Start backend dependencies and the backend container when configured."""
        if not self.launch_backend:
            return

        backend_docker = backend.config.get("docker", {})
        if not backend_docker or not _as_bool(backend_docker.get("enabled", True)):
            return

        for dependency in backend_docker.get("depends_on", []) or []:
            self.ensure_named_service_running(str(dependency))

        server = backend.config.get("server", {})
        health_url = backend_docker.get("health_url") or _health_url_from_server(server)
        self._ensure_container_running(
            name=backend.name,
            service_config=backend_docker,
            health_url=health_url,
        )

    def ensure_named_service_running(self, service_name: str) -> None:
        """Start a globally configured Docker service such as postprocess."""
        if not self.launch_backend:
            return

        service_config = self._service_config(service_name)
        if not service_config or not _as_bool(service_config.get("enabled", True)):
            return

        health_url = service_config.get("health_url")
        if health_url is None and service_name == "postprocess":
            runtime = self._runtime_config().get("postprocess_server", {})
            health_url = _health_url_from_server(runtime)

        self._ensure_container_running(
            name=service_name,
            service_config=service_config,
            health_url=health_url,
        )

    def service_statuses(self, backends: list[BackendSpec]) -> list[dict[str, Any]]:
        statuses: list[DockerServiceStatus] = []
        docker_cfg = self._docker_config()
        if not self.launch_backend:
            for service_name, service_config in docker_cfg.get("services", {}).items():
                statuses.append(
                    self._configured_disabled_status(
                        str(service_name), service_config
                    )
                )
            for backend in backends:
                statuses.append(
                    self._configured_disabled_status(
                        backend.name,
                        backend.config.get("docker", {}),
                    )
                )
            return [status.to_dict() for status in statuses]

        for service_name, service_config in docker_cfg.get("services", {}).items():
            statuses.append(self._status_for_config(str(service_name), service_config))

        for backend in backends:
            statuses.append(
                self._status_for_config(
                    backend.name,
                    backend.config.get("docker", {}),
                )
            )

        return [status.to_dict() for status in statuses]

    def _configured_disabled_status(
        self, name: str, service_config: dict[str, Any] | None
    ) -> DockerServiceStatus:
        if not service_config:
            return DockerServiceStatus(
                name=name,
                configured=False,
                enabled=False,
                container_name=None,
                image=None,
                status="not_configured",
            )
        return DockerServiceStatus(
            name=name,
            configured=True,
            enabled=_as_bool(service_config.get("enabled", True)),
            container_name=service_config.get("container_name")
            or f"assetserver-{name}",
            image=service_config.get("image"),
            status="launch_disabled",
        )

    def _ensure_container_running(
        self,
        name: str,
        service_config: dict[str, Any],
        health_url: str | None,
    ) -> None:
        container_name = service_config.get("container_name") or f"assetserver-{name}"
        image = service_config.get("image")
        if not image and not container_name:
            raise RuntimeError(f"Docker service '{name}' is missing image/container_name")

        container = self._get_container(container_name)

        if container is None:
            if not image:
                raise RuntimeError(
                    f"Docker container '{container_name}' does not exist and no image "
                    f"is configured for service '{name}'"
                )
            container = self._run_container(container_name, service_config)
            console_logger.info("Started new Docker container %s", container_name)
        else:
            container.reload()
            if container.status != "running":
                console_logger.info("Starting Docker container %s", container_name)
                container.start()

        if health_url:
            self._wait_until_healthy(name=name, health_url=health_url)

    def _run_container(self, container_name: str, service_config: dict[str, Any]):
        docker_module = self._docker_module()
        kwargs: dict[str, Any] = {
            "name": container_name,
            "detach": True,
        }

        command = service_config.get("command")
        if command:
            kwargs["command"] = command

        environment = service_config.get("environment")
        if environment:
            kwargs["environment"] = environment

        extra_hosts = _normalize_extra_hosts(service_config.get("extra_hosts"))
        if extra_hosts:
            kwargs["extra_hosts"] = extra_hosts

        volumes = _normalize_volumes(service_config.get("volumes"))
        if volumes:
            kwargs["volumes"] = volumes

        network = service_config.get("network") or self._docker_config().get("network")
        if network:
            if network == "host":
                kwargs["network_mode"] = "host"
            else:
                kwargs["network"] = network

        if "network_mode" not in kwargs:
            ports = _normalize_ports(service_config.get("ports"))
            if ports:
                kwargs["ports"] = ports

        if _as_bool(service_config.get("gpu", False)):
            gpu_device = service_config.get("gpu_device")
            request_kwargs: dict[str, Any] = {"capabilities": [["gpu"]]}
            if gpu_device not in (None, ""):
                request_kwargs["device_ids"] = [str(gpu_device)]
            else:
                request_kwargs["count"] = -1
            kwargs["device_requests"] = [
                docker_module.types.DeviceRequest(**request_kwargs)
            ]

        return self._docker_client().containers.run(service_config["image"], **kwargs)

    def _wait_until_healthy(self, name: str, health_url: str) -> None:
        timeout = float(self._docker_config().get("startup_timeout_s", 300))
        interval = float(self._docker_config().get("health_interval_s", 1.0))
        deadline = time.time() + timeout
        last_error = None

        while time.time() < deadline:
            try:
                response = requests.get(health_url, timeout=2)
                if response.status_code == 200:
                    return
                last_error = f"HTTP {response.status_code}"
            except requests.RequestException as e:
                last_error = str(e)
            time.sleep(interval)

        raise TimeoutError(
            f"Docker service '{name}' did not become healthy at {health_url}: "
            f"{last_error}"
        )

    def _status_for_config(
        self, name: str, service_config: dict[str, Any] | None
    ) -> DockerServiceStatus:
        if not service_config:
            return DockerServiceStatus(
                name=name,
                configured=False,
                enabled=False,
                container_name=None,
                image=None,
                status="not_configured",
            )

        container_name = service_config.get("container_name") or f"assetserver-{name}"
        try:
            container = self._get_container(container_name)
            status = "missing" if container is None else container.status
            return DockerServiceStatus(
                name=name,
                configured=True,
                enabled=_as_bool(service_config.get("enabled", True)),
                container_name=container_name,
                image=service_config.get("image"),
                status=status,
            )
        except Exception as e:
            return DockerServiceStatus(
                name=name,
                configured=True,
                enabled=_as_bool(service_config.get("enabled", True)),
                container_name=container_name,
                image=service_config.get("image"),
                status="error",
                error=str(e),
            )

    def _get_container(self, container_name: str):
        docker_module = self._docker_module()
        try:
            return self._docker_client().containers.get(container_name)
        except docker_module.errors.NotFound:
            return None

    def _docker_module(self):
        if self._docker is None:
            try:
                import docker
            except ImportError as e:
                raise RuntimeError(
                    "Docker lifecycle management is enabled but the docker SDK is "
                    "not installed"
                ) from e
            self._docker = docker
        return self._docker

    def _docker_client(self):
        if self._client is None:
            self._client = self._docker_module().from_env()
        return self._client

    def _docker_config(self) -> dict[str, Any]:
        if self._config is None or "docker" not in self._config:
            return {}
        return _to_plain_dict(self._config.docker)

    def _runtime_config(self) -> dict[str, Any]:
        if self._config is None or "runtime" not in self._config:
            return {}
        return _to_plain_dict(self._config.runtime)

    def _service_config(self, service_name: str) -> dict[str, Any]:
        return self._docker_config().get("services", {}).get(service_name, {})


def _health_url_from_server(server: dict[str, Any]) -> str | None:
    host = server.get("host")
    port = server.get("port")
    if not host or not port:
        return None
    return f"http://{host}:{port}/health"


def _normalize_ports(ports: Any) -> dict[str, Any]:
    if not ports:
        return {}
    if isinstance(ports, dict):
        return ports

    normalized: dict[str, Any] = {}
    for value in ports:
        text = str(value)
        host_port, container_port = text.split(":", maxsplit=1)
        normalized[f"{container_port}/tcp"] = int(host_port)
    return normalized


def _normalize_extra_hosts(extra_hosts: Any) -> dict[str, str]:
    if not extra_hosts:
        return {}
    if isinstance(extra_hosts, dict):
        return {str(host): str(address) for host, address in extra_hosts.items()}

    normalized: dict[str, str] = {}
    for value in extra_hosts:
        host, address = str(value).split(":", maxsplit=1)
        normalized[host] = address
    return normalized


def _normalize_volumes(volumes: Any) -> dict[str, dict[str, str]]:
    if not volumes:
        return {}
    if isinstance(volumes, dict):
        return volumes

    normalized: dict[str, dict[str, str]] = {}
    for value in volumes:
        parts = str(value).split(":")
        if len(parts) < 2:
            raise ValueError(f"Invalid Docker volume mapping: {value}")
        source = parts[0]
        target = parts[1]
        mode = parts[2] if len(parts) > 2 else "rw"
        if source.startswith(".") or source.startswith("~"):
            source = str(Path(source).expanduser().resolve())
        normalized[source] = {"bind": target, "mode": mode}
    return normalized


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _to_plain_dict(value: Any) -> dict[str, Any]:
    try:
        from omegaconf import OmegaConf

        container = OmegaConf.to_container(value, resolve=True)
        return container if isinstance(container, dict) else {}
    except Exception:
        return dict(value)
