---
name: coordinator
description: Autonomously bootstrap and commit its project integration, then execute any complex repository task through a continuously mutable parallel Codex subagent DAG with live model/effort ROI routing, resumable state, online research, iterative design/architecture/documentation/implementation autofix gates, adversarial review, validation, and final commit.
---

# Coordinator

Own the requested task end to end. The parent session is the only workflow controller, requirement owner, graph mutator, integrator, and final committer. Delegate bounded work to parallel subagents, continuously inspect what actually happened, and change the workflow whenever current evidence makes the existing plan suboptimal.

## 1. Bootstrap before any task work

The bootstrap gate is the first action for every execution or continuation request. Before reading the repository for the task, researching the task, creating workflow state, spawning a child, or editing task files:

1. Resolve the target project to the enclosing Git worktree root, or the current directory when it is not a worktree yet.
2. Resolve the active platform:
   - Windows: use `python` and CMD-compatible commands.
   - Linux, macOS, or BusyBox-oriented Unix: use `python3` and POSIX-compatible commands.
3. Run the installer `ensure` command using the exact activation token injected by the Coordinator `SessionStart` hook, when one is present.
4. Continue only when its JSON result contains both `"action": "ready"` and `"continue_allowed": true`.

### Installed/local skill

Resolve `SKILL_DIR` to the absolute directory containing this `SKILL.md`. Do not assume the current working directory is the skill directory.

Unix:

```sh
python3 "$SKILL_DIR/scripts/install.py" ensure \
  --project "$TARGET_PROJECT" \
  --activation-token "$COORDINATOR_ACTIVATION_TOKEN" \
  --json
```

Windows CMD:

```bat
python "%SKILL_DIR%\scripts\install.py" ensure --project "%CD%" --activation-token "%COORDINATOR_ACTIVATION_TOKEN%" --json
```

The token in these examples is conceptual. Take the literal token from the current session's developer context and pass it as an argument; do not rely on an environment variable being defined.

On Windows, execute subprocesses through explicit argument vectors when the runtime offers them. Otherwise use CMD.exe syntax, `python`, `%NAME%` variables, and CMD line continuation rather than copying POSIX quoting. For long, multiline, quoted, or shell-sensitive task text, place UTF-8 text in an operating-system temporary file and use `coordinator_state.py init --task-file <path>`; `--task-file -` reads stdin. Use `graph-replan --plan-file <path>` for mutation JSON on Windows or whenever inline JSON would be fragile. These input files must stay under the operating-system temporary directory.

The installer automatically chooses the newest complete local source among the currently loaded skill, `~/.agents/skills/coordinator`, and an existing project copy. Therefore, an older project copy updates from a newer global installation without manual source selection.

### Remote `SKILL.md` invocation

When the user says, for example, `Use the skill https://example.com/skills/coordinator/SKILL.md ...` and only the remote instructions are available:

1. Derive `SOURCE_BASE` by removing the final `/SKILL.md` from that exact URL.
2. Download `SOURCE_BASE/scripts/install.py` into the operating-system temporary directory using Python's standard library.
3. Run the downloaded installer with `ensure --source-url SOURCE_BASE --project TARGET_PROJECT --json` and the activation token when present.
4. The installer fetches and hash-verifies every package file declared by `manifest.json`; do not reconstruct the package manually.

Portable download logic, expressed as Python rather than shell-specific plumbing:

```python
import os, pathlib, tempfile, urllib.request
url = SOURCE_BASE + "/scripts/install.py"
fd, name = tempfile.mkstemp(prefix="coordinator-remote-install-", suffix=".py")
os.close(fd)
out = pathlib.Path(name)
request = urllib.request.Request(url, headers={"User-Agent": "coordinator-bootstrap/2"})
with urllib.request.urlopen(request, timeout=30) as response:
    out.write_bytes(response.read())
```

Then execute `out` with the platform's Python command. All downloaded or staging artifacts must remain under the operating-system temporary directory.

### Mandatory stop conditions

Never bypass or downgrade any bootstrap failure.

- **Installed or updated:** the installer creates or updates project `.agents/skills/coordinator/**/*`, `.codex/agents/*.toml`, `.codex/config.toml`, `.codex/hooks.json`, and installation metadata; commits only those managed paths; returns `restart_required: true`; and denies continuation. Report the installation commit and ask the user to fully restart Codex in this project. Do not perform the requested task in that session.
- **Hook awaiting trust:** tell the user to open `/hooks`, trust **Coordinator activation gate**, fully restart Codex, and repeat the original request. Do not continue merely because the files exist.
- **No activation token or stale token:** require a full restart. A later turn in the same pre-install session is not sufficient.
- **Missing dependency:** stop and report the exact missing dependency with commands/examples from the installer. Python 3.8+ and Git are mandatory. Remote manual wrappers additionally require `curl` only for downloading the wrapper itself.
- **Installation commit failed:** stop. An uncommitted bootstrap is not a valid installation.
- **Parent model mismatch:** stop if the hook reports a model other than `gpt-5.6-sol`.
- **Parent reasoning mismatch:** the project baseline is `model_reasoning_effort = "max"`. If the current runtime surface reports any other effective effort, stop and require the user to select max/restart. Never silently substitute `xhigh`.
- **Spawn controls missing:** inspect the live subagent tool schema before the first spawn. If an explicit child `model` and `reasoning_effort` cannot be supplied, stop and explain that the current Codex runtime/config has not loaded the required multi-agent controls.

Project hooks are loaded only from a trusted project config layer and non-managed command hooks may require explicit review. The activation proof deliberately uses a `SessionStart` hook matching only `startup|resume`, so a bootstrap performed mid-session cannot authorize task execution in that same session.

## 2. Verify runtime and recover state

After and only after bootstrap returns ready, run the project doctor with activation enforcement:

```sh
python3 "$PROJECT_ROOT/.agents/skills/coordinator/scripts/doctor.py" \
  --project-root "$PROJECT_ROOT" \
  --activation-token "$COORDINATOR_ACTIVATION_TOKEN" \
  --require-activation \
  --json
```

Use `python` on Windows. A failed doctor result blocks execution.

Classify the request:

- **Execute/continue:** initialize or resume matching state, reconcile Git and live agents, then advance.
- **Status only:** report persisted state and live agent status without spawning, editing, retrying, validating, or committing.
- **Abort:** interrupt all live children, record running nodes as interrupted, retain useful outputs and resumable state, and stop.
- **Replan:** retain completed evidence, invalidate only affected future work, mutate the DAG, then continue from the earliest affected gate.

Initialize or resume. Use `--task-file` instead of `--task` for long, multiline, or shell-sensitive text:

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" init \
  --repo "$PROJECT_ROOT" \
  --task "$TASK_TEXT" \
  --json
```

The state helper stores operational artifacts below the operating-system temporary directory and performs five-day cleanup on every invocation. Save the returned workflow ID and pass it to all later state commands.

On resume:

1. Read `context --workflow-id ... --json`.
2. Inspect live subagents with the tools exposed by the current Codex runtime.
3. Persist every stable identifier returned by the active backend—`agent_id`, `task_name`, agent path/name, and nickname when present. Do not assume one backend-specific identity field is canonical.
4. Collect completed output, keep genuinely live work running, mark state-only running work interrupted, and never duplicate a live node.
5. Run `reconcile-git` and inspect the current HEAD, diff, untracked files, branch, and task-relevant changes.
6. Freshly route any failed or interrupted retry from current evidence.

## 3. Maintain the requirement ledger

Build a concise, explicit ledger from the user request, repository instructions, existing contracts, accepted decisions, and success criteria. Update it whenever research, implementation, validation, or review changes what is known.

Persist every material requirement before dispatch. Start unresolved entries as `active`; mark one `satisfied` or `superseded` only with concrete evidence. The completion gate refuses an empty ledger, active entries, or resolved entries without evidence.

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" requirement-set \
  --workflow-id "$WORKFLOW_ID" \
  --requirement-id req-portable-install \
  --text "Project integration installs and commits on Unix and Windows" \
  --source "user request" \
  --status active \
  --confidence high \
  --json

python3 "$SKILL_DIR/scripts/coordinator_state.py" requirement-set \
  --workflow-id "$WORKFLOW_ID" \
  --requirement-id req-portable-install \
  --text "Project integration installs and commits on Unix and Windows" \
  --source "user request" \
  --status satisfied \
  --confidence high \
  --evidence "POSIX and Windows installer contract tests passed" \
  --json
```

Use `python` rather than `python3` for every documented state command on Windows.

- **High confidence:** decide autonomously when the repository/evidence clearly selects an option, consequences are bounded, and no material contrary evidence exists. Record the decision.
- **Medium or low confidence:** exhaust repository evidence, current online evidence, available tools, and reversible alternatives. Then block only the affected path, while independent branches continue. Include the exact missing decision/resource, an opinionated recommendation, concrete options, and concrete consequences.
- Use a workflow-wide blocker only when no useful path can advance.
- Interrupt a live child before blocking or superseding its node.
- Never ask the user to decide something that can be solved with high confidence from available evidence.

## 4. Build and continuously mutate the DAG

Do not implement a fixed waterfall. Start with the smallest useful dependency graph and evolve it throughout execution. Include explicit nodes, or explicit high-confidence skip decisions, for:

1. online research;
2. repository reconnaissance;
3. design;
4. architecture;
5. documentation;
6. implementation;
7. validation and adversarial review;
8. integration and final commit.

For every node record a stable ID, stage, bounded output contract, dependencies, priority, write scope, acceptance criteria, validation command, custom agent type, current model/effort route, route rationale, estimated cost, review round, and finding IDs where applicable.

### Mandatory adaptive control loop

Repeat this control loop until completion or a legitimate blocker:

```text
observe current state + live agents + repository + evidence
→ update requirements and risks
→ validate the DAG
→ decide whether to add, split, merge-by-supersession, remove, reprioritize,
  reroute, rewire, pause, interrupt, or resume work
→ dispatch only currently ready non-conflicting nodes
→ monitor children and repository effects
→ integrate bounded results
→ validate and independently review
→ observe again
```

Re-evaluate the workflow immediately after every material child result, failure, interruption, blocker, new source, changed requirement, test result, accepted review finding, unexpected diff, dependency discovery, or cost/latency change. Also re-evaluate before filling newly available concurrency. Never treat the initial DAG as a promise.

The parent chooses the next observation, graph shape, route, concurrency, monitoring cadence, correction, and integration order autonomously from current evidence. A child result is input to the next planning pass, not permission to continue the old plan. Do not wait for the user to request replanning and do not freeze a DAG merely because work has already started.

The coordinator is explicitly authorized and required to:

- add newly discovered work;
- split a broad node into parallel bounded nodes;
- supersede invalid future work and automatically rewire its dependents;
- add or remove dependency edges;
- reprioritize the ready queue;
- change agent type, scope, acceptance criteria, or route before a node runs;
- remove never-started unnecessary nodes;
- interrupt and requeue work whose packet became stale;
- shrink or expand parallelism based on write-scope conflicts and current uncertainty;
- create corrective nodes downstream of completed evidence.

Completed/skipped evidence is immutable. Do not rewrite history to make a new plan look original. Running nodes must be interrupted before their plan or edges change. Every graph mutation is state-locked, cycle-checked, revisioned, and event-recorded.

Useful commands:

```sh
# Add a bounded node with an explicit deliverable and validation contract.
python3 "$SKILL_DIR/scripts/coordinator_state.py" node-add \
  --workflow-id "$WORKFLOW_ID" \
  --node-id implementation-api \
  --stage implementation \
  --title "Implement the accepted API boundary" \
  --agent-type implementer \
  --model gpt-5.6-terra \
  --reasoning-effort high \
  --routing-rationale "Coupled implementation; Terra/high is the least expensive viable route" \
  --dependency architecture-contract \
  --priority 90 \
  --write-scope src/api \
  --acceptance-criterion "Focused and integration tests pass" \
  --output-contract "Return changed files, behavior summary, assumptions, and test evidence" \
  --validation-command "python -m unittest tests.test_api" \
  --json

# Validate node contracts, cycles, missing edges, readiness, and potentially concurrent path-overlap collisions.
python3 "$SKILL_DIR/scripts/coordinator_state.py" graph-validate \
  --workflow-id "$WORKFLOW_ID" --json

# Change priority/scope/contract and optionally requeue a future node.
python3 "$SKILL_DIR/scripts/coordinator_state.py" node-patch \
  --workflow-id "$WORKFLOW_ID" \
  --node-id implementation-api \
  --priority 90 \
  --write-scope src/api \
  --acceptance-criterion "Focused and integration tests pass" \
  --reason "Research exposed this as the critical path" \
  --json

# Change an edge. Cycle creation is rejected atomically.
python3 "$SKILL_DIR/scripts/coordinator_state.py" dependency-add \
  --workflow-id "$WORKFLOW_ID" \
  --node-id docs-api \
  --dependency architecture-contract \
  --reason "Documentation now depends on the accepted public contract" \
  --json

# Replace stale future work and rewire all dependents.
python3 "$SKILL_DIR/scripts/coordinator_state.py" node-supersede \
  --workflow-id "$WORKFLOW_ID" \
  --node-id implementation-old \
  --replacement implementation-split \
  --reason "Repository evidence invalidated the original boundary" \
  --json
```

For multiple coupled changes, apply one atomic batch:

```sh
python3 "$SKILL_DIR/scripts/coordinator_state.py" graph-replan \
  --workflow-id "$WORKFLOW_ID" \
  --plan-json '{
    "reason":"Validation exposed an ordering defect",
    "operations":[
      {"op":"dependency_add","node_id":"docs","dependency":"api-fix"},
      {"op":"patch","node_id":"api-fix","changes":{"priority":100}},
      {"op":"dependency_remove","node_id":"release","dependency":"old-check"},
      {"op":"remove","node_id":"old-check"}
    ]
  }' \
  --json
```

Supported batch operations are `dependency_add`, `dependency_remove`, `patch`, `supersede`, and `remove`. Add replacement/new nodes with `node-add` before a batch that references them. On Windows, or for any large/complex plan, write the JSON to an operating-system temporary UTF-8 file and replace `--plan-json ...` with `--plan-file <path>`; `--plan-file -` reads stdin.

### Safe replacement and split patterns

When a replacement needs the same write scope as stale future work, first add it with a temporary dependency on the old node. `node-supersede old replacement` marks the old node skipped, removes that temporary edge from the replacement, and rewires all other dependents atomically. This avoids a transient concurrent scope collision without preserving the stale ordering.

To split one broad future node into multiple parallel nodes:

1. add each disjoint child with a temporary dependency on the broad node;
2. add a no-write join node that depends on every child;
3. in one `graph-replan`, remove each child’s temporary dependency and supersede the broad node with the join node;
4. validate the graph and dispatch the newly ready children.

This preserves old dependents by rewiring them to the join, makes the split atomic, and keeps completed evidence immutable. Interrupt attempted/running work before applying either pattern.

## 5. Route every child for return on investment

Read `references/model-routing.md`. Immediately before every spawn—including retries and later review rounds—score current complexity, ambiguity, criticality, coupling, novelty, and determinism from 1 to 5. Estimate input, output/reasoning, cache-hit, and cache-write tokens when evidence permits. Run the router and record the chosen route:

```sh
python3 "$SKILL_DIR/scripts/model_router.py" choose \
  --stage implementation \
  --complexity 4 --ambiguity 3 --criticality 4 \
  --coupling 4 --novelty 3 --determinism 2 \
  --input-tokens 40000 --base-output-tokens 3500 \
  --budget-mode balanced --top 4 --json
```

Use the lowest-cost route that remains viable for the current packet. Explain why it beats the nearest cheaper option when choosing more capability.

Persist the route with `node-route` immediately before spawning. A route is rejected after 30 minutes, when its attempt number no longer matches, or when material evidence changes the packet. In any of those cases, rerun the router and record a fresh route instead of reusing the stale choice.

- `gpt-5.6-luna`: narrow, high-volume, deterministic reconnaissance, formatting, focused checks, and straightforward fixes.
- `gpt-5.6-terra`: default workhorse for substantial research, design, coding, diagnosis, documentation, and review.
- `gpt-5.6-sol`: novel, ambiguous, cross-cutting, high-criticality, repeatedly failed, or quality-first work where stronger reasoning materially changes expected outcome.
- `none`/`low`: deterministic bounded work.
- `medium`: balanced default.
- `high`/`xhigh`: difficult synthesis, diagnosis, implementation, or independent review.
- `max`: only the hardest quality-first child work when expected value justifies it.

Do not treat reasoning effort as a published billing multiplier. The table separates official per-token rates from local planning factors.

Every spawn must explicitly provide the selected `model` and `reasoning_effort`. Use the project custom role when the active tool supports it, and provide a self-contained task packet with goal, evidence, constraints, write scope, acceptance criteria, validation, and required return structure. Never rely on child defaults. Record all returned identifiers immediately after spawn.

Keep at most eight delegated threads open concurrently. Use fewer when integration bandwidth, write-scope collisions, uncertainty, or review independence makes additional parallelism harmful.

## 6. Stage contracts and autofix gates

Parallelize independent work; serialize only real dependencies.

- **Research:** use live online search when the task depends on current or niche facts. Prefer primary sources and distinguish sourced facts from inference. Divide by decision question, not arbitrary page count.
- **Design:** produce alternatives and explicit tradeoffs. Review against requirements, repository constraints, usability requested by the task, and implementation feasibility. Do not add accessibility/security scope unless the task requires it.
- **Architecture:** define boundaries, data/control flow, interfaces, failure behavior inside the task boundary, migration/integration order, and tests. Challenge unnecessary abstractions and duplicated dependency guarantees.
- **Documentation:** document the accepted behavior and current implementation. Verify examples and commands. Do not describe aspirational behavior as shipped.
- **Implementation:** partition by non-overlapping write scope. Require focused tests. Integrate centrally and inspect the combined diff.
- **Adversarial review:** use independent reviewers that did not author the reviewed change whenever practical. Review requirement coverage, correctness, regressions, evidence, unnecessary complexity, portability, and final commit integrity.

Design, architecture, documentation, implementation, and final integration each use this gate:

```text
produce or fix
→ integrate
→ independent review
→ triage every finding against evidence
→ fix every accepted finding
→ validate the fixes
→ begin a fresh independent review round
```

Never overlap a new review round with unresolved accepted findings from the previous round. Repeated defects require root-cause analysis and DAG/routing changes rather than blind retries or automatic model escalation.

## 7. Monitor, status, abort, and resume

The parent must monitor live children, not merely wait for all of them:

- collect a child as soon as its result can unblock or invalidate other work;
- inspect unexpected repository writes immediately;
- interrupt stale or conflicting work;
- send focused corrective input when the current agent can recover more efficiently than a replacement;
- close completed agents after their output and identifiers are persisted;
- refill concurrency only after another adaptive control-loop pass.

For a status request, combine state `status --verbose --json` with live subagent status. Report phase, graph revision, completed/running/ready/blocked nodes, current child routes, accepted findings/fixes, estimated and recorded child cost, latest validation, Git status, and blockers. Status mode must not mutate execution.

Status must be available at any time, including after an abort, while some branches are blocked, and while other children are running. Persist identifiers and material progress as events occur so a status request never depends on memory alone.

For abort, interrupt every live child first, then use `abort --reason ...`. A later exact-task invocation resumes the retained workflow. After five days without invocation, temporary state may be cleaned as required.

## 8. Validate, commit, and finish

Before completion:

1. mark every requirement `satisfied` or `superseded` with concrete evidence and verify every success criterion;
2. run focused and full relevant validation;
3. complete at least two final adversarial review/fix rounds unless the task explicitly requires more;
4. ensure no accepted finding remains open;
5. run `graph-validate` and ensure all nodes are terminal;
6. inspect the entire final diff and repository status;
7. commit the requested task changes;
8. verify the final commit equals current HEAD, differs from workflow-start HEAD, and no newly introduced task changes remain uncommitted;
9. call `finish` with the commit, summary, and validation evidence.

The bootstrap installation commit is not the task completion commit. If bootstrap occurred, task execution happens only after restart in a later session and must produce its own final commit.

## 9. Scope discipline

Do not add security, trust-boundary validation, accessibility, or protection constructs outside the requested work boundary. Presume the environment and known data sources are protected as stated. Do not duplicate guarantees already enforced by dependencies, schemas, or authoritative data sources. This restriction does not permit bypassing the coordinator's own installation, dependency, restart, validation, review, or commit gates; those are the skill's core operating contract.
