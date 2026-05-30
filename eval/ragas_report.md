# RAGAS Report

- Sample count: 10
- Mode: `real`
- Ground truth source: `none`

## Metric means
- **faithfulness**: 0.4068

## Skipped traces
- budget_exceeded: 0
- error: 7
- no_trace: 4
- no_context: 17

## How this was generated

`ragas_mode=real` — real `ragas` faithfulness evaluation over retrieved contexts.
`ground_truth_source=none` — no artificial or self-generated ground truth is used; the report measures whether the final claims are supported by retrieved contexts.

Trace source precedence (Phase 8 A6.1): explicit `--trace-dir` Phase 8 A2 dumps first at `<trace_dir>/<run_id>.json`; on miss, fall back to the SQLite `traces` table (`storage.load_trace`). The run-id discovery set is the union of SQLite-resident runs and `<trace_dir>/*.json` files.
Each RAGAS sample corresponds to one recorded `search_failure_memory` or `search_eval_cases` tool call: `question` is the tool query, `contexts` are that tool result's items, and `answer` is the final `propose_eval_case` actual_behavior plus evidence claims.
