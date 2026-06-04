# Agent Quality Eval Report

- Started: `2026-06-03T07:36:01+00:00`
- Finished: `2026-06-03T07:36:01+00:00`
- Wall-clock total: **0.0s**
- Agent mode: `auto`
- Agent model: `gpt-5.4-mini-2026-03-17`
- VLM model: `gpt-5.4-mini-2026-03-17`
- Prompt version: `v6_guided_autonomy`
- Prompt SHA-256: `1c8acf3867f3a11e810957074ddb9f637e895b154b1762a6bad0d50f551f5a8e`
- VLM high-detail prompt version: `v1_task_context`
- VLM high-detail prompt SHA-256: `d08eecc36f8cd65eb514c1d2d5fdfbad846be1db71ec1362a314d5c4e4e38595`
- Grading policy: `binary_primary_multi_label_or`
- Graded samples: **31**
- Golden set filter: original=35, excluded_failure_memory_overlap=4, evaluated=31
- Skipped: not_importable=0, agent_error=0
- **Total cost: $1.1033 USD**

## Binary verdict accuracy vs. baselines

**Primary metric.** Does the agent correctly identify whether the trajectory succeeded or failed at its task? Coarser than failure_type classification but far more reliable — human inter-annotator agreement is high on this axis.

| Method | Binary accuracy | N | Notes |
|---|---|---|---|
| **Random baseline** (50/50, analytical) |  50.0% | 31 | Uniform over 2 classes |
| **Majority baseline** (always predict `success`, analytical) |  54.8% | 31 | success=17, failed=14 |
| **Agent** (`auto`) |  80.6% | 31 | task-completion verdict |

## Recall breakdown by gold class

| Metric | Value | N |
|---|---|---|
| Success-verdict recall |  82.4% | 17 |
| Failure-verdict recall |  78.6% | 14 |

## Cost & latency

| Metric | Value |
|---|---|
| Mean wall-clock latency / run | 48.27s |
| Mean tool_call_count / run | 1.39 |
| Mean `get_step_detail` / run | 1.00 |
| Mean LLM input tokens / run | 40156 |
| Mean LLM output tokens / run | 627 |
| Total LLM input tokens | 1244842 |
| Total LLM output tokens | 19430 |
| Total VLM input tokens | 42983 |
| Total VLM output tokens | 11117 |
| Agent input cost | $0.9336 |
| Agent output cost | $0.0874 |
| VLM input cost | $0.0322 |
| VLM output cost | $0.0500 |
| **Total cost** | **$1.1033** |

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
| Mean high-detail VLM calls / run | 1.00 |
| Actual high-detail VLM tokens (total) | 46500 |
| Naive high-detail VLM tokens (total, hypothetical) | 526500 |
| **Savings ratio** | ** 91.2%** |

## Per-category breakdown

Binary acc is the primary signal. failure_type acc is advisory (see below).

| Category | N | Binary acc | failure_type acc (advisory) | Mean tool_calls | Mean latency (s) |
|---|---|---|---|---|---|
| `allrecipes` | 4 | 100.0% |  50.0% | 1.25 | 41.30 |
| `amazon` | 5 | 100.0% | 100.0% | 1.00 | 31.70 |
| `apple` | 4 |  50.0% |  50.0% | 1.00 | 28.20 |
| `arxiv` | 3 | 100.0% |   0.0% | 1.67 | 55.41 |
| `booking` | 4 |  75.0% |  50.0% | 1.25 | 115.45 |
| `github` | 5 |  60.0% |  50.0% | 1.60 | 43.69 |
| `google_flight` | 3 | 100.0% |   0.0% | 1.67 | 30.92 |
| `huggingface` | 3 |  66.7% |  50.0% | 2.00 | 40.17 |

## Advisory: failure-type classification

**Not a primary quality signal.** The 5-class `failure_type` taxonomy has overlapping definitions (e.g. `inefficient_search` vs `missed_constraint` overlap in many real trajectories) and high inter-annotator noise — some samples are genuinely hard even for humans to classify. Reported here for qualitative observation only; do not use as a headline number.

| Method | failure_type top-1 | N | Notes |
|---|---|---|---|
| Random baseline (uniform over 5 classes) |  30.0% | 14 | E[hit] = mean(\|label_set\| / vocab_size) |
| Majority baseline (`wrong_result`) |  42.9% | 14 | Always predicts dominant class |
| Agent |  50.0% | 14 | Multi-label OR policy |

`failure_step` localization (±2): ** 64.3%** (N=14) — also advisory; depends on the agent picking the same root-cause step a human did, subject to the same multi-failure ambiguity as failure_type.

## Evidence quality

- Total `EvidenceItem`s across runs: **105**
- `source="unavailable"` items: 3 (in 3 runs — agent honestly flagged missing evidence)

| `source` | count |
|---|---|
| `step_detail_high` | 73 |
| `trajectory` | 21 |
| `trajectory_digest` | 5 |
| `unavailable` | 3 |
| `eval_case` | 3 |

## Termination reasons

| terminated_by | count |
|---|---|
| `propose_eval_case` | 31 |

## Caveats

- **Primary metric is `binary_verdict_accuracy`.** `failure_type` top-1 and `failure_step` ±2 are reported as advisory only. The 5-class failure_type taxonomy has overlapping definitions, and the source labels carry non-trivial inter-annotator noise — treating them as a quality scoreboard conflates agent capability with labeling noise.
- Multi-label OR grading (for the advisory failure_type metric): a failed sample is correct iff the agent's single proposed `failure_type` appears in the labeled set (`;`-separated). Loosens the metric but does not lift the inter-annotator noise floor.
- Per-class precision/recall is **not** reported: per-class N (1–4 in the v1 golden set) is too small for class-level numbers to be meaningful.
- `failure_step` localization is only computed when both the label and the agent's proposal carry a step value, and the sample is labeled `failed`.
- Samples whose `trajectory_id` is not importable into storage are excluded from all metrics; their count is reported in the `skipped` block.
- Baselines (`Random`, `Majority`) are computed analytically from the label distribution and do not involve any agent run; same numbers regardless of mock vs real LLM.
