# Agent Coordinator

Agent Coordinator turns complex Codex work into a bounded multi-agent workflow with durable state,
explicit ownership, evidence-based routing, and verified recovery. Coordinator keeps its state outside
the repository being orchestrated.

## Install

Ask your agent exactly:

> `Install https://github.com/alanhoff/agent-coordinator by following INSTALL.md`

The agent-facing procedure is documented in [INSTALL.md](INSTALL.md). It verifies Python 3.11 or newer,
places the skill and specialist roles in current-user locations, preserves unrelated configuration, and
checks the installed entry points before reporting success.

## Why Coordinator

- **Recovery is part of the protocol.** Atomic, revisioned snapshots record launch claims before
  external calls, require reconciliation after uncertain responses, and fence replaced controllers.
- **Ownership is explicit.** Every dependency node carries acceptance criteria, one role, and a
  repository-relative write scope; overlapping work is serialized.
- **Routing follows evidence.** Eight specialist roles—`architect`, `designer`, `documenter`, `fixer`,
  `implementer`, `researcher`, `reviewer`, and `validator`—have no model pins. The controller selects a
  validated model and effort for each attempt.
- **Completion is verifiable.** Requirements, blockers, decisions, attempts, costs, and the final Git
  checkpoint live in one strictly validated state document.
- **Your project stays clean.** Skill files, roles, sessions, locks, recovery data, and workflow state
  live under current-user locations rather than in the target repository.

## Give Codex a controller brief

Each prompt explicitly invokes `$coordinator`. These are controller instructions, not fixed topologies:
Coordinator derives the smallest useful dependency graph and routes attempts from current evidence.

**Deliver a feature with bounded parallel ownership**

```text
$coordinator Ship the saved-search feature. Inspect repository instructions, persist the acceptance
criteria, and build the smallest dependency graph. Parallelize only independent write scopes, route
each node from evidence, validate the integrated behavior, and finish only at a verified commit.
```

**Diagnose an intermittent test before changing production**

```text
$coordinator Reproduce and diagnose the intermittent checkout test first. Keep diagnosis, the
smallest owner-layer fix, and independent validation as bounded dependent work. Preserve failure
evidence, reconcile uncertain launches, and do not mark the workflow complete without a verified run.
```

**Resume a migration safely**

```text
$coordinator Resume the interrupted schema migration workflow. Reconcile the repository and every
uncertain launch before retrying, preserve completed evidence, replan only unclaimed future work,
and validate rollback and forward-migration behavior before finishing.
```

**Compare architectures from repository evidence**

```text
$coordinator Compare the two proposed event-processing architectures against current repository
boundaries and requirements. Route bounded research and architecture nodes, state tradeoffs and
missing evidence, and return a recommendation without implementing either option.
```

## Operate

The installed `SKILL.md` is the controller protocol. In compact form, a launch moves through
**claim → bind → run → validate → finish**:

1. The controller creates a dependency-safe node with acceptance criteria, route, and write scope.
2. It commits a unique launch claim before calling an agent provider, then binds the observed child.
3. The specialist works only its node; the controller inspects artifacts and validation evidence.
4. `finish` requires successful visible nodes, resolved requirements and blockers, validation, and a
   full verified commit checkpoint.

If a provider response is uncertain, Coordinator records `reconcile_required`; the controller must
find and bind the existing child or prove that none exists before retrying. A replacement controller
uses explicit `controller-takeover` and `resume`, fencing the prior epoch before further mutation.

Read-only state inspection remains available through the state adapter:

```sh
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py list --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py status --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py context --workflow-id WORKFLOW --json
```

On Windows, use `python` and paths under `%USERPROFILE%`.

## Current-user footprint

| Location | Contents |
|---|---|
| `~/.agents/skills/coordinator` | Skill, references, adapters, and Python runtime |
| `~/.codex/agents/coordinator-*.toml` | Eight standalone specialist role definitions |
| `~/.codex/config.toml` | Required multi-agent settings; unrelated configuration is preserved |
| `~/.agent-coordinator` | Private sessions, locks, recovery data, and workflow state |

Coordinator-owned state is never placed in repositories it orchestrates.

## Project

Agent Coordinator is MIT licensed. Contributions and focused reports are welcome:

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md) — report vulnerabilities privately, not in a public issue
- [License](LICENSE)
- [GitHub repository](https://github.com/alanhoff/agent-coordinator)
- [Public issues](https://github.com/alanhoff/agent-coordinator/issues)
