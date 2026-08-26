# Deep analysis of the coordinator demo

## Scope and evidence boundary

This analysis keeps three evidence classes separate:

1. The original demo failure is the user's observation: the run appeared to deliver a Markdown file
   instead of the requested Node.js backend.
2. Coordinator behavior is assessed from production state transitions, persisted workflow metadata,
   graph diagnostics, and coordinator-only regression tests.
3. The generated demo project is manual-evaluation-only. Repository checks and the Lean verifier do
   not evaluate `prompt.md`, inspect generated files under `data/`, or decide whether the demo
   application satisfies its product request.

That boundary is deliberate. A general coordinator can prove that planned artifact scopes changed,
that dependencies and decomposition obligations were preserved, and that acceptance evidence was
recorded. It cannot prove arbitrary source code implements an English request without a task-specific
semantic oracle. Adding a hidden to-do-app oracle would test this demo rather than the coordinator.

The repair is greenfield schema v6. It contains no migration or compatibility path for older state.
The latest fresh Docker run exited successfully and persisted workflow
`wf-6cb0953830b15d537699` as completed at revision 59. All observations about that run below come from
its coordinator metadata, not its generated project.

## Executive diagnosis

| Reported symptom | Root cause | Repair at the invariant owner |
|---|---|---|
| A Markdown-only result could be declared complete | Completion trusted prose and had no attempt-scoped proof that declared artifacts were created or changed | Fingerprint every declared scope at claim, require a materialized and changed scope at `done`, persist before/after evidence, and reserve an empty scope list for explicit evidence-only work |
| High-complexity nodes stayed executable | Threshold comparisons were exclusive, the aggregate default was too permissive, and forward progress was not fenced on a whole-graph planning fixed point | Make total and dimension boundaries inclusive, use a default total threshold of 6, block route/readiness/claim until every assessable leaf is executable, and require recursive coverage-preserving splits |
| Ambiguity looked Boolean | One scalar hid the source of uncertainty, while the old executable range naturally displayed only 0 or 1 | Record five independently scored 0–4 factors, derive total and peak, expose the complete factor map, and force refinement on either a material factor or aggregate uncertainty |

The symptoms shared one architectural flaw: the persisted model was weaker than the promise made by
the controller. The repair therefore strengthens state invariants rather than adding demo-specific
prompting or generated-output tests.

## 1. Why prose could substitute for a backend

### Failure mechanism

The old node-completion boundary accepted nonblank `result` and `evidence` strings. It did not prove
that a declared output existed, and it did not tie any filesystem mutation to the attempt that claimed
the node. Workflow completion consequently had no generic way to distinguish implementation evidence
from a convincing narrative.

The previous design also coupled completion to version-control-shaped data. That did not solve the
semantic problem, complicated fresh-directory operation, and made runtime correctness depend on an
unrelated external subsystem. The final runtime removes that coupling entirely.

### Implemented invariant

Artifact evidence is now filesystem-native and attempt-scoped:

- A node declares repository-relative `write_scopes`. An empty list explicitly means evidence-only
  work and requires `change_surface=0`. Any positive change-surface score requires at least one scope,
  and any declared scope requires a positive score. This closes the prose-only opt-out at the state
  boundary instead of relying on controller discipline.
- Claiming a launch captures a SHA-256 fingerprint or `null` baseline for every current scope.
- A `done` transition requires every declared scope to be a materialized regular file or directory.
  Its current fingerprint must differ from that attempt's baseline.
- The attempt stores exact `before` and `after` fingerprints for every scope. Evidence-only attempts
  store empty baseline and evidence maps.
- `finish` rechecks that every done artifact scope remains materialized and that all requirements,
  blockers, and visible nodes resolve through actual done work, decomposition, or supersede.
- An explicitly deleted path cannot serve as its own durable completion scope. Deletion work declares
  a containing directory that remains materialized or is modeled as evidence-only.

Directory fingerprints are deterministic over relative names, entry kinds, modes, file contents, and
link targets without following links. Length-framed fields, fixed-size content digests, and native
filesystem-name bytes make the tree encoding unambiguous even for non-UTF-8 POSIX filenames. Unicode
scope names are NFC-normalized, and names with Win32 alias behavior are rejected on that platform.
Repository identity binds the canonical path to its stable filesystem object. Each snapshot verifies that identity; on
POSIX it traverses from a no-follow root descriptor, so renaming and replacing the repository root
cannot forge evidence. Declared paths and their ancestors are checked for containment;
unrelated pre-existing entries below a broad directory are not recursively forbidden. Scope ownership
uses case behavior probed from the target filesystem, with a conservative case-insensitive result when
the probe cannot run.

Production runtime state contains no version-control object, repository revision, branch, or status
snapshot. The CLI has no completion checkpoint argument and uses the generic
`reconcile-mutation` command for uncertain persistence. The Docker generator starts from a genuinely
fresh workspace and performs no repository initialization or status inspection.

### What this proves—and what it does not

The invariant proves that an artifact-producing attempt changed every scope it claimed and left a
material artifact. It does not prove that the bytes implement the intended product. A dishonest or
mistaken controller could still define the wrong graph or scopes; preventing that would require
understanding arbitrary user intent. The controller protocol mitigates this by deriving nodes from
requirements and preserving their obligations, while final product judgment remains manual for this
demo.

The latest run's metadata records six artifact nodes, each with one declared scope and distinct
claim-time/final fingerprints. Its seventh node, `validation`, has no scopes and empty scope maps. This
is coordinator evidence only; it is not an automated judgment of the generated backend.

## 2. Why high-complexity work did not split

### Failure mechanism

The previous policy treated advertised maxima as exclusive comparisons:

```text
total > threshold
dimension > threshold
```

A score exactly at the boundary therefore remained executable. The prior aggregate default also
allowed totals that still represented several coupled behaviors. Even after fixing the comparison,
keeping that permissive default would have left C6 and C7 nodes too broad for the requested small-step
graph.

### Implemented fixed-point policy

Rubric v2 sums five 0–4 dimensions: breadth, change surface, coupling, novelty, and verification. A leaf
is `split_required` when:

```text
complexity_total >= node_complexity_split_threshold
or any dimension >= dimension_complexity_split_threshold
```

The greenfield defaults are total 6 and per-dimension 3. The consequences are enforced:

- Route, readiness, and claim are unavailable until every nonblocked assessable leaf has a current
  `executable` assessment. Fixing only the next candidate cannot bypass another broad leaf.
- A current `split_required` node cannot be rescored downward through `node-refine`; it must use
  `node-split`.
- A stale formerly broad node may be honestly recalculated below policy when changed inputs genuinely
  reduce its work. The gate distinguishes reassessment from evasion.
- A split requires at least two children, and every child must have lower total complexity than its
  parent. Children are assessed immediately, so an at-threshold child recursively blocks progress.
- Coverage maps preserve every effective requirement, output, and acceptance item. Objectives, inputs,
  constraints, non-goals, and artifact provenance are carried as lineage obligations. Prerequisites
  are preserved separately as direct or transitive dependency paths in the rewritten DAG.
- Artifact-scoped work cannot be transformed into an all-evidence-only decomposition or supersede
  chain. Full effective obligations—not only the narrow coverage fields—must reach resolvable work.
- Rewritable dependents are explicitly rewired and made stale. Active work cannot be split or refined.
- Maximum depth and state-capacity reserves reject graphs that could create an over-budget leaf without
  room for its required children.

The state layer does not invent semantic child tasks. Only the controller can decide how to divide an
objective without losing its meaning. “Automatic splitting” means the state machine makes further
execution impossible until the controller supplies a valid semantic split, then validates the entire
replacement atomically.

### Direct decomposition evidence

The archived forced-decomposition run `wf-df23a76170ded8e9b272` deliberately entered one C13 backend
root. Its evolution was:

| Revision | State |
|---:|---|
| r3 | C13 root is `split_required`; route and dispatch are empty |
| r4 | Root splits into five children; the HTTP child is C6 and still blocks the fixed point |
| r5 | C6 HTTP child splits again into routing and startup; all six final leaves are C5 |
| r44 | Six bounded leaves are done; both historical parents remain visibly decomposed |

This is the direct proof that reaching either boundary forces recursive decomposition. Historical
parent scores remain recorded rather than being rewritten to make the final graph look artificially
small.

The latest fresh run is the complementary valid outcome. Its controller created seven already-bounded
C5 nodes, with every dimension below 3. Zero `node_split` events is therefore correct: auto-splitting is
conditional, not compulsory graph inflation.

## 3. Why ambiguity looked like 0 or 1

### Failure mechanism

The old model stored one 0–4 scalar, but allowed execution only at 0 or 1. Final executable nodes
naturally displayed those two values, and the scalar could not explain whether uncertainty came from
the objective, inputs, boundaries, dependencies, or acceptance.

### Implemented ambiguity model

Rubric v2 stores five independent factors, each 0–4:

- `objective`
- `inputs`
- `boundaries`
- `dependencies`
- `acceptance`

The state layer derives `ambiguity_total` and `ambiguity_peak`; callers cannot forge them. Refinement is
required when any factor reaches 2, the total reaches 4, or open questions remain. Scores and questions
must agree: a material factor requires a concrete open question, and an open question requires at least
one material factor. A value of 1 means a bounded assumption that cannot change execution or
acceptance—not “true” in a Boolean field.

This produces useful states that the earlier display could not represent:

- Several factor-1 assumptions can yield executable totals 2 or 3.
- One factor at 2 or 3 identifies the category that blocks execution.
- Four low assumptions yield total 4 and force refinement even though no single factor is material.
- Diagnostics expose the complete factor map, total, and peak for every assessable leaf.

The latest run's final factor maximum is `0, 0, 0, 1, 0`; the server has one bounded dependency
assumption and ambiguity total 1, while the other final leaves have total 0. Low final ambiguity is the
desired result of refinement and a precise task, not evidence that the input scale is Boolean.
Coordinator regressions exercise factor values 2 and 3, executable totals 2 and 3, and aggregate
refinement at total 4. None of those tests reads or evaluates the generated demo.

## 4. Runtime adaptation and parallelism

An assessment digest covers the node specification, effective lineage obligations, referenced
requirement semantics, write scopes, policy, and dependency outputs/disposition/result/evidence. When
one of those inputs changes, future dependent work becomes `stale` and must be recalculated before it
can route or launch.

The execution loop is:

```text
assess -> refine ambiguity -> split complexity -> global fixed point
       -> route -> atomic claim -> execute -> record scope/evidence
       -> stale affected dependents -> repeat
```

Planning diagnostics distinguish graph capacity from real executor capacity. They compute frontier
width, usable and occupied capacity, available parallelism, remaining critical-path load, and a
deterministic dispatch order. The controller inspects whether delegation is actually callable before
selecting claims. With delegation it fills independent capacity; without delegation it claims exactly
one inline node and takes that attempt terminal before claiming another.

The latest run used `max_parallel=4` with reserve 1, so usable capacity was 3. Metadata shows:

| Revisions | Evolution |
|---:|---|
| r7–r13 | Seven C5 nodes are added; database, manifest, and health form a width-3 frontier |
| r17–r25 | All three nodes are claimed, bound to distinct children, and running concurrently—the full usable capacity |
| r26–r31 | The first wave completes; create, read, and server are recalculated from new dependency evidence |
| r34–r39 | Create and read are claimed and run concurrently as a width-2 frontier |
| r40–r48 | Both handlers finish; server is recalculated, runs, finishes, and stales validation |
| r48–r53 | The evidence-only validation node is recalculated, routed, and completed |
| r54–r59 | Five requirements are resolved and workflow completion is persisted |

The run contains five `node_refined` events, seven routes, and seven attempts. It demonstrates live
adaptation and real parallel delegation while remaining below the complexity split boundary.

## 5. Recovery and mutation integrity

Adaptive execution still needs a bounded recovery boundary:

- Advisory locking and expected revisions serialize controllers.
- Mutation IDs and persisted receipts make retries idempotent; `reconcile-mutation` distinguishes an
  already-persisted mutation from one absent after an uncertain write outcome.
- Controller takeover advances the epoch, fences the old controller, and marks claimed, bound, or
  running launches `reconcile_required`. Provider reconciliation precedes retry.
- Claim is persisted before delegation. Pending or blocked nodes cannot retain active launches, and
  uncertain launches cannot be silently rebound to a different child.
- Attempt and receipt bounds fail explicitly instead of adding journals, archives, or a second state
  subsystem.
- Supersede chains remain acyclic, retain prerequisites, and transfer every missing effective
  obligation to the replacement.

These controls are independent of artifact storage and do not invoke a version-control system.

## 6. Docker-run evidence and manual boundary

Before the latest demo, the previous `data/` directory was moved intact to
`/tmp/agent-coordinator-pre-vcs-removal-demo-20260826-data`, and a new empty `data/` directory was
created. `docker compose run --rm coordinator` then exited 0 after 11 minutes 19 seconds.

Coordinator metadata records:

- schema version 6, revision 59, status `completed`;
- seven C5 nodes, all `done`;
- no split-required, refinement-required, stale, or active nodes at completion;
- three concurrent first-wave launches and two concurrent second-wave launches;
- six artifact attempts with complete before/after scope evidence;
- one evidence-only validation attempt with empty scope maps;
- five requirements recorded and later marked satisfied.

The metadata also contains controller-authored validation prose. This analysis treats it only as a
recorded assertion, not independent proof of application semantics. It does not inspect the generated
source, run its application, or add tests for it.

The graph visualization presents this latest run first and retains the archived C13→C5 run as the
direct demonstration of forced recursive splitting.

## 7. Implemented repair summary

| Area | Production change |
|---|---|
| Complexity | Five-dimension rubric, inclusive total/dimension boundaries, default total threshold 6, recursive lower-complexity splits, full-obligation provenance, fixed-point fence, depth/capacity reserves |
| Ambiguity | Five 0–4 factors, derived total/peak, factor and aggregate refinement gates, open-question consistency, categorized diagnostics |
| Completion | Scope/change-surface coupling, root-object-anchored filesystem baselines, platform-safe scope aliases, changed/materialized scope requirement at `done`, explicit evidence-only nodes, finish-time materialization check, exact completion dispositions |
| Runtime independence | No version-control subprocesses, state fields, completion arguments, VCS-specific reconciliation command, or Compose setup/status logic |
| Adaptation | Dependency-sensitive assessment digests, automatic staleness, atomic refinement/split/rewire operations, repeated planning fixed point |
| Parallelism | Critical-path ordering, real-capability-aware claim batches, atomic pending-to-claimed promotion, scope exclusion, full use of three-worker demo capacity |
| Recovery | Revision fencing, idempotent receipts, controller takeover, uncertain-launch reconciliation, bounded in-document history |
| Contracts | Schema v6, CLI, skill, reference documents, README, Compose, and coordinator-only tests updated together; no migration |

## Residual limits

- Complexity and ambiguity scores are structured expert judgments. The state layer validates their
  derivation and consequences, not the intellectual perfection of the inputs.
- Semantic decomposition remains a controller responsibility; automatic text slicing cannot reliably
  preserve arbitrary requirements.
- Filesystem fingerprints prove attempt-scoped mutation and durable presence, not product behavior.
- Available parallelism is an upper bound constrained by dependencies, scopes, and callable executors.
- The generated demo remains manual-evaluation-only by explicit request.

## Verification and review record

The production checks cover coordinator behavior only: Ruff, bytecode compilation, Docker Compose
configuration validation, scoped diff whitespace validation, and 67 coordinator tests. The suite
contains no generated-demo oracle and does not evaluate `prompt.md` or `data/project`.

A fresh independent adversarial review of the final uncommitted implementation reported
`No relevant issue remains.` It rechecked the root-object and scope-alias defenses, the 67-test result,
the scope/change-surface invariant, runtime contracts, and documentation. The final Lean outcome is
recorded only after the implementation and analysis are committed. Both review scopes explicitly
exclude the demo prompt, generated output, and any claim about generated application semantics.
