# Dynamic workflow protocol

The parent Coordinator is the only global planner, graph owner, requirement owner, integrator, and final committer. Children receive bounded packets and return evidence; they do not independently redefine the whole workflow.

## Gate zero: project bootstrap

Before task research, repository reconnaissance, state creation, or child dispatch:

1. resolve the platform and project root;
2. run `scripts/install.py ensure` from the active complete package or remote package base;
3. stop after every install/update commit and require a full Codex restart;
4. on the restarted session, pass the exact `SessionStart` activation token;
5. run `doctor.py --require-activation`;
6. inspect the live spawn interface for explicit child model and reasoning controls.

Any missing dependency, failed commit, untrusted/not-loaded hook, stale activation, parent model/effort mismatch, or missing spawn override blocks execution. Do not implement part of the requested task before this gate passes.

Use platform-native invocation. On Windows, use `python`, CMD.exe-compatible arguments, and file-backed input for long or shell-sensitive values: `init --task-file <temp-path>` and `graph-replan --plan-file <temp-path>`. On Unix use `python3`; never require Bash-specific syntax. Input/staging files remain below the operating-system temporary directory.

## Requirement ledger

Maintain a current ledger of explicit requirements, repository constraints, accepted decisions, success evidence, non-goals, and unresolved questions. Persist entries with `requirement-set`; unresolved entries remain `active`, while `satisfied` and `superseded` entries require concrete evidence. Update the ledger after every material observation. Solve high-confidence ambiguity autonomously. Exhaust evidence before creating a medium/low-confidence blocker, make it node-scoped when possible, and provide a recommendation, examples, and consequences. Completion is forbidden with an empty ledger, active requirements, or evidence-free resolved entries.

## Mutable DAG, not a waterfall

Create only the smallest useful initial graph. Represent research, reconnaissance, design, architecture, documentation, implementation, validation/review, integration, and commit with concrete nodes or recorded high-confidence skip decisions.

Repeat continuously:

```text
observe state, agents, repository, sources, tests, costs, and requirements
→ diagnose changed assumptions, critical path, and risk
→ validate graph
→ mutate graph/routing/concurrency where expected value improves
→ dispatch ready non-conflicting nodes
→ monitor and integrate bounded outputs as soon as useful
→ validate and independently review
→ observe again
```

A control-loop pass is mandatory after every material result, failure, interruption, source, blocker, requirement change, test outcome, accepted finding, unexpected diff, dependency discovery, or cost/latency shift, and before refilling concurrency.

The parent autonomously chooses and continuously updates graph shape, route, priority, concurrency, monitoring cadence, correction strategy, and integration order. An initial DAG is only the current hypothesis. Never wait for a user prompt to replan when observed evidence makes a better workflow apparent.

The parent may autonomously:

- add newly discovered nodes;
- split broad work into parallel bounded units;
- supersede stale future work and rewire dependents;
- add/remove edges;
- reprioritize ready work;
- change future scope, acceptance criteria, role, model, or effort;
- remove never-started unnecessary work;
- interrupt/requeue stale work;
- increase or decrease concurrency;
- introduce root-cause/corrective paths after validation.

Completed evidence remains immutable. Running nodes are interrupted before mutation. Every mutation is atomic, locked, cycle-checked, revisioned, and event-recorded. Write-scope collision analysis normalizes separators and treats path case as insignificant on Windows.

For a same-scope replacement, add the replacement temporarily depending on the stale node, then supersede the stale node; supersession removes the temporary edge and rewires other dependents. To split broad future work, add disjoint children temporarily depending on the broad node, add a no-write join depending on those children, then atomically remove the temporary child edges and supersede the broad node with the join. Validate after every mutation.

## Node packet

Each child packet contains:

| Field | Contract |
|---|---|
| Stable node/task name | Lowercase identifier usable for reconciliation. |
| Goal and why now | One bounded outcome and dependency impact. |
| Repository and relevant paths | Absolute root plus focused context. |
| Accepted requirements/decisions | Current ledger subset. |
| Evidence | Sources, parent findings, completed dependency outputs. |
| Read/write scope | Exact ownership; use `none` for read-only work. |
| Constraints/non-goals | Prevent accidental scope expansion. |
| Acceptance criteria | Observable completion proof. |
| Validation | Commands/evidence expected from child and parent. |
| Return contract | Changed files/findings, validation, assumptions, confidence. |
| Stop conditions | Missing resource or medium/low-confidence decision. |

Use minimal inherited history and prefer a self-contained packet. Every node persists a nonempty output contract and validation command before dispatch. Record a fresh model/effort route immediately before each spawn and persist every returned identifier immediately afterward. Routes older than 30 minutes, routes for another attempt, and routes invalidated by new evidence must be recomputed.

## Parallelism and monitoring

Keep no more than eight delegated workers open, and fewer when write conflicts, uncertainty, integration capacity, or review independence reduce expected value. Do not wait blindly for a cohort. Collect useful outputs early, inspect repository writes, interrupt obsolete/conflicting work, send focused corrections when cheaper than replacement, and run another control-loop pass before dispatching more.

## Model and effort routing

Use `model_router.py` for every attempt. Score current complexity, ambiguity, criticality, coupling, novelty, determinism, token/cache estimates, and budget mode. Choose the least expensive viable route and record why it beats the nearest cheaper option. Reroute all retries from current evidence.

- Luna: deterministic, narrow, high-volume, mechanical work.
- Terra: substantial default research, design, coding, documentation, diagnosis, and review.
- Sol: novel, ambiguous, cross-cutting, repeatedly failed, or quality-critical work.

Model escalation addresses capability failure, not bad packets, missing evidence, oversized ownership, or conflicting architecture.

## Stage gates and autofix

For design, architecture, documentation, implementation, and final integration:

```text
produce/fix → integrate → independent review → evidence-based triage
          ↑                                      ↓
validate accepted fixes ← fix every accepted finding
          └──────────── fresh review round ──────┘
```

A reviewer does not silently author its own fixes. A fresh round starts only after accepted findings are fixed and validated. A node reaches `done` only with a nonblank summary and concrete validation evidence. Repeated defects trigger root-cause graph changes rather than blind retries.

## Status, abort, resume

Status is read-only and combines persisted state, live agents, graph revision/readiness, current routes/costs, findings/fixes, validation, Git status, and blockers.

Abort interrupts live children first, persists interruption, and leaves exact-task state resumable. Resume reconciles live identifiers, repository partial output, Git state, and stale running nodes before dispatching anything new.

## Final integration

The parent inspects all outputs and the entire diff, reconciles contracts, removes out-of-scope work, resolves the evidence-backed requirement ledger, runs focused and full validation, performs at least two independent final review/fix rounds, validates the final graph, commits task changes, and proves the recorded commit equals current HEAD with no newly introduced uncommitted task entries.
