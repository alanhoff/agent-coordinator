# Agent Coordinator

Agent Coordinator turns complex Codex work into a bounded workflow with durable state, explicit
ownership, evidence-based routing, and recovery. It delegates to specialist subagents when the active
runtime supports them and performs the same work inline when it does not.

## Install

Ask your agent exactly:

> `Install https://github.com/alanhoff/agent-coordinator by following INSTALL.md`

The agent-facing procedure in [INSTALL.md](INSTALL.md) places one self-contained Coordinator skill in
the current-user skill directory. It does not change Codex settings or register global agent profiles.

## Why Coordinator

- **Recovery is part of the protocol.** Atomic, revisioned snapshots record an execution claim before
  work starts, require reconciliation after uncertain delegation, and fence replaced controllers.
- **Ownership is explicit.** Every dependency node carries acceptance criteria, one specialist role,
  and a repository-relative write scope; overlapping work is serialized.
- **Routing follows current evidence.** The router ranks arbitrary model/effort candidates advertised
  by the runtime. Without a current catalog, execution inherits the parent model and effort.
- **Delegation is optional.** Role profiles ship inside the skill and travel in each delegated task.
  If no subagent tool is callable, the controller applies that profile and executes the node inline.
- **Your project stays clean.** Coordinator code and profiles live in its skill directory; workflow
  state lives in a private current-user state directory.

## Give Codex a controller brief

Each prompt explicitly invokes `$coordinator`. Coordinator derives the smallest useful dependency
graph and delegates only where bounded ownership helps.

```text
$coordinator Ship the saved-search feature. Inspect repository instructions, persist the acceptance
criteria, build the smallest dependency graph, parallelize only independent write scopes, validate
the integrated behavior, and finish only at a verified commit.
```

```text
$coordinator Reproduce and diagnose the intermittent checkout test before changing production. Keep
diagnosis, the smallest owner-layer fix, and independent validation as bounded dependent work.
```

```text
$coordinator Resume the interrupted schema migration workflow. Reconcile the repository and every
uncertain delegation before retrying, preserve completed evidence, and validate rollback and forward
migration behavior before finishing.
```

```text
$coordinator Compare the proposed event-processing architectures against current repository
boundaries and requirements. State tradeoffs and missing evidence, then recommend one without
implementing either option.
```

## Operate

The installed `SKILL.md` is the controller protocol. In compact form, each node moves through
**route → claim → execute → validate → finish**:

1. The controller creates dependency-safe work with a specialist role, acceptance criteria, and write
   scope. A model route is optional.
2. It reads the role profile from its own skill folder and either passes it in a subagent invocation or
   applies it to inline execution.
3. It inspects artifacts and validation evidence, reconciling an uncertain delegation before retrying.
4. Workflow completion requires resolved requirements and blockers, successful visible nodes, and a
   full verified Git checkpoint.

Read-only state inspection is available through the state adapter:

```sh
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py list --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py status --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py context --workflow-id WORKFLOW --json
```

On Windows, use `python` and paths under `%USERPROFILE%`.

## Docker demo

The demo uses OpenAI's official Codex universal image and the `OPENAI_API_KEY` already stored in the
ignored root `.env` file. Generate the backend with Coordinator:

```sh
docker compose run --rm coordinator
```

All mutable output stays under the ignored `data/` directory: the generated application is in
`data/backend/`, while Codex sessions, Coordinator's complete workflow graph, and SQLite data use
their own sibling subdirectories. Run the backend's tests or start the API:

```sh
docker compose run --rm backend -c 'cd /workspace/backend && npm test'
docker compose up backend
```

The API is then available at `http://localhost:3000`; its SQLite database persists under `data/`.

## Current-user footprint

| Location | Contents |
|---|---|
| `~/.agents/skills/coordinator` | Skill, role profiles, references, adapters, and Python runtime |
| `~/.agent-coordinator` | Private sessions, locks, recovery data, and workflow state |

Coordinator-owned files are never placed in repositories it orchestrates.

## Project

Agent Coordinator is MIT licensed. Contributions and focused reports are welcome:

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [License](LICENSE)
- [GitHub repository](https://github.com/alanhoff/agent-coordinator)
- [Public issues](https://github.com/alanhoff/agent-coordinator/issues)
