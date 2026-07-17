import asyncio
import sys

from types import ModuleType

import pytest

from assetserver.generation_server.protocol import GenerationRequest
from assetserver.generation_server.runtime import GenerationRuntime
from assetserver.generation_server.standalone import (
    create_runtime,
    load_pipeline_config,
)


class Pipeline:
    name = "test"

    def __init__(self, *, fail=False):
        self.fail = fail
        self.loads = 0
        self.generations = 0
        self.cleanups = 0

    def load(self):
        self.loads += 1
        if self.fail:
            raise RuntimeError("load exploded")

    def generate(self, request, output_path):
        self.generations += 1
        if self.fail:
            raise RuntimeError("generation exploded")
        output_path.write_bytes(b"glb")

    def cleanup_request(self):
        self.cleanups += 1

    def tool_versions(self):
        return {"test": "1"}


def _request(tmp_path, identifier="id"):
    return GenerationRequest(identifier, tmp_path / "input.png", None, {})


@pytest.mark.asyncio
async def test_lazy_load_once_and_cleanup_on_every_request(tmp_path, monkeypatch):
    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", inline)
    pipeline = Pipeline()
    runtime = GenerationRuntime(pipeline, preload=False)
    await runtime.generate(_request(tmp_path, "one"), tmp_path / "one.glb")
    await runtime.generate(_request(tmp_path, "two"), tmp_path / "two.glb")
    assert pipeline.loads == 1
    assert pipeline.generations == 2
    assert pipeline.cleanups == 2


@pytest.mark.asyncio
async def test_runtime_serializes_concurrent_requests(tmp_path, monkeypatch):
    active = 0
    maximum = 0

    async def delayed(function, *args):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        try:
            return function(*args)
        finally:
            active -= 1

    monkeypatch.setattr(asyncio, "to_thread", delayed)
    runtime = GenerationRuntime(Pipeline(), preload=False)
    await asyncio.gather(
        runtime.generate(_request(tmp_path, "one"), tmp_path / "one.glb"),
        runtime.generate(_request(tmp_path, "two"), tmp_path / "two.glb"),
    )
    assert maximum == 1


def test_standalone_resolves_environment_and_validates_factory(tmp_path, monkeypatch):
    config_path = tmp_path / "backend.yaml"
    config_path.write_text(
        "name: test\n"
        "role: generate\n"
        "server: {host: 127.0.0.1, port: 7000}\n"
        "generation:\n"
        "  pipeline: fake_generation_pipeline\n"
        "  preload: false\n"
        "  model: {root: '${oc.env:TEST_MODEL_ROOT,/models}'}\n"
    )
    monkeypatch.setenv("TEST_MODEL_ROOT", "/custom-models")
    module = ModuleType("fake_generation_pipeline")
    module.create_pipeline = lambda config: Pipeline()
    monkeypatch.setitem(sys.modules, module.__name__, module)

    config = load_pipeline_config(config_path)
    runtime = create_runtime(config)
    assert config["generation"]["model"]["root"] == "/custom-models"
    assert runtime.pipeline.name == "test"


def test_standalone_rejects_name_mismatch(tmp_path, monkeypatch):
    config_path = tmp_path / "backend.yaml"
    config_path.write_text(
        "name: configured\n"
        "role: generate\n"
        "server: {port: 7000}\n"
        "generation: {pipeline: mismatch_pipeline, preload: true, model: {}}\n"
    )
    module = ModuleType("mismatch_pipeline")
    module.create_pipeline = lambda config: Pipeline()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(ValueError, match="does not match"):
        create_runtime(load_pipeline_config(config_path))
