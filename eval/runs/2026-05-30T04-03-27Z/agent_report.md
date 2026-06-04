# Agent Quality Eval Report

- Started: `2026-05-30T04:03:27+00:00`
- Finished: `2026-05-30T04:10:42+00:00`
- Wall-clock total: **434.3s**
- Agent mode: `auto`
- Agent model: `gpt-5.4-mini-2026-03-17`
- VLM model: `gpt-5.4-mini-2026-03-17`
- Prompt version: `v1_minimal`
- Prompt SHA-256: `1974f3d3dfa42f369ab2d1e96309c3e7b33b9924347a9c96740441a8bb832e16`
- VLM high-detail prompt version: `v1_task_context`
- VLM high-detail prompt SHA-256: `d08eecc36f8cd65eb514c1d2d5fdfbad846be1db71ec1362a314d5c4e4e38595`
- Grading policy: `binary_primary_multi_label_or`
- Graded samples: **31**
- Golden set filter: original=35, excluded_failure_memory_overlap=4, evaluated=31
- Skipped: not_importable=0, agent_error=0
- **Total cost: $0.9484 USD**

## Binary verdict accuracy vs. baselines

**Primary metric.** Does the agent correctly identify whether the trajectory succeeded or failed at its task? Coarser than failure_type classification but far more reliable — human inter-annotator agreement is high on this axis.

| Method | Binary accuracy | N | Notes |
|---|---|---|---|
| **Random baseline** (50/50, analytical) |  50.0% | 31 | Uniform over 2 classes |
| **Majority baseline** (always predict `success`, analytical) |  54.8% | 31 | success=17, failed=14 |
| **Agent** (`auto`) |  74.2% | 31 | task-completion verdict |

## Recall breakdown by gold class

| Metric | Value | N |
|---|---|---|
| Success-verdict recall |  58.8% | 17 |
| Failure-verdict recall |  92.9% | 14 |

## Cost & latency

| Metric | Value |
|---|---|
| Mean wall-clock latency / run | 14.01s |
| Mean tool_call_count / run | 4.03 |
| Mean `get_step_detail` / run | 1.65 |
| Mean LLM input tokens / run | 29380 |
| Mean LLM output tokens / run | 954 |
| Total LLM input tokens | 910791 |
| Total LLM output tokens | 29565 |
| Total VLM input tokens | 69641 |
| Total VLM output tokens | 17787 |
| Agent input cost | $0.6831 |
| Agent output cost | $0.1330 |
| VLM input cost | $0.0522 |
| VLM output cost | $0.0800 |
| **Total cost** | **$0.9484** |

Prices used (USD per 1M tokens):

- agent input: $0.75
- agent output: $4.50
- VLM input: $0.75
- VLM output: $4.50

## Coarse-to-fine VLM savings

Compares actual high-detail `get_step_detail` cost against the naive baseline of inspecting every step at high detail. Per-step low-detail preprocessing cost is shared by both and is excluded from this diff.

| Metric | Value |
|---|---|
| Mean step_count / run | 11.3 |
| Mean high-detail VLM calls / run | 1.65 |
| Actual high-detail VLM tokens (total) | 76500 |
| Naive high-detail VLM tokens (total, hypothetical) | 526500 |
| **Savings ratio** | ** 85.5%** |

## Per-category breakdown

Binary acc is the primary signal. failure_type acc is advisory (see below).

| Category | N | Binary acc | failure_type acc (advisory) | Mean tool_calls | Mean latency (s) |
|---|---|---|---|---|---|
| `allrecipes` | 4 |  50.0% | 100.0% | 4.00 | 13.92 |
| `amazon` | 5 | 100.0% |  50.0% | 3.40 | 15.27 |
| `apple` | 4 |  75.0% |  50.0% | 3.75 | 12.88 |
| `arxiv` | 3 |  66.7% |   0.0% | 5.33 | 14.38 |
| `booking` | 4 | 100.0% |  50.0% | 4.00 | 14.13 |
| `github` | 5 |  60.0% | 100.0% | 3.60 | 9.77 |
| `google_flight` | 3 |  66.7% |   0.0% | 4.33 | 21.82 |
| `huggingface` | 3 |  66.7% |   0.0% | 4.67 | 12.24 |

## Advisory: failure-type classification

**Not a primary quality signal.** The 5-class `failure_type` taxonomy has overlapping definitions (e.g. `inefficient_search` vs `missed_constraint` overlap in many real trajectories) and high inter-annotator noise — some samples are genuinely hard even for humans to classify. Reported here for qualitative observation only; do not use as a headline number.

| Method | failure_type top-1 | N | Notes |
|---|---|---|---|
| Random baseline (uniform over 5 classes) |  30.0% | 14 | E[hit] = mean(\|label_set\| / vocab_size) |
| Majority baseline (`wrong_result`) |  42.9% | 14 | Always predicts dominant class |
| Agent |  50.0% | 14 | Multi-label OR policy |

`failure_step` localization (±2): ** 71.4%** (N=14) — also advisory; depends on the agent picking the same root-cause step a human did, subject to the same multi-failure ambiguity as failure_type.

## Evidence quality

- Total `EvidenceItem`s across runs: **151**
- `source="unavailable"` items: 6 (in 6 runs — agent honestly flagged missing evidence)

| `source` | count |
|---|---|
| `step_detail_high` | 78 |
| `trajectory_digest` | 25 |
| `trajectory` | 22 |
| `failure_memory` | 18 |
| `unavailable` | 6 |
| `eval_case` | 1 |
| `successful_run` | 1 |

## Termination reasons

| terminated_by | count |
|---|---|
| `error` | 1 |
| `propose_eval_case` | 30 |

## Caveats

- **Primary metric is `binary_verdict_accuracy`.** `failure_type` top-1 and `failure_step` ±2 are reported as advisory only. The 5-class failure_type taxonomy has overlapping definitions, and the source labels carry non-trivial inter-annotator noise — treating them as a quality scoreboard conflates agent capability with labeling noise.
- Multi-label OR grading (for the advisory failure_type metric): a failed sample is correct iff the agent's single proposed `failure_type` appears in the labeled set (`;`-separated). Loosens the metric but does not lift the inter-annotator noise floor.
- Per-class precision/recall is **not** reported: per-class N (1–4 in the v1 golden set) is too small for class-level numbers to be meaningful.
- `failure_step` localization is only computed when both the label and the agent's proposal carry a step value, and the sample is labeled `failed`.
- Samples whose `run_id` is not importable into storage are excluded from all metrics; their count is reported in the `skipped` block.
- Baselines (`Random`, `Majority`) are computed analytically from the label distribution and do not involve any agent run; same numbers regardless of mock vs real LLM.
