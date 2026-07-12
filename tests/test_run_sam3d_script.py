from pathlib import Path


def test_run_script_initializes_bind_mounted_asset_store_for_service_user():
    script = Path("scripts/run_sam3d_docker.sh").read_text()

    initializer = 'run --rm --user 0 --entrypoint /bin/sh'
    service_run = 'run --name assetserver-sam3d'
    assert initializer in script
    assert '-v "$ASSETS:/assets"' in script
    assert "chown 10001:10001 /assets" in script
    assert script.index(initializer) < script.index(service_run)


def test_manual_and_gateway_launch_share_outputs_asset_root():
    script = Path("scripts/run_sam3d_docker.sh").read_text()
    config = Path("config/generate/sam3d.yaml").read_text()

    assert 'ASSETS="${SAM3D_ASSETS:-$PWD/outputs/sam3d}"' in script
    assert (
        '"${oc.env:ASSETSERVER_HOST_ROOT,.}/outputs/sam3d:'
        '/var/lib/sam3d/assets"'
    ) in config
