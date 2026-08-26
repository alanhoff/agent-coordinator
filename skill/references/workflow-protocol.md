# Workflow protocol

The parent session is the only controller. It owns repository reconciliation, requirements, the
dependency graph, assessment, refinement and splitting, write scopes, routing, execution claims,
integration, and completion. Each specialist receives one bounded executable node and returns evidence;
specialists never mutate the graph.

## Control loop

1. Inspect repository instructions, require a readable target directory, and open private workflow
   state outside the repository.
2. Record requirements and build the smallest useful DAG. Give every node a complete specification,
   acceptance criteria, and rubric-v2 complexity dimensions plus objective, input, boundary, dependency,
   and acceptance ambiguity factors. Declare artifact write scopes, or an empty scope list for
   evidence-only work. Score `change_surface` as 0 exactly when the scope list is empty; the state
   owner rejects either mismatch. Scope names use NFC-normalized platform-safe segments so filesystem
   aliases cannot claim independent ownership of one artifact.
3. Read planning diagnostics. For every non-blocked assessable leaf, reassess `stale` work, use
   `node-refine` for unresolved or changed specification, and use `node-split` for over-budget work.
   Re-read after each revisioned mutation until all such leaves are current and `executable`. Blocked
   leaves stay diagnosed without fencing independent dispatch; a workflow-level blocker still stops it.
4. Reject capacity-stranding plans: every assessable leaf whose current recorded total or dimension
   scores reach an inclusive split threshold counts even when `stale` or `refinement_required`. It cannot be at maximum
   depth, and two unused node records remain reserved for it.
5. Treat routes created by add, refine, or split as provisional. After the latest assessment and global
   fixed point, derive the existing router request, rank only runtime-advertised candidates, and persist
   an explicit `node-route`; inherit the parent route when no candidate is available.
6. Inspect the tool surface before selecting claims. With callable delegation, select a maximal
   genuinely runnable subset of the ordered frontier: remaining capacity first, then dependency and
   write-scope safety. Without callable delegation, runtime capacity is one: select and claim one node,
   take that inline attempt terminal, and only then claim the next. Never preclaim an inline backlog.
   Order candidates by descending critical-path load, priority, and node ID. Live dependency ordering
   and remaining-work load both stop through terminal-success bridges because downstream is
   concurrently runnable. Recompute after each claim rather than assuming the original frontier remains
   safe.
7. Read each selected role TOML. Include native specification, effective requirements/outputs/
   acceptance, and lineage provenance in its task packet, then claim and delegate or execute inline.
   Inline execution consumes the parent and is sequential.
8. Reconcile an ambiguous delegation before inline fallback or retry. Never duplicate uncertain work.
9. Persist terminal results and evidence, then reassess affected work. Dependency effective-output
   changes, normalized terminal disposition, result/evidence, or retry can stale direct assessable
   dependents; nonterminal status transitions cannot.
10. Repeat refinement and execution until no runnable work remains. Resolve blockers and requirements,
   validate the integrated result, confirm every declared artifact scope has attempt-scoped change
   evidence anchored to the original repository filesystem object and remains materialized, finish
   the workflow, and close the private session. Evidence-only
   nodes use persisted result/evidence and empty scope maps.

## Mutation boundaries and recovery

Active work is immutable to graph planning. Never refine, split, rewire, or replace a node whose launch
is `claimed`, `reconcile_required`, `bound`, or `running`. Let it reach a terminal outcome first. A
`failed` leaf with an `unclaimed` or `terminal` launch may then be refined or split, but its completed
attempt record remains part of history.

Likewise, do not rewrite a linked requirement's `text` or `source` while a resolution endpoint is
active or done. Reconcile an uncertain launch back to rewritable work before changing requirement
semantics. Requirement status and evidence may still resolve the workflow-level requirement gate.

When reconciliation returns uncertain active work to `unclaimed`, re-derive its assessment; changed
inputs make it `stale`, requiring refinement and a fresh explicit route before retry.

Refinement atomically replaces one eligible leaf's native specification, current scopes, and assessment
inputs while first preserving its full prior effective specification and scope provenance as carried
lineage obligations. Splitting atomically replaces one leaf with children, explicitly covers every
native and carried requirement/output/acceptance obligation, carries objectives, inputs, constraints,
and non-goals to every child, preserves artifact scope provenance, and validates the final DAG. It maps,
rewires, and stales every
current rewritable assessable direct dependent. It omits direct terminal-success dependents, atomically
prunes their obsolete parent edge, and never maps them to children. A child must retain each original
prerequisite directly or through only other new children; retained terminal intermediaries do not
witness it. Recursive splitting repeats effective coverage. Supersede copies every missing source
effective obligation (native plus carried) into the replacement's carried obligations, preserves every
source prerequisite directly or transitively, and stales the replacement.
Supersede chains are always acyclic. Outside
aborted recovery, they and each carried item's combined coverage/supersede path must resolve acyclically
to live, active, repairable, or done work; dead ends and cycle-only resolution reject. Direct skip/cancel
is reserved for atomic decomposition, supersede, or abort, and replanning has no obligation-dropping
remove operation. Partial application is never visible.

A stale node is not executable even when its previous score was below every threshold. Reassessment
must incorporate the evidence that changed its digest; copying the prior rationale is not a
recalculation. Repeat until the assessment states stabilize, because one split or new piece of evidence
can make additional assessable work stale or reveal a new boundary.

Use file-backed inputs for multiline or shell-sensitive content. Mutation IDs are operation identities,
not labels to reuse. Receipts and up to 32 attempts per node remain in the atomic workflow snapshot;
explicit capacity exhaustion requires operator action instead of a second persistence layer. A revision
conflict requires a fresh read and decision; it is never solved by overwriting newer state. Controller
takeover converts every claimed, bound, or running launch to `reconcile_required`; resume does not waive
provider reconciliation. Only an unclaimed future node may be blocked.
