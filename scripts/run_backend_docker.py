#!/usr/bin/env python3
"""Run a configured backend container from config/generate or config/retrieve."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess

from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from assetserver.config import backend_specs, load_assetserver_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", help="Backend name, e.g. sam3d or articulated")
    parser.add_argument("--config", default="config/server.yaml")
    parser.add_argument("--host-root", default=os.getcwd())
    parser.add_argument("--sudo", action="store_true", help="Prefix docker with sudo")
    parser.add_argument("--detach", action="store_true", help="Run container detached")
    parser.add_argument("--rm", action="store_true", default=True)
    parser.add_argument("--no-rm", action="store_false", dest="rm")
    parser.add_argument("--replace", action="store_true", help="Remove same-named container first")
    parser.add_argument("--print", action="store_true", help="Print command without running")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    host_root = str(Path(args.host_root).resolve())
    os.environ["ASSETSERVER_HOST_ROOT"] = host_root
    cfg = load_assetserver_config(args.config)
    backend = next((item for item in backend_specs(cfg) if item.name == args.backend), None)
    if backend is None:
        raise SystemExit(f"Unknown backend: {args.backend}")

    docker_cfg = backend.config.get("docker") or {}
    if not docker_cfg:
        raise SystemExit(f"Backend '{args.backend}' has no docker config")

    env = {
        **os.environ,
        "ASSETSERVER_HOST_ROOT": host_root,
    }
    resolved = OmegaConf.to_container(OmegaConf.create(docker_cfg), resolve=True)
    assert isinstance(resolved, dict)

    container_name = str(resolved.get("container_name") or f"assetserver-{args.backend}")
    image = resolved.get("image")
    if not image:
        raise SystemExit(f"Backend '{args.backend}' docker config is missing image")

    docker = ["sudo", "docker"] if args.sudo else ["docker"]
    if args.replace:
        rm_cmd = [*docker, "rm", "-f", container_name]
        if args.print:
            print(shlex.join(rm_cmd))
        else:
            subprocess.run(rm_cmd, check=False)

    cmd = [*docker, "run"]
    if args.rm:
        cmd.append("--rm")
    if args.detach:
        cmd.append("-d")
    cmd.extend(["--name", container_name])

    if resolved.get("gpu"):
        gpu_device = resolved.get("gpu_device")
        gpu_arg = f"device={gpu_device}" if gpu_device not in (None, "") else "all"
        cmd.extend(["--gpus", gpu_arg])

    for port in resolved.get("ports") or []:
        cmd.extend(["-p", str(port)])

    for volume in resolved.get("volumes") or []:
        cmd.extend(["-v", str(volume)])

    for item in resolved.get("environment") or []:
        cmd.extend(["-e", str(item)])

    for item in resolved.get("extra_hosts") or []:
        cmd.extend(["--add-host", str(item)])

    network = resolved.get("network") or cfg.docker.get("network")
    if network:
        cmd.extend(["--network", str(network)])

    cmd.append(str(image))
    command = resolved.get("command")
    if command:
        cmd.extend(str(part) for part in command)

    if args.print:
        print(shlex.join(cmd))
        return 0

    return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
