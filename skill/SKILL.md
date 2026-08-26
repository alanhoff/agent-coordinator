---
name: coordinator
description: Coordinate complex work through durable specialist workflows that delegate when available and execute inline otherwise.
---

# Coordinator

Use Coordinator when a task benefits from bounded specialist passes, durable recovery, or explicit
dependency and write-scope control. The parent task remains the sole controller: it owns requirements,
graph mutations, integration, reconciliation, and completion. A specialist owns only its assigned node.

## Start

Resolve this file's directory as `SKILL_DIR`. Keep Coordinator code and role profiles inside that
directory. Never copy them into the target repository, edit Codex settings, or register custom agents.
The only persistent external Coordinator-owned data is private runtime state under
`~/.agent-coordinator`; initialization creates and removes one private-name case-behavior probe.

Inspect repository instructions, confirm the target is a readable directory, and open a controller
session with Python 3.11 or newer:

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" session-open \
  --repo /absolute/repository --session-file /private/path/session.json --json
python3 "$SKILL_DIR/scripts/coordinator_state.py" init \
  --repo /absolute/repository --task-file /private/path/task.txt \
  --session-file /private/path/session.json --mutation-id init-001 --json
```

Keep the returned workflow ID and revision. Every ordinary mutation requires the private session file,
a never-reused mutation ID, and the exact observed prior revision. If a persistence outcome is uncertain,
run `reconcile-mutation` with the same mutation ID before deciding whether to retry.

## Build a bounded graph

Create the smallest useful dependency graph. Each node has an execution specification, acceptance
criteria, zero or more repository-relative write scopes, one role, and a rubric-v2 assessment. Omit
write scopes only for evidence-only work that will not change repository artifacts, and score its
`change_surface` as 0. Any positive `change_surface` requires at least one scope, and any declared
scope requires a positive `change_surface`; the state owner rejects mismatches. The workflow's
schema-v6 conventions default `node_complexity_split_threshold` to 6,
`dimension_complexity_split_threshold` to 3, `node_ambiguity_refine_threshold` to 4,
`factor_ambiguity_refine_threshold` to 2, and `max_refinement_depth` to 8. Thresholds are inclusive:
reaching one requires another planning mutation. Independent nodes may not overlap scopes.

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" node-add \
  --workflow-id WORKFLOW --session-file /private/path/session.json \
  --mutation-id add-api-001 --expected-revision REVISION \
  --node-id api --title "Implement API" --stage implementation --priority 70 \
  --write-scope src/api --role implementer \
  --objective "Implement the accepted API behavior" --output "Working API implementation" \
  --acceptance "Focused API tests pass" --breadth 2 --change-surface 2 --coupling 1 \
  --novelty 1 --verification 2 \
  --ambiguity-objective 0 --ambiguity-inputs 0 --ambiguity-boundaries 0 \
  --ambiguity-dependencies 0 --ambiguity-acceptance 0 \
  --complexity-rationale "Known owner-layer change with focused integration evidence" \
  --rationale "Initial role; route again immediately before execution" --json
```

`node-add` records an invalid provisional route attempt. It does not authorize a claim, even when a
role, model, or effort was supplied; run an explicit `node-route` after the latest assessment.

Before routing anything, repeatedly inspect planning diagnostics and repair every non-blocked
assessable leaf whose assessment is `stale`, `refinement_required`, or `split_required`:

1. Reassess stale work against the current specification and effective obligations; requirements;
   dependency effective outputs, normalized terminal disposition, result, and evidence; scopes; and
   planning conventions. Use `node-refine --node-id ...
   (--refinement-json|--refinement-file)` to atomically persist clarified or reassessed inputs.
2. Resolve ambiguity through concrete inputs and refine the leaf. Resolve or remove every open question
   before launch; keep bounded non-decision uncertainty in the ambiguity score. A factor of 2–4 must
   identify at least one open question, and every open question requires a factor of at least 2.
3. Decompose over-budget work with `node-split (--plan-json|--plan-file)`. The plan must cover every
   native and carried requirement, output, and acceptance obligation; map every current rewritable
   assessable direct dependent for explicit rewiring and staling; preserve each original prerequisite
   directly or through only other new children; preserve a valid DAG; respect depth; and make measurable
   complexity progress. Omit direct terminal-success dependents—their obsolete parent edge is atomically
   pruned, and they must not map to children. Retained terminal intermediaries do not witness a
   prerequisite.
4. Re-read the new revision and repeat until every non-blocked assessable leaf is current and
   `executable`. This global pre-route loop must reach a fixed point, not merely repair the next
   dispatch candidate. Blocked leaves stay in diagnostics but do not fence independent work; a
   workflow-level blocker still stops dispatch.

At `max_refinement_depth`, an assessable leaf's current recorded total and dimension scores must be
bounded. State capacity reserves two unused node records for every assessable leaf whose raw scores are
at or beyond policy, even when its assessment is `stale` or `refinement_required`. `node-add`, `node-refine`,
and `node-split` reject capacity-stranding candidate states.

Supersede chains are always acyclic. Outside aborted recovery, every effective obligation must also have
an acyclic path through recursive decomposition coverage and supersede to live, active, repairable, or
done work; reject dead ends and cycle-only resolution. Supersede transfers the source's full effective
obligations (native plus carried) and every source prerequisite, directly or transitively. Direct
skip/cancel is reserved for decomposition, supersede, or abort.

Never refine or split active work. A failed leaf with an `unclaimed` or `terminal` launch may be
repaired without erasing its attempt record. Dependency evidence or a requirement text/source change
can stale assessable leaves. Requirement semantics are immutable while referenced execution is active
or completed; first reconcile uncertain launches back to `unclaimed`. Run the same fixed-point loop
after each semantic mutation. See
`references/complexity-accounting.md` for the rubric, payloads, and split rules.
`node-refine` cannot rescore a current `split_required` leaf below policy; that leaf must use
`node-split`.

Only a non-blocked leaf with a current `executable` assessment at the global fixed point may be routed,
returned as ready, or claimed. `node-add`, `node-refine`, and `node-split` leave the next route attempt
invalid; after the latest assessment, build the existing `model_router.py choose` request from that
persisted assessment and persist an explicit `node-route`. Do not invent a second complexity score or
change the router API. Rank only model/effort candidates the active runtime currently advertises; never
invent a catalog or probe models. If the runtime exposes no catalog or route selection is unavailable
or fails, keep model and effort unset so the executor inherits the parent route. The exact adapter is
in `references/model-routing.md`.

A claim may consume a routed `pending` frontier leaf directly; the state owner atomically promotes it
to `ready` before recording `claimed`, so `pending+claimed` is never persisted. Scope overlap is checked
from NFC-normalized, platform-safe repository-relative ownership using case behavior detected from that
repository. At completion, each declared path and its ancestors must stay inside the original
repository filesystem object; unrelated pre-existing entries below a declared directory are allowed.

## Execute adaptively

Planning diagnostics order ready work by descending critical-path load, then priority, then node ID.
Inspect the current tool surface before selecting a claim batch. If no delegation tool is callable,
runtime capacity is one: select and claim exactly one inline node, then take it terminal before claiming
another. Never preclaim work for later inline execution. Otherwise fill all genuinely available
delegation capacity with independent nodes from the ordered frontier, subject to dependencies,
write-scope exclusion, the workflow maximum, and reserve. Recompute the safe frontier after every claim
or result. Terminal-success bridges stop live dependency ordering and remaining-work critical paths;
downstream is concurrently runnable, while repairable failed work retains its complexity. Do not leave
a real slot idle when a compliant leaf can run. For every selected node:

1. Read `agents/roles/ROLE.toml` from `SKILL_DIR`. Build one task packet containing its `description`
   and `developer_instructions` verbatim, plus the repository, node objective, dependencies, write
   scopes, native specification, effective requirements/outputs/acceptance, lineage provenance,
   required evidence, and a ban on graph mutation. Carried obligations remain acceptance commitments;
   do not hide them behind the child's narrower native fields. This packet is the specialist profile;
   do not depend on a globally registered agent.
2. Confirm the current tool surface. Treat delegation as enabled only when a subagent creation or
   delegation tool is callable. Do not infer it from a settings file or a configured maximum.
3. Persist a unique launch claim before execution. If delegation is enabled, pass the task packet in the
   tool's task/message argument. Pass the routed `model` and map `effort` to the tool's reasoning-effort
   argument only when each value is set and the tool schema accepts it; otherwise omit it so the child
   inherits its parent.
4. Bind the returned child ID. If no delegation tool is callable, or a call definitively creates no
   child, bind `inline-` followed by the lowercase SHA-256 digest of the request ID, then execute the
   same packet in the parent under the same role instructions. Inline execution is a full node attempt,
   not weaker acceptance.
5. If a delegation result could have created a child, persist `reconcile_required` and inspect the
   provider edge. Bind the existing child if found; bind the inline executor only after proving no
   child exists. Never duplicate uncertain work.
6. Mark the bound executor running, inspect its actual outputs, run the node's acceptance checks, and
   persist result and evidence. Reassess affected assessable work to the fixed point before its next
   route.

Monitor delegated work according to expected duration. Inline nodes run sequentially in the parent;
do not count them as parallel. Serialize overlapping write ownership in both modes. A reported frontier
width is not permission to exceed actual tool capacity or the capacity-safe, non-overlapping subset.

## Complete or recover

Use node-scoped blockers when independent work can continue. A satisfied or superseded requirement
needs concrete evidence. `finish` succeeds only when every runtime node is `done`, every decomposed
parent is `skipped`, every superseded leaf is `skipped`, and no ordinary `cancelled` node remains; all
requirements and blockers are resolved, validation is recorded, and all artifact evidence remains valid.
A node can become blocked only before its launch is claimed; pending and blocked nodes never retain an
active launch.
When a launch is claimed, the state owner fingerprints every declared artifact scope. A `done`
transition requires each declared scope to be a materialized regular file or directory whose fingerprint
changed during that attempt; the before/after evidence remains in the attempt record. Each snapshot
is rooted in the persisted repository filesystem identity, using anchored descriptor traversal where
supported. `finish` rechecks that every done artifact scope remains materialized. Evidence-only nodes declare no write scopes and
must have `change_surface=0`; they finish using their persisted result and evidence. Coordinator does not invoke or inspect a
version-control system. An explicitly deleted path cannot itself be a completed scope; deletion work
must declare a containing directory that remains materialized or be modeled as evidence-only work.

A replacement controller uses `controller-takeover`, which marks every claimed, bound, or running
launch `reconcile_required`, then `resume` before provider reconciliation and ordinary mutation.
Mutation receipts and up to 32 attempts per node remain in the atomic workflow snapshot; exhaustion is
reported instead of adding a second persistence system. Close the private session file with
`session-close` after completion. Read-only `list`, `status`, and `context` commands inspect persisted
state without mutation.

See `references/workflow-protocol.md`, `references/complexity-accounting.md`,
`references/state-schema.md`, and `references/model-routing.md` for the stable contracts.
