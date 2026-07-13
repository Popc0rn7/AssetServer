import subprocess

import pytest

from assetserver.blender_scene_worker import BlenderRecipeError, _render_device


def test_render_device_uses_explicit_nvidia_assignment(monkeypatch):
    monkeypatch.setenv("ASSETSERVER_RENDER_DEVICE", "gpu")
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "GPU-selected")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "0, GPU-selected, NVIDIA Test GPU\n", ""
        ),
    )

    assert _render_device() == "NVIDIA[GPU-selected]/0, GPU-selected, NVIDIA Test GPU"


def test_render_device_disabled_is_explicit_and_non_fallback(monkeypatch):
    monkeypatch.setenv("ASSETSERVER_RENDER_DEVICE", "disabled")

    with pytest.raises(BlenderRecipeError, match="GPU rendering is disabled"):
        _render_device()
