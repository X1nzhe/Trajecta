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

## Experiment Table

| Round | Prompt version | Change | Metric delta | Conclusion |
| --- | --- | --- | --- | --- |
| 1 | `v1_minimal` | Baseline prompt with minimal failure-shape instructions. | Baseline: binary acc. 0.742; success recall 0.588; failure recall 0.929; mean tools 4.03; mean latency 14.01 s. | Strong failure sensitivity, but too many successful runs are treated as failures. |
| 2 | `v2_success_rubric` | Adds explicit success-case rubric. | vs v1: binary acc. +0.032; success recall +0.294; failure recall -0.286; mean tools -1.68; mean latency -2.55 s. | Success hallucinations drop, but the agent becomes too conservative on failures. |
| 3 | `v3_balanced_rubric` | Balances success and failure criteria and tightens stop conditions. | vs v2: binary acc. +0.032; success recall -0.118; failure recall +0.214; mean tools -0.68; mean latency -1.50 s. | Best headline accuracy (0.806) with a healthier success/failure recall balance and lower tool use. |
| 4 | `v4_search_strategy_rubric` | Clarifies when to retrieve successful runs versus failure memory. | vs v3: binary acc. -0.065; success recall -0.118; failure recall +0.000; mean tools +0.29; mean latency -0.61 s. | Retrieval guidance improves failure-type accuracy (0.571) but does not improve the headline metric. |
| 5 | `v5_constraint_verification` | Emphasizes constraint evidence and failure verification. | vs v4: binary acc. -0.065; success recall -0.235; failure recall +0.143; mean tools -0.35; mean latency +1.22 s. | Failure recall reaches 1.000 and step localization is strongest, but success recall collapses; v5 is a failure-sensitive trade-off, not the best general prompt. |
| 6 | `v6_guided_autonomy` | Legible per-tool contract + explicit investigation freedom; strict verdict/evidence rules (burden-of-proof, `not_visible` split). | vs v3: binary acc. +0.000 (0.806); success recall +0.059; failure recall -0.071; 70% of evidence from high-detail reads; mean latency +38 s (longer prompt → more reasoning). | Matches v3's headline accuracy with a cleaner, better-grounded evidence trail; the current featured prompt. Higher latency is the cost. |

## A7.1 Conclusion

Use `v3_balanced_rubric` or `v6_guided_autonomy` as the best general agent-eval
prompt by the primary metric (both at 0.806 binary accuracy). `v6_guided_autonomy`
is featured because it grounds more claims in high-detail inspection (70% of
cited evidence) at the cost of higher per-run latency. Use
`v5_constraint_verification` only when the objective is to catch every failure
and tolerate more false positives on successful runs.

Dual LLM judge (v6 run): both judges accept 20 / 31 and agree on all 31 →
κ_LLM,LLM = 1.0 (≥ 0.6 target), after fixing the `regression_case_usefulness`
assertion that had been failing success-shape drafts for omitting failure-only
fields (κ 0.674 → 1.0; fix applied identically to both provider rubrics).
Caveat: κ=1.0 reflects a largely objective checklist at temperature 0 over
n=31, not a claim that acceptability judgment is solved.

Semantic metric: re-framed from retrieval-hit faithfulness to
**evidence-grounding faithfulness** (`--context-mode evidence`), which measures
whether eval-case claims are faithful to the agent's visible evidence
(high-detail reads + digest + retrieved precedent) rather than to auxiliary RAG
hits. Evidence-mode `faithfulness = 0.93` (n=10, real), corroborating the
judge's `evidence_support` assertion.

Caveat: the `v6_guided_autonomy` `agent_report` (run `2026-06-03T05-45-39Z`)
predates later refinements to the v6 prompt (the `not_visible` / precedence
edits); those were not re-run over the full 31-case set.

## Note on Spotlighting (B6)

The Phase 8 B6 Spotlighting defense (delimiting wrap + anti-injection
preamble) is a small production hardening feature, not an experiment in
this log. It is shipped and unit-tested but deliberately **unmeasured** —
there is no injection golden set, ablation, or `injection_resistance_rate`
in Phase 8. A formal prompt-injection benchmark would be a separate
security-evaluation phase. See
[docs/security_governance.md](security_governance.md) Mechanism 9 for the
defense description and threat model.
