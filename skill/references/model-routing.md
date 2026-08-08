# Model routing contract

`model_router.py choose` accepts one UTF-8 JSON object through `--task-file PATH` or `-`. Unknown fields are rejected.

Required fields:

- `summary`: non-blank task summary;
- `stage`: `architecture`, `design`, `documentation`, `fix`, `implementation`, `integration`, `research`, `review`, or `validation`;
- `complexity`, `ambiguity`, `criticality`, `coupling`, `novelty`, and `determinism`: numbers from 1 through 5.

Optional non-negative integers `input_tokens` and `expected_output_tokens` default to 25,000 and 2,000. An optional profile file may contain only `allowed_models`, `allowed_efforts`, and `budget` (`value`, `balanced`, or `quality`).

The output selects one of the installed roles and an allowed model/effort pair. Capacity and relative-cost scores are deterministic planning heuristics, not price or quality guarantees. The controller must inspect current evidence, record a rationale, and persist a fresh route immediately before launch.
