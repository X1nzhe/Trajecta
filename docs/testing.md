# Testing

## RAGAS Evaluation

Create `backend/app/ragas_eval.py`.

Run one minimal RAGAS eval over failure memory RAG.

Preferred metrics:

- `faithfulness`
- `context_precision`

Input shape:

```python
{
  "question": "What failure pattern does this trajectory most closely match?",
  "answer": agent_generated_failure_analysis,
  "contexts": retrieved_failure_memory_texts,
  "ground_truth": "missed_constraint"
}
```

Output files:

```text
eval/ragas_report.json
eval/ragas_report.md
```

If RAGAS setup is too slow, create a fallback script with a stub interface and document how to run real RAGAS.

## Pytest

Use pytest.

Required tests:

```text
tests/test_schema.py
- validate trajectory fixture schema
- reject missing run_id
- reject invalid step action type

tests/test_importer.py
- import small MolmoWeb-HumanSkills sample
- convert raw sample to Trajecta JSON
- preserve screenshot path and raw action text

tests/test_coordinates.py
- validate coordinates when image dimensions are available
- mark invalid coordinates as out_of_bounds
- do not draw overlay for invalid coordinates

tests/test_tools.py
- get_run returns known run
- get_step returns correct step
- generate_eval_case returns required fields

tests/test_rag.py
- ChromaDB collection initializes
- search_similar_cases returns missed_constraint case for constraint query
- top_k length is respected

tests/test_eval_case.py
- exported eval case contains task, failure_step, failure_type, expected_behavior, actual_behavior, evidence, regression_rule
```

## Acceptance Criteria

Project is complete when:

- `pytest` passes
- Backend starts locally
- Frontend starts locally
- At least 5 imported or fixture trajectory runs load
- User can select a run and step
- Screenshot and action details display
- Coordinate overlay is shown only when validated
- Eval Agent can analyze a run
- ChromaDB retrieves similar failure cases
- Eval case draft is generated as JSON
- User can export eval case
- RAGAS or fallback eval report exists
- README clearly explains agent, tools, RAG, eval, tests, LangGraph, ChromaDB, and roadmap
