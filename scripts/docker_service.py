#!/usr/bin/env python3
"""Build and manage the explicitly containerized AssetServer services."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "docker" / "services.yaml"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
VERSIONS_PATH = ROOT / "docker" / "versions.env"


def _registry() -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(REGISTRY_PATH.read_text()) or {}
    return {
        name: _resolve_service(name, dict(service))
        for name, service in dict(raw.get("services") or {}).items()
    }


def _resolve_service(name: str, service: dict[str, Any]) -> dict[str, Any]:
    """Resolve host endpoint settings shared with an AssetServer backend."""
    config_value = service.get("backend_config")
    if not config_value:
        return service

    config_path = (ROOT / str(config_value)).resolve()
    if not config_path.is_file():
        raise SystemExit(f"{name}: backend config not found: {config_path}")
    backend = yaml.safe_load(config_path.read_text()) or {}
    endpoint = dict(backend.get("server") or {})
    host = endpoint.get("host")
    port = endpoint.get("port")
    if not isinstance(host, str) or not host.strip():
        raise SystemExit(f"{name}: backend config requires server.host")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise SystemExit(f"{name}: backend config requires a valid server.port")

    container_port = service.get("container_port")
    if not isinstance(container_port, int) or not 1 <= container_port <= 65535:
        raise SystemExit(f"{name}: service requires a valid container_port")
    service["port"] = f"{host}:{port}:{container_port}"

    ready_path = service.get("ready_path")
    if ready_path is not None:
        if not isinstance(ready_path, str) or not ready_path.startswith("/"):
            raise SystemExit(f"{name}: ready_path must start with '/'")
        service["ready_url"] = f"http://{host}:{port}{ready_path}"
    return service


def _versions() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in VERSIONS_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _docker(sudo: bool) -> list[str]:
    return ["sudo", "docker"] if sudo else ["docker"]


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, check=check)


def _image(service: dict[str, Any], tag: str = "dev") -> str:
    override = os.environ.get(str(service.get("image_env", "")))
    return override or f"{service['image']}:{tag}"


def _gpu_architecture() -> str:
    override = os.environ.get("TORCH_CUDA_ARCH_LIST")
    if override:
        return override
    if shutil.which("nvidia-smi"):
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=False,
        )
        values = sorted(
            {line.strip() for line in result.stdout.splitlines() if line.strip()}
        )
        if values:
            return ";".join(values)
    raise SystemExit("Cannot detect GPU architecture; set TORCH_CUDA_ARCH_LIST.")


def build(name: str, service: dict[str, Any], args: argparse.Namespace) -> None:
    versions = _versions()
    docker = _docker(args.sudo)
    git = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    revision = git.stdout.strip() or "dev"
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if dirty.stdout.strip():
        revision = f"{revision}-dirty"
    command = [
        *docker,
        "build",
        "-f",
        str(DOCKERFILE.relative_to(ROOT)),
        "--target",
        str(service["target"]),
    ]
    common = ("CUDA_VERSION", "PYTHON_VERSION", "TORCH_VERSION", "TORCHVISION_VERSION")
    for key in common:
        command.extend(["--build-arg", f"{key}={versions[key]}"])
    command.extend(
        ["--build-arg", f"UV_HTTP_TIMEOUT={os.environ.get('UV_HTTP_TIMEOUT', '300')}"]
    )
    if os.environ.get("PYPI"):
        command.extend(["--build-arg", f"PYPI_INDEX_URL={os.environ['PYPI']}"])
    if os.environ.get("GITHUB"):
        command.extend(["--build-arg", f"GITHUB_URL_PREFIX={os.environ['GITHUB']}"])
    if name == "sam3d":
        command.extend(["--build-arg", f"TORCH_CUDA_ARCH_LIST={_gpu_architecture()}"])
        for key in (
            "SAM3_REVISION",
            "SAM3D_OBJECTS_REVISION",
            "NVDIFFRAST_REVISION",
            "PYTORCH3D_REVISION",
            "MOGE_REVISION",
            "DINOV2_REVISION",
        ):
            command.extend(["--build-arg", f"{key}={versions[key]}"])
        command.extend(["--build-arg", f"IMAGE_VERSION={revision}"])
    if name == "scene-viewer":
        for key in ("BLENDER_VERSION", "DRAKE_VERSION"):
            command.extend(["--build-arg", f"{key}={versions[key]}"])
        command.extend(["--build-arg", f"IMAGE_VERSION={revision}"])
    if name == "hunyuan3d" and os.environ.get("HUNYUAN3D_COMMIT"):
        command.extend(
            ["--build-arg", f"HUNYUAN3D_COMMIT={os.environ['HUNYUAN3D_COMMIT']}"]
        )
    if args.proxy:
        command.extend(["--add-host", "host.docker.internal:host-gateway"])
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            command.extend(["--build-arg", f"{key}={args.proxy}"])
        for key in ("NO_PROXY", "no_proxy"):
            command.extend(["--build-arg", f"{key}=localhost,127.0.0.1"])
    if args.clean:
        command.append("--no-cache")
    command.extend(["-t", _image(service, revision), "-t", _image(service)])
    command.extend(args.extra)
    command.append(".")
    _run(command)


def _ensure_network(docker: list[str]) -> None:
    inspected = subprocess.run(
        [*docker, "network", "inspect", "assetserver"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inspected.returncode:
        _run([*docker, "network", "create", "assetserver"])


def _validate_model(service: dict[str, Any]) -> None:
    validator = service.get("model_validator")
    if not validator:
        return
    model_root = (ROOT / str(service["model_host"])).resolve()
    _run([str(ROOT / ".venv/bin/python"), "-m", str(validator), str(model_root)])


def _wait_ready(url: str, timeout_s: float, docker: list[str], container: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 400:
                    print(f"Ready: {url}")
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(2)
    _run([*docker, "logs", "--tail", "200", container], check=False)
    raise SystemExit(f"Timed out waiting for {url}")


def run_service(name: str, service: dict[str, Any], args: argparse.Namespace) -> None:
    docker = _docker(args.sudo)
    _validate_model(service)
    _ensure_network(docker)
    container = str(service["container"])
    _run([*docker, "rm", "-f", container], check=False)
    command = [
        *docker,
        "run",
        "--name",
        container,
        "--network",
        "assetserver",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=1g",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--label",
        "org.assetserver.managed=true",
        "--label",
        f"org.assetserver.service={name}",
    ]
    gpu_policy = service.get("gpu")
    if gpu_policy is True or (gpu_policy == "optional" and not args.no_gpu):
        command.extend(["--gpus", f"device={args.gpu}"])
    if service.get("run_as_host"):
        command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    if service.get("port"):
        command.extend(["-p", str(service["port"])])
    model_host = service.get("model_host")
    if model_host:
        command.extend(
            [
                "-v",
                f"{(ROOT / str(model_host)).resolve()}:{service['model_container']}:ro",
            ]
        )
    data_policy = service.get("data")
    if data_policy:
        data = (ROOT / "data").resolve()
        data.mkdir(parents=True, exist_ok=True)
        (data / "assets").mkdir(parents=True, exist_ok=True)
        mode = "rw" if data_policy == "read-write" else "ro"
        command.extend(["-v", f"{data}:/data:{mode}"])
        if service.get("assets_read_only"):
            command.extend(["-v", f"{data / 'assets'}:/data/assets:ro"])
    if service.get("outputs"):
        outputs = (ROOT / "outputs").resolve()
        outputs.mkdir(parents=True, exist_ok=True)
        command.extend(["-v", f"{outputs}:/outputs:rw"])
    if service.get("cache_host"):
        cache = (ROOT / str(service["cache_host"])).resolve()
        cache.mkdir(parents=True, exist_ok=True)
        command.extend(["-v", f"{cache}:{service['cache_container']}:rw"])
    elif service.get("cache_volume"):
        command.extend(
            ["-v", f"{service['cache_volume']}:{service['cache_container']}:rw"]
        )
    for key, value in dict(service.get("environment") or {}).items():
        command.extend(["-e", f"{key}={value}"])
    if name == "scene-viewer" and args.no_gpu:
        command.extend(["-e", "ASSETSERVER_RENDER_DEVICE=disabled"])
    if args.foreground:
        command.append("--rm")
    else:
        command.append("-d")
    command.append(_image(service))
    _run(command)
    if not args.foreground and service.get("ready_url"):
        _wait_ready(
            str(service["ready_url"]),
            float(service.get("ready_timeout_s", 300)),
            docker,
            container,
        )
    elif not args.foreground:
        print(f"Started {name} as {container}")


def _require_service(
    services: dict[str, dict[str, Any]], name: str | None
) -> tuple[str, dict[str, Any]]:
    if not name or name not in services:
        choices = ", ".join(sorted(services))
        raise SystemExit(f"Service required; choose one of: {choices}")
    return name, services[name]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "run", "stop", "logs", "status"))
    parser.add_argument("service", nargs="?")
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--proxy")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--gpu", default="0")
    parser.add_argument("--no-gpu", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--no-follow", action="store_true")
    args, extra = parser.parse_known_args()
    args.extra = extra
    services = _registry()
    docker = _docker(args.sudo)
    if args.action == "status":
        _run(
            [
                *docker,
                "ps",
                "-a",
                "--filter",
                "label=org.assetserver.managed=true",
                "--format",
                "table {{.Names}}\t{{.Image}}\t{{.Status}}",
            ]
        )
        return 0
    name, service = _require_service(services, args.service)
    if args.action == "build":
        build(name, service, args)
    elif args.action == "run":
        run_service(name, service, args)
    elif args.action == "stop":
        _run([*docker, "stop", str(service["container"])], check=False)
    elif args.action == "logs":
        command = [*docker, "logs"]
        if not args.no_follow:
            command.append("-f")
        command.append(str(service["container"]))
        _run(command, check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
