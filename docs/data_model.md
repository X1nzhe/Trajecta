# Data Model

Create `backend/app/schemas.py`.

## Coordinate

```python
from pydantic import BaseModel, Field
from typing import Literal, Optional, List, Dict, Any


class Coordinate(BaseModel):
    x: float
    y: float


class BBox(BaseModel):
    x: float
    y: float
    width: float
    height: float
```

## Action

```python
class StepAction(BaseModel):
    type: Literal["click", "type", "scroll", "navigate", "wait", "unknown"]
    label: Optional[str] = None
    text: Optional[str] = None
    coordinates: Optional[Coordinate] = None
    bbox: Optional[BBox] = None
    raw: Optional[str] = None
```

## Observation

```python
class StepObservation(BaseModel):
    screenshot: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    visible_text: Optional[str] = None
    vlm_summary: Optional[str] = None
    visual_evidence: List[str] = Field(default_factory=list)
```

`screenshot` should be a filename or run-relative path under
`data/runs/{run_id}/screenshots/`, not an absolute local filesystem path. The API
is responsible for converting it into a frontend-accessible screenshot URL.

## Result

```python
class StepResult(BaseModel):
    status: Literal["success", "failed", "unknown"] = "unknown"
    error: Optional[str] = None
```

## Coordinate Validation

```python
class CoordinateValidation(BaseModel):
    status: Literal["validated", "out_of_bounds", "missing", "unknown"] = "unknown"
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    reason: Optional[str] = None
```

## Trajectory Step

```python
class TrajectoryStep(BaseModel):
    index: int
    timestamp: Optional[str] = None
    observation: StepObservation
    action: StepAction
    result: StepResult = Field(default_factory=StepResult)
    coordinate_validation: CoordinateValidation = Field(default_factory=CoordinateValidation)
    metadata: Dict[str, Any] = Field(default_factory=dict)
```

## Trajectory Run

```python
class TrajectoryRun(BaseModel):
    run_id: str
    task: str
    source: str = "allenai/MolmoWeb-HumanSkills"
    status: Literal["success", "failed", "unknown"] = "unknown"
    steps: List[TrajectoryStep]
    metadata: Dict[str, Any] = Field(default_factory=dict)
```

## Failure Memory Case

Failure memory `case_id` values should be stable and human-readable:

```text
case_{failure_type}_{NNN}
```

Example: `case_missed_constraint_001`.

```python
class FailureMemoryCase(BaseModel):
    case_id: str
    failure_type: str
    summary: str
    fix_hint: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    source_run_id: Optional[str] = None
```

## Eval Case

Eval case `case_id` values should be stable for the source run and failure step:

```text
eval_{source_run_id}_step_{failure_step}_{failure_type}
```

```python
class EvalCase(BaseModel):
    case_id: str
    source_run_id: str
    task: str
    failure_step: int
    failure_type: str
    expected_behavior: str
    actual_behavior: str
    evidence: List[str]
    regression_rule: str
    retrieved_context_ids: List[str] = Field(default_factory=list)
    human_validated: bool = False
```
