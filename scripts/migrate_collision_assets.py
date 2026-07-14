#!/usr/bin/env python3
"""Create explicit Scene IR revisions that reference collision-ready assets."""

from __future__ import annotations

import argparse
import asyncio
import json

from dataclasses import replace
from pathlib import Path

from assetserver.asset_store import ContentAddressedAssetStore
from assetserver.config import config_to_container, load_assetserver_config
from assetserver.postprocess.collision import CollisionPostprocessor
from assetserver.postprocess.config import PostprocessConfig
from assetserver.scene_ir import SceneIR, dump_scene_yaml, load_scene_yaml
from assetserver.scene_ir_store import IRSceneStore


async def migrate(args: argparse.Namespace) -> dict[str, str]:
    cfg = config_to_container(load_assetserver_config(args.config))
    data_root = Path(args.data_root or cfg["server"]["storage"]["data_root"])
    assets = ContentAddressedAssetStore(data_root / "assets")
    postprocess = PostprocessConfig.from_mapping(cfg["runtime"]["postprocess"])
    if args.database:
        postprocess = replace(postprocess, database=Path(args.database))
    if args.staging_root:
        postprocess = replace(postprocess, staging_root=Path(args.staging_root))
    service = CollisionPostprocessor(assets, postprocess)
    scenes = IRSceneStore(data_root / "scenes", assets)
    mapping: dict[str, str] = {}

    scene_dirs = sorted((data_root / "scenes").glob("*/manifest.json"))
    selected = set(args.scene_id or [])
    for manifest_path in scene_dirs:
        if json.loads(manifest_path.read_text()).get("schema_version") != "scene-ir/v1":
            continue
        scene_id = manifest_path.parent.name
        if selected and scene_id not in selected:
            continue
        revision = scenes.revision(scene_id)
        scene = load_scene_yaml(scenes.read(scene_id, revision.revision))
        for ref in sorted(scene.asset_refs()):
            if ref not in mapping:
                mapping[ref] = (
                    await service.ensure_simulation_ready(ref)
                ).asset_ref
        updated = _replace_refs(scene, mapping)
        if updated != scene:
            scenes.update(
                scene_id,
                dump_scene_yaml(updated),
                base_revision=revision.revision,
            )
    return mapping


def _replace_refs(scene: SceneIR, mapping: dict[str, str]) -> SceneIR:
    value = scene.model_dump(mode="json")
    for room in value["rooms"]:
        room["shell"]["asset_ref"] = mapping[room["shell"]["asset_ref"]]
    for item in value["objects"]:
        item["asset_ref"] = mapping[item["asset_ref"]]
    return SceneIR.model_validate(value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill collision assets and create explicit Scene IR revisions"
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--database", default=None)
    parser.add_argument("--staging-root", default=None)
    parser.add_argument("--scene-id", action="append")
    parser.add_argument("--mapping-output", required=True)
    args = parser.parse_args()
    mapping = asyncio.run(migrate(args))
    Path(args.mapping_output).write_text(
        json.dumps(mapping, indent=2, sort_keys=True) + "\n"
    )
    print(f"migrated {len(mapping)} asset reference(s)")


if __name__ == "__main__":
    main()
