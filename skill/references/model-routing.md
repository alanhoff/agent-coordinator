# Model routing contract

`model_router.py choose` accepts one UTF-8 JSON object through `--task-file PATH` or `-`. Unknown fields
are rejected.

Required fields are `summary`; a `stage` of `architecture`, `design`, `documentation`, `fix`,
`implementation`, `integration`, `research`, `review`, or `validation`; and numeric `complexity`,
`ambiguity`, `criticality`, `coupling`, `novelty`, and `determinism` scores from 1 through 5.

An optional profile contains `budget` (`value`, `balanced`, or `quality`) and up to 128 `candidates`.
Each candidate is one combination currently advertised by the runtime:

```json
{
  "model": "runtime-model-id",
  "effort": "runtime-effort-id",
  "capacity": 4.2,
  "relative_cost": 1.0
}
```

`model` accepts any non-blank runtime identifier. `effort` may be `null` when the model's default should
apply. Capacity and relative cost are non-negative caller-supplied planning heuristics, not model
allowlists, prices, or quality guarantees.

The router selects the stage's specialist role and ranks supplied candidates against task demand and
budget. With no profile or an empty candidate list, it returns `null` model and effort so a delegated
or inline executor inherits the parent route. If optional route selection cannot complete, the
controller uses the same inherited route instead of blocking the node. It persists a fresh route
immediately before each attempt and omits unset model/effort arguments from delegated invocations.
