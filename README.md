<h1 align="center">Agent Coordinator</h1>
<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.pt-BR.md">Português (Brasil)</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>
<p align="center"><strong>Give Codex a complex job. Get a clear plan and a checked result.</strong></p>
<p align="center">Keep long jobs understandable, make progress easy to follow, and return to a checked result after an interruption.</p>
<p align="center">
  <img src=".github/readme/agent-coordinator-hero.png" width="880" alt="Illustration of one complex request moving through several bounded work paths with checkpoints and returning as one checked result.">
</p>
<p align="center">
  <a href="#install-in-one-prompt"><strong>Install in one prompt</strong></a>
  ·
  <a href="#see-it-on-an-everyday-task">See an everyday example</a>
</p>
<p align="center"><sub>MIT licensed · Installs for your user account · Does not change Codex settings</sub></p>

## What you get

- A clear plan you can follow from the request to the finish.
- Accountable parts, each with a clear purpose and owner.
- A checked, recoverable result that can survive interruptions.

## Is Agent Coordinator a fit?

| Use it when | You probably do not need it when |
|---|---|
| The work has multiple dependent steps, files, or specialties. | The task is one small, obvious step. |
| Several independent pieces can move ahead safely. | One quick answer or tiny edit is enough. |
| An interruption would make progress hard to reconstruct. | You could easily restart from the original prompt. |

## See it on an everyday task

> Add saved search to my app without breaking checkout.

1. **Make the finish line clear:** identify the saved-search behavior, the checkout safeguard, and how each will be checked.
2. **Keep progress understandable:** separate discovery, the focused change, and the regression check so every part has a clear purpose.
3. **Check before finishing:** review the changed files and verification evidence; after an interruption, continue from recorded progress instead of starting over.

The task is finished only when saved search meets its acceptance check and the existing checkout checks still pass.

## Install in one prompt

Ask Codex to follow the [repository-owned installer](INSTALL.md):

```text
Install https://github.com/alanhoff/agent-coordinator by following INSTALL.md
```

The procedure clones the repository to a temporary directory, records its commit, copies the self-contained `skill/` directory for the current user, removes the temporary checkout, and reports the installed path and source commit. It replaces an existing destination only when that destination identifies itself as Coordinator.

Running Coordinator requires Python 3.11 or newer and no third-party runtime packages. Installation does not edit Codex settings or register global custom-agent profiles.

| Current-user location | What goes there |
|---|---|
| `~/.agents/skills/coordinator` | The skill, role profiles, references, Python adapters, and bundled runtime code |
| `~/.agent-coordinator` | Private sessions, locks, recovery data, and workflow state |

## Try a first task

In a project, send this starter prompt:

```text
$coordinator Review this project's README for confusing setup steps. Do not edit files.
Return the three highest-impact fixes, cite the evidence for each, and confirm that no files changed.
```

A successful response meets three conditions:

1. Three fixes are ranked by impact.
2. Each fix cites evidence from the project.
3. The response confirms that no files changed.

## How it works

Coordinator follows the same four steps for each job:

1. **Understand:** make the requested outcome, constraints, and proof of success explicit.
2. **Divide:** break the job into the smallest useful parts, with clear boundaries and dependencies.
3. **Do:** work through ready parts in a safe order. Specialist agents are optional; when they are unavailable, Coordinator performs each part inline through the same process.
4. **Check and recover:** inspect the result and its evidence, reconcile uncertain work before retrying, and finish only after requirements and blockers are resolved.

The durable command path compiles those steps into safe operations:

```text
plan-apply → next → node-route-auto → node-claim → node-start → node-complete
           ↘ refine/split/reconcile as required ↗
                         workflow-complete
```

`next` is read-only and reports the next legal action class without embedding the full workflow state.

## Common questions

<details>
<summary>Does this require multiple agents?</summary>

No. When extra agent capacity is available, Coordinator can send independent pieces to separate workers; otherwise it completes them inline, one at a time.

</details>

<details>
<summary>Do I need to invoke it explicitly?</summary>

Yes. The documented prompt pattern starts each coordinated task with `$coordinator`; installation itself only places the skill and changes no settings.

</details>

<details>
<summary>What does it add to my project or settings?</summary>

It adds no persistent Coordinator-owned file to the target project and does not edit Codex settings or global custom-agent profiles. During initialization, it creates and removes one private-name file solely to detect the repository filesystem's case behavior; Windows determines this without a probe file.

</details>

<details>
<summary>What happens if work is interrupted?</summary>

A new Coordinator run can resume from private state. It marks work that may still be active for reconciliation before retrying, preserves completed evidence, and avoids starting an uncertain step twice.

</details>

## Reference

<details>
<summary>Prompt patterns</summary>

Use an explicit `$coordinator` prefix and state the finish line. These patterns cover implementation, diagnosis, recovery, and evidence-only comparison.

```text
$coordinator Ship the saved-search feature. Inspect repository instructions, preserve the acceptance
criteria, separate only independent work, validate the integrated behavior, and finish with material
evidence.
```

```text
$coordinator Reproduce and diagnose the intermittent checkout test before changing production. Keep
diagnosis, the smallest owner-layer fix, and independent validation as dependent parts.
```

```text
$coordinator Resume the interrupted schema migration workflow. Reconcile uncertain work before
retrying, preserve completed evidence, and validate rollback and forward migration behavior.
```

```text
$coordinator Compare the proposed event-processing architectures against current repository
boundaries and requirements. State tradeoffs and missing evidence, then recommend one without
implementing either option.
```

</details>

<details>
<summary>Assessment and lifecycle</summary>

Coordinator records five 0–4 complexity dimensions: breadth, change surface, coupling, novelty, and verification. It separately records 0–4 ambiguity factors for the objective, inputs, boundaries, dependencies, and acceptance.

Default limits are inclusive: complexity total 6 or any dimension 3 requires splitting, while ambiguity total 4 or any factor 2 requires refinement. The default maximum refinement depth is 8.

```text
assess → refine or split → route → claim → execute → validate → reassess
```

Every non-blocked assessable leaf must be current and executable before routing begins. Changed requirements or effective dependency results can make later work stale, so the fixed-point check repeats after relevant evidence changes.

</details>

<details>
<summary>Ownership, routing, and completion</summary>

- Each executable part has acceptance criteria, one role, and zero or more normalized repository-relative `write_scopes`.
- Empty scopes mean evidence-only work and require `change_surface=0`. Artifact work requires a positive change-surface score and at least one scope.
- Independent live work cannot overlap scopes. Case comparison follows behavior detected from the target filesystem.
- Routing ranks only candidates advertised by the active runtime. If no current catalog is available or selection fails, execution inherits the parent model and effort.
- A claim records a SHA-256 baseline for every artifact scope. Completion requires each declared scope to remain materialized and to have changed during that attempt.
- Workflow completion requires resolved requirements and blockers, valid evidence, and only allowed terminal states. Coordinator does not invoke or inspect a version-control system.

</details>

<details>
<summary>State inspection, including Windows</summary>

Persisted schema-v6 workflow documents live under `~/.agent-coordinator/workflows`. These commands are read-only and never create, lock, repair, normalize, cache, or clean state.

```sh
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py list --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py status --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py context --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py next --workflow-id WORKFLOW --json
```

In Windows Command Prompt, use `python` and the current-user path:

```bat
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" list --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" status --workflow-id WORKFLOW --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" context --workflow-id WORKFLOW --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" next --workflow-id WORKFLOW --json
```

Windows state lives under `%USERPROFILE%\.agent-coordinator\workflows`.

</details>

<details>
<summary>Docker demo</summary>

The demo builds a disposable Node.js container, installs Codex in it, and mounts this repository's Coordinator skill read-only. Set `OPENAI_API_KEY` in the ignored `demo/.env` file:

```dotenv
OPENAI_API_KEY=your-api-key
```

Run the demo from the repository root:

```sh
cd demo
./run.sh
```

Each run deletes `demo/home/` before generating a fresh application, so preserve any output you need before running it again.

The generated application and its `todos.db` database remain in `demo/home/app/`. Coordinator state is stored in `demo/home/state/`, and Codex state is stored in `demo/home/codex/`. Compose resources are removed when the run finishes, while these bind-mounted files remain available for inspection.

The generated project provides its own `npm start` command. Run it with a current Node.js release:

```sh
cd demo/home/app
npm start
```

</details>

## Project

- [MIT license](LICENSE)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [GitHub repository](https://github.com/alanhoff/agent-coordinator)
- [Public issue tracker](https://github.com/alanhoff/agent-coordinator/issues)
