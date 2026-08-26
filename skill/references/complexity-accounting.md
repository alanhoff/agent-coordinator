# Complexity accounting and decomposition

Assessment is an execution gate, not a reporting estimate. The controller must bring every non-blocked
assessable leaf to a fixed point of current executable work before routing anything. Node-scoped blocked
leaves remain diagnosed but do not fence independent dispatch. Active work is never rewritten to make
a score fit.

## Rubric version 1

Score every dimension with an integer from 0 through 4. Use the highest description that applies and
record concrete reasons; optimism is not evidence.

| Score | `breadth` | `change_surface` | `coupling` | `novelty` | `verification` |
|---:|---|---|---|---|---|
| 0 | No new behavior | Read-only or no artifact change | Independent | Fully mechanical | Existing evidence is sufficient |
| 1 | One narrow behavior or path | One localized owner surface | One stable local interface | Direct repository precedent | One deterministic focused check |
| 2 | Several related behaviors | Several files in one owner boundary | Several known dependencies | Adaptation of a known pattern | Several focused checks or fixtures |
| 3 | Multiple components or paths | Multiple owner boundaries or a shared contract | Sequenced cross-boundary integration | New design with bounded research | Integration plus meaningful edge cases |
| 4 | Cross-system or product-wide behavior | Broad public, persistence, or cross-cutting change | Tight or uncertain multi-party coordination | No reliable precedent or an unproven approach | Broad, external, nondeterministic, or high-risk proof |

`total` is derived as the sum of `breadth`, `change_surface`, `coupling`, `novelty`, and
`verification`; callers never supply it. Score ambiguity separately:

- 0: every execution-affecting decision is resolved.
- 1: bounded uncertainty remains but no unresolved decision can change execution or acceptance.
- 2: an unresolved choice can change implementation or verification inside the current boundary.
- 3: unresolved choices can change contracts, ownership, dependencies, or acceptance.
- 4: the objective, required outputs, or success conditions are materially unknown.

Open questions must agree with the ambiguity score. Any listed open question forces refinement even if
its numeric ambiguity is within policy; score 1 represents residual uncertainty, not an unresolved
decision.

## Thresholds and derived state

The workflow policy defaults are `max_node_complexity = 8`, `max_dimension_complexity = 3`,
`max_node_ambiguity = 1`, and `max_refinement_depth = 8`. The state owner derives an assessment's
rubric version, total, input digest, and state from validated inputs.

A leaf needs refinement when it has any open question or ambiguity exceeds `max_node_ambiguity`. It
needs splitting when total exceeds `max_node_complexity` or any dimension exceeds
`max_dimension_complexity`. Resolve material ambiguity before decomposition because the missing
decision can change the correct split. A node with recorded children is `decomposed`. An assessable leaf
whose stored digest no longer matches current inputs is `stale`, regardless of its previous score. An
assessable leaf has no children and is either pending, ready, or blocked with an `unclaimed` launch, or
`failed` with an `unclaimed` or `terminal` launch. Only a current leaf within all thresholds is
`executable`.

At `max_refinement_depth`, an assessable leaf's current recorded total and dimension scores must be
within policy. Globally, the 128-node state reserves two unused records for every assessable leaf whose
raw scores are over budget, even when its derived state is `stale` or `refinement_required`. Candidate
states produced by add, refine, or split are rejected when they strand such a leaf without the minimum
two-child capacity. Children may require another split only when both depth and the global reserve
remain sufficient.

The digest covers native specification and acceptance; carried lineage obligations and their effective
requirements; write scopes; score inputs; ambiguity; planning policy; and each dependency's identity,
effective outputs, normalized terminal disposition, result, and evidence. Disposition is `done`,
`failed`, `skipped`, or `cancelled` for terminal work and `nonterminal` otherwise. Nonterminal status
transitions therefore do not stale dependents. Output changes, terminal completion/failure/skip/cancel,
and retry from failure can stale direct assessable dependents; requirement changes stale every affected
assessable leaf. Reassess against the new snapshot instead of copying the former score.

## Refinement

`node-refine --node-id NODE (--refinement-json JSON|--refinement-file PATH)` accepts exactly:

```json
{
  "spec": {
    "objective": "One bounded outcome",
    "inputs": ["Current dependency evidence"],
    "outputs": ["Named artifact or decision"],
    "constraints": ["Repository and protocol limits"],
    "non_goals": ["Explicitly excluded adjacent work"],
    "requirement_ids": ["req-1"],
    "open_questions": []
  },
  "acceptance": ["Observable proof of the bounded outcome"],
  "write_scopes": ["repository/relative/path"],
  "assessment": {
    "dimensions": {
      "breadth": 1,
      "change_surface": 1,
      "coupling": 1,
      "novelty": 1,
      "verification": 1
    },
    "ambiguity": 0,
    "rationale": "Why each score fits the current evidence"
  }
}
```

The mutation atomically replaces the eligible leaf's native specification, acceptance, scopes, and
assessment inputs. Carried lineage obligations are not part of this payload and survive unchanged.
Newly added roots start empty, but a root replacement may carry obligations transferred by supersede;
refinement preserves them while replacing native fields. `rubric_version`, `total`, `input_digest`, and
state are derived and must not be supplied. Refinement clears model/effort and leaves the next route
attempt invalid until an explicit `node-route` follows the latest assessment. A claimed,
`reconcile_required`, bound, or running node is active and cannot be refined. A `failed` leaf can be
refined when its launch is `unclaimed` or `terminal`; its attempt history is retained.

## Split plan

`node-split (--plan-json JSON|--plan-file PATH)` accepts an object with exactly `parent_id`, `reason`,
`children`, `coverage`, and `dependent_replacements`:

```json
{
  "parent_id": "api",
  "reason": "Separate contract, implementation, and proof to reduce change surface",
  "children": [
    {
      "id": "api-contract",
      "title": "Define API contract",
      "stage": "design",
      "priority": 80,
      "dependencies": [],
      "write_scopes": ["src/api/schema.py"],
      "role": "architect",
      "model": null,
      "effort": null,
      "acceptance": ["Contract tests describe every required response"],
      "spec": {
        "objective": "Define the accepted API contract",
        "inputs": ["req-1"],
        "outputs": ["API contract"],
        "constraints": ["Preserve public compatibility"],
        "non_goals": ["Implement handlers"],
        "requirement_ids": ["req-1"],
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
        "ambiguity": 0,
        "rationale": "One contract surface with a deterministic check"
      },
      "route_rationale": "Initial design role; route again before execution",
      "estimated_cost": null
    },
    {
      "id": "api-implementation",
      "title": "Implement API contract",
      "stage": "implementation",
      "priority": 70,
      "dependencies": ["api-contract"],
      "write_scopes": ["src/api/handlers.py"],
      "role": "implementer",
      "model": null,
      "effort": null,
      "acceptance": ["Focused API tests pass"],
      "spec": {
        "objective": "Implement the accepted API contract",
        "inputs": ["API contract"],
        "outputs": ["Working API implementation"],
        "constraints": ["Conform to the accepted contract"],
        "non_goals": ["Redesign the contract"],
        "requirement_ids": ["req-1"],
        "open_questions": []
      },
      "assessment": {
        "dimensions": {
          "breadth": 1,
          "change_surface": 1,
          "coupling": 1,
          "novelty": 1,
          "verification": 2
        },
        "ambiguity": 0,
        "rationale": "One handler surface with focused contract tests"
      },
      "route_rationale": "Initial implementation role; route again before execution",
      "estimated_cost": null
    }
  ],
  "coverage": {
    "requirements": {"req-1": ["api-contract"]},
    "outputs": {"Working API implementation": ["api-implementation"]},
    "acceptance": {"Focused API tests pass": ["api-implementation"]}
  },
  "dependent_replacements": {
    "api-integration": ["api-implementation"]
  }
}
```

Each child object uses exactly `id`, `title`, `stage`, `priority`, `dependencies`, `write_scopes`,
`role`, `model`, `effort`, `acceptance`, `spec`, `assessment`, `route_rationale`, and
`estimated_cost`. `model`, `effort`, and `estimated_cost` are nullable; child assessment contains only
the input fields shown above. Dependencies may refer to retained nodes or other children, but must
produce an acyclic graph. Collectively the children must preserve every parent prerequisite: for each
original parent prerequisite, at least one child must depend on it directly or through a path containing
only other new children. Retained nodes, including terminal intermediaries, do not witness it.

Coverage is total and exact. Its three maps have one key for every effective parent requirement,
output, and acceptance obligation—the ordered union of native and previously carried items—with no
extra keys and a nonempty child-ID list for each. Each mapping is materialized on the selected child's
`lineage.obligations`; it is durable provenance, not a one-time validation hint. Child native fields
remain the child's narrower task definition while effective obligations include both native and carried
items. `dependent_replacements` has exactly one key for every current rewritable assessable direct
dependent of the parent, no extras, and maps it to a nonempty list of child IDs. These dependents are
future leaves—pending, ready, or blocked with an `unclaimed` launch—or repairable failed leaves with an
`unclaimed` or `terminal` launch. Each is explicitly rewired and becomes stale for reassessment.

Direct terminal-success dependents (`done`, `skipped`, or `cancelled`) no longer need the prerequisite.
Omit them from `dependent_replacements`; their decomposed-parent edge is atomically pruned, and they must
not map to future children. Any other direct dependent is not rewritable, so the split is rejected. Use
an empty object when no current rewritable assessable direct dependent exists.

The parent must be a non-active leaf. A split creates at least two children without exceeding the
workflow's 128-node bound or its two-record reserve for every assessable leaf whose current recorded
total or dimension scores are over policy. That raw over-budget test applies even while assessment state
is `stale` or `refinement_required`; ambiguity cannot hide split liveness. Such a leaf cannot be at
`max_refinement_depth`. Children must have coherent scopes and dependencies, and each child's derived
total must be strictly lower than the parent's. Split children also start with a provisional route
attempt and require explicit `node-route`. The mutation records parent and child lineage, preserves any
failed attempt record, rewires and stales eligible dependents, prunes terminal-success edges, and
validates the complete DAG atomically.

## Lineage obligations

`lineage.obligations` has exactly `requirements`, `outputs`, and `acceptance` lists. Newly added roots
start with empty lists. Split coverage populates child lists, and recursive split coverage must include
both the child's native items and everything it carries. Refinement replaces native fields but preserves
these lists. Supersede transfers missing carried obligations to a rewritable replacement and stales it;
carried work cannot be silently removed, skipped, or cancelled.

Supersede chains are always acyclic. Outside aborted recovery, they must terminate in resolvable work,
and each carried item's combined decomposition-coverage and supersede graph must provide an acyclic
path from every carrier to a live leaf, active launch, repairable failed leaf, or `done` resolver. Dead
ends and cycle-only resolution are rejected.

Task packets must state native specification separately from effective requirements, outputs, and
acceptance, and include parent/depth/split provenance. A specialist is accountable for carried
obligations even when their text is absent from its native fields.

## Fixed point and parallel dispatch

After every graph mutation or evidence change:

1. Read a fresh revision and planning diagnostics.
2. Reassess stale eligible leaves with current evidence.
3. Refine ambiguity above policy.
4. Split every over-budget eligible leaf and reassess its children.
5. Repeat until no non-blocked assessable leaf is stale, `refinement_required`, or `split_required`.

Depth, progress, and capacity-stranding validation prevent endless or impossible decomposition. If
missing information prevents refinement, record a node-scoped blocker. The leaf remains visible in
diagnostics but no longer fences independent dispatch; unblocking it puts it back inside the global
fixed-point gate. A workflow-level blocker still stops all dispatch.

At the fixed point, use `dispatch_order`: descending `critical_path_load`, then priority, then ID.
The load represents remaining work. Terminal-success (`done`, `skipped`, or `cancelled`) and decomposed
nodes have zero load and sever the downstream bridge; a repairable failed leaf retains its assessment
complexity. Other remaining leaves contribute their assessment total plus the greatest reachable
downstream load.
`usable_parallelism` is the workflow maximum less reserve; `available_parallelism` is the smaller of
the executable frontier width and usable capacity remaining after active launches. Launch every ordered
leaf up to that bound. Write-scope ordering uses live dependency reachability. It stops through a
`done`, `skipped`, or `cancelled` bridge because downstream work is concurrently runnable; overlapping
scopes on those live peers are therefore rejected. Recompute after every claim and terminal result;
inline execution remains one node at a time.
