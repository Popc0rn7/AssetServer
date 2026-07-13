import zipfile
from io import BytesIO

import httpx
import pytest

from assetserver.scene_renderer import SceneRendererClient, SceneRendererError


def _zip() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("top.webp", b"image")
    return output.getvalue()


@pytest.mark.asyncio
async def test_renderer_client_posts_package_and_options_as_multipart():
    async def handler(request):
        body = await request.aread()
        assert b'name="package"' in body
        assert b'name="options"' in body
        assert b'"views": ["top"]' in body
        return httpx.Response(
            200, content=_zip(), headers={"content-type": "application/zip"}
        )

    client = SceneRendererClient(
        "http://renderer", transport=httpx.MockTransport(handler)
    )
    result = await client.render(b"scene-package", {"views": ["top"]})

    assert result == _zip()


@pytest.mark.asyncio
async def test_renderer_client_rejects_invalid_success_response():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=b"not zip", headers={"content-type": "text/plain"}
        )
    )
    client = SceneRendererClient("http://renderer", transport=transport)

    with pytest.raises(SceneRendererError, match="invalid renderer response"):
        await client.render(b"scene-package", {"views": ["top"]})


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["top.png", "nested/top.webp", "top.webp/extra"])
async def test_renderer_client_requires_exact_root_view_filenames(filename):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(filename, b"image")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=output.getvalue(),
            headers={"content-type": "application/zip"},
        )
    )
    client = SceneRendererClient("http://renderer", transport=transport)

    with pytest.raises(SceneRendererError, match="invalid renderer response"):
        await client.render(b"scene-package", {"views": ["top"], "format": "webp"})
