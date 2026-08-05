# Temporary state schema and recovery semantics

`coordinator_state.py` stores one workflow per directory beneath the operating-system temporary root:

```text
<temp>/codex-coordinator/<workflow-id>/state.json
```

`COORDINATOR_TMP_ROOT` is available for tests and controlled deployment but must still resolve beneath the operating-system temporary directory. Every invocation removes Coordinator-owned entries older than five days. `init` accepts `--task-file` and `graph-replan` accepts `--plan-file` (with `-` for stdin), so CMD.exe does not need to inline multiline or quote-heavy content.

Writes use atomic replacement and a cross-platform atomic-directory lock with stale-lock recovery. Event history is capped because this state is resumable operational context, not permanent repository documentation.

## Workflow shape

```json
{
  "schema_version": 1,
  "workflow_id": "20260805T120000Z-0123456789-abcd",
  "task": "Original task text",
  "task_digest": "sha256(repo + task)",
  "repo": "/absolute/repository/path",
  "status": "planning|running|blocked|completed|aborted",
  "phase": "current mutable phase",
  "graph_revision": 0,
  "created_at": "UTC timestamp",
  "updated_at": "UTC timestamp",
  "retention_days": 5,
  "nodes": {},
  "blockers": [],
  "requirements": [],
  "decisions": [],
  "events": [],
  "git": {
    "initial": {},
    "latest": {},
    "final_commit": null
  },
  "completion": null
}
```

Completed workflows are immutable. Aborted workflows remain resumable until retention cleanup.

## Node shape

```json
{
  "node_id": "implementation_parser",
  "stage": "implementation",
  "title": "Implement parser boundary",
  "agent_type": "implementer",
  "priority": 80,
  "model": "gpt-5.6-terra",
  "reasoning_effort": "high",
  "routing_rationale": "Bounded but coupled implementation",
  "estimated_cost_usd": 0.12,
  "route_history": [],
  "actual_cost_usd": null,
  "attempt_costs": [],
  "dependencies": ["architecture_contract"],
  "write_scope": ["src/parser.py", "tests/test_parser.py"],
  "acceptance_criteria": ["Focused tests pass"],
  "output_contract": ["Return changed files, behavior summary, assumptions, and evidence"],
  "validation_commands": ["python -m unittest tests.test_parser"],
  "artifacts": [],
  "validation_evidence": [],
  "status": "pending|ready|running|blocked|failed|done|skipped|interrupted",
  "attempt": 0,
  "revision": 0,
  "supersedes": [],
  "superseded_by": null,
  "agent_id": null,
  "task_name": null,
  "agent_name": null,
  "agent_nickname": null,
  "review_round": null,
  "finding_ids": [],
  "summary": null,
  "error": null,
  "created_at": "UTC timestamp",
  "started_at": null,
  "completed_at": null
}
```

A route-history entry is added immediately before each attempt. Returned backend identifiers are stored verbatim; no one identifier is assumed universal.

## Requirement shape

```json
{
  "requirement_id": "req-portability",
  "text": "The project installer works on Unix and Windows",
  "source": "user request",
  "status": "active|satisfied|superseded",
  "confidence": "high|medium|low",
  "evidence": ["POSIX and Windows contract tests passed"],
  "created_at": "UTC timestamp",
  "updated_at": "UTC timestamp"
}
```

Resolved requirements (`satisfied` or `superseded`) require concrete evidence. The ledger is persisted in status/context output and cannot remain empty or active at completion.

## Graph invariants

- Every dependency names an existing node.
- No node depends on itself.
- The graph is acyclic.
- Priority is an integer from 0 through 100; higher ready priority dispatches first.
- Every node has nonempty write scope, acceptance criteria, output contract, and validation commands. Read-only scope is represented by `none`.
- A `done` node has a nonblank summary and at least one concrete validation-evidence entry. A `skipped` node has a nonblank explanation.
- Completed/skipped nodes cannot be patched, rewired, removed, or superseded as mutable work.
- A running node must be interrupted before graph mutation.
- Removal is allowed only for never-started pending/ready nodes without dependents or active blockers.
- Supersession marks old future work skipped, records the replacement, and rewires dependents atomically.
- Every successful graph mutation increments `graph_revision`, increments affected node revisions, and appends an event.
- A failed batch replan leaves the original graph unchanged.

`graph-validate` reports invalid node contracts, missing or duplicate dependencies, cycles, path-overlap collisions between nonterminal nodes that could run concurrently, current graph revision, and ready nodes. Scope separators are normalized on every platform; scope comparison is case-insensitive on Windows.

## Live graph operations

| Command | Purpose |
|---|---|
| `node-add` | Add newly discovered bounded work. |
| `node-patch` | Change future title, stage, agent type, priority, write scope, acceptance criteria, output contract, validation commands, or retry state. |
| `dependency-add` / `dependency-remove` | Rewire one dependency with atomic cycle validation. |
| `node-supersede` | Replace stale future work and automatically rewire dependents. |
| `node-remove` | Remove unnecessary never-started leaf work. |
| `graph-replan` | Apply a coupled set of patch/edge/supersede/remove operations atomically. |
| `graph-validate` | Read-only structural and readiness diagnostics. |

New replacement nodes are added before a batch that references them. Corrective work can depend on immutable completed evidence without altering that evidence.

For a replacement with the same write scope, add the replacement with a temporary dependency on the old future node, then supersede the old node; supersession removes that temporary edge. For a split, add disjoint children temporarily behind the broad node, add a no-write join depending on the children, then atomically remove the temporary child edges and supersede the broad node with the join.

## Lifecycle and readiness

A pending/failed/interrupted node becomes ready when every dependency is `done` or `skipped`, no active blocker targets it, the workflow is not aborted/completed, and no workflow-wide blocker is active. Ready output is priority-ordered.

Running attempts require a stable returned agent identifier. Terminal success requires a summary plus validation evidence; skip requires a summary. Failure/interruption requires an error. Retries require a fresh route and attempt increment. A route is fresh for at most 30 minutes and must match the next attempt number.

## Blockers and confidence

A blocker records scope (`node` or `workflow`), confidence, recommendation, examples, consequences, status, timestamps, and resolution. Medium/low-confidence ambiguity blocks only the affected node whenever independent work can continue. A workflow blocker is valid only when no useful branch can advance.

No blocker may be created over a live child: interrupt and persist the node first. Resolving a blocker restores a retryable node or the workflow's prior planning/running status.

## Abort, resume, and reconciliation

Abort records the prior workflow status, marks live attempts interrupted, and exposes no ready nodes. Resume restores planning/running state. The coordinator then reconciles persisted identifiers with the live backend, inspects repository changes for useful partial output, updates Git state, and reroutes retries.

## Completion gate

`finish` refuses completion unless:

1. the DAG is valid and nonempty;
2. every node is terminal;
3. no active blocker remains;
4. the requirement ledger is nonempty, every entry is resolved, and every resolved entry has evidence;
5. every done node retains concrete validation evidence and every terminal node has an explanation;
6. final workflow validation evidence is nonblank;
7. the supplied commit equals current repository HEAD;
8. final HEAD differs from the workflow-start HEAD when one existed;
9. no newly introduced task changes remain uncommitted.

The final task commit is distinct from any earlier Coordinator bootstrap commit.
