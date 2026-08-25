---
name: coordinator
description: Coordinate complex work through durable, routed specialist-agent workflows with resumable state.
---

# Coordinator

Use Coordinator when a task benefits from multiple bounded specialist agents, durable recovery, or
explicit dependency and write-scope control. The parent task is the sole controller: it owns
requirements, graph mutations, integration, reconciliation, and completion. Specialists own only their
assigned nodes.

## Start safely

Coordinator is installed in a current-user skill directory. Never copy its files, roles, configuration,
locks, or workflow state into the repository being orchestrated.

Resolve this file's directory as `SKILL_DIR`. Inspect the target repository's instructions, confirm it is
a readable Git worktree, and use Python 3.11 or newer. Create a private session file outside the
repository, then open a controller session:

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" session-open \
  --repo /absolute/repository --session-file /private/path/session.json --json
```

Initialize a workflow with a file-backed task and a unique mutation identifier:

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" init \
  --repo /absolute/repository --task-file /private/path/task.txt \
  --session-file /private/path/session.json --mutation-id init-001 --json
```

Keep the returned workflow ID and revision. Every ordinary mutation requires the session file, a
never-reused mutation ID, and the exact observed prior revision. If a commit outcome is uncertain, run
`reconcile-commit` with that same mutation ID before deciding whether to retry.

## Build and route the graph

Create the smallest useful dependency graph. Each node must have one role, route, acceptance list, and
repository-relative write scope. Independent nodes may not overlap write scopes.

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" node-add \
  --workflow-id WORKFLOW --session-file /private/path/session.json \
  --mutation-id add-api-001 --expected-revision REVISION \
  --node-id api --title "Implement API" --stage implementation --priority 70 \
  --write-scope src/api --role implementer --model gpt-5.6-terra --effort high \
  --acceptance "Focused API tests pass" --rationale "Bounded owner-layer change" --json
```

For evidence-based routing, write the task packet described in `references/model-routing.md` and run:

```sh
python3 "$SKILL_DIR/scripts/model_router.py" choose --task-file /private/path/route.json --json
```

Persist a fresh route with `node-route` before launch. Route files define no default model or effort;
the controller supplies both per attempt.

## Launch without duplication

Before calling an agent provider, commit a unique request identity:

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" node-update \
  --workflow-id WORKFLOW --session-file /private/path/session.json \
  --mutation-id claim-api-001 --expected-revision REVISION --node-id api \
  --launch-state claimed --request-id request-api-001 --json
```

After the provider returns, bind the child with `--launch-state bound --child-id ID`, then mark the node
running. If the provider response is uncertain, persist `reconcile_required`. Inspect the provider edge
before either binding the existing child or returning to `unclaimed` with explicit reconciliation
evidence. Never blindly launch a second child.

Monitor agents according to expected work duration. Validate their actual outputs, persist results and
evidence, and change the future graph when evidence invalidates the plan. `graph-replan` accepts a
file-backed atomic plan for dependency, priority, removal, and supersession operations.

## Requirements, blockers, and completion

Persist user and mandatory requirements with `requirement-set`. A satisfied or superseded requirement
needs concrete evidence. Use node-scoped blockers when independent work can continue. Record decisions
and material events, not routine narration.

Mark a node done only with a result and validation evidence. `finish` succeeds only after all visible
nodes are terminal-successful, all requirements are resolved, all blockers are resolved, and a verified
commit is supplied.

If a new controller takes over, `controller-takeover` fences the prior epoch. The new controller must
then run `resume` before ordinary mutation. Close the private session file with `session-close` after
completion.

Read-only `list`, `status`, and `context` commands inspect committed state without mutation.

See `references/workflow-protocol.md`, `references/state-schema.md`, and
`references/model-routing.md` for the stable contracts.
