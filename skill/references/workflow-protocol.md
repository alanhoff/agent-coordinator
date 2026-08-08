# Workflow protocol

The parent session is the only controller. It owns repository reconciliation, requirements, the dependency graph, write scopes, routing, launch claims, integration, and final completion. Each specialist receives one bounded node and returns evidence; specialists do not mutate the workflow graph.

1. Run doctor preflight and inspect repository instructions.
2. Open a private controller session outside the repository and initialize the task.
3. Record explicit requirements and build the smallest useful DAG.
4. Route ready nodes from current evidence. Keep reserve capacity for correction and integration.
5. Persist each launch claim before calling the provider. Bind, run, and complete from observed outcomes.
6. Inspect actual artifacts and tests. Replan only future unclaimed work; serialize overlapping write ownership.
7. Reconcile uncertain provider or state commits before retrying.
8. Resolve blockers and requirements with evidence, validate the integrated result, then finish and close the session.

Use file-backed task and plan inputs for multiline or shell-sensitive content. Mutation IDs are operation identities, not labels to reuse. A revision conflict requires a fresh read and decision; it is never solved by overwriting newer state.
