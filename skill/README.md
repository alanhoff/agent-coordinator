# Coordinator operator entry points

Project README: [English](../README.md) · [Português (Brasil)](../README.pt-BR.md) · [Español](../README.es.md) · [简体中文](../README.zh-CN.md)

`SKILL.md` is the adaptive workflow protocol. Role profiles remain in `agents/roles/` and are passed
with delegated tasks or applied to inline execution. The installed commands are:

- `scripts/coordinator_state.py` for durable workflow and controller state. The normal path is
  `plan-apply` → `next` → `node-route-auto` → `node-claim` → `node-start` → `node-complete` →
  `workflow-complete`; low-level mutation commands remain available for reconciliation and recovery.
- `scripts/model_router.py choose` for direct evidence-based role and runtime-candidate selection when
  testing or integrating the selector independently.

Source and documentation: <https://github.com/alanhoff/agent-coordinator>
