from pathlib import Path


def test_scene_worker_startup_log_is_explicit_and_readable():
    source = Path("assetserver/job_worker.py").read_text()

    assert "| %(levelname)-5s | scene-viewer |" in source
    assert '"READY  worker=%s  handlers=[%s]"' in source
    assert '"Polling %s every %.1fs  (job heartbeat %.0fs, lease %.0fs)"' in source
    assert "heartbeat_ready.wait(timeout=5)" in source
