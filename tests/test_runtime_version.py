import json

import pytest

import assetserver.runtime_version as versions
from assetserver.runtime_version import (
    deployed_scene_job_cache_version,
    register_runtime,
    worker_model_version,
)


def test_api_and_worker_record_shared_schema_and_build_version(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSETSERVER_BUILD_VERSION", "build-123")
    api = register_runtime(tmp_path, role="api", instance_id="api-1")
    worker = register_runtime(tmp_path, role="scene-worker", instance_id="worker-1")
    assert api["scene_ir_model_version"] == worker["scene_ir_model_version"]
    assert api["build_version"] == worker["build_version"] == "build-123"
    assert worker_model_version(tmp_path) == versions.SCENE_IR_MODEL_VERSION
    assert deployed_scene_job_cache_version(tmp_path).endswith("+build=build-123")


def test_job_cache_uses_deployed_worker_build_not_api_build(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSETSERVER_BUILD_VERSION", "worker-build")
    register_runtime(tmp_path, role="scene-worker", instance_id="worker")
    monkeypatch.setenv("ASSETSERVER_BUILD_VERSION", "api-build")

    assert deployed_scene_job_cache_version(tmp_path).endswith("+build=worker-build")


def test_worker_refuses_api_scene_ir_model_drift(tmp_path, monkeypatch):
    register_runtime(tmp_path, role="api", instance_id="api-1")
    monkeypatch.setattr(versions, "SCENE_IR_MODEL_VERSION", "old-worker-model")
    with pytest.raises(RuntimeError, match="SceneIR model version drift"):
        register_runtime(tmp_path, role="scene-worker", instance_id="worker-old")


def test_api_can_publish_upgrade_before_replacing_old_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, "SCENE_IR_MODEL_VERSION", "old-worker-model")
    register_runtime(tmp_path, role="scene-worker", instance_id="worker-old")
    monkeypatch.setattr(
        versions,
        "SCENE_IR_MODEL_VERSION",
        "scene-ir/v1+procedural-room-shell/v1",
    )
    identity = register_runtime(tmp_path, role="api", instance_id="api-new")
    recorded = json.loads((tmp_path / "runtime" / "api.json").read_text())
    assert recorded["scene_ir_model_version"] == identity["scene_ir_model_version"]
    assert worker_model_version(tmp_path) == "old-worker-model"
