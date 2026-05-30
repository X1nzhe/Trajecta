# Failure Analysis

Phase 8 A8 reviews the v5 formal run:

- Agent report: `eval/runs/2026-05-30T04-43-34Z/agent_report.json`
- Judge agreement: `eval/runs/2026-05-30T04-43-34Z/judge/judge_agreement_report.json`
- Agent prompt: `v5_constraint_verification`
- Judge result: κ_LLM,LLM = 0.7406 on 31 cases

The agent reached 67.7% binary verdict accuracy with 100.0% failure recall but only 41.2% success recall. The main qualitative pattern is conservative verification: the agent is good at finding missing evidence, but it often turns uncertainty into a failure case.

## Case 1: Success Marked As Missed Constraint

- Run: `32e7dbe84bcaf8206d7d28a43e9c7b26e3553c33e509c30618bddb34fe8aaef4`
- Category: `github`
- Golden outcome: `success`
- Agent output: `failed`, `failure_type=missed_constraint`, `failure_step=6`
- Judges: A `unacceptable`, B `unacceptable`

The task involved finding a GitHub repository related to smart city technologies updated within the past week. The agent proposed a reusable regression rule requiring visible verification of the recency constraint before exit. That rule is useful in isolation, but it contradicted the golden success label.

Root cause: v5 treats absence of visible verification as failure evidence. This improves caution on constraint-heavy tasks, but it can overrule the golden label when the human triage accepted the run as successful. Judge A also flagged an evidence-support issue: the draft claimed the agent clicked `Recently updated`, while its own high-detail evidence said the sort option had not yet been applied.

Lesson: verification prompts need a clearer distinction between "not visible in the trace" and "known unsatisfied." Missing visual confirmation should be recorded as uncertainty unless the golden/task evidence requires a failure.

## Case 2: Correct Failure Verdict, Wrong Failure Type

- Run: `945d14f4290efaa35e70185ecdb2a66c4a6acf826ae9ddb17995cc355aae0cac`
- Category: `arxiv`
- Golden outcome: `failed`, `failure_type=wrong_result`, `failure_step=7`
- Agent output: `failed`, `failure_type=missed_constraint`, `failure_step=7`
- Judges: A `unacceptable`, B `unacceptable`

The agent correctly identified that the arXiv run failed and localized the issue to the final step. It stopped on a broad search-results page without verifying that the visible result belonged to the requested `Probabilistic Models` category.

Root cause: the taxonomy boundary is ambiguous. The same evidence can be narrated as "selected the wrong result" or "missed a category constraint." The golden reference explicitly required `wrong_result` and forbade `missed_constraint`, so both judges rejected the draft even though the failure verdict and step were correct.

Lesson: `failure_type` should remain advisory unless the taxonomy is tightened. For regression generation, a wrong type can make an otherwise useful eval case incompatible with the labelled golden set.

## Case 3: Useful Rule, Contaminated By Memory And Step Drift

- Run: `7357a951f990f531ebaa4761106d7f960054de3bb213e31a24a99f2be64264c6`
- Category: `huggingface`
- Golden outcome: `failed`, `failure_type=wrong_result;wrong_target`, `failure_step=4`
- Agent output: `failed`, `failure_type=wrong_target`, `failure_step=7`
- Judges: A `unacceptable`, B `unacceptable`

The run ended on a Hugging Face model card for `google-bert/bert-base-uncased` rather than a blog/article discussing BERT improvements. The proposed regression rule was good: verify that the final page type matches the requested content type before exiting.

Root cause: the agent mixed a compatible wrong-target analysis with an incompatible retrieved memory. It cited `fm_early_terminated_001` and included an early-termination precedent, while the golden reference forbade that framing. The proposed failure step also drifted to the final exit step instead of the earlier wrong-target decision point.

Lesson: retrieved failure memory should guide pattern selection, not become evidence when it conflicts with the golden failure mode. Step localization should prefer the first irreversible wrong decision, not the final `[EXIT]`.

## Cross-Cutting Findings

- The v5 prompt improved failure sensitivity, but the success-recall gap shows a bias toward converting uncertainty into failure.
- Many rejected cases are not unsupported; they are misaligned with golden labels or forbidden facts, especially around `missed_constraint` versus `wrong_result`.
- RAG memory is helpful for pattern priors, but it should not be treated as trajectory evidence. This matches the A6 RAGAS result: no-ground-truth faithfulness over retrieved contexts was 0.4068, indicating that short failure-memory summaries do not support the full factual content of final eval cases.

Trade-off: stricter verification raises failure recall and produces useful regression rules, but it increases false positives on successful traces and adds latency/cost through extra high-detail inspection.
