# Workflow state and recovery

Coordinator stores one bounded `schema-v4` JSON document with `schema_version` equal to 4 per
workflow under `~/.agent-coordinator/workflows`. The state owner validates the complete document on
every read and before every commit. It rejects unknown fields, unsafe identifiers and write paths,
missing or cyclic dependencies, concurrent scope collisions, invalid transitions, inconsistent
execution state, and capacity violations.

## Planning policy and node records

Workflow `conventions` contains the execution capacity settings plus these integer planning limits:

- `max_node_complexity` defaults to 8.
- `max_dimension_complexity` defaults to 3.
- `max_node_ambiguity` defaults to 1.
- `max_refinement_depth` defaults to 8.

Every node contains `spec`, `assessment`, and `lineage` alongside its execution fields. Exact-key
validation applies at every level.

`spec` contains `objective`, `inputs`, `outputs`, `constraints`, `non_goals`, `requirement_ids`, and
`open_questions`. `assessment` contains `rubric_version`, `dimensions`, `total`, `ambiguity`,
`rationale`, `input_digest`, and `state`. Rubric version 1 dimensions are integer `breadth`,
`change_surface`, `coupling`, `novelty`, and `verification`, each 0 through 4; `total` is their derived
sum. Assessment state is exactly `executable`, `split_required`, `refinement_required`, `stale`, or
`decomposed`.

`lineage` contains exactly nullable `parent_id`, integer `depth`, `child_ids`, nullable `split_reason`,
and `obligations`. `obligations` contains exactly `requirements`, `outputs`, and `acceptance` lists. New
roots start with depth 0 and empty carried obligations; supersede may later add carried obligations to a
rewritable root replacement. A successful split records all child IDs and the reason on the parent,
gives every child the parent ID and next depth, materializes its coverage assignments as carried
obligations, and makes the parent `decomposed` and ineligible for execution.

A node's effective requirements, outputs, and acceptance are ordered unions of its native
`spec.requirement_ids`, `spec.outputs`, and `acceptance` with the corresponding carried obligations.
Refinement replaces native fields but preserves carried obligations. Recursive splitting covers native
plus carried items; supersede transfers carried items to a rewritable replacement. Carried obligations
cannot disappear through an ordinary remove, skip, or cancel. They participate in assessment digests,
requirement invalidation, effective dependency outputs, and specialist task packets.

Supersede chains are always acyclic. Outside aborted recovery, they must terminate in resolvable work,
and each carried obligation's combined decomposition-coverage and supersede graph must have an acyclic
path to a live leaf, active launch, repairable failed leaf, or `done` resolver. A dead end or cycle-only
resolution is invalid.

`input_digest` is derived from native specification and acceptance, carried obligations and linked
effective requirements, write scopes, dimension and ambiguity inputs, planning conventions, and each
dependency's identity, effective outputs, normalized terminal disposition, result, and evidence. A
dependency disposition is its exact `done`, `failed`, `skipped`, or `cancelled` status, or
`nonterminal` for every other status. Nonterminal status transitions do not change the digest; output
changes, terminal disposition/result/evidence, and retry from failure can stale direct dependents.

An assessable leaf has no children and is either pending, ready, or blocked with an `unclaimed` launch,
or `failed` with an `unclaimed` or `terminal` launch. A digest mismatch makes such a leaf `stale`; stale
work cannot be ready, routed, or claimed. Changed effective requirements stale every affected
assessable leaf.

For a split, `dependent_replacements` names exactly the parent's current rewritable assessable direct
dependents; each is explicitly rewired and staled. Direct terminal-success dependents (`done`, `skipped`,
or `cancelled`) are omitted and must not map to children: their obsolete parent edge is atomically
pruned. Any other current direct dependent rejects the split.

Each node stores one specialist role. Model and effort are bounded strings supplied by the active
runtime, or `null` to inherit the parent route; Coordinator has no built-in model catalog. A non-blocked
assessable leaf with a current `executable` assessment may be routed at the global fixed point; routing
a failed leaf resets it for retry. `ready_nodes` and claim additionally require an unclaimed future
leaf with satisfied dependencies.

The workflow planning fixed point requires every non-blocked assessable leaf to have a current
`executable` assessment. Node-scoped blocked leaves remain in planning diagnostics but do not fence
independent dispatch. A workflow-level blocker still empties the frontier. At
`max_refinement_depth`, an assessable leaf cannot have current recorded over-budget total or dimension
scores. State validation reserves two unused node records for every assessable leaf with those raw
scores, even if its derived assessment state is `stale` or `refinement_required`; add, refine, and split
mutations reject capacity-stranding candidate states.

Planning diagnostics expose `over_budget_nodes`, `ambiguous_nodes`, `refinement_required_nodes`,
`stale_nodes`, and `decomposed_nodes`. They also expose `frontier_width`, `available_parallelism`,
`usable_parallelism`, the node-to-load map `critical_path_load`, and `dispatch_order`; graph validation
retains `ready_nodes` as an alias. Dispatch order is descending critical-path load, then priority, then
node ID. Critical-path load measures remaining work. Terminal-success and decomposed nodes contribute
zero and sever the bridge to downstream dependents; a repairable failed leaf retains its assessment
complexity. Other remaining leaves contribute their assessment total plus the greatest reachable
downstream dependent load. `usable_parallelism` is
`max_parallel - reserve`; `available_parallelism` is the smaller of frontier width and usable capacity
remaining after active launches. These diagnostics do not relax dependencies, write-scope exclusion,
reserve, or actual runtime capacity.

Write-scope ordering uses live dependency reachability. Traversal stops at a `done`, `skipped`, or
`cancelled` bridge because its downstream work is concurrently runnable; overlapping scopes between
those live peers remain a collision.

Node add and split create route attempt 0; refinement sets the route attempt to the number of completed
attempts. Both forms are provisional and invalid for the next claim. After the latest assessment and
global fixed point, an explicit `node-route` must persist attempt `len(attempts) + 1`. Routing a failed
retry resets its disposition to nonterminal and can stale direct dependents before claim.

Each mutation supplies a unique mutation ID and expected prior revision. A committed receipt makes
retry reconciliation idempotent; reuse of an ID for different content and stale revisions are rejected.
Atomic replacement and durability flushing ensure readers observe a complete old or new snapshot.
`reconcile-commit` distinguishes a recorded mutation from one absent from committed state.

One controller session owns an epoch. Bearer values exist only in the caller-selected private file and
private session registry, never in workflow state or command output. A takeover advances the epoch,
fences the old controller, and requires explicit `resume`.

Launch states distinguish `unclaimed`, `claimed`, `reconcile_required`, `bound`, `running`, and
`terminal`. Commit `claimed` before execution. A delegated executor binds its returned child ID; an
inline executor binds `inline-` plus the lowercase SHA-256 digest of the request ID, keeping the
derived identifier within the state limit. An uncertain delegation becomes `reconcile_required` and
must be reconciled before binding or retrying.

Read-only `list`, `status`, and `context` operations never create, lock, repair, normalize, cache, or
clean state.
