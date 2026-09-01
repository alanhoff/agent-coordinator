# Coordinator operator entry points

`SKILL.md` is the adaptive workflow protocol. Role profiles remain in `agents/roles/` and are passed
with delegated tasks or applied to inline execution. The installed commands are:

## Portable installation

This directory is the complete Coordinator installation artifact. Copy it unchanged to
`~/.agents/skills/coordinator`. The command adapters load their bundled standard-library-only runtime
from `scripts/lib/coordinator`, so installation needs no repository checkout, assembly step, or
`PYTHONPATH` configuration.

## Commands

- `scripts/coordinator_state.py` for durable workflow and controller state. The normal path is
  `plan-apply` → `next` → `node-route-auto` → `node-claim` → `node-start` → `node-complete` →
  `workflow-complete`. Runtime adaptation adds `node-observe`, `graph-reconcile`,
  `graph-expand-auto`, `judge-gate-add`, and `judge-complete`; low-level mutation commands remain
  available for reconciliation and recovery.
- `scripts/model_router.py choose` for direct evidence-based role and runtime-candidate selection when
  testing or integrating the selector independently.

Project source and documentation: <https://github.com/alanhoff/agent-coordinator>

Runtime graph payloads, bounds, gate semantics, nested shapes, and logical feedback iterations are
documented in `references/dynamic-runtime-graphs.md`.
