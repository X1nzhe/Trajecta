# RAGAS Report

- Sample count: 58
- Mode: `real`
- Ground truth source: `none`

## Metric means
- **faithfulness**: 0.9572

## Skipped traces
- budget_exceeded: 0
- error: 0
- no_trace: 5
- no_context: 0

## Retrieval evidence summary

Retrieved contexts are what the RAG tools returned; cited context ids are the subset the final `propose_eval_case` referenced — the two need not match. The per-tool table below is scoped to each search tool, while the occurrence and citation tables are aggregated across all tools.

| Tool | Samples | Retrieved contexts |
| --- | ---: | ---: |
| `search_failure_memory` | 0 | 0 |
| `search_failure_eval_cases` | 0 | 0 |
| `evidence` | 58 | 179 |

### Evidence context occurrences

| Context id | Occurrences in retrieved contexts |
| --- | ---: |
| `[trajectory digest] {"trajectory_id"` | 58 |
| `ec_2febd85ececb302695eef85707d169858f38857cadb51c07133c39de1e851714_step_15` | 10 |
| `ec_8f3ef841a0cdc1798c85d5a4e996f536319c7ee9026227b7a7837aabe5cc5de4_step_32` | 10 |
| `ec_1ec82bdc3cf7a04b325f161137006bcbecf7c70e0754d63088ed052dddafa134_step_1` | 7 |
| `ec_50d6d2f1a7e0476de7674ecd56bce65515ddfd6d21f4b2a9f95206b8d9a75bec_step_2` | 6 |
| `ec_e25282127f17d877f4bae26fccf4b750d11501ad7d5aa33c1e6d326cc66b9302_step_1` | 6 |
| `ec_5bbd13b41ee5ef33a18dbecf048e8d5ca53f291b414e4a7141367067b20bc54c_step_4` | 4 |
| `ec_7f0d1a127a5f8d92e315c8ee0b52e741c99a9831661d0e4288737e56157bce17_step_6` | 3 |
| `ec_dfc9bebaecfc5b144fda386c65ca157e60e748e4e0d55daa078395d336d717a7_step_5` | 3 |
| `ec_258ca520af5e5391bac0b3f8a2d06134cd9c1700d544f433c356b485a90dd9a3_step_9` | 2 |
| `fm_missed_constraint_001` | 2 |
| `[step 1 high-detail] <TRAJECTA_DATA_78cadb8e>page_state` | 1 |
| `[step 1 high-detail] <TRAJECTA_DATA_91a0c510>page_state` | 1 |
| `[step 1 high-detail] <TRAJECTA_DATA_aafa6404>page_state` | 1 |
| `[step 1 high-detail] <TRAJECTA_DATA_bd1ec18c>page_state` | 1 |
| `[step 10 high-detail] <TRAJECTA_DATA_16b40f2e>page_state` | 1 |
| `[step 10 high-detail] <TRAJECTA_DATA_3be9d364>page_state` | 1 |
| `[step 10 high-detail] <TRAJECTA_DATA_b4da0e3b>page_state` | 1 |
| `[step 10 high-detail] <TRAJECTA_DATA_d9bf8d0d>page_state` | 1 |
| `[step 11 high-detail] <TRAJECTA_DATA_3168774f>page_state` | 1 |
| `[step 13 high-detail] <TRAJECTA_DATA_5e1e8fd8>page_state` | 1 |
| `[step 13 high-detail] <TRAJECTA_DATA_6bf1b534>page_state` | 1 |
| `[step 14 high-detail] <TRAJECTA_DATA_cbc3284a>page_state` | 1 |
| `[step 14 high-detail] <TRAJECTA_DATA_dd68dfad>page_state` | 1 |
| `[step 14 high-detail] <TRAJECTA_DATA_fe7200bd>page_state` | 1 |
| `[step 15 high-detail] <TRAJECTA_DATA_2a5d16f5>page_state` | 1 |
| `[step 15 high-detail] <TRAJECTA_DATA_d6038ef3>page_state` | 1 |
| `[step 16 high-detail] <TRAJECTA_DATA_3697edf3>page_state` | 1 |
| `[step 16 high-detail] <TRAJECTA_DATA_f731a1b8>page_state` | 1 |
| `[step 17 high-detail] <TRAJECTA_DATA_1783c8d5>page_state` | 1 |
| `[step 17 high-detail] <TRAJECTA_DATA_91c1abd0>page_state` | 1 |
| `[step 18 high-detail] <TRAJECTA_DATA_57909bdc>page_state` | 1 |
| `[step 18 high-detail] <TRAJECTA_DATA_6bf1b534>page_state` | 1 |
| `[step 18 high-detail] <TRAJECTA_DATA_74691962>page_state` | 1 |
| `[step 18 high-detail] <TRAJECTA_DATA_f731a1b8>page_state` | 1 |
| `[step 19 high-detail] <TRAJECTA_DATA_2a9c5301>page_state` | 1 |
| `[step 19 high-detail] <TRAJECTA_DATA_5022ae38>page_state` | 1 |
| `[step 23 high-detail] <TRAJECTA_DATA_3e60f545>page_state` | 1 |
| `[step 23 high-detail] <TRAJECTA_DATA_90cf50e5>page_state` | 1 |
| `[step 32 high-detail] <TRAJECTA_DATA_66719b6d>page_state` | 1 |
| `[step 39 high-detail] <TRAJECTA_DATA_65a532f0>page_state` | 1 |
| `[step 4 high-detail] <TRAJECTA_DATA_099f801d>page_state` | 1 |
| `[step 4 high-detail] <TRAJECTA_DATA_2a506319>page_state` | 1 |
| `[step 4 high-detail] <TRAJECTA_DATA_c3f64f65>page_state` | 1 |
| `[step 4 high-detail] <TRAJECTA_DATA_cf321a0b>page_state` | 1 |
| `[step 4 high-detail] <TRAJECTA_DATA_e2f98b3e>page_state` | 1 |
| `[step 4 high-detail] <TRAJECTA_DATA_f40410ab>page_state` | 1 |
| `[step 43 high-detail] <TRAJECTA_DATA_021cd53d>page_state` | 1 |
| `[step 5 high-detail] <TRAJECTA_DATA_26c4fbaf>page_state` | 1 |
| `[step 5 high-detail] <TRAJECTA_DATA_460d0d69>page_state` | 1 |
| `[step 5 high-detail] <TRAJECTA_DATA_d41f5d8c>page_state` | 1 |
| `[step 5 high-detail] <TRAJECTA_DATA_d4a07178>page_state` | 1 |
| `[step 5 high-detail] <TRAJECTA_DATA_e52b884e>page_state` | 1 |
| `[step 6 high-detail] <TRAJECTA_DATA_431eba23>page_state` | 1 |
| `[step 6 high-detail] <TRAJECTA_DATA_65b51729>page_state` | 1 |
| `[step 6 high-detail] <TRAJECTA_DATA_867160cb>page_state` | 1 |
| `[step 6 high-detail] <TRAJECTA_DATA_8d393905>page_state` | 1 |
| `[step 6 high-detail] <TRAJECTA_DATA_8f85829e>page_state` | 1 |
| `[step 6 high-detail] <TRAJECTA_DATA_bd9e15e6>page_state` | 1 |
| `[step 6 high-detail] <TRAJECTA_DATA_e0fe4d97>page_state` | 1 |
| `[step 6 high-detail] <TRAJECTA_DATA_e52b884e>page_state` | 1 |
| `[step 6 high-detail] <TRAJECTA_DATA_ec755428>page_state` | 1 |
| `[step 7 high-detail] <TRAJECTA_DATA_6dadf0f7>page_state` | 1 |
| `[step 7 high-detail] <TRAJECTA_DATA_72977417>page_state` | 1 |
| `[step 7 high-detail] <TRAJECTA_DATA_823ba97c>page_state` | 1 |
| `[step 7 high-detail] <TRAJECTA_DATA_a1d3af52>page_state` | 1 |
| `[step 7 high-detail] <TRAJECTA_DATA_ad1e2b9b>page_state` | 1 |
| `[step 7 high-detail] <TRAJECTA_DATA_d8eee2b0>page_state` | 1 |
| `[step 7 high-detail] <TRAJECTA_DATA_e63181e6>page_state` | 1 |
| `[step 8 high-detail] <TRAJECTA_DATA_11204fce>page_state` | 1 |
| `[step 8 high-detail] <TRAJECTA_DATA_2db6668c>page_state` | 1 |
| `[step 8 high-detail] <TRAJECTA_DATA_76637f4a>page_state` | 1 |
| `[step 9 high-detail] <TRAJECTA_DATA_cab71303>page_state` | 1 |
| `[step 9 high-detail] <TRAJECTA_DATA_ce40e5b4>page_state` | 1 |
| `[step 9 high-detail] <TRAJECTA_DATA_ff738ff4>page_state` | 1 |
| `fm_early_terminated_001` | 1 |
| `fm_inefficient_search_001` | 1 |
| `fm_wrong_result_001` | 1 |
| `fm_wrong_target_001` | 1 |

### Cited context ids

- Traces with a proposal: 58
- Unique cited context ids: `ec_258ca520af5e5391bac0b3f8a2d06134cd9c1700d544f433c356b485a90dd9a3_step_9`, `ec_2febd85ececb302695eef85707d169858f38857cadb51c07133c39de1e851714_step_15`, `ec_5bbd13b41ee5ef33a18dbecf048e8d5ca53f291b414e4a7141367067b20bc54c_step_4`, `ec_7f0d1a127a5f8d92e315c8ee0b52e741c99a9831661d0e4288737e56157bce17_step_6`, `ec_8f3ef841a0cdc1798c85d5a4e996f536319c7ee9026227b7a7837aabe5cc5de4_step_32`, `ec_dfc9bebaecfc5b144fda386c65ca157e60e748e4e0d55daa078395d336d717a7_step_5`, `ec_e25282127f17d877f4bae26fccf4b750d11501ad7d5aa33c1e6d326cc66b9302_step_1`, `fm_inefficient_search_001`
- Total cited-id references (deduped per trace): 14

| Context id | Traces citing it |
| --- | ---: |
| `ec_e25282127f17d877f4bae26fccf4b750d11501ad7d5aa33c1e6d326cc66b9302_step_1` | 4 |
| `ec_8f3ef841a0cdc1798c85d5a4e996f536319c7ee9026227b7a7837aabe5cc5de4_step_32` | 3 |
| `ec_2febd85ececb302695eef85707d169858f38857cadb51c07133c39de1e851714_step_15` | 2 |
| `ec_258ca520af5e5391bac0b3f8a2d06134cd9c1700d544f433c356b485a90dd9a3_step_9` | 1 |
| `ec_5bbd13b41ee5ef33a18dbecf048e8d5ca53f291b414e4a7141367067b20bc54c_step_4` | 1 |
| `ec_7f0d1a127a5f8d92e315c8ee0b52e741c99a9831661d0e4288737e56157bce17_step_6` | 1 |
| `ec_dfc9bebaecfc5b144fda386c65ca157e60e748e4e0d55daa078395d336d717a7_step_5` | 1 |
| `fm_inefficient_search_001` | 1 |

## How this was generated

`ragas_mode=real` — real `ragas` faithfulness evaluation over retrieved contexts.
`ground_truth_source=none` — no artificial or self-generated ground truth is used; the report measures whether the final claims are supported by retrieved contexts.

Trace source precedence (Phase 8 A6.1): explicit `--trace-dir` Phase 8 A2 dumps first at `<trace_dir>/<trajectory_id>.json`; on miss, fall back to the SQLite `traces` table (`storage.load_trace`). The run-id discovery set is the union of SQLite-resident runs and `<trace_dir>/*.json` files.
Each RAGAS sample corresponds to one recorded `search_failure_memory` or `search_failure_eval_cases` tool call: `question` is the tool query, `contexts` are that tool result's items, and `answer` is the final `propose_eval_case` actual_behavior plus evidence claims.
