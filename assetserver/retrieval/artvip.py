"""Contract adapter for SceneSmith-preprocessed ArtVIP SDF assets."""

from __future__ import annotations

import json
import hashlib
import math
import xml.etree.ElementTree as ET

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


SUPPORTED_SDF_VERSION = "1.11"
SUPPORTED_JOINT_TYPES = {"revolute", "prismatic"}


class ArtVipContractError(ValueError):
    """The asset is outside the deliberately narrow ArtVIP P1 contract."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"unsupported ArtVIP layout ({reason}): {detail}")
        self.reason = reason


@dataclass(frozen=True)
class ArtVipLayout:
    sdf_path: Path
    root: Path
    model_name: str
    base_link: str
    links: tuple[str, ...]
    joints: tuple[dict, ...]
    visual_parts: tuple[dict, ...]
    dependency_files: tuple[Path, ...]
    dependency_aliases: tuple[tuple[str, Path], ...]

    @property
    def articulated(self) -> bool:
        return bool(self.joints)


def inspect_artvip_sdf(sdf_path: str | Path) -> ArtVipLayout:
    """Validate and describe one preprocessed ArtVIP model directory."""
    sdf_path = Path(sdf_path).resolve()
    root_dir = sdf_path.parent
    try:
        sdf = ET.parse(sdf_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ArtVipContractError("invalid_xml", str(exc)) from exc
    if sdf.tag != "sdf" or sdf.get("version") != SUPPORTED_SDF_VERSION:
        raise ArtVipContractError(
            "sdf_version", f"expected sdf {SUPPORTED_SDF_VERSION}"
        )
    worlds = sdf.findall("world")
    if len(worlds) != 1:
        raise ArtVipContractError("world_count", f"expected 1, found {len(worlds)}")
    models = worlds[0].findall("model")
    if len(models) != 1:
        raise ArtVipContractError("model_count", f"expected 1, found {len(models)}")
    model = models[0]
    model_name = model.get("name")
    if not model_name:
        raise ArtVipContractError("model_name", "model has no name")
    if (model.findtext("static") or "").strip().lower() == "true":
        raise ArtVipContractError("static_model", "preprocessed models must not be static")

    link_elements = model.findall("link")
    link_names = [link.get("name") for link in link_elements]
    if not link_names or any(not name for name in link_names):
        raise ArtVipContractError("links", "links must have non-empty names")
    if len(link_names) != len(set(link_names)):
        raise ArtVipContractError("links", "link names must be unique")
    known_links = set(link_names)

    child_links: set[str] = set()
    joints = []
    joint_names: set[str] = set()
    for joint in model.findall("joint"):
        name = joint.get("name")
        joint_type = joint.get("type")
        parent = (joint.findtext("parent") or "").strip()
        child = (joint.findtext("child") or "").strip()
        if not name or name in joint_names:
            raise ArtVipContractError("joint_name", "joint names must be unique")
        if joint_type not in SUPPORTED_JOINT_TYPES:
            raise ArtVipContractError("joint_type", f"{name}: {joint_type}")
        if parent not in known_links or child not in known_links or parent == child:
            raise ArtVipContractError(
                "joint_links", f"{name}: parent={parent!r}, child={child!r}"
            )
        if child in child_links:
            raise ArtVipContractError("joint_tree", f"link {child} has multiple parents")
        axis = _finite_vector(joint.findtext("axis/xyz") or "", 3, f"{name}.axis")
        lower = _finite_scalar(joint.findtext("axis/limit/lower"), f"{name}.lower")
        upper = _finite_scalar(joint.findtext("axis/limit/upper"), f"{name}.upper")
        if lower > upper:
            raise ArtVipContractError("joint_limits", f"{name}: lower exceeds upper")
        pose_element = joint.find("pose")
        pose = _finite_vector(
            pose_element.text if pose_element is not None and pose_element.text else "0 0 0 0 0 0",
            6,
            f"{name}.pose",
        )
        joint_names.add(name)
        child_links.add(child)
        joints.append(
            {
                "name": name,
                "type": joint_type,
                "parent_link": parent,
                "child_link": child,
                "axis": axis,
                "pose": pose,
                "pose_relative_to": (
                    pose_element.get("relative_to") if pose_element is not None else None
                ),
                "limits": {"lower": lower, "upper": upper},
            }
        )

    roots = known_links - child_links
    if len(roots) != 1:
        raise ArtVipContractError(
            "kinematic_roots", f"expected 1, found {sorted(roots)}"
        )

    dependency_files = {sdf_path}
    dependency_aliases: dict[str, Path] = {}
    visual_parts = []
    for link in link_elements:
        link_name = str(link.get("name"))
        for offset, visual in enumerate(link.findall("visual")):
            uri = _mesh_uri(visual, f"{link_name}.visual[{offset}]")
            if Path(uri).suffix.lower() != ".gltf":
                raise ArtVipContractError("visual_format", f"expected glTF: {uri}")
            path = _resolve_dependency(root_dir, uri)
            gltf_files, gltf_aliases = _gltf_dependencies(root_dir, path)
            dependency_files.update(gltf_files)
            dependency_aliases.update(gltf_aliases)
            pose_element = visual.find("pose")
            pose = _finite_vector(
                pose_element.text if pose_element is not None and pose_element.text else "0 0 0 0 0 0",
                6,
                f"{link_name}.visual[{offset}].pose",
            )
            visual_parts.append(
                {
                    "link": link_name,
                    "entrypoint": path.relative_to(root_dir).as_posix(),
                    "pose": pose,
                    "pose_relative_to": (
                        pose_element.get("relative_to") if pose_element is not None else None
                    ),
                }
            )

        for offset, collision in enumerate(link.findall("collision")):
            geometry = collision.find("geometry")
            mesh = geometry.find("mesh") if geometry is not None else None
            if mesh is None:
                continue
            uri_element = mesh.find("uri")
            if uri_element is None or not (uri_element.text or "").strip():
                raise ArtVipContractError(
                    "collision_uri", f"{link_name}.collision[{offset}]"
                )
            dependency_files.add(
                _resolve_dependency(root_dir, (uri_element.text or "").strip())
            )

    if not visual_parts:
        raise ArtVipContractError("visuals", "model has no glTF visual parts")
    return ArtVipLayout(
        sdf_path=sdf_path,
        root=root_dir,
        model_name=model_name,
        base_link=next(iter(roots)),
        links=tuple(str(name) for name in link_names),
        joints=tuple(sorted(joints, key=lambda item: item["name"])),
        visual_parts=tuple(visual_parts),
        dependency_files=tuple(sorted(dependency_files)),
        dependency_aliases=tuple(sorted(dependency_aliases.items())),
    )


def audit_artvip_dataset(root: str | Path) -> dict:
    """Return a deterministic support report for one local ArtVIP tree."""
    root = Path(root)
    supported = []
    unsupported = []
    joint_types: Counter[str] = Counter()
    visual_formats: Counter[str] = Counter()
    for sdf_path in sorted(root.rglob("*.sdf")):
        resource_id = sdf_path.parent.relative_to(root).as_posix()
        try:
            layout = inspect_artvip_sdf(sdf_path)
        except ArtVipContractError as exc:
            unsupported.append({"resource_id": resource_id, "reason": exc.reason})
            continue
        supported.append(resource_id)
        joint_types.update(item["type"] for item in layout.joints)
        visual_formats.update(
            Path(item["entrypoint"]).suffix.lower().lstrip(".")
            for item in layout.visual_parts
        )
    return {
        "schema_version": "artvip-audit/v1",
        "sdf_count": len(supported) + len(unsupported),
        "supported_count": len(supported),
        "unsupported_count": len(unsupported),
        "joint_types": dict(sorted(joint_types.items())),
        "visual_formats": dict(sorted(visual_formats.items())),
        "supported": supported,
        "unsupported": unsupported,
    }


def _mesh_uri(element: ET.Element, field: str) -> str:
    uri = element.findtext("geometry/mesh/uri")
    if not uri or not uri.strip():
        raise ArtVipContractError("visual_uri", field)
    return uri.strip()


def _finite_vector(value: str, length: int, field: str) -> list[float]:
    try:
        output = [float(item) for item in value.split()]
    except ValueError as exc:
        raise ArtVipContractError("numeric_value", field) from exc
    if len(output) != length or not all(math.isfinite(item) for item in output):
        raise ArtVipContractError("numeric_value", field)
    return output


def _finite_scalar(value: str | None, field: str) -> float:
    if value is None:
        raise ArtVipContractError("joint_limits", f"missing {field}")
    return _finite_vector(value, 1, field)[0]


def _resolve_dependency(root: Path, uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme or parsed.netloc or uri.startswith(("/", "\\")):
        raise ArtVipContractError("external_uri", uri)
    relative = Path(unquote(parsed.path))
    if ".." in relative.parts:
        raise ArtVipContractError("path_escape", uri)
    path = (root / relative).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise ArtVipContractError("missing_dependency", uri)
    return path


def _gltf_dependencies(root: Path, gltf: Path) -> tuple[set[Path], dict[str, Path]]:
    try:
        document = json.loads(gltf.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtVipContractError("invalid_gltf", str(gltf)) from exc
    files = {gltf}
    aliases: dict[str, Path] = {}
    for record in [*(document.get("buffers") or []), *(document.get("images") or [])]:
        uri = record.get("uri") if isinstance(record, dict) else None
        if not uri or uri.startswith("data:"):
            continue
        relative_to_root = gltf.parent.relative_to(root) / unquote(urlparse(uri).path)
        try:
            files.add(_resolve_dependency(root, relative_to_root.as_posix()))
        except ArtVipContractError as exc:
            # SceneSmith's ArtVIP preprocessing leaves some root-level link glTFs
            # referring to textures stored in the generated *_meshes directory.
            # Its asset manager copies those textures beside the glTF. Preserve
            # that known dataset repair as an explicit alias, without accepting
            # arbitrary missing dependencies.
            if exc.reason != "missing_dependency" or Path(uri).name != uri:
                raise
            candidates = sorted(root.glob(f"*_meshes/{uri}"))
            if not candidates:
                raise
            digests = set()
            for candidate in candidates:
                with candidate.open("rb") as stream:
                    digests.add(hashlib.file_digest(stream, "sha256").digest())
            if len(digests) != 1:
                raise ArtVipContractError("ambiguous_texture", uri) from exc
            source = candidates[0].resolve()
            files.add(source)
            aliases[relative_to_root.as_posix()] = source
    return files, aliases
