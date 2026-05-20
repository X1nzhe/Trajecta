# SKILL: create-eval-case

## Trigger
Use when a user wants to convert a trajectory failure into a reusable eval case.

## Inputs
- `run_id`
- `step_id`
- Optional human-provided failure label override

## Procedure
1. Load run and locate step evidence.
2. Analyze failure signals.
3. Retrieve similar failure memories.
4. Draft `EvalCase` JSON.
5. Present to human for confirmation/editing.
