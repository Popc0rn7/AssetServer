import hashlib

import httpx
import pytest

from assetserver.openclip_server.app import create_app
from assetserver.openclip_server.model_bundle import create_manifest, validate_bundle


def test_openclip_bundle_manifest_validates_checkpoint(tmp_path):
    model = tmp_path / "open_clip_pytorch_model.bin"
    model.write_bytes(b"PK\x03\x04weights")

    manifest = create_manifest(tmp_path, revision="dfn5b")
    bundle = validate_bundle(tmp_path)

    assert manifest["model"] == "ViT-H-14-378-quickgelu"
    assert bundle.checkpoint == model.resolve()
    assert bundle.sha256 == hashlib.sha256(model.read_bytes()).hexdigest()


def test_openclip_bundle_rejects_modified_checkpoint(tmp_path):
    model = tmp_path / "open_clip_pytorch_model.bin"
    model.write_bytes(b"PK\x03\x04weights")
    create_manifest(tmp_path, revision="dfn5b")
    model.write_bytes(b"PK\x03\x04brokenx")

    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        validate_bundle(tmp_path)


@pytest.mark.asyncio
async def test_text_embeddings_are_served_as_a_batch(tmp_path):
    app = create_app(
        ready_check=lambda: (True, None),
        text_embed=lambda inputs, normalize: [[float(len(item)), 1.0] for item in inputs],
        image_embed=lambda images, normalize: [[float(len(item)), 2.0] for item in images],
        model_info={"model": "test", "revision": "rev", "dimension": 2},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/embeddings/text",
            json={"inputs": ["wood", "metal"], "normalize": True},
        )

    assert response.status_code == 200
    assert response.json() == {
        "model": "test",
        "revision": "rev",
        "dimension": 2,
        "embeddings": [[4.0, 1.0], [5.0, 1.0]],
    }


@pytest.mark.asyncio
async def test_image_embeddings_accept_repeated_multipart_images():
    app = create_app(
        ready_check=lambda: (True, None),
        text_embed=lambda inputs, normalize: [],
        image_embed=lambda images, normalize: [[float(len(item))] for item in images],
        model_info={"model": "test", "revision": "rev", "dimension": 1},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/embeddings/images",
            files=[
                ("images", ("a.png", b"abc", "image/png")),
                ("images", ("b.jpg", b"12345", "image/jpeg")),
            ],
        )

    assert response.status_code == 200
    assert response.json()["embeddings"] == [[3.0], [5.0]]


@pytest.mark.asyncio
async def test_embedding_endpoint_reports_not_ready():
    app = create_app(
        ready_check=lambda: (False, "model loading failed"),
        text_embed=lambda inputs, normalize: [],
        image_embed=lambda images, normalize: [],
        model_info={"model": "test", "revision": "rev", "dimension": 1},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/embeddings/text", json={"inputs": ["x"]})

    assert response.status_code == 503
    assert response.json()["detail"] == "model loading failed"
