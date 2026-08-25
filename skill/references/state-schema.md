# Workflow state and recovery

Coordinator stores one bounded schema-v3 JSON document per workflow under
`~/.agent-coordinator/workflows`. The state owner validates the complete document on every read and
before every commit. It rejects unknown fields, unsafe identifiers and write paths, missing or cyclic
dependencies, concurrent scope collisions, invalid transitions, inconsistent execution state, and
capacity violations.

Each node stores one specialist role. Model and effort are bounded strings supplied by the active
runtime, or `null` to inherit the parent route; Coordinator has no built-in model catalog.

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
