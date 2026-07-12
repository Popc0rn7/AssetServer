import os
import subprocess

from pathlib import Path


def _executable(path: Path, content: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + content)
    path.chmod(0o755)


def test_download_splits_hf_mirror_and_dino_proxy(tmp_path):
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()
    (checkpoints / "sam3.pt").write_bytes(b"sam")
    (checkpoints / "pipeline.yaml").write_text("{}\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "network.log"

    _executable(
        fake_bin / "hf",
        f"""
printf 'hf endpoint=%s upper=%s lower=%s\n' "${{HF_ENDPOINT:-}}" \
  "${{HTTPS_PROXY:-unset}}" "${{https_proxy:-unset}}" >> {log}
mkdir -p "$SAM3D_CHECKPOINTS/hf-cache/hub/models--Ruicheng--moge-vitl/snapshots/979e84da9415762c30e6c0cf8dc0962896c793df"
printf moge > "$SAM3D_CHECKPOINTS/hf-cache/hub/models--Ruicheng--moge-vitl/snapshots/979e84da9415762c30e6c0cf8dc0962896c793df/model.pt"
""",
    )
    _executable(
        fake_bin / "curl",
        f"""
printf 'curl upper=%s lower=%s\n' "${{HTTPS_PROXY:-unset}}" \
  "${{https_proxy:-unset}}" >> {log}
output=''
while [[ $# -gt 0 ]]; do
  if [[ "$1" == '--output' ]]; then output="$2"; shift 2; else shift; fi
done
printf dino > "$output"
""",
    )
    _executable(fake_bin / "sha256sum", "exit 0\n")

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SAM3D_CHECKPOINTS": str(checkpoints),
        "HTTPS_PROXY": "http://proxy.example:7890",
        "https_proxy": "http://proxy-lower.example:7890",
    }
    subprocess.run(
        ["bash", "scripts/download_sam3d_ckpt.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    entries = log.read_text().splitlines()
    assert entries[0] == (
        "hf endpoint=https://hf-mirror.com upper=unset lower=unset"
    )
    assert entries[1] == (
        "curl upper=http://proxy.example:7890 "
        "lower=http://proxy-lower.example:7890"
    )
