# Workflow protocol

The parent session is the only controller. It owns repository reconciliation, requirements, the
dependency graph, assessment, refinement and splitting, write scopes, routing, execution claims,
integration, and completion. Each specialist receives one bounded executable node and returns evidence;
specialists never mutate the graph.

## Control loop

1. Inspect repository instructions and open private workflow state outside the repository.
2. Record requirements and build the smallest useful DAG. Give every node a complete specification,
   acceptance criteria, scopes, and rubric-v1 complexity and ambiguity inputs.
3. Read planning diagnostics. For every non-blocked assessable leaf, reassess `stale` work, use
   `node-refine` for unresolved or changed specification, and use `node-split` for over-budget work.
   Re-read after each revisioned mutation until all such leaves are current and `executable`. Blocked
   leaves stay diagnosed without fencing independent dispatch; a workflow-level blocker still stops it.
4. Reject capacity-stranding plans: every assessable leaf whose current recorded total or dimension
   scores are over policy counts even when `stale` or `refinement_required`. It cannot be at maximum
   depth, and two unused node records remain reserved for it.
5. Treat routes created by add, refine, or split as provisional. After the latest assessment and global
   fixed point, derive the existing router request, rank only runtime-advertised candidates, and commit
   an explicit `node-route`; inherit the parent route when no candidate is available.
6. Select a maximal genuinely runnable subset of the ordered frontier: remaining capacity first,
   then dependency and write-scope safety. Order candidates by descending critical-path load, priority,
   and node ID. Live dependency ordering and remaining-work load both stop through terminal-success
   bridges because downstream is concurrently runnable. Recompute after each claim rather than assuming
   the original frontier remains safe.
7. Read each selected role TOML. Include native specification, effective requirements/outputs/
   acceptance, and lineage provenance in its task packet, then claim and delegate or execute inline.
   Inline execution consumes the parent and is sequential.
8. Reconcile an ambiguous delegation before inline fallback or retry. Never duplicate uncertain work.
9. Persist terminal results and evidence, then reassess affected work. Dependency effective-output
   changes, normalized terminal disposition, result/evidence, or retry can stale direct assessable
   dependents; nonterminal status transitions cannot.
10. Repeat refinement and execution until no runnable work remains. Resolve blockers and requirements,
   validate the integrated result, finish at a verified commit, and close the private session.

## Mutation boundaries and recovery

Active work is immutable to graph planning. Never refine, split, rewire, or replace a node whose launch
is `claimed`, `reconcile_required`, `bound`, or `running`. Let it reach a terminal outcome first. A
`failed` leaf with an `unclaimed` or `terminal` launch may then be refined or split, but its completed
attempt record remains part of history.

When reconciliation returns uncertain active work to `unclaimed`, re-derive its assessment; changed
inputs make it `stale`, requiring refinement and a fresh explicit route before retry.

Refinement atomically replaces one eligible leaf's native specification, acceptance, scopes, and
assessment inputs while preserving carried lineage obligations. Splitting atomically replaces one leaf
with children, covers every native and carried requirement/output/acceptance obligation, materializes
coverage as child lineage provenance, and validates the final DAG. It maps, rewires, and stales every
current rewritable assessable direct dependent. It omits direct terminal-success dependents, atomically
prunes their obsolete parent edge, and never maps them to children. A child must retain each original
prerequisite directly or through only other new children; retained terminal intermediaries do not
witness it. Recursive splitting repeats effective coverage. Supersede chains are always acyclic. Outside
aborted recovery, they and each carried item's combined coverage/supersede path must resolve acyclically
to live, active, repairable, or done work; dead ends and cycle-only resolution reject. Remove, skip, and
cancel cannot silently discard carried work. Partial application is never visible.

A stale node is not executable even when its previous score was below every threshold. Reassessment
must incorporate the evidence that changed its digest; copying the prior rationale is not a
recalculation. Repeat until the assessment states stabilize, because one split or new piece of evidence
can make additional assessable work stale or reveal a new boundary.

Use file-backed inputs for multiline or shell-sensitive content. Mutation IDs are operation identities,
not labels to reuse. A revision conflict requires a fresh read and decision; it is never solved by
overwriting newer state.
