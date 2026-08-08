# Read-only dashboard

`dashboard.py` selects all workflows, one canonical repository, or one workflow ID. Selection and reads never initialize or mutate Coordinator state.

- `watch --once` prints one committed summary without terminal-control effects.
- `serve` binds only `127.0.0.1`, uses an in-memory capability, rejects wrong authority/origin/method/capability, and exposes no mutating route.
- `render --out PATH` writes one self-contained report to the explicit safe target.

All modes use the same validated derivation for progress, ready order, graph layers and critical path, blockers, launch/recovery state, capacity, attempts, and known or unknown costs. Completed workflows are 100%; active empty workflows are 0%; superseded nodes do not distort active progress.

Stored text is untrusted. The interactive view uses text-only DOM construction, the report HTML-escapes values, neither view loads remote assets, and both use restrictive content security policy. Corrupt, changing, oversized, or invalid state is diagnosed rather than partly rendered.
