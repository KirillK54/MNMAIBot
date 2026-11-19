import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MilitaryChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metrics: dict[str, float] = Field(default_factory=dict)
    units: dict[str, float] = Field(default_factory=dict)
    equipment: dict[str, float] = Field(default_factory=dict)
    ammunition: dict[str, float] = Field(default_factory=dict)

    @field_validator("metrics", "units", "equipment", "ammunition")
    @classmethod
    def validate_numeric_deltas(cls, values: dict[str, float]) -> dict[str, float]:
        if len(values) > 40:
            raise ValueError("Слишком много военных показателей")
        for key, amount in values.items():
            if (
                not key
                or len(key) > 100
                or not math.isfinite(float(amount))
                or abs(float(amount)) > 1_000_000_000
            ):
                raise ValueError("Некорректное военное изменение")
        return values


class ProjectProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    facility_type: str = Field(min_length=1, max_length=80)
    cost: float = Field(ge=0, le=1_000_000_000_000)
    resource_costs: dict[str, float] = Field(default_factory=dict)
    production_per_tick: dict[str, float]
    input_per_tick: dict[str, float] = Field(default_factory=dict)
    operating_cost: float = Field(default=0, ge=0, le=1_000_000_000)
    interval_hours: int = Field(default=4, ge=1, le=720)
    duration_turns: int = Field(ge=1, le=100)
    required_technology: str | None = Field(default=None, max_length=160)

    @field_validator("resource_costs", "production_per_tick", "input_per_tick")
    @classmethod
    def validate_project_resources(cls, values: dict[str, float]) -> dict[str, float]:
        if len(values) > 20:
            raise ValueError("Слишком много ресурсов в проекте")
        for key, amount in values.items():
            if (
                not key
                or len(key) > 100
                or not math.isfinite(float(amount))
                or amount <= 0
                or amount > 1_000_000
            ):
                raise ValueError("Некорректный состав проекта")
        return values


class ResearchProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    technology_name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2000)
    duration_turns: int = Field(ge=1, le=5)


class ProposedChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metrics: dict[str, float] = Field(default_factory=dict)
    resources: dict[str, float] = Field(default_factory=dict)
    military: MilitaryChanges = Field(default_factory=MilitaryChanges)
    projects: list[ProjectProposal] = Field(default_factory=list, max_length=3)
    research: list[ResearchProposal] = Field(default_factory=list, max_length=1)

    @field_validator("metrics", "resources")
    @classmethod
    def finite_and_bounded(cls, values: dict[str, float]) -> dict[str, float]:
        if len(values) > 40:
            raise ValueError("Слишком много показателей в изменениях")
        for key, amount in values.items():
            if (
                not key
                or len(key) > 100
                or not math.isfinite(float(amount))
                or abs(float(amount)) > 1_000_000_000_000
            ):
                raise ValueError("Некорректное игровое изменение")
        return values


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_game_action: bool = True
    status: Literal[
        "success",
        "partial_success",
        "failure",
        "catastrophic_failure",
        "rejected",
        "delayed",
        "conditions_required",
    ]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    reasoning: str = Field(default="", max_length=4000)
    positives: list[str] = Field(default_factory=list, max_length=8)
    negatives: list[str] = Field(default_factory=list, max_length=8)
    conditions: list[str] = Field(default_factory=list, max_length=8)
    changes: ProposedChanges = Field(default_factory=ProposedChanges)
    duration_turns: int = Field(default=0, ge=0, le=100)


JUDGE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_game_action",
        "status",
        "title",
        "description",
        "reasoning",
        "positives",
        "negatives",
        "conditions",
        "changes",
        "duration_turns",
    ],
    "properties": {
        "is_game_action": {"type": "boolean"},
        "status": {
            "type": "string",
            "enum": [
                "success",
                "partial_success",
                "failure",
                "catastrophic_failure",
                "rejected",
                "delayed",
                "conditions_required",
            ],
        },
        "title": {"type": "string", "maxLength": 200},
        "description": {"type": "string", "maxLength": 4000},
        "reasoning": {"type": "string", "maxLength": 4000},
        "positives": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "negatives": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "conditions": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "changes": {
            "type": "object",
            "additionalProperties": False,
            "required": ["metrics", "resources", "military", "projects", "research"],
            "properties": {
                "metrics": {"type": "object", "additionalProperties": {"type": "number"}},
                "resources": {"type": "object", "additionalProperties": {"type": "number"}},
                "military": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["metrics", "units", "equipment", "ammunition"],
                    "properties": {
                        "metrics": {"type": "object", "additionalProperties": {"type": "number"}},
                        "units": {"type": "object", "additionalProperties": {"type": "number"}},
                        "equipment": {"type": "object", "additionalProperties": {"type": "number"}},
                        "ammunition": {
                            "type": "object",
                            "additionalProperties": {"type": "number"},
                        },
                    },
                },
                "projects": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "name",
                            "facility_type",
                            "cost",
                            "resource_costs",
                            "production_per_tick",
                            "input_per_tick",
                            "operating_cost",
                            "interval_hours",
                            "duration_turns",
                            "required_technology",
                        ],
                        "properties": {
                            "name": {"type": "string", "maxLength": 120},
                            "facility_type": {"type": "string", "maxLength": 80},
                            "cost": {"type": "number", "minimum": 0, "maximum": 1000000000000},
                            "resource_costs": {
                                "type": "object",
                                "additionalProperties": {"type": "number"},
                            },
                            "production_per_tick": {
                                "type": "object",
                                "additionalProperties": {"type": "number"},
                            },
                            "input_per_tick": {
                                "type": "object",
                                "additionalProperties": {"type": "number"},
                            },
                            "operating_cost": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1000000000,
                            },
                            "interval_hours": {"type": "integer", "minimum": 1, "maximum": 720},
                            "duration_turns": {"type": "integer", "minimum": 1, "maximum": 100},
                            "required_technology": {"type": ["string", "null"], "maxLength": 160},
                        },
                    },
                },
                "research": {
                    "type": "array",
                    "maxItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["technology_name", "description", "duration_turns"],
                        "properties": {
                            "technology_name": {"type": "string", "maxLength": 160},
                            "description": {"type": "string", "maxLength": 2000},
                            "duration_turns": {"type": "integer", "minimum": 1, "maximum": 5},
                        },
                    },
                },
            },
        },
        "duration_turns": {"type": "integer", "minimum": 0, "maximum": 100},
    },
}
