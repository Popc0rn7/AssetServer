import tomllib

from pathlib import Path


def test_default_environment_installs_huggingface_cli():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]

    assert any(item.startswith("huggingface-hub") for item in dependencies)
