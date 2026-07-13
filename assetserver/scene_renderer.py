"""HTTP client for the external static-scene renderer contract."""

import io
import json
import zipfile

from pathlib import PurePosixPath
from typing import Any

import httpx


class SceneRendererError(Exception):
    def __init__(
        self, message: str, *, error: str = "render_failed", status: int = 502
    ):
        super().__init__(message)
        self.error = error
        self.status = status


class SceneRendererClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 300,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.transport = transport

    async def render(self, package: bytes, options: dict[str, Any]) -> bytes:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s, transport=self.transport, trust_env=False
            ) as client:
                response = await client.post(
                    f"{self.base_url}/v1/render",
                    files={
                        "package": ("scene.zip", package, "application/zip"),
                        "options": (None, json.dumps(options)),
                    },
                )
        except httpx.TimeoutException as exc:
            raise SceneRendererError(
                "renderer request timed out", error="render_timed_out", status=504
            ) from exc
        except httpx.HTTPError as exc:
            raise SceneRendererError(
                f"renderer is unavailable: {exc}",
                error="render_backend_unavailable",
                status=503,
            ) from exc

        if response.status_code >= 400:
            try:
                data = response.json()
                message = str(
                    data.get("message") or data.get("detail") or "render failed"
                )
                error = str(data.get("error") or "render_failed")
            except ValueError:
                message, error = "render failed", "render_failed"
            raise SceneRendererError(message, error=error, status=502)
        if (
            response.headers.get("content-type", "").split(";", 1)[0]
            != "application/zip"
        ):
            raise SceneRendererError(
                "invalid renderer response: expected application/zip"
            )
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                files = [name for name in archive.namelist() if not name.endswith("/")]
                if not files:
                    raise zipfile.BadZipFile("empty archive")
                requested = set(options.get("views") or [])
                image_format = options.get("format", "webp")
                expected = {f"{view}.{image_format}" for view in requested}
                returned = set(files)
                if (
                    len(files) != len(returned)
                    or any(len(PurePosixPath(name).parts) != 1 for name in files)
                    or expected != returned
                ):
                    raise zipfile.BadZipFile(
                        f"expected files {sorted(expected)}, got {sorted(returned)}"
                    )
        except zipfile.BadZipFile as exc:
            raise SceneRendererError(f"invalid renderer response: {exc}") from exc
        return response.content
