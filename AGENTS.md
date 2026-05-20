# AGENTS

## Purpose
EvalTrace Lite runs an eval-focused agent that analyzes browser-agent trajectory evidence and drafts regression eval cases for human review.

## Rules
- Human review is required before final failure labels are considered confirmed.
- The agent must use tools (step lookup, memory retrieval, and analysis) before drafting an eval case.
