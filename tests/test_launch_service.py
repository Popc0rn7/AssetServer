from pathlib import Path
import subprocess


SCRIPT = Path("scripts/launch_service.sh")


def test_launch_service_is_valid_bash_and_replaces_model_service():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111
    assert not Path("scripts/model_service.sh").exists()
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_launch_service_manages_all_four_local_services():
    source = SCRIPT.read_text()

    assert "services=(openclip sam3d scene-viewer postprocess)" in source
    assert "assetserver.openclip_server.standalone" in source
    assert "assetserver.generation_server.standalone" in source
    assert "config/generate/sam3d.yaml" in source
    assert "assetserver.postprocess_server.standalone_server" in source
    assert '"$PYTHON" -m assetserver.job_worker' in source
    assert "observe=assetserver.scene_job_handlers:observe" in source
    assert "validate=assetserver.scene_job_handlers:validate" in source
    assert "export=assetserver.scene_job_handlers:export" in source


def test_launch_service_help_lists_services(tmp_path):
    result = subprocess.run(
        [str(SCRIPT), "help"],
        env={
            "PATH": "/usr/bin:/bin",
            "ASSETSERVER_SERVICE_STATE_ROOT": str(tmp_path / "state"),
            "ASSETSERVER_SERVICE_LOG_ROOT": str(tmp_path / "logs"),
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert "openclip, sam3d, scene-viewer, postprocess, all" in result.stdout
