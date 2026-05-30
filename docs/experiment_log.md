# Experiment Log

## Status

A7.1 agent-eval prompt iteration is complete for the formal v1→v5 run.
The table below is populated only from concrete local source artefacts:

```text
eval/runs/2026-05-30T04-03-27Z/agent_report.json  # v1_minimal
eval/runs/2026-05-30T04-11-14Z/agent_report.json  # v2_success_rubric
eval/runs/2026-05-30T04-31-33Z/agent_report.json  # v3_balanced_rubric
eval/runs/2026-05-30T04-37-53Z/agent_report.json  # v4_search_strategy_rubric
eval/runs/2026-05-30T04-43-34Z/agent_report.json  # v5_constraint_verification
```

Audit summary:

- All five reports evaluate the same 31-run filtered golden set.
- Each report has `skipped.agent_error = 0`.
- Each report directory contains 31 per-sample trace JSON files.
- `v1_minimal` and `v2_success_rubric` each contain one sample with
  `terminated_by="error"` that still produced a graded proposal; this is
  reported as a caveat rather than hidden.

The v5 quality columns for judge `acceptable_rate` and `κ_LLM,LLM` remain
blocked until an operator runs the live Gemini/OpenAI judge pair. They are
not inferred from the agent-eval report.

## Metrics Snapshot

| Prompt version | Binary acc. | Success recall | Failure recall | Failure type acc. | Step ±2 | Mean tools | Mean latency | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `v1_minimal` | 0.742 | 0.588 | 0.929 | 0.500 | 0.714 | 4.03 | 14.01 s | $0.948 |
| `v2_success_rubric` | 0.774 | 0.882 | 0.643 | 0.429 | 0.429 | 2.35 | 11.46 s | $1.201 |
| `v3_balanced_rubric` | 0.806 | 0.765 | 0.857 | 0.500 | 0.714 | 1.68 | 9.96 s | $1.022 |
| `v4_search_strategy_rubric` | 0.742 | 0.647 | 0.857 | 0.571 | 0.643 | 1.97 | 9.34 s | $0.930 |
| `v5_constraint_verification` | 0.677 | 0.412 | 1.000 | 0.571 | 0.786 | 1.61 | 10.57 s | $0.929 |

## Experiment Table

| Round | Prompt version | Change | Metric delta | Conclusion |
| --- | --- | --- | --- | --- |
| 1 | `v1_minimal` | Baseline prompt with minimal failure-shape instructions. | Baseline: binary acc. 0.742; success recall 0.588; failure recall 0.929; mean tools 4.03; mean latency 14.01 s. | Strong failure sensitivity, but too many successful runs are treated as failures. |
| 2 | `v2_success_rubric` | Adds explicit success-case rubric. | vs v1: binary acc. +0.032; success recall +0.294; failure recall -0.286; mean tools -1.68; mean latency -2.55 s. | Success hallucinations drop, but the agent becomes too conservative on failures. |
| 3 | `v3_balanced_rubric` | Balances success and failure criteria and tightens stop conditions. | vs v2: binary acc. +0.032; success recall -0.118; failure recall +0.214; mean tools -0.68; mean latency -1.50 s. | Best headline accuracy (0.806) with a healthier success/failure recall balance and lower tool use. |
| 4 | `v4_search_strategy_rubric` | Clarifies when to retrieve successful runs versus failure memory. | vs v3: binary acc. -0.065; success recall -0.118; failure recall +0.000; mean tools +0.29; mean latency -0.61 s. | Retrieval guidance improves failure-type accuracy (0.571) but does not improve the headline metric. |
| 5 | `v5_constraint_verification` | Emphasizes constraint evidence and failure verification. | vs v4: binary acc. -0.065; success recall -0.235; failure recall +0.143; mean tools -0.35; mean latency +1.22 s. | Failure recall reaches 1.000 and step localization is strongest, but success recall collapses; v5 is a failure-sensitive trade-off, not the best general prompt. |

## A7.1 Conclusion

Use `v3_balanced_rubric` as the best agent-eval prompt by primary metric.
Use `v5_constraint_verification` only when the objective is to catch every
failure and tolerate more false positives on successful runs. The judge
agreement columns remain pending live A/B judge execution.
