from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Coordinates(BaseModel):
    x: float
    y: float


class TrajectoryStep(BaseModel):
    step_id: str
    action: str
    target: str | None = None
    screenshot_path: str
    timestamp: str | None = None
    success: bool = True
    error: str | None = None
    coordinates: Coordinates | None = None

    @model_validator(mode="after")
    def validate_error_consistency(self) -> "TrajectoryStep":
        if self.success and self.error:
            raise ValueError("error must be empty when success is true")
        return self


class TrajectoryRun(BaseModel):
    run_id: str
    source: str = "fixture"
    steps: list[TrajectoryStep] = Field(default_factory=list)


class FailureMemoryCase(BaseModel):
    case_id: str
    failure_label: str
    summary: str
    tags: list[str] = Field(default_factory=list)


class EvalAnalysis(BaseModel):
    failure_label: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class EvalCase(BaseModel):
    eval_case_id: str
    run_id: str
    step_id: str
    failure_label: str
    status: Literal["draft", "confirmed"] = "draft"
    summary: str
    evidence: list[str] = Field(default_factory=list)
    similar_case_ids: list[str] = Field(default_factory=list)
