import os
import subprocess

from pathlib import Path


def test_build_script_uses_plain_docker_build_with_separate_tags(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.args"
    sudo_log = tmp_path / "sudo.args"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {log}\n"
    )
    docker.chmod(0o755)
    sudo = fake_bin / "sudo"
    sudo.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {sudo_log}\n"
        'exec "$@"\n'
    )
    sudo.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TORCH_CUDA_ARCH_LIST": "8.9",
        "GITHUB": "https://github-mirror.example/https://github.com/",
        "PYPI": "https://pypi-mirror.example/simple",
    }

    subprocess.run(
        [
            "bash",
            "scripts/docker_service.sh",
            "build",
            "sam3d",
            "--proxy",
            "http://host.docker.internal:7890",
            "--sudo",
            "--progress=plain",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    args = log.read_text().splitlines()
    sudo_args = sudo_log.read_text().splitlines()
    assert sudo_args[0] == "docker"
    assert args[:5] == [
        "build",
        "-f",
        "docker/Dockerfile",
        "--target",
        "sam3d-runtime",
    ]
    assert "buildx" not in args
    assert "assetserver/sam3d:dev" in args
    assert "GITHUB_URL_PREFIX=https://github-mirror.example/https://github.com/" in args
    assert "PYPI_INDEX_URL=https://pypi-mirror.example/simple" in args
    assert "UV_HTTP_TIMEOUT=300" in args
    assert "HTTP_PROXY=http://host.docker.internal:7890" in args
    assert "HTTPS_PROXY=http://host.docker.internal:7890" in args
    assert "host.docker.internal:host-gateway" in args
    assert "--progress=plain" in args
    assert args[-1] == "."
