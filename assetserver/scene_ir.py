"""Agent-facing, versioned scene intermediate representation."""

from __future__ import annotations

import hashlib
import math
import re

from typing import Any, Literal

import yaml

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "scene-ir/v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ASSET_REF = re.compile(r"^asset://sha256/[0-9a-f]{64}$")


class SceneIRValidationError(ValueError):
    """The submitted Scene IR is syntactically or semantically invalid."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Transform(StrictModel):
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_rpy_degrees: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @field_validator("translation", "rotation_rpy_degrees")
    @classmethod
    def finite_values(cls, value: tuple[float, float, float]):
        if not all(math.isfinite(item) for item in value):
            raise ValueError("transform values must be finite")
        return value


class RoomShell(StrictModel):
    asset_ref: str

    @field_validator("asset_ref")
    @classmethod
    def valid_asset_ref(cls, value: str) -> str:
        if not _ASSET_REF.fullmatch(value):
            raise ValueError("asset_ref must be asset://sha256/<64 lowercase hex>")
        return value


class Room(StrictModel):
    id: str
    type: str = "room"
    name: str | None = None
    transform: Transform = Field(default_factory=Transform)
    shell: RoomShell
    metadata: dict[str, str | float | bool] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid room id")
        return value


class Placement(StrictModel):
    parent_object_id: str
    support_surface: str | None = None

    @field_validator("parent_object_id")
    @classmethod
    def valid_parent_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid parent object id")
        return value


class SceneObject(StrictModel):
    id: str
    room_id: str
    name: str
    description: str = ""
    category: str
    asset_ref: str
    transform: Transform = Field(default_factory=Transform)
    scale: float = Field(default=1.0, gt=0)
    mobility: Literal["static", "dynamic"] = "static"
    initial_joints: dict[str, float] = Field(default_factory=dict)
    placement: Placement | None = None
    immutable: bool = False
    metadata: dict[str, str | float | bool] = Field(default_factory=dict)

    @field_validator("id", "room_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid object or room id")
        return value

    @field_validator("asset_ref")
    @classmethod
    def valid_asset_ref(cls, value: str) -> str:
        if not _ASSET_REF.fullmatch(value):
            raise ValueError("asset_ref must be asset://sha256/<64 lowercase hex>")
        return value

    @field_validator("scale")
    @classmethod
    def finite_scale(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("scale must be finite")
        return value

    @field_validator("initial_joints")
    @classmethod
    def finite_joints(cls, value: dict[str, float]) -> dict[str, float]:
        if not all(_ID.fullmatch(name) and math.isfinite(position) for name, position in value.items()):
            raise ValueError("joint names must be valid ids and positions finite")
        return value


class SceneIR(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    scene_id: str | None = None
    description: str = ""
    rooms: list[Room] = Field(min_length=1)
    objects: list[SceneObject] = Field(default_factory=list)
    metadata: dict[str, str | float | bool] = Field(default_factory=dict)

    @field_validator("scene_id")
    @classmethod
    def valid_scene_id(cls, value: str | None) -> str | None:
        if value is not None and not _ID.fullmatch(value):
            raise ValueError("invalid scene id")
        return value

    @model_validator(mode="after")
    def references_are_consistent(self):
        room_ids = [room.id for room in self.rooms]
        object_ids = [obj.id for obj in self.objects]
        if len(room_ids) != len(set(room_ids)):
            raise ValueError("duplicate room id")
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("duplicate object id")
        known_rooms = set(room_ids)
        known_objects = set(object_ids)
        for obj in self.objects:
            if obj.room_id not in known_rooms:
                raise ValueError(f"object {obj.id} references unknown room {obj.room_id}")
            if obj.placement:
                parent = obj.placement.parent_object_id
                if parent == obj.id or parent not in known_objects:
                    raise ValueError(f"object {obj.id} has invalid parent {parent}")
        return self

    def asset_refs(self) -> set[str]:
        return {room.shell.asset_ref for room in self.rooms} | {
            obj.asset_ref for obj in self.objects
        }


def load_scene_yaml(content: bytes | str) -> SceneIR:
    """Safely parse and validate one complete Scene IR YAML document."""
    try:
        value = yaml.safe_load(content)
        if not isinstance(value, dict):
            raise SceneIRValidationError("Scene IR must be a YAML mapping")
        return SceneIR.model_validate(value)
    except SceneIRValidationError:
        raise
    except Exception as exc:
        raise SceneIRValidationError(str(exc)) from exc


def dump_scene_yaml(scene: SceneIR) -> bytes:
    """Return deterministic, normalized YAML for hashing and revisions."""
    value: dict[str, Any] = scene.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(
        value,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).encode("utf-8")


def scene_sha256(scene: SceneIR) -> str:
    return hashlib.sha256(dump_scene_yaml(scene)).hexdigest()
