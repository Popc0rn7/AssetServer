"""Engine-neutral validation and export metadata for simulation assets."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET

from pathlib import PurePosixPath
from typing import Any

from assetserver.asset_store import StoredAsset


class SimulationAssetError(ValueError):
    pass


def simulation_asset_payload(stored: StoredAsset) -> dict[str, Any]:
    """Validate collision references and describe portable simulation content."""
    manifest = stored.manifest
    simulation = manifest.get("simulation")
    if not isinstance(simulation, dict):
        raise SimulationAssetError("asset has no simulation entrypoint")
    simulation_name = simulation["entrypoint"]
    records = {item["path"]: item for item in manifest["files"]}
    content = stored.root.joinpath("files", *simulation_name.split("/")).read_bytes()
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise SimulationAssetError("simulation entrypoint is invalid XML") from exc
    articulated = bool(manifest.get("joints")) or bool(
        (manifest.get("metadata") or {}).get("articulated")
    )
    declared = {
        item["entrypoint"]: item for item in manifest.get("collision") or []
    }
    visual_names = {manifest["visual"]["entrypoint"]} | {
        part["entrypoint"] for part in manifest["visual"].get("parts") or []
    }
    if root.tag == "sdf":
        geometries = _sdf_collisions(
            root,
            simulation_name,
            records,
            declared,
            visual_names,
            articulated,
        )
    elif root.tag == "robot":
        geometries = _urdf_collisions(
            root,
            simulation_name,
            records,
            declared,
            visual_names,
            articulated,
        )
    else:
        raise SimulationAssetError("unsupported simulation XML root")
    links = {item.get("name") for item in root.iter("link") if item.get("name")}
    if not links:
        raise SimulationAssetError("simulation contains no links")
    covered = {item["link"] for item in geometries}
    required = links if articulated else {simulation["base_link"]}
    missing = sorted(required - covered)
    if missing:
        raise SimulationAssetError(f"links have no collision geometry: {missing}")
    for item in geometries:
        if "path" in item:
            item["entrypoint"] = item["path"]
            item["path"] = _asset_file_path(stored, item["path"])
    return {
        "asset_ref": stored.asset_ref,
        "asset_digest": stored.digest,
        "simulation": {
            "path": _asset_file_path(stored, simulation_name),
            "sha256": records[simulation_name]["sha256"],
            "base_link": simulation["base_link"],
            "transform_to_asset": simulation["transform_to_asset"],
        },
        "collision_geometries": geometries,
    }


def _sdf_collisions(
    root: ET.Element,
    simulation_name: str,
    records: dict[str, dict[str, Any]],
    declared: dict[str, dict[str, Any]],
    visual_names: set[str],
    articulated: bool,
) -> list[dict[str, Any]]:
    output = []
    for link in root.iter("link"):
        link_name = link.get("name")
        if not link_name:
            continue
        for index, collision in enumerate(link.findall("collision")):
            geometry = collision.find("geometry")
            if geometry is None:
                raise SimulationAssetError("collision has no geometry")
            item = _geometry(
                geometry,
                uri=geometry.findtext("mesh/uri"),
                simulation_name=simulation_name,
                records=records,
                declared=declared,
                visual_names=visual_names,
                articulated=articulated,
                convex_declared=(
                    geometry.find("mesh/{drake.mit.edu}declare_convex") is not None
                ),
                xml_style="sdf",
            )
            pose = collision.find("pose")
            output.append(
                {
                    "link": link_name,
                    "name": collision.get("name") or f"collision_{index:03d}",
                    "pose": _numbers(pose.text, 6) if pose is not None and pose.text else [0.0] * 6,
                    **item,
                }
            )
    return output


def _urdf_collisions(
    root: ET.Element,
    simulation_name: str,
    records: dict[str, dict[str, Any]],
    declared: dict[str, dict[str, Any]],
    visual_names: set[str],
    articulated: bool,
) -> list[dict[str, Any]]:
    output = []
    for link in root.findall("link"):
        link_name = link.get("name")
        if not link_name:
            continue
        for index, collision in enumerate(link.findall("collision")):
            geometry = collision.find("geometry")
            if geometry is None:
                raise SimulationAssetError("collision has no geometry")
            mesh = geometry.find("mesh")
            origin = collision.find("origin")
            xyz = _numbers(origin.get("xyz"), 3) if origin is not None else [0.0] * 3
            rpy = _numbers(origin.get("rpy"), 3) if origin is not None else [0.0] * 3
            output.append(
                {
                    "link": link_name,
                    "name": collision.get("name") or f"collision_{index:03d}",
                    "pose": xyz + rpy,
                    **_geometry(
                        geometry,
                        uri=mesh.get("filename") if mesh is not None else None,
                        simulation_name=simulation_name,
                        records=records,
                        declared=declared,
                        visual_names=visual_names,
                        articulated=articulated,
                        convex_declared=False,
                        xml_style="urdf",
                    ),
                }
            )
    return output


def _geometry(
    geometry: ET.Element,
    *,
    uri: str | None,
    simulation_name: str,
    records: dict[str, dict[str, Any]],
    declared: dict[str, dict[str, Any]],
    visual_names: set[str],
    articulated: bool,
    convex_declared: bool,
    xml_style: str,
) -> dict[str, Any]:
    if uri is not None:
        resolved = _resolve_uri(simulation_name, uri)
        if resolved not in records:
            raise SimulationAssetError(f"unresolved collision mesh: {uri}")
        if resolved in visual_names and not articulated:
            raise SimulationAssetError("rigid collision still references the visual mesh")
        declaration = declared.get(resolved)
        method = declaration.get("method") if declaration else None
        if not articulated and method in {None, "triangle-mesh"} and not convex_declared:
            raise SimulationAssetError("rigid collision mesh is not declared convex")
        convex = convex_declared or method in {"coacd", "convex", "convex-hull"}
        return {
            "representation": "convex-mesh" if convex else "mesh",
            "path": resolved,
            "sha256": records[resolved]["sha256"],
            "method": method or ("declared-convex" if convex_declared else "asset-provided"),
            "profile": declaration.get("profile") if declaration else None,
            "parameters_sha256": declaration.get("parameters_sha256") if declaration else None,
            "transform_to_asset": (
                declaration.get("transform_to_asset") if declaration else None
            ),
        }
    for shape in ("box", "sphere", "cylinder", "capsule"):
        element = geometry.find(shape)
        if element is None:
            continue
        if xml_style == "sdf":
            parameters = {
                child.tag: _numbers(child.text, None)
                for child in element
                if child.text
            }
        else:
            parameters = {
                key: _numbers(value, None) for key, value in sorted(element.attrib.items())
            }
        return {"representation": "primitive", "shape": shape, "parameters": parameters}
    raise SimulationAssetError("unsupported collision geometry")


def _resolve_uri(simulation_name: str, value: str) -> str:
    if "://" in value or value.startswith("/") or "\\" in value:
        raise SimulationAssetError("collision mesh URI must be package-relative")
    base = PurePosixPath(simulation_name).parent.as_posix()
    resolved = posixpath.normpath(posixpath.join(base, value.strip()))
    if resolved.startswith("../"):
        raise SimulationAssetError("collision mesh URI escapes the asset")
    return resolved


def _asset_file_path(stored: StoredAsset, relative: str) -> str:
    return f"assets/sha256/{stored.digest[:2]}/{stored.digest}/files/{relative}"


def _numbers(value: str | None, expected: int | None) -> list[float] | float:
    if value is None:
        values: list[float] = []
    else:
        try:
            values = [float(item) for item in value.split()]
        except ValueError as exc:
            raise SimulationAssetError("collision parameter is not numeric") from exc
    if expected is not None and len(values) != expected:
        raise SimulationAssetError("collision parameter has the wrong dimension")
    return values[0] if expected is None and len(values) == 1 else values
