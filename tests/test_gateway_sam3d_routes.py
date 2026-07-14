from omegaconf import OmegaConf

from assetserver.asset_acquisition_server.server_app import (
    AssetAcquisitionApp,
    rewrite_sam3d_download_url,
)


def test_gateway_exposes_backend_specific_sam3d_routes():
    config = OmegaConf.create(
        {
            "server": {},
            "docker": {"launch_backend": False},
            "runtime": {},
            "backends": {},
        }
    )
    paths = AssetAcquisitionApp(config=config).app.openapi()["paths"]

    assert "/v1/generate/sam3d" in paths
    assert "/v1/assets/sam3d/{asset_id}" in paths


def test_gateway_rewrites_sam3d_asset_url():
    body = {
        "backend": "sam3d",
        "asset": {"asset_id": "abc", "download_url": "/v1/sam3d/assets/abc"},
    }

    assert rewrite_sam3d_download_url(body)["asset"]["download_url"] == (
        "/v1/assets/sam3d/abc"
    )
