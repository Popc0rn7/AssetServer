import json

from pathlib import Path

import pytest

from assetserver.retrieval.artvip import (
    ArtVipContractError,
    audit_artvip_dataset,
    inspect_artvip_sdf,
)


def _write_model(root: Path, *, second_root: bool = False) -> Path:
    root.mkdir(parents=True)
    (root / "body.gltf").write_text(
        json.dumps({"asset": {"version": "2.0"}, "scenes": [{"nodes": []}]})
    )
    extra = '<link name="orphan"/>' if second_root else ""
    (root / "model.sdf").write_text(
        f"""<sdf version="1.11"><world name="root"><model name="cabinet">
        <link name="body"><visual name="body_visual"><geometry><mesh>
        <uri>body.gltf</uri></mesh></geometry></visual></link>
        <link name="door"/>{extra}
        <joint name="door_joint" type="revolute"><pose relative_to="body">0 0 1 0 0 0</pose>
        <parent>body</parent><child>door</child><axis><xyz>0 0 1</xyz>
        <limit><lower>-1.5</lower><upper>0</upper></limit></axis></joint>
        </model></world></sdf>"""
    )
    return root / "model.sdf"


def test_inspect_artvip_derives_declared_root_joints_and_visuals(tmp_path):
    layout = inspect_artvip_sdf(_write_model(tmp_path / "cabinet"))

    assert layout.base_link == "body"
    assert layout.links == ("body", "door")
    assert layout.joints[0]["child_link"] == "door"
    assert layout.joints[0]["limits"] == {"lower": -1.5, "upper": 0.0}
    assert layout.visual_parts[0]["entrypoint"] == "body.gltf"


def test_inspect_artvip_rejects_multiple_kinematic_roots(tmp_path):
    with pytest.raises(ArtVipContractError, match="kinematic_roots"):
        inspect_artvip_sdf(_write_model(tmp_path / "cabinet", second_root=True))


def test_audit_reports_supported_and_unsupported_resources(tmp_path):
    _write_model(tmp_path / "category" / "supported")
    _write_model(tmp_path / "category" / "unsupported", second_root=True)

    report = audit_artvip_dataset(tmp_path)

    assert report["sdf_count"] == 2
    assert report["supported_count"] == 1
    assert report["unsupported"] == [
        {"resource_id": "category/unsupported", "reason": "kinematic_roots"}
    ]
