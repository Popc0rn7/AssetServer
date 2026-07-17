from omegaconf import OmegaConf

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp


def test_gateway_exposes_only_public_v2_generation_route():
    config = OmegaConf.create(
        {"server": {}, "runtime": {}, "backends": {}}
    )
    paths = AssetAcquisitionApp(config=config).app.openapi()["paths"]

    assert "/v2/generate/{backend}" in paths
    generation_paths = {
        path for path in paths if "generate" in path or "sam3d" in path
    }
    assert generation_paths == {"/v2/generate/{backend}"}
