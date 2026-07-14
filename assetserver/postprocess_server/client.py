"""Async client for the isolated postprocess worker."""

from __future__ import annotations

from typing import Any

import httpx


class PostprocessClientError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status_code: int = 503):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class PostprocessClient:
    def __init__(self, url: str, timeout_s: float = 300) -> None:
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s

    async def decompose(
        self,
        *,
        request_id: str,
        asset_digest: str,
        entrypoint: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        parameters = {
            key: value for key, value in profile.items() if key not in {"name", "method"}
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s, trust_env=False
            ) as client:
                response = await client.post(
                    f"{self.url}/v1/decompositions",
                    json={
                        "request_id": request_id,
                        "source": {
                            "asset_digest": asset_digest,
                            "entrypoint": entrypoint,
                        },
                        "profile": {
                            "name": profile["name"],
                            "method": profile["method"],
                            "parameters": parameters,
                        },
                    },
                )
        except httpx.RequestError as exc:
            raise PostprocessClientError(str(exc), retryable=True) from exc
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise PostprocessClientError(
                f"postprocess worker returned {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise PostprocessClientError(
                f"postprocess worker rejected asset: {response.text}",
                retryable=False,
                status_code=422,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise PostprocessClientError("invalid worker response", retryable=True) from exc
        if not data.get("success") or not data.get("pieces"):
            raise PostprocessClientError("worker returned no hulls", retryable=False, status_code=422)
        return data


# Transitional import name for downstream users. The old arbitrary-path methods
# are intentionally not retained.
ConvexDecompositionClient = PostprocessClient
