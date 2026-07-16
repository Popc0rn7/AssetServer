"""Strict public schemas for placement-intent and placement jobs."""

from __future__ import annotations

import math
import urllib.parse

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PLACEMENT_CONSTRAINT_TYPES = {
    "inside_room",
    "floor_supported",
    "surface_supported",
    "against_wall",
    "facing",
    "distance_range",
    "clearance_zone",
    "avoid_openings",
    "articulation_clearance",
    "reachable",
}
ALLOWED_OPERATIONS = {"translate", "rotate_z", "snap_to_support"}


class PlacementSchemaError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_placement_intent"):
        super().__init__(message)
        self.code = code


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupportIntent(StrictModel):
    type: Literal["floor", "surface"]
    room_id: str | None = None
    parent_object_id: str | None = None
    support_surface_id: str | None = None

    @model_validator(mode="after")
    def valid_reference(self):
        if self.type == "floor" and not self.room_id:
            raise ValueError("floor support requires room_id")
        if self.type == "surface" and (
            not self.parent_object_id or not self.support_surface_id
        ):
            raise ValueError("surface support requires parent_object_id and support_surface_id")
        return self


class PlacementConstraint(StrictModel):
    id: str
    type: str
    required: bool = True
    room_id: str | None = None
    wall: Literal["north", "south", "east", "west"] | None = None
    distance_range: tuple[float, float] | None = None
    target: dict[str, Any] | None = None
    angular_tolerance_degrees: float | None = Field(default=None, ge=0, le=180)
    zone_id: str | None = None
    parent_object_id: str | None = None
    support_surface_id: str | None = None
    object_id: str | None = None

    @model_validator(mode="after")
    def supported_and_valid(self):
        if self.type not in PLACEMENT_CONSTRAINT_TYPES:
            raise PlacementSchemaError(
                f"unsupported placement constraint: {self.type}",
                code="unsupported_placement_constraint",
            )
        if self.distance_range is not None:
            low, high = self.distance_range
            if not (math.isfinite(low) and math.isfinite(high) and 0 <= low <= high):
                raise ValueError("distance_range must be finite, non-negative, and ordered")
        return self


class PlacementIntent(StrictModel):
    schema_version: Literal["placement-intent/v1"]
    object_id: str
    support: SupportIntent | None = None
    constraints: list[PlacementConstraint] = Field(default_factory=list, max_length=64)
    locked_object_ids: list[str] = Field(default_factory=list, max_length=256)
    allowed_operations: list[str] = Field(
        default_factory=lambda: ["translate", "rotate_z"], max_length=3
    )

    @field_validator("allowed_operations")
    @classmethod
    def valid_operations(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - ALLOWED_OPERATIONS)
        if unknown:
            raise ValueError(f"unsupported allowed_operations: {unknown}")
        return list(dict.fromkeys(value))


class ProposalOptions(StrictModel):
    max_candidates: int = Field(default=5, ge=1, le=50)
    seed: int = 0
    collision_margin: float = Field(default=0.002, ge=0, le=0.1)
    solver_time_limit_seconds: float = Field(default=20, gt=0, le=300)


class PlacementProposalRequest(StrictModel):
    revision: int = Field(ge=1)
    scene_sha256: str
    intents: list[PlacementIntent] = Field(min_length=1, max_length=32)
    options: ProposalOptions = Field(default_factory=ProposalOptions)

    @field_validator("scene_sha256")
    @classmethod
    def valid_sha(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("scene_sha256 must be 64 lowercase hexadecimal characters")
        return value


class ReachabilityOptions(StrictModel):
    enabled: bool = False
    agent_radius: float = Field(default=0.3, gt=0, le=2)
    grid_resolution: float = Field(default=0.05, gt=0, le=0.5)


class RoomPlacementValidationRequest(StrictModel):
    revision: int = Field(ge=1)
    scene_sha256: str
    profile: Literal["room-placement/v1"] = "room-placement/v1"
    intents: list[PlacementIntent] = Field(default_factory=list, max_length=32)
    penetration_epsilon: float = Field(default=0.000001, ge=0)
    support_contact_tolerance: float = Field(default=0.001, ge=0)
    room_boundary_tolerance: float = Field(default=0.001, ge=0)
    reachability: ReachabilityOptions = Field(default_factory=ReachabilityOptions)

    _valid_sha = field_validator("scene_sha256")(
        PlacementProposalRequest.valid_sha.__func__
    )


class RepairOptions(StrictModel):
    seed: int = 0
    solver_time_limit_seconds: float = Field(default=10, gt=0, le=300)


class PlacementRepairRequest(StrictModel):
    revision: int = Field(ge=1)
    scene_sha256: str
    issue_ids: list[str] = Field(min_length=1, max_length=64)
    intents: list[PlacementIntent] = Field(default_factory=list, max_length=32)
    locked_object_ids: list[str] = Field(default_factory=list, max_length=256)
    allowed_operations: list[str] = Field(min_length=1, max_length=3)
    options: RepairOptions = Field(default_factory=RepairOptions)

    _valid_sha = field_validator("scene_sha256")(
        PlacementProposalRequest.valid_sha.__func__
    )
    _valid_operations = field_validator("allowed_operations")(
        PlacementIntent.valid_operations.__func__
    )


def issue_id(code: str, object_ids: list[str], constraint_id: str | None = None) -> str:
    parts = [code, constraint_id or "-", *sorted(set(object_ids))]
    return ":".join(urllib.parse.quote(part, safe="-._~") for part in parts)


def issue(
    code: str,
    object_ids: list[str],
    *,
    metric: float,
    threshold: float | list[float],
    units: str,
    message: str,
    constraint_id: str | None = None,
    evidence: dict[str, Any] | None = None,
    repair_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    objects = sorted(set(object_ids))
    value = {
        "issue_id": issue_id(code, objects, constraint_id),
        "code": code,
        "severity": "error",
        "object_ids": objects,
        "constraint_id": constraint_id,
        "metric": metric,
        "threshold": threshold,
        "units": units,
        "message": message,
        "evidence": evidence or {},
    }
    if repair_hint is not None:
        value["repair_hint"] = repair_hint
    return value
