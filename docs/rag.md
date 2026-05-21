# ChromaDB RAG Design

v1 uses ChromaDB for text-based retrieval over failure memories, eval cases, and VLM-generated screenshot summaries.

This is **multimodal-informed RAG**, not full image-vector multimodal RAG.

The screenshot is analyzed by the VLM first. The VLM output is converted into structured text evidence and embedded into ChromaDB.

## Collections

### Collection 1: `failure_memory`

Stores reusable failure patterns.

Fields:

- `case_id`
- `failure_type`
- `summary`
- `fix_hint`
- `tags`
- `source_run_id`

Text to embed:

```text
failure_type + summary + fix_hint + tags
```

### Collection 2: `eval_cases`

Stores generated or human-validated eval cases.

Fields:

- `case_id`
- `task`
- `failure_type`
- `expected_behavior`
- `actual_behavior`
- `evidence`
- `regression_rule`
- `human_validated`

Text to embed:

```text
task + failure_type + expected_behavior + actual_behavior + evidence + regression_rule
```

### Collection 3: `step_summaries`

Optional in v1.

Stores VLM-generated summaries for trajectory steps.

Fields:

- `run_id`
- `step_index`
- `action`
- `observation_summary`
- `visual_summary`
- `possible_issue`

Text to embed:

```text
action + observation_summary + visual_summary + possible_issue
```

## RAG Flow

1. Load selected trajectory run.
2. Build analysis query from task, selected step, action, and VLM screenshot summary.
3. Retrieve similar failure memories from ChromaDB.
4. Inject retrieved cases into Eval Agent context.
5. Eval Agent generates failure analysis and eval case draft.
6. RAGAS evaluates whether the generated analysis is faithful to retrieved context.
