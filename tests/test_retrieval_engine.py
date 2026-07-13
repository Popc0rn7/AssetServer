import json
import zipfile

from pathlib import Path

import numpy as np
import yaml

from assetserver.retrieval.assets import AssetCatalog
from assetserver.retrieval.engine import RetrievalEngine
from assetserver.retrieval.sources import ArticulatedSource, MaterialsSource


def _write_material_dataset(root: Path) -> tuple[Path, Path]:
    data = root / "materials"
    embeddings = data / "embeddings"
    embeddings.mkdir(parents=True)
    np.save(embeddings / "clip_embeddings.npy", np.array([[1.0, 0.0], [0.0, 1.0]]))
    (embeddings / "embedding_index.yaml").write_text(yaml.safe_dump(["Wood001", "Metal001"]))
    (embeddings / "metadata_index.yaml").write_text(
        yaml.safe_dump(
            {
                "Wood001": {"category": "Wood", "tags": ["warm"]},
                "Metal001": {"category": "Metal", "tags": ["cold"]},
            }
        )
    )
    for material in ("Wood001", "Metal001"):
        folder = data / material
        folder.mkdir()
        (folder / f"{material}_Color.jpg").write_bytes(b"color")
        (folder / f"{material}_NormalGL.jpg").write_bytes(b"normal")
        (folder / f"{material}_Roughness.jpg").write_bytes(b"rough")
    return data, embeddings


def test_materials_source_searches_precomputed_embeddings_and_registers_asset(tmp_path):
    data, embeddings = _write_material_dataset(tmp_path)
    source = MaterialsSource(data_root=data, embeddings_root=embeddings)

    results = source.search(np.array([1.0, 0.0]), num_candidates=1)

    assert results[0].resource_key == "Wood001"
    assert results[0].score == 1.0
    assert results[0].metadata["category"] == "Wood"


def test_material_asset_is_packaged_as_deterministic_zip(tmp_path):
    data, embeddings = _write_material_dataset(tmp_path)
    source = MaterialsSource(data_root=data, embeddings_root=embeddings)
    candidate = source.search(np.array([1.0, 0.0]), num_candidates=1)[0]
    catalog = AssetCatalog(cache_root=tmp_path / "cache")
    descriptor = source.describe_asset(candidate)
    asset_id = catalog.register(descriptor)

    first = catalog.package(asset_id)
    second = catalog.package(asset_id)

    assert first.path == second.path
    assert first.sha256 == second.sha256
    with zipfile.ZipFile(first.path) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "Wood001_Color.jpg",
            "Wood001_NormalGL.jpg",
            "Wood001_Roughness.jpg",
        }
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["resource_key"] == "Wood001"


def test_articulated_source_filters_type_and_packages_object_directory(tmp_path):
    data = tmp_path / "artvip"
    embeddings = data / "embeddings"
    obj = data / "objects" / "cabinet"
    embeddings.mkdir(parents=True)
    obj.mkdir(parents=True)
    (obj / "model.sdf").write_text(
        """<sdf version="1.11"><world name="root"><model name="cabinet">
        <link name="body"><visual name="visual"><geometry><mesh>
        <uri>body.gltf</uri></mesh></geometry></visual></link>
        </model></world></sdf>"""
    )
    (obj / "body.gltf").write_text(
        json.dumps({"asset": {"version": "2.0"}, "scenes": [{"nodes": []}]})
    )
    np.save(embeddings / "clip_embeddings.npy", np.array([[1.0, 0.0]]))
    (embeddings / "embedding_index.yaml").write_text(yaml.safe_dump(["cabinet"]))
    (embeddings / "metadata_index.yaml").write_text(
        yaml.safe_dump(
            {
                "cabinet": {
                    "sdf_path": "objects/cabinet/model.sdf",
                    "description": "wood cabinet",
                    "is_manipuland": False,
                    "placement_type": "floor",
                    "bounding_box_min": [0, 0, 0],
                    "bounding_box_max": [1, 0.5, 2],
                }
            }
        )
    )
    source = ArticulatedSource(
        sources={"artvip": {"data_root": data, "embeddings_root": embeddings}},
        clip_pool_size=5,
    )

    results = source.search(
        np.array([1.0, 0.0]),
        num_candidates=1,
        object_type="FURNITURE",
        desired_dimensions=[1, 0.5, 2],
    )
    without_dimensions = source.search(
        np.array([1.0, 0.0]), num_candidates=1, object_type="FURNITURE"
    )
    descriptor = source.describe_asset(results[0])
    catalog = AssetCatalog(cache_root=tmp_path / "cache")
    packaged = catalog.package(catalog.register(descriptor))

    assert results[0].resource_key == "artvip:cabinet"
    assert results[0].metadata["base_link"] == "body"
    assert results[0].metadata["articulation"]["joint_count"] == 0
    assert results[0].metadata == without_dimensions[0].metadata
    with zipfile.ZipFile(packaged.path) as archive:
        assert "model.sdf" in archive.namelist()
        assert "body.gltf" in archive.namelist()


def test_retrieval_engine_returns_gateway_asset_urls(tmp_path):
    data, embeddings = _write_material_dataset(tmp_path)

    class Embeddings:
        async def text_embedding(self, text):
            return np.array([1.0, 0.0])

    engine = RetrievalEngine(
        sources={"materials": MaterialsSource(data_root=data, embeddings_root=embeddings)},
        embedding_client=Embeddings(),
        cache_root=tmp_path / "cache",
    )

    import asyncio

    result = asyncio.run(
        engine.retrieve("materials", {"description": "warm wood", "num_candidates": 1})
    )

    assert result["results"][0]["download_url"].startswith("/v1/assets/materials/")


def test_configured_delivery_cache_is_used_without_loading_source_at_startup(tmp_path, monkeypatch):
    from omegaconf import OmegaConf

    data, embeddings = _write_material_dataset(tmp_path)
    monkeypatch.setattr("assetserver.retrieval.engine.project_root", lambda: tmp_path)
    config = OmegaConf.create(
        {
            "embedding_providers": {"openclip": {"base_url": "http://openclip"}},
            "backends": {
                "materials": {
                    "name": "materials",
                    "type": "materials",
                    "role": "retrieve",
                    "enabled": True,
                    "dataset": {"root": str(data), "embeddings": str(embeddings)},
                    "delivery": {"cache_dir": "derived/materials"},
                }
            },
        }
    )

    engine = RetrievalEngine.from_config(config)

    assert callable(engine.sources["materials"])
    assert engine.catalogs["materials"].cache_root == tmp_path / "derived/materials"
