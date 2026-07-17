from pathlib import Path

import tomllib


def test_heavy_worker_dependencies_are_owned_by_dependency_groups():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    groups = project["dependency-groups"]

    assert not any(item.startswith("coacd") for item in dependencies)
    assert groups["postprocess"] == ["coacd>=1.0.7"]
    assert groups["scene-viewer"] == ["bpy==4.5.4", "drake==1.47.0"]
    assert groups["retrieve"] == [
        "open-clip-torch==3.2.0",
        "torch==2.5.1",
        "torchvision==0.20.1",
    ]
    assert "generate" not in groups
    assert "openclip" not in project["project"].get("optional-dependencies", {})
    assert not any("objathor" in item for item in groups["retrieve"])


def test_blender_dependency_uses_the_official_explicit_index():
    project = tomllib.loads(Path("pyproject.toml").read_text())

    assert project["tool"]["uv"]["sources"]["bpy"] == {"index": "blender"}
    assert {
        "name": "blender",
        "url": "https://download.blender.org/pypi/",
        "explicit": True,
    } in project["tool"]["uv"]["index"]
