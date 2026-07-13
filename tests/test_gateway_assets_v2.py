from httpx import ASGITransport, AsyncClient

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.asset_store import ContentAddressedAssetStore, canonical_source_frame


GLB = b"glTF" + b"\0" * 16


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
            {"visual/model.glb": GLB},
            visual="visual/model.glb",
            bounds={"min": [-0.5, -0.5, 0], "max": [0.5, 0.5, 1]},
            metadata={"category": "chair", "description": "wood chair"},
            source={"type": "dataset", "name": source, "resource_id": candidate_id},
            source_frame=canonical_source_frame(),
        )


async def test_v2_candidate_and_materialize_contract_never_exposes_paths(tmp_path):
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
        digest = payload["asset_ref"].rsplit("/", 1)[1]
        metadata = await client.get(f"/v2/assets/{digest}")
        assert metadata.status_code == 200
        assert "mesh_path" not in metadata.text
