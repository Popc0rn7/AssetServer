import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from assetserver.scenes import SceneConflictError, ScenePackageError, SceneStore


def _package(sdf: str, files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("scene.sdf", sdf)
        for name, content in (files or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _sdf(uri: str = "meshes/chair.glb") -> str:
    return (
        '<sdf version="1.10"><model name="room"><link name="chair">'
        f'<visual name="visual"><geometry><mesh><uri>{uri}</uri></mesh>'
        "</geometry></visual></link></model></sdf>"
    )


def test_create_scene_extracts_assets_and_creates_first_revision(tmp_path):
    store = SceneStore(tmp_path)

    scene = store.create(_package(_sdf(), {"meshes/chair.glb": b"mesh"}))

    assert scene.revision == 1
    assert store.read_sdf(scene.scene_id, 1) == _sdf().encode()
    assert (
        tmp_path / scene.scene_id / "assets/meshes/chair.glb"
    ).read_bytes() == b"mesh"


def test_update_preserves_history_and_rejects_stale_base_revision(tmp_path):
    store = SceneStore(tmp_path)
    scene = store.create(_package(_sdf(), {"meshes/chair.glb": b"mesh"}))
    changed = _sdf().replace('name="room"', 'name="changed"')

    revision = store.update_sdf(scene.scene_id, changed.encode(), base_revision=1)

    assert revision.revision == 2
    assert store.read_sdf(scene.scene_id, 1) == _sdf().encode()
    assert store.read_sdf(scene.scene_id) == changed.encode()
    with pytest.raises(SceneConflictError):
        store.update_sdf(scene.scene_id, _sdf().encode(), base_revision=1)


def test_scene_package_rejects_unsafe_or_unresolved_assets(tmp_path):
    store = SceneStore(tmp_path)

    with pytest.raises(ScenePackageError, match="unsafe archive path"):
        store.create(_package(_sdf(), {"../escape.glb": b"mesh"}))
    with pytest.raises(ScenePackageError, match="unresolved asset"):
        store.create(_package(_sdf()))
    with pytest.raises(ScenePackageError, match="unsupported asset URI"):
        store.create(_package(_sdf("file:///etc/passwd")))


def test_build_package_uses_requested_revision_and_shared_assets(tmp_path):
    store = SceneStore(tmp_path)
    scene = store.create(_package(_sdf(), {"meshes/chair.glb": b"mesh"}))
    changed = _sdf().replace('name="room"', 'name="changed"')
    store.update_sdf(scene.scene_id, changed.encode(), base_revision=1)

    package = store.build_package(scene.scene_id, revision=1)

    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert archive.read("scene.sdf") == _sdf().encode()
        assert archive.read("meshes/chair.glb") == b"mesh"


def test_scene_package_rejects_excessive_uncompressed_content(tmp_path):
    store = SceneStore(tmp_path, max_package_bytes=512)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("scene.sdf", "<sdf/>")
        archive.writestr("meshes/large.glb", b"x" * 1024)

    with pytest.raises(ScenePackageError, match="uncompressed size"):
        store.create(buffer.getvalue())


def test_static_sdf_rejects_plugins_and_unsafe_texture_paths(tmp_path):
    store = SceneStore(tmp_path)
    plugin = '<sdf version="1.10"><model name="x"><plugin name="x" filename="evil.so"/></model></sdf>'
    texture = '<sdf version="1.10"><model name="x"><link name="x"><visual name="x"><material><pbr><metal><albedo_map>../secret.png</albedo_map></metal></pbr></material></visual></link></model></sdf>'

    with pytest.raises(ScenePackageError, match="unsupported static SDF element"):
        store.create(_package(plugin))
    with pytest.raises(ScenePackageError, match="unsupported asset URI"):
        store.create(_package(texture))


def test_concurrent_updates_create_only_one_next_revision(tmp_path):
    store = SceneStore(tmp_path)
    scene = store.create(_package(_sdf(), {"meshes/chair.glb": b"mesh"}))

    def update(name):
        sdf = _sdf().replace('name="room"', f'name="{name}"')
        return store.update_sdf(scene.scene_id, sdf.encode(), base_revision=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update, name) for name in ("one", "two")]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except SceneConflictError:
            outcomes.append("conflict")

    assert sum(item == "conflict" for item in outcomes) == 1
    assert store.latest_revision(scene.scene_id) == 2


def test_scene_package_rejects_duplicate_asset_paths(tmp_path):
    store = SceneStore(tmp_path)
    buffer = io.BytesIO()
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("scene.sdf", _sdf())
            archive.writestr("meshes/chair.glb", b"one")
            archive.writestr("meshes/chair.glb", b"two")

    with pytest.raises(ScenePackageError, match="duplicate archive path"):
        store.create(buffer.getvalue())
