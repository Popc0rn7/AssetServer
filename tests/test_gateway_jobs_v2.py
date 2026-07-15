import hashlib

import yaml

from httpx import ASGITransport, AsyncClient

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.asset_store import AssetStore
from assetserver.jobs import SQLiteJobStore
from assetserver.scene_ir_store import IRSceneStore


async def _app_and_scene(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    asset = assets.ingest({"room.glb": b"v"}, visual="room.glb")
    scenes = IRSceneStore(tmp_path / "scenes", assets)
    document = {"rooms": [{"id": "main", "shell": {"asset_ref": asset.asset_ref}}]}
    scene = scenes.create(yaml.safe_dump(document).encode())
    jobs = SQLiteJobStore(tmp_path / "jobs/jobs.sqlite3")
    gateway = AssetAcquisitionApp(ir_scene_store=scenes, job_store=jobs)
    gateway._scene_data_root = tmp_path / "data"
    gateway._scene_output_root = tmp_path / "outputs"
    return gateway, scene, jobs


async def test_scene_jobs_are_async_revision_pinned_and_idempotent(tmp_path):
    gateway, scene, _ = await _app_and_scene(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        first = await client.post(
            f"/v2/scenes/{scene.scene_id}/observe",
            json={"revision": 1, "views": ["top"]},
        )
        assert first.status_code == 202
        assert first.json()["scene_revision"] == 1
        assert first.json()["deduplicated"] is False
        duplicate = await client.post(
            f"/v2/scenes/{scene.scene_id}/observe",
            json={"revision": 1, "views": ["top"]},
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == first.json()["job_id"]
        assert duplicate.json()["deduplicated"] is True
        status = await client.get(first.json()["status_url"])
        assert status.status_code == 200
        assert status.json()["status"] == "queued"
        assert status.json()["request"] == {"views": ["top"]}


async def test_job_cancel_and_missing_scene_errors(tmp_path):
    gateway, scene, _ = await _app_and_scene(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        submitted = await client.post(f"/v2/scenes/{scene.scene_id}/validate", json={})
        cancelled = await client.post(f"/v2/jobs/{submitted.json()['job_id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        again = await client.post(f"/v2/jobs/{submitted.json()['job_id']}/cancel")
        assert again.status_code == 409
        missing = await client.post(
            "/v2/scenes/00000000-0000-0000-0000-000000000000/exports", json={}
        )
        assert missing.status_code == 404
        missing_job = await client.get("/v2/jobs/does-not-exist")
        assert missing_job.status_code == 404


async def test_failed_observe_is_recreated_and_enters_worker(tmp_path):
    gateway, scene, jobs = await _app_and_scene(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        first = await client.post(
            f"/v2/scenes/{scene.scene_id}/observe", json={"views": ["top"]}
        )
        claimed = jobs.claim("old-worker")
        jobs.fail(
            claimed.job_id,
            "old-worker",
            code="schema_error",
            message="old worker schema",
            retryable=False,
        )

        second = await client.post(
            f"/v2/scenes/{scene.scene_id}/observe", json={"views": ["top"]}
        )

    assert second.status_code == 202
    assert second.json()["deduplicated"] is False
    assert second.json()["job_id"] != first.json()["job_id"]
    assert jobs.claim("new-worker").job_id == second.json()["job_id"]


async def test_completed_observation_and_export_are_downloadable(tmp_path):
    gateway, scene, jobs = await _app_and_scene(tmp_path)
    data = gateway._scene_data_root
    outputs = gateway._scene_output_root
    observation, _ = jobs.submit("observe", scene.scene_id, 1, {})
    jobs.claim("worker")
    observation_dir = (
        data / "scenes" / scene.scene_id / "observations" / observation.job_id
    )
    observation_dir.mkdir(parents=True)
    (observation_dir / "top.webp").write_bytes(b"image")
    (observation_dir / "manifest.json").write_text(
        '{"views":[{"view":"top","path":"top.webp"}]}\n'
    )
    jobs.complete(
        observation.job_id,
        "worker",
        {
            "manifest_path": (observation_dir / "manifest.json")
            .relative_to(data)
            .as_posix(),
            "views": [
                {
                    "view": "top",
                    "path": (observation_dir / "top.webp").relative_to(data).as_posix(),
                }
            ],
        },
    )

    exported, _ = jobs.submit("export", scene.scene_id, 1, {})
    jobs.claim("worker")
    archive = outputs / scene.scene_id / exported.job_id / "scene.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"zip")
    jobs.complete(
        exported.job_id,
        "worker",
        {"zip_path": archive.relative_to(outputs).as_posix(), "sha256": "abc"},
    )

    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        manifest = await client.get(f"/v2/observations/{observation.job_id}")
        assert manifest.status_code == 200
        assert manifest.json()["views"][0]["url"].endswith("/views/top")
        view_record = manifest.json()["views"][0]
        artifact_metadata = await client.get(
            f"/v2/artifacts/{view_record['artifact_id']}"
        )
        assert artifact_metadata.json()["schema_version"] == "artifact/v1"
        assert (
            artifact_metadata.json()["sha256"] == hashlib.sha256(b"image").hexdigest()
        )
        artifact_content = await client.get(view_record["content_url"])
        assert artifact_content.content == b"image"
        assert artifact_content.headers["content-length"] == "5"
        assert artifact_content.headers["etag"] == (
            f'"{hashlib.sha256(b"image").hexdigest()}"'
        )
        view = await client.get(manifest.json()["views"][0]["url"])
        assert view.content == b"image"
        assert view.headers["content-type"] == "image/webp"
        download = await client.get(f"/v2/exports/{exported.job_id}")
        assert download.content == b"zip"
        assert download.headers["x-export-sha256"] == hashlib.sha256(b"zip").hexdigest()

        (observation_dir / "top.webp").unlink()
        gone = await client.get(view_record["content_url"])
        assert gone.status_code == 410
        assert gone.json() == {
            "error": "artifact_gone",
            "message": "artifact content is no longer retained",
            "retryable": False,
        }
        regenerated = await client.post(f"/v2/scenes/{scene.scene_id}/observe", json={})
        assert regenerated.status_code == 202
        assert regenerated.json()["deduplicated"] is False
        assert regenerated.json()["job_id"] != observation.job_id

        rebuilt_job = jobs.claim("worker")
        assert rebuilt_job.job_id == regenerated.json()["job_id"]
        rebuilt_dir = (
            data / "scenes" / scene.scene_id / "observations" / rebuilt_job.job_id
        )
        rebuilt_dir.mkdir(parents=True)
        (rebuilt_dir / "top.webp").write_bytes(b"image-v2")
        (rebuilt_dir / "manifest.json").write_text(
            '{"views":[{"view":"top","path":"top.webp"}]}\n'
        )
        jobs.complete(
            rebuilt_job.job_id,
            "worker",
            {
                "manifest_path": (rebuilt_dir / "manifest.json")
                .relative_to(data)
                .as_posix(),
                "views": [
                    {
                        "view": "top",
                        "path": (rebuilt_dir / "top.webp").relative_to(data).as_posix(),
                    }
                ],
            },
        )
        rebuilt_manifest = await client.get(f"/v2/observations/{rebuilt_job.job_id}")
        rebuilt_artifact_id = rebuilt_manifest.json()["views"][0]["artifact_id"]
        assert rebuilt_artifact_id != view_record["artifact_id"]
        assert (await client.get(view_record["content_url"])).status_code == 410


async def test_validation_job_publishes_verifiable_report_artifact(tmp_path):
    gateway, scene, jobs = await _app_and_scene(tmp_path)
    validation, _ = jobs.submit("validate", scene.scene_id, 1, {})
    jobs.claim("worker")
    report = (
        gateway._scene_data_root
        / "scenes"
        / scene.scene_id
        / "validations"
        / validation.job_id
        / "report.json"
    )
    report.parent.mkdir(parents=True)
    report.write_bytes(b'{"issues":[],"valid":true}\n')
    jobs.complete(
        validation.job_id,
        "worker",
        {
            "valid": True,
            "issues": [],
            "scene_sha256": scene.sha256,
            "report_path": report.relative_to(gateway._scene_data_root).as_posix(),
        },
    )

    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        status = await client.get(f"/v2/jobs/{validation.job_id}")
        result = status.json()["result"]
        assert "report_path" not in result
        assert result["artifact"]["kind"] == "validation_report"
        assert result["artifact"]["provenance"]["scene_revision"] == 1
        content = await client.get(result["artifact"]["content_url"])
        assert content.content == report.read_bytes()
        assert result["sha256"] == hashlib.sha256(content.content).hexdigest()
