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

The v5 quality columns are populated from the live Gemini/OpenAI judge
agreement run at:

```text
eval/runs/2026-05-30T04-43-34Z/judge/judge_agreement_report.json
```

Judge A (`gemini-3.1-flash-lite`, `v1_acceptability_gemini`) marked
13 / 31 drafts acceptable. Judge B (`gpt-5.4-mini-2026-03-17`,
`v1_acceptability_openai`) marked 15 / 31 drafts acceptable. Their
κ_LLM,LLM is 0.741 on the full 31-case set.

## Metrics Snapshot

| Prompt version | Binary acc. | Success recall | Failure recall | Failure type acc. | Step ±2 | Mean tools | Mean latency | Cost | Judge quality |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `v1_minimal` | 0.742 | 0.588 | 0.929 | 0.500 | 0.714 | 4.03 | 14.01 s | $0.948 | Not judged |
| `v2_success_rubric` | 0.774 | 0.882 | 0.643 | 0.429 | 0.429 | 2.35 | 11.46 s | $1.201 | Not judged |
| `v3_balanced_rubric` | 0.806 | 0.765 | 0.857 | 0.500 | 0.714 | 1.68 | 9.96 s | $1.022 | Not judged |
| `v4_search_strategy_rubric` | 0.742 | 0.647 | 0.857 | 0.571 | 0.643 | 1.97 | 9.34 s | $0.930 | Not judged |
| `v5_constraint_verification` | 0.677 | 0.412 | 1.000 | 0.571 | 0.786 | 1.61 | 10.57 s | $0.929 | A acceptable 0.419; B acceptable 0.484; κ 0.741 |
| `v6_guided_autonomy` | 0.806 | 0.824 | 0.786 | 0.500 | 0.643 | 1.39 | 48.27 s | $1.103 | A acceptable 0.645; B acceptable 0.645; κ 1.000 (after rubric fix) |

Formal v6 + model-ablation source artefacts (same 31-run filtered golden set;
`skipped.agent_error = 0`; 31 trace JSONs each). These paths are **tracked in
git** (whitelist in `.gitignore`); v1→v5 runs under `eval/runs/2026-05-30T04-*`
are tracked the same way.

```text
eval/runs/2026-06-03T05-45-39Z/agent_report.json   # v6, agent+VLM gpt-5.4-mini
eval/runs/2026-06-03T05-45-39Z/judge/judge_agreement_report.json
eval/runs/2026-06-04T06-04-20Z/agent_report.json   # v6, agent gpt-5.4, VLM mini
eval/runs/2026-06-04T06-04-20Z/judge/judge_agreement_report.json
```

## Model ablation (v6, agent only)

Same prompt (`v6_guided_autonomy`, same SHA on both runs). VLM stays
`gpt-5.4-mini-2026-03-17`. Only `TRAJECTA_AGENT_MODEL` changes.

| Agent model | Run | Binary acc. | Success recall | Failure recall | Mean `get_step_detail` | Cost | Judge (A / B acceptable) | κ |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| `gpt-5.4-mini-2026-03-17` | `2026-06-03T05-45-39Z` | 0.806 | 0.824 | 0.786 | 1.00 | $1.103 | 20 / 31; 20 / 31 | 1.000 |
| `gpt-5.4-2026-03-05` | `2026-06-04T06-04-20Z` | 0.677 | 0.412 | 1.000 | 2.32 | $4.577 | 16 / 31; 14 / 31 | 0.743 |

vs mini: binary acc. −0.129; success recall −0.412; failure recall +0.214;
mean high-detail VLM calls +1.32; total cost +$3.474 (different agent list
prices and more tool turns — not a like-for-like unit-price comparison).

**Profile:** `gpt-5.4` on v6 shifts toward a failure-sensitive trade-off (same
shape as Round 5). See [Why gpt-5.4 did not improve headline accuracy](#why-gpt-54-did-not-improve-headline-accuracy)
for interpretation—not weaker reasoning, but a different verdict calibration on
the same prompt.

**Digest cache vs. token/cost accounting:** Preprocess digest cache skips
per-step low-detail VLM and does **not** add to trace `vlm_*_tokens` on a
hit; the digest text still enters the agent prompt, so **agent `input_tokens`
are unchanged** by cache. On these runs, mini had digest cache hits on 8/31
traces (23 rebuilt); gpt-5.4 had 31/31 hits. gpt-5.4 still recorded **higher**
total VLM tokens because it issued more `get_step_detail` calls. Reported
`cost_usd` multiplies usage by per-model list prices; it does not apply
provider prompt-cache billing discounts if the API offers them.

### Why gpt-5.4 did not improve headline accuracy

**In one sentence:** gpt-5.4 lost headline accuracy not because it reasons worse
but because it applies the success/failure threshold too aggressively—on the
same fully-visible evidence it judged 10 trajectories more harshly than mini,
*all in the success→failed direction*, which cost 7 false failures on
gold-success runs and dropped binary accuracy 0.806 → 0.677. This is a verdict
**calibration** shift, not a reasoning regression.

This subsection explains the ablation numbers above. Swapping only
`TRAJECTA_AGENT_MODEL` to `gpt-5.4-2026-03-05` did **not** produce a better
general Eval Agent on the 31-case golden set under `v6_guided_autonomy`.

**Metric pattern (not weaker reasoning).** Failure recall rises from 0.786 to
1.000 while success recall falls from 0.824 to 0.412 and binary accuracy from
0.806 to 0.677. That is the same success/failure trade-off seen in Round 5
(`v5_constraint_verification`), not a random capability regression: the larger
model is **more willing to call failure**, not less able to reason. Two
sub-metrics confirm the reasoning is, if anything, *sharper*: on the failures it
does flag, `failure_type_top1_accuracy` rises 0.500 → 0.643 and
`failure_step_localization_within_2` rises 0.643 → 0.929. gpt-5.4 types and
locates real failures better; it simply applies the success/failure threshold
too aggressively. The regression is verdict **calibration**, not analysis
quality.

**Per-sample mechanism.** Comparing `samples` in
`eval/runs/2026-06-03T05-45-39Z/agent_report.json` (mini agent) vs
`eval/runs/2026-06-04T06-04-20Z/agent_report.json` (gpt-5.4 agent), **10 / 31**
cases flip on `binary_verdict_correct`, and **all 10 move in the same direction**:
mini said `success`, gpt-5.4 said `failed`. There is **not one** flip the other
way (no case where mini said `failed` and gpt-5.4 said `success`). That
monotonicity is the cleanest possible signature of a single verdict-threshold
shift rather than mixed capability changes. Of the 10, **seven** are gold
`success` trajectories that mini grades correctly and gpt-5.4 false-fails—the
dominant error mode—and the remaining **three** are gold `failed` trajectories
mini wrongly passed and gpt-5.4 correctly caught. The shift shows up in the
marginal verdict distribution too: mini proposes **17 success / 14 failed**
(matching the gold base rate of 17 / 14 almost exactly), while gpt-5.4 proposes
**7 success / 24 failed**—it reclassifies 10 trajectories success→failed and is
wrong on 7 of them. Because the golden set has 17 success and 14 failed labels,
false failures on success hurt the primary metric more than missing a failure
hurts it when failure recall is already high on mini.

**Prompt × model interaction.** `v6_guided_autonomy` couples two pressures in
[`prompts/eval_agent/v6_guided_autonomy/system.md`](../prompts/eval_agent/v6_guided_autonomy/system.md):
the **Decision threshold** block puts the burden of proof on failure (default
success-shape), while the opening **How to work** block encourages 2–4
investigation tool calls, constraint-step reads, and high-detail inspection
before `propose_eval_case`. Weaker or more cost-sensitive models often stop once
evidence is sufficient; stronger models more consistently execute the “investigate
deeper” branch. On this run that shows up as mean `get_step_detail` **2.32** vs
**1.00** per trajectory, and on the seven false-failures specifically gpt-5.4
issued 2–3 high-detail reads where mini issued exactly 1. The extra reads do not
fail because the model *saw less*: on **all seven** false-failures
`evidence_unavailable = 0` for both runs—the evidence was fully visible to both.
What changes is interpretation. gpt-5.4 re-reads visible, success-shaped evidence
and upgrades non-perfect-but-acceptable UI states into failures, typing them as
`missed_constraint` (×3), `wrong_result` (×2), and `early_terminated` (×2). In
other words it over-verifies constraints and holds completed tasks to a stricter
bar than the trajectory's actual goal requires—even when mini, on the same VLM
evidence, accepts success-shape. gpt-5.4 does not gain much on failures mini
already caught (failure recall was already 0.786 on mini).

**VLM ceiling unchanged.** Both runs set `TRAJECTA_VLM_MODEL=gpt-5.4-mini-2026-03-17`.
High-detail screenshots are read by the same VLM; a larger Eval Agent only
re-interprets the **same** digest and `get_step_detail` outputs. Better headline
accuracy therefore cannot come from “seeing more pixels,” only from different
judgment on identical visible evidence.

**Judge corroboration.** The dual LLM judge uses the **same** rubric bundles on
both trace sets. On gpt-5.4 drafts, acceptable rates drop (Judge A 16/31, Judge
B 14/31 vs 20/31 each on mini) and κ_LLM,LLM falls to 0.743 with four A/B
disagreements. Lower κ here reflects **worse regression-case drafts** (more
aggressive failure shapes, weaker success paths), not a change in judge prompts.

**What did not explain the verdict gap.**

- **Missing/unavailable evidence:** ruled out. `evidence_unavailable = 0` on all
  seven false-failures for both runs, so the gap is not "gpt-5.4 saw fewer
  screenshots / hit more `not_visible` gaps." Both models judged the same fully
  visible evidence and disagreed on the verdict.
- **Digest cache:** mini had preprocess cache hits on 8/31 traces (23 rebuilt);
  gpt-5.4 had 31/31 hits. Cache skips low-detail VLM billing on a hit but still
  injects the digest into the agent prompt, so it does not explain a systematic
  shift toward marking successes as failed.
- **Cost / latency:** higher `cost_usd` on gpt-5.4 reflects higher agent list
  prices and more tool turns, not proof of higher-quality verdicts on this
  benchmark. Note the penalty is in dollars, not wall-clock: gpt-5.4 was actually
  *faster* per trajectory (mean latency 25.7 s vs 48.3 s) despite doubling tool
  calls, so "slower" is not part of the trade-off.

**Implications.**

- **Featured default:** `v6_guided_autonomy` with **`gpt-5.4-mini-2026-03-17`**
  as the Eval Agent for balanced `binary_verdict_accuracy` on this golden set.
- **When to use gpt-5.4:** only if the product goal prioritizes **failure recall**
  (catch every labeled failure) and accepts more false failures on successful
  trajectories—similar to choosing `v5_constraint_verification` over v3/v6 on
  prompts alone.
- **Future work (not done here):** since the gap is verdict calibration on
  visible evidence (not investigation depth or missing pixels), a model-specific
  variant should raise the **bar for a violation**—strengthen the success-shape
  default and require an explicit, goal-relevant constraint breach before
  `failed`, so extra reads inform the verdict without lowering the failure
  threshold. Tune and re-run on the full 31-case set before treating gpt-5.4 as
  the default agent.

## Experiment Table

| Round | Prompt version | Change | Metric delta | Conclusion |
| --- | --- | --- | --- | --- |
| 1 | `v1_minimal` | Baseline prompt with minimal failure-shape instructions. | Baseline: binary acc. 0.742; success recall 0.588; failure recall 0.929; mean tools 4.03; mean latency 14.01 s. | Strong failure sensitivity, but too many successful runs are treated as failures. |
| 2 | `v2_success_rubric` | Adds explicit success-case rubric. | vs v1: binary acc. +0.032; success recall +0.294; failure recall -0.286; mean tools -1.68; mean latency -2.55 s. | Success hallucinations drop, but the agent becomes too conservative on failures. |
| 3 | `v3_balanced_rubric` | Balances success and failure criteria and tightens stop conditions. | vs v2: binary acc. +0.032; success recall -0.118; failure recall +0.214; mean tools -0.68; mean latency -1.50 s. | Best headline accuracy (0.806) with a healthier success/failure recall balance and lower tool use. |
| 4 | `v4_search_strategy_rubric` | Clarifies when to retrieve successful runs versus failure memory. | vs v3: binary acc. -0.065; success recall -0.118; failure recall +0.000; mean tools +0.29; mean latency -0.61 s. | Retrieval guidance improves failure-type accuracy (0.571) but does not improve the headline metric. |
| 5 | `v5_constraint_verification` | Emphasizes constraint evidence and failure verification. | vs v4: binary acc. -0.065; success recall -0.235; failure recall +0.143; mean tools -0.35; mean latency +1.22 s. | Failure recall reaches 1.000 and step localization is strongest, but success recall collapses; v5 is a failure-sensitive trade-off, not the best general prompt. |
| 6 | `v6_guided_autonomy` | Legible per-tool contract + explicit investigation freedom; strict verdict/evidence rules (burden-of-proof, `not_visible` split). | vs v3: binary acc. +0.000 (0.806); success recall +0.059; failure recall -0.071; 70% of evidence from high-detail reads; mean latency +38 s (longer prompt → more reasoning). | Matches v3's headline accuracy with a cleaner, better-grounded evidence trail; the current featured prompt. Higher latency is the cost. |
| 7 | `v6_guided_autonomy` + `gpt-5.4-2026-03-05` agent | Same v6 prompt; swap Eval Agent to full `gpt-5.4`, VLM unchanged (mini). | vs v6 mini (`2026-06-03T05-45-39Z`): binary acc. −0.129; success recall −0.412; failure recall +0.214; mean `get_step_detail` +1.32; cost +$3.474; judge κ 1.0 → 0.743. | Stronger failure catching and step localization, but worse headline accuracy and more false failures on success; higher cost. Featured default remains mini agent. |

## Conclusion

Use `v3_balanced_rubric` or `v6_guided_autonomy` as the best general agent-eval
prompt by the primary metric (both at 0.806 binary accuracy). `v6_guided_autonomy`
is featured because it grounds more claims in high-detail inspection (70% of
cited evidence) at the cost of higher per-run latency. The featured **agent model**
remains `gpt-5.4-mini-2026-03-17`; swapping to `gpt-5.4-2026-03-05` on the same v6
prompt lowers headline accuracy—see [Why gpt-5.4 did not improve headline accuracy](#why-gpt-54-did-not-improve-headline-accuracy).
Use `v5_constraint_verification` only when the objective is to catch every failure
and tolerate more false positives on successful runs.

Dual LLM judge on **v6 mini-agent** traces (`2026-06-03T05-45-39Z`): both
judges accept 20 / 31 and agree on all 31 → κ_LLM,LLM = 1.0 (≥ 0.6 target),
after fixing the `regression_case_usefulness` assertion that had been failing
success-shape drafts for omitting failure-only fields (κ 0.674 → 1.0; fix
applied identically to both provider rubrics).

On **v6 gpt-5.4-agent** traces (`2026-06-04T06-04-20Z`), the same judge pair
accepts fewer drafts (A 16/31, B 14/31) with κ = 0.743 and 4 disagreements —
still above the 0.6 target, but lower acceptability reflects worse drafts, not
a rubric change.

Caveat: κ=1.0 on the mini run reflects a largely objective checklist at
temperature 0 over n=31, not a claim that acceptability judgment is solved.

Semantic metric: re-framed from retrieval-hit faithfulness to
**evidence-grounding faithfulness** (`--context-mode evidence`), which measures
whether eval-case claims are faithful to the agent's visible evidence
(high-detail reads + digest + retrieved precedent) rather than to auxiliary RAG
hits. Evidence-mode `faithfulness = 0.93` (n=10, real), corroborating the
judge's `evidence_support` assertion.

Caveat: the `v6_guided_autonomy` `agent_report` (run `2026-06-03T05-45-39Z`)
predates later refinements to the v6 prompt (the `not_visible` / precedence
edits); those were not re-run over the full 31-case set.

## Note on Spotlighting

The Spotlighting defense (delimiting wrap + anti-injection
preamble) is a small production hardening feature, not an experiment in
this log. It is shipped and unit-tested but deliberately **unmeasured** —
there is no injection golden set, ablation, or `injection_resistance_rate`
in Phase 8. A formal prompt-injection benchmark would be a separate
security-evaluation phase. See
[docs/security_governance.md](security_governance.md) Mechanism 9 for the
defense description and threat model.
