# Agent Quality Eval Report

- Started: `2026-06-04T06:04:23+00:00`
- Finished: `2026-06-04T06:23:33+00:00`
- Wall-clock total: **797.0s**
- Agent mode: `auto`
- Agent model: `gpt-5.4-2026-03-05`
- VLM model: `gpt-5.4-mini-2026-03-17`
- Prompt version: `v6_guided_autonomy`
- Prompt SHA-256: `1c8acf3867f3a11e810957074ddb9f637e895b154b1762a6bad0d50f551f5a8e`
- VLM high-detail prompt version: `v1_task_context`
- VLM high-detail prompt SHA-256: `d08eecc36f8cd65eb514c1d2d5fdfbad846be1db71ec1362a314d5c4e4e38595`
- Grading policy: `binary_primary_multi_label_or`
- Graded samples: **31**
- Golden set filter: original=35, excluded_failure_memory_overlap=4, evaluated=31
- Skipped: not_importable=0, agent_error=0
- **Total cost: $4.5770 USD**

## Binary verdict accuracy vs. baselines

**Primary metric.** Does the agent correctly identify whether the trajectory succeeded or failed at its task? Coarser than failure_type classification but far more reliable — human inter-annotator agreement is high on this axis.

| Method | Binary accuracy | N | Notes |
|---|---|---|---|
| **Random baseline** (50/50, analytical) |  50.0% | 31 | Uniform over 2 classes |
| **Majority baseline** (always predict `success`, analytical) |  54.8% | 31 | success=17, failed=14 |
| **Agent** (`auto`) |  67.7% | 31 | task-completion verdict |

## Recall breakdown by gold class

| Metric | Value | N |
|---|---|---|
| Success-verdict recall |  41.2% | 17 |
| Failure-verdict recall | 100.0% | 14 |

## Cost & latency

| Metric | Value |
|---|---|
| Mean wall-clock latency / run | 25.71s |
| Mean tool_call_count / run | 3.03 |
| Mean `get_step_detail` / run | 2.32 |
| Mean LLM input tokens / run | 50785 |
| Mean LLM output tokens / run | 970 |
| Total LLM input tokens | 1574344 |
| Total LLM output tokens | 30064 |
| Total VLM input tokens | 100761 |
| Total VLM output tokens | 25470 |
| Agent input cost | $3.9359 |
| Agent output cost | $0.4510 |
| VLM input cost | $0.0756 |
| VLM output cost | $0.1146 |
| **Total cost** | **$4.5770** |

Prices used (USD per 1M tokens):

- agent input: $2.50
- agent output: $15.00
- VLM input: $0.75
- VLM output: $4.50

## Coarse-to-fine VLM savings

Compares actual high-detail `get_step_detail` cost against the naive baseline of inspecting every step at high detail. Per-step low-detail preprocessing cost is shared by both and is excluded from this diff.

| Metric | Value |
|---|---|
| Mean step_count / run | 11.3 |
| Mean high-detail VLM calls / run | 2.32 |
| Actual high-detail VLM tokens (total) | 108000 |
| Naive high-detail VLM tokens (total, hypothetical) | 526500 |
| **Savings ratio** | ** 79.5%** |

## Per-category breakdown

Binary acc is the primary signal. failure_type acc is advisory (see below).

| Category | N | Binary acc | failure_type acc (advisory) | Mean tool_calls | Mean latency (s) |
|---|---|---|---|---|---|
| `allrecipes` | 4 |  50.0% |  50.0% | 2.50 | 19.52 |
| `amazon` | 5 |  80.0% | 100.0% | 3.20 | 25.30 |
| `apple` | 4 |  75.0% |  50.0% | 3.25 | 25.48 |
| `arxiv` | 3 |  66.7% | 100.0% | 2.67 | 22.08 |
| `booking` | 4 |  50.0% |  50.0% | 3.25 | 30.11 |
| `github` | 5 |  60.0% |  50.0% | 3.20 | 24.30 |
| `google_flight` | 3 | 100.0% |   0.0% | 2.67 | 27.51 |
| `huggingface` | 3 |  66.7% | 100.0% | 3.33 | 33.23 |

## Advisory: failure-type classification

**Not a primary quality signal.** The 5-class `failure_type` taxonomy has overlapping definitions (e.g. `inefficient_search` vs `missed_constraint` overlap in many real trajectories) and high inter-annotator noise — some samples are genuinely hard even for humans to classify. Reported here for qualitative observation only; do not use as a headline number.

| Method | failure_type top-1 | N | Notes |
|---|---|---|---|
| Random baseline (uniform over 5 classes) |  30.0% | 14 | E[hit] = mean(\|label_set\| / vocab_size) |
| Majority baseline (`wrong_result`) |  42.9% | 14 | Always predicts dominant class |
| Agent |  64.3% | 14 | Multi-label OR policy |

`failure_step` localization (±2): ** 92.9%** (N=14) — also advisory; depends on the agent picking the same root-cause step a human did, subject to the same multi-failure ambiguity as failure_type.

## Evidence quality

- Total `EvidenceItem`s across runs: **143**
- `source="unavailable"` items: 0 (in 0 runs — agent honestly flagged missing evidence)

| `source` | count |
|---|---|
| `step_detail_high` | 81 |
| `trajectory` | 39 |
| `eval_case` | 20 |
| `trajectory_digest` | 3 |

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
