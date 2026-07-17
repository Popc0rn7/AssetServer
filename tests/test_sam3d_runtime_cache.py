import json
import sys

from types import ModuleType, SimpleNamespace

import pytest
import yaml

from assetserver.generation_pipelines.sam3d.pipeline import (
    SAM3DPipeline,
    ensure_runtime_cache_dirs,
    force_local_dinov2_hub,
    seed_dinov2_cache,
)


def test_runtime_cache_directories_are_created_for_an_existing_volume(tmp_path):
    cache_root = tmp_path / "existing-volume"
    cache_root.mkdir()

    ensure_runtime_cache_dirs(cache_root)

    assert (cache_root / "xdg").is_dir()
    assert (cache_root / "config").is_dir()
    assert (cache_root / "matplotlib").is_dir()
    assert (cache_root / "hf").is_dir()
    assert (cache_root / "torch").is_dir()
    assert (cache_root / "torch-extensions").is_dir()


def test_seed_dinov2_cache_rebuilds_from_image_code_and_model_weight(tmp_path):
    source = tmp_path / "image" / "dinov2"
    source.mkdir(parents=True)
    (source / "hubconf.py").write_text("# local code\n")
    weight = tmp_path / "checkpoints" / "dinov2.pth"
    weight.parent.mkdir()
    weight.write_bytes(b"weights")
    torch_home = tmp_path / "cache" / "torch"

    seed_dinov2_cache(
        SimpleNamespace(dino_weights=weight),
        source_root=source,
        torch_home=torch_home,
    )

    assert (torch_home / "hub/facebookresearch_dinov2_main/hubconf.py").is_file()
    cached_weight = torch_home / "hub/checkpoints/dinov2_vitl14_reg4_pretrain.pth"
    assert cached_weight.read_bytes() == b"weights"


def test_dinov2_github_hub_call_is_forced_to_local_source(tmp_path, monkeypatch):
    source = tmp_path / "dinov2"
    source.mkdir()
    (source / "hubconf.py").write_text("# local")
    calls = []

    def original(repo_or_dir, model, *args, **kwargs):
        calls.append((repo_or_dir, model, args, kwargs))
        return "model"

    torch = SimpleNamespace(hub=SimpleNamespace(load=original))
    monkeypatch.setitem(sys.modules, "torch", torch)

    force_local_dinov2_hub(source)
    result = torch.hub.load(
        repo_or_dir="facebookresearch/dinov2",
        model="dinov2_vitl14_reg",
        source="github",
        verbose=False,
    )

    assert result == "model"
    assert calls == [
        (
            str(source.resolve()),
            "dinov2_vitl14_reg",
            (),
            {"source": "local", "verbose": False},
        )
    ]


def test_dinov2_redirect_does_not_change_unrelated_hub_calls(tmp_path, monkeypatch):
    source = tmp_path / "dinov2"
    source.mkdir()
    (source / "hubconf.py").write_text("# local")
    calls = []

    def original(repo_or_dir, model, *args, **kwargs):
        calls.append((repo_or_dir, model, kwargs))

    torch = SimpleNamespace(hub=SimpleNamespace(load=original))
    monkeypatch.setitem(sys.modules, "torch", torch)
    force_local_dinov2_hub(source)

    torch.hub.load("owner/other", "model", source="github")

    assert calls == [("owner/other", "model", {"source": "github"})]


def test_runtime_passes_moge_checkpoint_file_not_snapshot_directory(
    tmp_path, monkeypatch
):
    root = tmp_path / "models"
    root.mkdir()
    sam3 = root / "sam3.pt"
    pipeline = root / "pipeline.yaml"
    moge = root / "hf-cache/models--Ruicheng--moge-vitl/snapshots/rev/model.pt"
    dino = root / "torch-cache/hub/checkpoints/dinov2_vitl14_reg4_pretrain.pth"
    for path in (sam3, moge, dino):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")
    pipeline.write_text(yaml.safe_dump({}))

    import assetserver.generation_pipelines.sam3d.model_bundle as model_bundle

    manifest = model_bundle.create_manifest(root, "test")
    assert json.loads((root / "model-manifest.json").read_text()) == manifest
    dinov2_source = tmp_path / "dinov2-source"
    dinov2_source.mkdir()
    (dinov2_source / "hubconf.py").write_text("# local")
    cache = tmp_path / "cache"
    runtime = SAM3DPipeline(
        {
            "generation": {
                "model": {"root": str(root)},
                "sources": {
                    "sam3": str(tmp_path),
                    "sam3d_objects": str(tmp_path),
                    "dinov2": str(dinov2_source),
                },
                "cache": {"root": str(cache)},
                "defaults": {},
            }
        }
    )
    runtime._prepare_bundle()

    assert runtime.bundle.moge_model == moge.resolve()
    assert __import__("os").environ["SAM3D_MOGE_MODEL_PATH"] == str(moge.resolve())
    assert __import__("os").environ["TORCH_HOME"] == str(cache / "torch")


def test_pipeline_requires_configured_thirdparty_sources(tmp_path):
    runtime = SAM3DPipeline(
        {
            "generation": {
                "model": {"root": str(tmp_path / "models")},
                "sources": {
                    "sam3": str(tmp_path / "SAM3"),
                    "sam3d_objects": str(tmp_path / "sam-3d-objects"),
                },
                "cache": {"root": str(tmp_path / "cache")},
            }
        }
    )

    with pytest.raises(RuntimeError, match="dinov2"):
        runtime._prepare_sources()


def _ready_runtime() -> SAM3DPipeline:
    runtime = SAM3DPipeline.__new__(SAM3DPipeline)
    runtime.bundle = SimpleNamespace(
        sam3_checkpoint="sam3.pt", pipeline_config="pipeline.yaml"
    )
    runtime.defaults = {"mode": "foreground", "threshold": 0.5}
    return runtime


def _fake_torch(events: list[str]) -> ModuleType:
    class InferenceMode:
        def __enter__(self):
            events.append("inference_enter")

        def __exit__(self, *_):
            events.append("inference_exit")

    cuda = SimpleNamespace(
        is_available=lambda: True,
        synchronize=lambda: events.append("synchronize"),
        memory_allocated=lambda: 3 * 1024**2,
        memory_reserved=lambda: 5 * 1024**2,
        empty_cache=lambda: events.append("empty_cache"),
    )
    torch = ModuleType("torch")
    torch.cuda = cuda
    torch.inference_mode = InferenceMode
    return torch


def test_generation_uses_inference_mode_and_releases_cuda_cache(monkeypatch, tmp_path):
    events = []
    calls = []
    manager = ModuleType(
        "assetserver.generation_pipelines.sam3d.pipeline_manager"
    )

    def generate_with_sam3d(**kwargs):
        events.append("generate")
        calls.append(kwargs)

    manager.generate_with_sam3d = generate_with_sam3d
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(events))
    monkeypatch.setitem(
        sys.modules,
        "assetserver.generation_pipelines.sam3d.pipeline_manager",
        manager,
    )
    monkeypatch.setattr(
        "assetserver.generation_pipelines.sam3d.pipeline.gc.collect",
        lambda: events.append("gc_collect"),
    )

    runtime = _ready_runtime()
    from assetserver.generation_server.protocol import GenerationRequest

    runtime.generate(
        GenerationRequest("id", tmp_path / "input.png", None, {"threshold": 0.4}),
        tmp_path / "output.glb",
    )
    runtime.cleanup_request()

    assert events == [
        "inference_enter",
        "generate",
        "inference_exit",
        "gc_collect",
        "synchronize",
        "empty_cache",
    ]
    assert calls[0]["use_pipeline_caching"] is True
    assert calls[0]["threshold"] == 0.4


def test_generation_failure_still_releases_cuda_cache(monkeypatch, tmp_path):
    events = []
    manager = ModuleType(
        "assetserver.generation_pipelines.sam3d.pipeline_manager"
    )

    def generate_with_sam3d(**_):
        events.append("generate")
        raise RuntimeError("generation failed")

    manager.generate_with_sam3d = generate_with_sam3d
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(events))
    monkeypatch.setitem(
        sys.modules,
        "assetserver.generation_pipelines.sam3d.pipeline_manager",
        manager,
    )
    monkeypatch.setattr(
        "assetserver.generation_pipelines.sam3d.pipeline.gc.collect",
        lambda: events.append("gc_collect"),
    )

    from assetserver.generation_server.protocol import GenerationRequest

    runtime = _ready_runtime()
    with pytest.raises(RuntimeError, match="generation failed"):
        runtime.generate(
            GenerationRequest("id", tmp_path / "input.png", None, {}),
            tmp_path / "output.glb",
        )
    runtime.cleanup_request()

    assert events[-3:] == ["gc_collect", "synchronize", "empty_cache"]
