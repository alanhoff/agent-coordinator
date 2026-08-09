# Workflow state and recovery

Coordinator stores one bounded schema-v3 JSON document per workflow under `~/.agent-coordinator/workflows`. The state owner validates the complete document on every read and before every commit. It rejects unknown fields, unsafe identifiers and write paths, missing or cyclic dependencies, potentially concurrent scope collisions, invalid transitions, inconsistent launch/attempt/result state, and capacity violations.

Each mutation supplies a unique mutation ID and expected prior revision. A committed receipt makes retry reconciliation idempotent; reuse of an ID for different content and stale revisions are rejected. Atomic replacement and durability flushing ensure readers observe a complete old or new snapshot. `reconcile-commit` distinguishes a recorded mutation from one not present in the committed state.

One controller session owns an epoch. Session bearer values exist only in the caller-selected private file and private session registry, never in workflow state or command output. A takeover advances the epoch, fences the old controller, and requires explicit `resume`.

Launch states distinguish `unclaimed`, `claimed`, `reconcile_required`, `bound`, `running`, and `terminal`. Commit `claimed` with a request ID before an external call. An uncertain response becomes `reconcile_required`; return to `unclaimed` only with evidence that no child exists, or bind the child found at the provider edge.

Read-only list, status, context, and dashboard operations never create, lock, repair, normalize, cache, or clean state.
