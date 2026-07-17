import hashlib
import json

import yaml

from httpx import ASGITransport, AsyncClient

import assetserver.scene_job_handlers as handlers
from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.asset_store import AssetStore
from assetserver.jobs import JobWorker, SQLiteJobStore
from assetserver.scene_ir_store import IRSceneStore


def _document():
    return {
        "rooms": [
            {
                "id": "office",
                "shell": {
                    "kind": "procedural",
                    "dimensions": [3.2, 2.8, 2.7],
                    "openings": [
                        {
                            "id": "entry",
                            "opening_type": "door",
                            "wall": "south",
                            "offset_m": 1.0,
                            "width": 0.9,
                            "height": 2.1,
                        }
                    ],
                },
            }
        ]
    }


async def test_post_observe_validate_export_through_real_job_worker(
    tmp_path, monkeypatch
):
    data = tmp_path / "data"
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("ASSETSERVER_DATA_ROOT", str(data))
    monkeypatch.setenv("ASSETSERVER_OUTPUT_ROOT", str(outputs))
    assets = AssetStore(data / "assets")
    scenes = IRSceneStore(data / "scenes", assets)
    jobs = SQLiteJobStore(data / "jobs" / "jobs.sqlite3")
    gateway = AssetAcquisitionApp(
        ir_scene_store=scenes, job_store=jobs, asset_store=assets
    )
    gateway._scene_data_root = data
    gateway._scene_output_root = outputs

    def render(
        recipe_path,
        output_dir,
        *,
        views,
        image_format,
        blend_path=None,
        **_,
    ):
        recipe = json.loads(recipe_path.read_text())
        shell = recipe["instances"][0]["procedural_shell"]
        assert shell["generator_version"] == "procedural-room-shell/v1"
        output_dir.mkdir(parents=True, exist_ok=True)
        rendered = []
        for view in views:
            path = output_dir / f"{view}.{image_format}"
            path.write_bytes(f"image:{view}".encode())
            rendered.append(
                {
                    "view": view,
                    "path": str(path),
                    "camera_location": [1, 2, 3],
                    "target": [0, 0, 0],
                }
            )
        if blend_path:
            blend_path.write_bytes(b"blend")
        return rendered

    monkeypatch.setattr(handlers, "render_recipe", render)
    # The renderer is stubbed in this closed-loop test, so its placeholder
    # .blend and Drake package must not be passed to installed native runtimes.
    monkeypatch.setattr(handlers, "_validate_blend", lambda path: None)
    monkeypatch.setattr(handlers, "_validate_drake_package", lambda package: None)
    monkeypatch.setattr(
        handlers,
        "_validate_result",
        lambda job: {
            "valid": True,
            "issues": [],
            "model_count": 1,
            "physics_backend": "closed-loop-test",
        },
    )

    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v2/scenes",
            content=yaml.safe_dump(_document()),
            headers={"content-type": "application/yaml"},
        )
        assert created.status_code == 201
        scene_id = created.json()["scene_id"]
        submissions = {
            "observe": await client.post(
                f"/v2/scenes/{scene_id}/observe", json={"views": ["top"]}
            ),
            "validate": await client.post(f"/v2/scenes/{scene_id}/validate", json={}),
            "export": await client.post(
                f"/v2/scenes/{scene_id}/exports", json={"views": ["top"]}
            ),
        }
        assert all(response.status_code == 202 for response in submissions.values())

        worker = JobWorker(
            jobs,
            "closed-loop-worker",
            {
                "observe": handlers.observe,
                "validate": handlers.validate,
                "export": handlers.export,
            },
            lease_seconds=30,
            heartbeat_seconds=5,
        )
        completed = [worker.run_once(), worker.run_once(), worker.run_once()]
        assert all(job is not None and job.status == "completed" for job in completed)

        statuses = {
            name: (await client.get(f"/v2/jobs/{response.json()['job_id']}")).json()
            for name, response in submissions.items()
        }
        assert all(value["status"] == "completed" for value in statuses.values())

        observation = await client.get(statuses["observe"]["result"]["manifest_url"])
        view = observation.json()["views"][0]
        content = await client.get(view["content_url"])
        assert hashlib.sha256(content.content).hexdigest() == view["sha256"]
        assert observation.json()["provenance"]["procedural_shells"]["office"]

        validation_artifact = statuses["validate"]["result"]["artifact"]
        validation_content = await client.get(validation_artifact["content_url"])
        assert (
            hashlib.sha256(validation_content.content).hexdigest()
            == (validation_artifact["sha256"])
        )

        export_artifact = statuses["export"]["result"]["artifact"]
        export_content = await client.get(export_artifact["content_url"])
        assert (
            hashlib.sha256(export_content.content).hexdigest()
            == (export_artifact["sha256"])
        )


async def test_schema_error_is_non_retryable(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    gateway = AssetAcquisitionApp(
        ir_scene_store=IRSceneStore(tmp_path / "scenes", assets),
        asset_store=assets,
    )
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v2/scenes",
            content=yaml.safe_dump(
                {
                    "rooms": [
                        {
                            "id": "bad",
                            "shell": {
                                "kind": "procedural",
                                "dimensions": [3, 0, 2.7],
                            },
                        }
                    ]
                }
            ),
            headers={"content-type": "application/yaml"},
        )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_room_dimensions"
    assert response.json()["retryable"] is False


async def test_gateway_rejects_jobs_when_deployed_worker_schema_has_drifted(tmp_path):
    data = tmp_path / "data"
    assets = AssetStore(data / "assets")
    scenes = IRSceneStore(data / "scenes", assets)
    jobs = SQLiteJobStore(data / "jobs.sqlite3")
    gateway = AssetAcquisitionApp(
        ir_scene_store=scenes, job_store=jobs, asset_store=assets
    )
    gateway._scene_data_root = data
    runtime = data / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "scene-worker.json").write_text(
        json.dumps({"scene_ir_model_version": "scene-ir/v1-old-worker"})
    )
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v2/scenes",
            content=yaml.safe_dump(_document()),
            headers={"content-type": "application/yaml"},
        )
        response = await client.post(
            f"/v2/scenes/{created.json()['scene_id']}/observe", json={}
        )
    assert response.status_code == 503
    assert response.json() == {
        "error": "scene_worker_schema_mismatch",
        "message": "scene worker SceneIR model version does not match the API",
        "retryable": False,
    }
