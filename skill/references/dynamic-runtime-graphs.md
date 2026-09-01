# Dynamic runtime graph contract

Coordinator schema version 7 adds a parent-owned runtime control plane. Workers may report evidence,
but only the state owner derives projections, selects a rewrite, materializes topology, moves a gate,
creates another iteration, or persists a verdict. Every command below uses the ordinary private session,
unique mutation ID, exact expected revision, state lock, receipt, and complete-state validation.

## Observe and reconcile remaining work

Use `node-observe` only for material evidence discovered while executing or retrying a leaf. The payload
has exactly these fields:

```json
{
  "progress": 45,
  "dimensions": {
    "breadth": 2,
    "change_surface": 2,
    "coupling": 1,
    "novelty": 1,
    "verification": 2
  },
  "ambiguity_factors": {
    "objective": 0,
    "inputs": 1,
    "boundaries": 0,
    "dependencies": 0,
    "acceptance": 0
  },
  "estimated_remaining_cost": 12.5,
  "confidence": 60,
  "signals": ["new integration boundary found"],
  "note": "The original leaf now spans discovery and implementation."
}
```

`progress` and `confidence` are integers from 0 through 100. Progress cannot move backwards.
Dimensions and ambiguity factors use the rubric's 0–4 values. Remaining cost is non-negative or
`null`; signals contain at most 16 bounded strings. A node accepts at most 64 observations. Judge nodes
and `done`, `skipped`, `cancelled`, decomposed, or already-judging work cannot be observed; failed
leaves remain observable because they are retryable.

The state owner derives the latest projection and recomputes it during validation. Inclusive ambiguity
thresholds select `refine`; otherwise inclusive complexity thresholds select `split`; otherwise the
projection is `stable`. Callers cannot supply the recommendation, total, reason, or timestamp.

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" node-observe \
  --workflow-id WORKFLOW --session-file /private/path/session.json \
  --mutation-id observe-api-001 --expected-revision REVISION --node-id api \
  --observation-file /private/path/api-observation.json --json
```

Read `next`. When its action is `reconcile_runtime`, `graph-reconcile` ranks actionable projections by
descending live remaining critical-path load and stable node ID, then rewrites exactly one leaf. The
default rewrite is a discovery → execution pipeline. A consistent running attempt is first closed as
`adapted at runtime`; claimed, bound, and reconciliation-required launches cannot be adapted.

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" graph-reconcile \
  --workflow-id WORKFLOW --session-file /private/path/session.json \
  --mutation-id reconcile-runtime-001 --expected-revision REVISION --json
```

## Explicit runtime expansion and shape selection

Use `graph-expand-auto` when runtime evidence already identifies the bounded fragments. The expansion
object has exactly `parent_id`, `reason`, `shape`, `workload`, `fragments`, and `join`. `fragments`
contains two through sixteen ordinary strict plan-node manifests. `join` is another manifest or `null`.

```json
{
  "parent_id": "integration",
  "reason": "Runtime discovery exposed two independent adapters and one integration check",
  "shape": "auto",
  "workload": "heterogeneous",
  "fragments": [
    {
      "id": "adapter-a",
      "title": "Implement adapter A",
      "stage": "implementation",
      "priority": 70,
      "dependencies": [],
      "write_scopes": ["src/adapter_a.py"],
      "role": "implementer",
      "model": null,
      "effort": null,
      "acceptance": ["Adapter A focused checks pass"],
      "route_rationale": "Route after materialization",
      "estimated_cost": null,
      "spec": {
        "objective": "Implement adapter A",
        "inputs": [],
        "outputs": ["Adapter A"],
        "constraints": [],
        "non_goals": [],
        "requirement_ids": [],
        "open_questions": []
      },
      "assessment": {
        "dimensions": {
          "breadth": 1,
          "change_surface": 1,
          "coupling": 1,
          "novelty": 1,
          "verification": 1
        },
        "ambiguity_factors": {
          "objective": 0,
          "inputs": 0,
          "boundaries": 0,
          "dependencies": 0,
          "acceptance": 0
        },
        "rationale": "One bounded adapter"
      }
    },
    {
      "id": "adapter-b",
      "title": "Implement adapter B",
      "stage": "implementation",
      "priority": 70,
      "dependencies": [],
      "write_scopes": ["src/adapter_b.py"],
      "role": "implementer",
      "model": null,
      "effort": null,
      "acceptance": ["Adapter B focused checks pass"],
      "route_rationale": "Route after materialization",
      "estimated_cost": null,
      "spec": {
        "objective": "Implement adapter B",
        "inputs": [],
        "outputs": ["Adapter B"],
        "constraints": [],
        "non_goals": [],
        "requirement_ids": [],
        "open_questions": []
      },
      "assessment": {
        "dimensions": {
          "breadth": 1,
          "change_surface": 1,
          "coupling": 1,
          "novelty": 1,
          "verification": 1
        },
        "ambiguity_factors": {
          "objective": 0,
          "inputs": 0,
          "boundaries": 0,
          "dependencies": 0,
          "acceptance": 0
        },
        "rationale": "One bounded adapter"
      }
    }
  ],
  "join": {
    "id": "adapter-integration",
    "title": "Validate adapter integration",
    "stage": "integration",
    "priority": 60,
    "dependencies": [],
    "write_scopes": [],
    "role": "validator",
    "model": null,
    "effort": null,
    "acceptance": ["Both adapters pass integrated checks"],
    "route_rationale": "Route after materialization",
    "estimated_cost": null,
    "spec": {
      "objective": "Validate both adapters together",
      "inputs": ["Adapter A", "Adapter B"],
      "outputs": ["Integrated validation evidence"],
      "constraints": [],
      "non_goals": [],
      "requirement_ids": [],
      "open_questions": []
    },
    "assessment": {
      "dimensions": {
        "breadth": 1,
        "change_surface": 0,
        "coupling": 1,
        "novelty": 0,
        "verification": 2
      },
      "ambiguity_factors": {
        "objective": 0,
        "inputs": 0,
        "boundaries": 0,
        "dependencies": 0,
        "acceptance": 0
      },
      "rationale": "Evidence-only integration check"
    }
  }
}
```

`shape` is `auto`, `pipeline`, `parallel`, `fanout_fanin`, `map_reduce`, `diamond`, or `dag`.
`workload` is `homogeneous` or `heterogeneous`.

Auto selection is deterministic:

1. caller-declared internal fragment dependencies select `dag`;
2. a homogeneous joined expansion selects `map_reduce`;
3. exactly two branches with a join, when at least one is review/validation, select `diamond`;
4. another joined expansion selects `fanout_fanin`;
5. unjoined overlapping write ownership selects `pipeline`;
6. otherwise it selects `parallel`.

Auto-mode fragments are sorted by identifier. Explicit `dag` retains caller-declared internal edges,
rejects cycles with a fragment-local topological check and ordinary full-state DAG validation, and
derives exits as fragment sinks. Joined
shapes have one exit. Unjoined parallel or DAG expansions may have multiple exits and rewire former
dependents to every exit. A configured judge gate may follow an expansion only when there is exactly
one exit; use a join otherwise.

Each generated node receives a nested `graph_path`. Expanding a generated leaf extends that path rather
than flattening provenance. Physical dependencies always remain acyclic. When an active node-scoped
blocker targets a parent, its runtime-generated replacement descendants remain blocked until that same
blocker is explicitly resolved.

## Completion judges

Configure a gate before the target reaches a terminal status. Its payload has exactly `mode`,
`required`, `judges`, and `loop`. `judges` contains one through eight strict plan-node manifests with
empty write scopes, `change_surface=0`, and stage `review` or `validation`. Their dependencies are
extended with the target automatically.

```json
{
  "mode": "quorum",
  "required": 2,
  "judges": [
    {
      "id": "api-contract-judge",
      "title": "Judge API contract conformance",
      "stage": "validation",
      "priority": 80,
      "dependencies": [],
      "write_scopes": [],
      "role": "validator",
      "model": null,
      "effort": null,
      "acceptance": ["Verdict cites contract evidence"],
      "route_rationale": "Independent validation gate",
      "estimated_cost": null,
      "spec": {
        "objective": "Judge contract conformance",
        "inputs": ["Target candidate result"],
        "outputs": ["Pass or fail verdict"],
        "constraints": ["Use material evidence"],
        "non_goals": ["Modify artifacts"],
        "requirement_ids": [],
        "open_questions": []
      },
      "assessment": {
        "dimensions": {
          "breadth": 1,
          "change_surface": 0,
          "coupling": 1,
          "novelty": 0,
          "verification": 2
        },
        "ambiguity_factors": {
          "objective": 0,
          "inputs": 0,
          "boundaries": 0,
          "dependencies": 0,
          "acceptance": 0
        },
        "rationale": "Evidence-only contract judgment"
      }
    },
    {
      "id": "api-regression-judge",
      "title": "Judge regression evidence",
      "stage": "review",
      "priority": 80,
      "dependencies": [],
      "write_scopes": [],
      "role": "reviewer",
      "model": null,
      "effort": null,
      "acceptance": ["Verdict cites regression evidence"],
      "route_rationale": "Independent review gate",
      "estimated_cost": null,
      "spec": {
        "objective": "Judge regression safety",
        "inputs": ["Target candidate result"],
        "outputs": ["Pass or fail verdict"],
        "constraints": ["Use material evidence"],
        "non_goals": ["Modify artifacts"],
        "requirement_ids": [],
        "open_questions": []
      },
      "assessment": {
        "dimensions": {
          "breadth": 1,
          "change_surface": 0,
          "coupling": 1,
          "novelty": 0,
          "verification": 2
        },
        "ambiguity_factors": {
          "objective": 0,
          "inputs": 0,
          "boundaries": 0,
          "dependencies": 0,
          "acceptance": 0
        },
        "rationale": "Evidence-only regression judgment"
      }
    }
  ],
  "loop": {"id": "api-feedback", "max_iterations": 3}
}
```

For `all`, `required` equals the judge count. For `any`, it equals 1. For `quorum`, it is within the
panel size. Coordinator intentionally records the complete panel before resolving any mode, preserving
a full audit and preventing an unreported judge from disappearing after an early mathematical result.

When the gated target succeeds, its launch becomes terminal and status becomes `judging`. Candidate
result and evidence are retained on the target but are excluded from downstream dependency digests
until resolution. Route and execute judges through the ordinary claim/start lifecycle, then replace
`node-complete` with `judge-complete` for the terminal verdict:

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" judge-complete \
  --workflow-id WORKFLOW --session-file /private/path/session.json \
  --mutation-id judge-contract-001 --expected-revision REVISION \
  --node-id api-contract-judge --verdict pass \
  --result-file /private/path/judge-result.txt \
  --evidence-file /private/path/judge-evidence.txt --json
```

A passing policy makes the target `done`. A failed gate without a loop makes it `failed`.

## Bounded feedback without persisted cycles

A non-null `loop` declares a unique identifier and `max_iterations` from 2 through 16. If the full judge
panel rejects an iteration and capacity remains, the state owner:

1. freezes the failed target and its gate as history;
2. creates a fresh target and fresh judge panel with deterministic derived identifiers;
3. gives every generated node the next iteration number and preserved enclosing graph path;
4. supersedes the failed target and rewires eligible downstream work to the new target;
5. records the adaptation and requires fresh routing;
6. repeats until a gate passes or the hard limit is exhausted.

This is a logical cycle implemented as finite, versioned acyclic iterations. It never introduces
a dependency back-edge. If the hard limit is reached, the target is `failed` and the loop is
`exhausted`.

## Structural fences and capacity

Runtime mutations reject any candidate state that violates ordinary graph, scope, assessment,
execution, receipt, or recovery invariants. Additional limits are:

| Resource | Limit |
|---|---:|
| Workflow nodes | 128 |
| Fragments in one explicit expansion | 16 |
| Judges in one gate | 8 |
| Runtime loops | 32 |
| Iterations in one loop | 16 |
| Observations per node | 64 |
| Adaptation records | 256 |
| Nested graph-path identifiers | 32 |

Judge nodes cannot be structurally observed or expanded. Generic `node-split` and structural
`graph-replan` cannot mutate runtime-controlled judges, active gate targets, or active loop targets.
A configured gate can be retargeted only as part of one atomic runtime expansion with one exit. Active
or resolved gates cannot move.

`abort` resolves nonterminal runtime control records consistently while preserving uncertain-provider
reconciliation. `controller-takeover` and `resume` retain their existing launch fencing; runtime graph
state does not authorize duplicate execution. For a reconciliation action, `next` identifies `child_id`
as conditional input when restoring a bound or running child, and `status` plus `attempt_outcome` when
recording a terminal provider result. Reusing the same successful mutation ID and payload
returns its receipt instead of creating another expansion, gate, observation, verdict, or iteration.
