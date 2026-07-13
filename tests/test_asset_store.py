import pytest

from assetserver.asset_store import AssetStore, AssetStoreError


def test_asset_store_is_content_addressed_and_survives_recreation(tmp_path):
    store = AssetStore(tmp_path)
    first = store.ingest({"visual/chair.glb": b"mesh"}, visual="visual/chair.glb")
    second = store.ingest({"visual/chair.glb": b"mesh"}, visual="visual/chair.glb")
    assert first.asset_ref == second.asset_ref
    assert AssetStore(tmp_path).entrypoint(first.asset_ref, "visual").read_bytes() == b"mesh"


def test_asset_store_rejects_unsafe_paths_and_detects_corruption(tmp_path):
    store = AssetStore(tmp_path)
    with pytest.raises(AssetStoreError, match="unsafe"):
        store.ingest({"../chair.glb": b"mesh"}, visual="../chair.glb")
    asset = store.ingest({"chair.glb": b"mesh"}, visual="chair.glb")
    (asset.root / "files/chair.glb").write_bytes(b"changed")
    with pytest.raises(AssetStoreError, match="verification"):
        store.resolve(asset.asset_ref)
