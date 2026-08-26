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
  and zero or more repository-relative write scopes. No scopes explicitly means evidence-only work;
  it is valid only with a zero change-surface score. Artifact scopes require a positive change-surface
  score and participate in overlap checks using normalized platform-safe paths and the repository's
  detected case behavior. Completion proof is anchored to the original repository filesystem object.
- **Work is bounded before launch.** A versioned complexity and ambiguity assessment forces future
  work through refinement or coverage-checked decomposition without exhausting split depth or state
  capacity before it can be routed or claimed.
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
the integrated behavior, and finish only with materialized attempt evidence.
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

The installed `SKILL.md` is the controller protocol. In compact form, future work moves through
**assess → refine or split → route → claim → execute → validate → reassess** before the
workflow can finish:

1. The controller scores five 0–4 complexity dimensions and five 0–4 ambiguity factors. Inclusive
   thresholds (a default total-complexity threshold of 6) force it to refine or coverage-split every
   non-blocked assessable leaf until all are current and executable. Node-scoped blocked leaves remain
   diagnosed without fencing independent dispatch.
2. Node addition, refinement, and splitting leave routing provisional. Only after the latest assessment
   does the controller persist an explicit route, read the role profile, and delegate or execute inline.
   Active work is never rewritten.
3. It fills genuine available capacity with dependency- and write-scope-safe leaves, ordered by
   remaining-work critical-path load. Terminal-success bridges no longer serialize runnable downstream
   work; repairable failures retain their load. The controller checks delegation capability before
   selecting claims; without it, runtime capacity is one and inline work is claimed sequentially.
4. It inspects artifacts and validation evidence, reconciles uncertain delegation before retrying, and
   reassesses work staled by effective dependency outputs, terminal disposition, results, or evidence.
5. Workflow completion requires resolved requirements and blockers, runtime-done nodes plus only
   skipped decomposed parents or skipped superseded leaves. Every declared artifact scope must remain
   materialized and contain an attempt-scoped fingerprint change; evidence-only nodes declare no scopes.
   Scope presence and the change-surface score must agree, so artifact work cannot opt out of evidence.
   Coordinator does not invoke or inspect a version-control system.

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
`data/project/` (the backend itself is `data/project/backend/`), while Codex
sessions, Coordinator's complete workflow graph, and SQLite data use sibling subdirectories. The
demo output is intentionally left for manual evaluation and is not part of the repository's automated
verification or Lean gate. The generator requires a fresh `data/project/`; clear `data/` before a new
run. A person may manually start the generated backend:

```sh
docker compose up backend
```

The API is then available at `http://localhost:3000`; its SQLite database persists under `data/`.

## Current-user footprint

| Location | Contents |
|---|---|
| `~/.agents/skills/coordinator` | Skill, role profiles, references, adapters, and Python runtime |
| `~/.agent-coordinator` | Private sessions, locks, recovery data, and workflow state |

No persistent Coordinator-owned file or state is placed in repositories it orchestrates. Initialization
creates and removes one private-name file solely to probe the filesystem's case behavior.

## Project

Agent Coordinator is MIT licensed. Contributions and focused reports are welcome:

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [License](LICENSE)
- [GitHub repository](https://github.com/alanhoff/agent-coordinator)
- [Public issues](https://github.com/alanhoff/agent-coordinator/issues)
