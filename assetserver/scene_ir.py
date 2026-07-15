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

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_scene_ir",
        details: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


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


class RoomOpening(StrictModel):
    id: str
    opening_type: Literal["door", "window", "open"]
    wall: Literal["north", "south", "east", "west"]
    offset_m: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    sill_height: float = Field(default=0, ge=0)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid opening id")
        return value

    @field_validator("offset_m", "width", "height", "sill_height")
    @classmethod
    def finite_values(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("opening values must be finite")
        return value


class ProceduralRoomShell(StrictModel):
    kind: Literal["procedural"]
    dimensions: tuple[float, float, float]
    wall_thickness: float = Field(default=0.05, gt=0)
    floor_thickness: float = Field(default=0.1, gt=0)
    include_ceiling: bool = False
    openings: list[RoomOpening] = Field(default_factory=list)

    @field_validator("dimensions")
    @classmethod
    def valid_dimensions(
        cls, value: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        if not all(math.isfinite(item) and item > 0 for item in value):
            raise ValueError("room dimensions must be finite and positive")
        return value

    @field_validator("wall_thickness", "floor_thickness")
    @classmethod
    def finite_thickness(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("room thickness must be finite")
        return value


AssetRoomShell = RoomShell
RoomShellValue = RoomShell | ProceduralRoomShell


class Room(StrictModel):
    id: str
    type: str = "room"
    name: str | None = None
    transform: Transform = Field(default_factory=Transform)
    shell: RoomShellValue
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)

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
        if not all(
            _ID.fullmatch(name) and math.isfinite(position)
            for name, position in value.items()
        ):
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
                raise ValueError(
                    f"object {obj.id} references unknown room {obj.room_id}"
                )
            if obj.placement:
                parent = obj.placement.parent_object_id
                if parent == obj.id or parent not in known_objects:
                    raise ValueError(f"object {obj.id} has invalid parent {parent}")
        return self

    def asset_refs(self) -> set[str]:
        room_refs = {
            room.shell.asset_ref
            for room in self.rooms
            if isinstance(room.shell, AssetRoomShell)
        }
        return room_refs | {obj.asset_ref for obj in self.objects}


def load_scene_yaml(content: bytes | str) -> SceneIR:
    """Safely parse and validate one complete Scene IR YAML document."""
    try:
        value = yaml.safe_load(content)
        if not isinstance(value, dict):
            raise SceneIRValidationError("Scene IR must be a YAML mapping")
        _validate_procedural_shells(value)
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


def _validate_procedural_shells(value: dict[str, Any]) -> None:
    rooms = value.get("rooms")
    if not isinstance(rooms, list):
        return
    for room in rooms:
        if not isinstance(room, dict) or not isinstance(room.get("shell"), dict):
            continue
        shell = room["shell"]
        if "kind" in shell and shell.get("kind") != "procedural":
            raise SceneIRValidationError(
                f"room '{room.get('id', 'unknown')}' uses an unsupported shell kind",
                code="procedural_shell_unsupported",
                details={"room_id": str(room.get("id", "unknown"))},
            )
        if shell.get("kind") != "procedural":
            continue
        room_id = str(room.get("id", "unknown"))
        dimensions = shell.get("dimensions")
        if not (
            isinstance(dimensions, (list, tuple))
            and len(dimensions) == 3
            and all(
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(item)
                and item > 0
                for item in dimensions
            )
        ):
            raise SceneIRValidationError(
                f"room '{room_id}' dimensions must contain three positive finite values",
                code="invalid_room_dimensions",
                details={"room_id": room_id},
            )
        x, y, z = (float(item) for item in dimensions)
        openings = shell.get("openings", [])
        if not isinstance(openings, list):
            continue
        ids: set[str] = set()
        intervals: dict[str, list[tuple[float, float, str]]] = {}
        for opening in openings:
            if not isinstance(opening, dict):
                continue
            opening_id = str(opening.get("id", "unknown"))
            details = {"room_id": room_id, "opening_id": opening_id}
            if opening_id in ids:
                raise SceneIRValidationError(
                    f"duplicate opening id '{opening_id}' in room '{room_id}'",
                    code="duplicate_opening_id",
                    details=details,
                )
            ids.add(opening_id)
            try:
                wall = str(opening["wall"])
                offset = float(opening["offset_m"])
                width = float(opening["width"])
                height = float(opening["height"])
                sill = float(opening.get("sill_height", 0))
            except (KeyError, TypeError, ValueError):
                continue
            wall_length = x if wall in {"north", "south"} else y
            if (
                not all(math.isfinite(item) for item in (offset, width, height, sill))
                or offset < 0
                or width <= 0
                or height <= 0
                or sill < 0
                or offset + width > wall_length + 1e-9
                or sill + height > z + 1e-9
            ):
                raise SceneIRValidationError(
                    f"opening '{opening_id}' exceeds {wall} wall bounds",
                    code="opening_out_of_bounds",
                    details=details,
                )
            if opening.get("opening_type") == "open" and not (
                abs(sill) <= 1e-9 and abs(height - z) <= 1e-9
            ):
                raise SceneIRValidationError(
                    f"opening '{opening_id}' must be floor-to-ceiling",
                    code="invalid_opening_semantics",
                    details=details,
                )
            wall_intervals = intervals.setdefault(wall, [])
            for start, end, other_id in wall_intervals:
                if offset < end - 1e-9 and start < offset + width - 1e-9:
                    raise SceneIRValidationError(
                        f"opening '{opening_id}' overlaps '{other_id}' on {wall} wall",
                        code="opening_overlap",
                        details=details,
                    )
            wall_intervals.append((offset, offset + width, opening_id))
