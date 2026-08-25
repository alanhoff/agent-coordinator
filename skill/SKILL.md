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
The only external Coordinator-owned data is private runtime state under `~/.agent-coordinator`.

Inspect repository instructions, confirm the target is a readable Git worktree, and open a controller
session with Python 3.11 or newer:

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" session-open \
  --repo /absolute/repository --session-file /private/path/session.json --json
python3 "$SKILL_DIR/scripts/coordinator_state.py" init \
  --repo /absolute/repository --task-file /private/path/task.txt \
  --session-file /private/path/session.json --mutation-id init-001 --json
```

Keep the returned workflow ID and revision. Every ordinary mutation requires the private session file,
a never-reused mutation ID, and the exact observed prior revision. If a commit outcome is uncertain,
run `reconcile-commit` with the same mutation ID before deciding whether to retry.

## Build and route the graph

Create the smallest useful dependency graph. Each node has one role, an optional model/effort route,
acceptance criteria, and repository-relative write scopes. Independent nodes may not overlap scopes.

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" node-add \
  --workflow-id WORKFLOW --session-file /private/path/session.json \
  --mutation-id add-api-001 --expected-revision REVISION \
  --node-id api --title "Implement API" --stage implementation --priority 70 \
  --write-scope src/api --role implementer \
  --acceptance "Focused API tests pass" --rationale "Bounded owner-layer change" --json
```

Use `model_router.py choose` only with model/effort candidates the active runtime currently advertises;
never invent a catalog or probe models. The profile format is in `references/model-routing.md`. If the
runtime exposes no catalog or route selection is unavailable or fails, keep model and effort unset so
the executor inherits the parent route. Persist the selected role and any available route with
`node-route` immediately before each attempt.

## Execute adaptively

For every ready node:

1. Read `agents/roles/ROLE.toml` from `SKILL_DIR`. Build one task packet containing its `description`
   and `developer_instructions` verbatim, plus the repository, node objective, dependencies, write
   scopes, acceptance criteria, required evidence, and a ban on graph mutation. This packet is the
   specialist profile; do not depend on a globally registered agent.
2. Inspect the current tool surface. Treat delegation as enabled only when a subagent creation or
   delegation tool is callable. Do not infer it from a settings file.
3. Commit a unique launch claim before execution. If delegation is enabled, pass the task packet in the
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
   persist result and evidence. Replan only future unclaimed work.

Monitor delegated work according to expected duration. Inline nodes run sequentially in the parent;
do not pretend they are parallel. Serialize overlapping write ownership in both modes.

## Complete or recover

Use node-scoped blockers when independent work can continue. A satisfied or superseded requirement
needs concrete evidence. `finish` succeeds only after all visible nodes are terminal-successful, all
requirements and blockers are resolved, validation is recorded, and a full verified commit is supplied.

A replacement controller uses `controller-takeover`, then `resume`, before ordinary mutation. Close the
private session file with `session-close` after completion. Read-only `list`, `status`, and `context`
commands inspect committed state without mutation.

See `references/workflow-protocol.md`, `references/state-schema.md`, and
`references/model-routing.md` for the stable contracts.
