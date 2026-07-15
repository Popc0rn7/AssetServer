import asyncio
import hashlib
import io
import json
import zipfile

from httpx import ASGITransport, AsyncClient

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.asset_store import ContentAddressedAssetStore, canonical_source_frame


GLB = b"glTF" + b"\0" * 16
HULL = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"


class FakeRetrieval:
    def __init__(self, store):
        self.store = store

    async def retrieve(self, source, request):
        return {
            "source": source,
            "query": request["description"],
            "results": [
                {
                    "asset_id": "chair-1",
                    "score": 0.9,
                    "metadata": {
                        "category": "chair",
                        "description": "wood chair",
                        "dimensions": [1, 1, 1],
                    },
                    "mesh_path": "/data/private.glb",
                }
            ],
        }

    def materialize(self, source, candidate_id, store):
        assert candidate_id == "chair-1"
        return store.ingest(
            {"visual/model.glb": GLB, "collision/hull_000.obj": HULL},
            visual="visual/model.glb",
            collision={
                "entrypoint": "collision/hull_000.obj",
                "method": "convex",
            },
            bounds={"min": [-0.5, -0.5, 0], "max": [0.5, 0.5, 1]},
            metadata={"category": "chair", "description": "wood chair"},
            source={"type": "dataset", "name": source, "resource_id": candidate_id},
            source_frame=canonical_source_frame(),
        )


async def test_v2_candidate_and_materialize_contract_never_exposes_paths(
    tmp_path, monkeypatch
):
    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    # The sandboxed test runner does not service executor threads reliably.
    # Production keeps package creation off the Gateway event loop.
    monkeypatch.setattr(asyncio, "to_thread", immediate)
    store = ContentAddressedAssetStore(tmp_path / "assets")
    gateway = AssetAcquisitionApp(
        retrieval_engine=FakeRetrieval(store), asset_store=store
    )
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        candidates = await client.post(
            "/v2/retrieve/hssd", json={"description": "wood chair"}
        )
        assert candidates.status_code == 200
        assert "/data" not in candidates.text
        materialized = await client.post(
            candidates.json()["candidates"][0]["materialize_url"]
        )
        assert materialized.status_code == 201
        payload = materialized.json()
        assert payload["asset_ref"].startswith("asset://sha256/")
        assert payload["download_url"].endswith("/download")
        digest = payload["asset_ref"].rsplit("/", 1)[1]
        metadata = await client.get(f"/v2/assets/{digest}")
        assert metadata.status_code == 200
        assert "mesh_path" not in metadata.text
        package_artifact = metadata.json()["package_artifact"]
        assert package_artifact["schema_version"] == "artifact/v1"

        download = await client.get(payload["download_url"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert download.headers["x-asset-sha256"] == digest
        assert (
            download.headers["x-asset-package-sha256"]
            == hashlib.sha256(download.content).hexdigest()
        )
        assert download.headers["cache-control"] == (
            "public, immutable, max-age=31536000"
        )
        unified = await client.get(package_artifact["content_url"])
        assert unified.content == download.content
        assert unified.headers["x-artifact-sha256"] == package_artifact["sha256"]
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert archive.namelist() == [
                "manifest.json",
                "collision/hull_000.obj",
                "visual/model.glb",
            ]
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["digest"] == digest
            assert manifest["collision"][0]["entrypoint"] == ("collision/hull_000.obj")
            assert archive.read("collision/hull_000.obj") == HULL
