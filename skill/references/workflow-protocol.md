# Workflow protocol

The parent session is the only controller. It owns repository reconciliation, requirements, the
dependency graph, write scopes, routing, execution claims, integration, and completion. Each
specialist receives one bounded node and returns evidence; specialists never mutate the graph.

1. Inspect repository instructions and open private workflow state outside the repository.
2. Record requirements and build the smallest useful DAG.
3. Select a role for every node. Rank only model/effort candidates advertised by the current runtime;
   inherit the parent route when none are available.
4. Read the selected role TOML from the skill and include its description and instructions in the task
   packet.
5. Commit a claim, then use a callable delegation tool or bind and run the same packet inline. An
   ambiguous delegation must be reconciled before inline fallback or retry.
6. Inspect artifacts and acceptance evidence. Replan only future unclaimed work and serialize
   overlapping write scopes.
7. Resolve blockers and requirements, validate the integrated result, finish at a verified commit, and
   close the private session.

Use file-backed inputs for multiline or shell-sensitive content. Mutation IDs are operation identities,
not labels to reuse. A revision conflict requires a fresh read and decision; it is never solved by
overwriting newer state.
