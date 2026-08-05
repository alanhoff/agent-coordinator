# Coordinator

Coordinator is a self-deploying Codex skill for complex repository work. A globally installed copy can bootstrap its project integration, commit that integration, require a clean Codex restart, and then run a continuously adaptable parallel-subagent workflow for research, design, architecture, documentation, implementation, validation, adversarial review, repair, and the final task commit.

Package version: **2.2.1**.

The archive is deliberately rooted at the skill directory. Extract its contents directly into:

- Linux/macOS: `~/.agents/skills/coordinator/`
- Windows: `%USERPROFILE%\.agents\skills\coordinator\`

After extraction, `SKILL.md`, `manifest.json`, `scripts/`, `references/`, `assets/`, and `agents/` must be immediate children of that directory.

## Runtime requirements

Coordinator blocks rather than silently degrading when a required dependency or runtime capability is absent.

| Requirement | Linux/macOS/BusyBox-oriented Unix | Windows 11 Home |
|---|---|---|
| Python | Python 3.8+ as `python3` | Python 3.8+ as `python` |
| Git | `git` on `PATH` with author name/email configured | Git for Windows on `PATH` with author name/email configured |
| Codex runtime | A trusted project configuration layer and subagent tools that expose explicit child `model` and `reasoning_effort` overrides | Same |
| Shell for manual wrapper | POSIX `sh`; `curl` only downloads the wrapper | `CMD.exe`; built-in `curl.exe` downloads the wrapper |

The installer itself uses only Python's standard library and Git. It does not require Bash, PowerShell, package managers, `jq`, `rsync`, `tar`, or third-party Python modules. A `codex` executable on `PATH` is not required when the skill is already running inside a Codex host; the skill instead validates the live subagent surface before dispatching work.

Configure Git identity before first use when needed:

```sh
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

The equivalent commands work in Windows CMD.

## Global installation from this ZIP

### Linux or macOS

```sh
mkdir -p "$HOME/.agents/skills/coordinator"
python3 -m zipfile -e coordinator-portable-reviewed.zip "$HOME/.agents/skills/coordinator"
python3 "$HOME/.agents/skills/coordinator/scripts/install.py" cleanup
```

### Windows CMD

```bat
mkdir "%USERPROFILE%\.agents\skills\coordinator" 2>nul
python -m zipfile -e coordinator-portable-reviewed.zip "%USERPROFILE%\.agents\skills\coordinator"
python "%USERPROFILE%\.agents\skills\coordinator\scripts\install.py" cleanup
```

The command uses Python's standard-library ZIP extractor. An ordinary graphical ZIP extractor is also sufficient. Do not add another enclosing `coordinator` directory when extracting.

## Normal use

Open Codex in a Git project and invoke the skill:

```text
$coordinator implement a chat bot
```

On the first invocation, or whenever the global package is newer/different from the project installation, Coordinator performs only the bootstrap:

1. finds the enclosing Git worktree, or initializes Git in the requested project;
2. validates Python, Git, Git author identity, the package manifest, and all source hashes;
3. creates or updates the project copy under `.agents/skills/coordinator/`;
4. creates or updates `.codex/agents/*.toml`;
5. merges Coordinator-owned values into `.codex/config.toml` without replacing unrelated settings;
6. merges the `Coordinator activation gate` into `.codex/hooks.json` without replacing unrelated hooks;
7. writes `.codex/coordinator-install.json`;
8. commits only those managed paths, preserving unrelated staged changes;
9. stops with `restart_required: true`.

The requested task is **not** allowed to continue in that session. Fully restart Codex in the project. When Codex asks about the project command hook, open `/hooks`, trust **Coordinator activation gate**, restart again, and repeat the original request. A later chat turn in the pre-install session is not a restart.

After a trusted restart, the project `SessionStart` hook injects a project-, generation-, session-, model-, and time-bound activation token. Coordinator verifies that token before repository task work. This prevents an old session from pretending that newly written Codex configuration has already been loaded.

## Manual project install or update

The manual commands install or update and commit project integration, but never execute a task. An install/update exits with code `10` and `restart_required: true`; treat that as an intentional hard stop and fully restart Codex.

### Local global package — Unix

```sh
python3 "$HOME/.agents/skills/coordinator/scripts/install.py" install \
  --source "$HOME/.agents/skills/coordinator" \
  --project . \
  --json
```

### Local global package — Windows CMD

```bat
python "%USERPROFILE%\.agents\skills\coordinator\scripts\install.py" install --source "%USERPROFILE%\.agents\skills\coordinator" --project . --json
```

### Remote package — Unix one-liner

Replace the placeholder with the directory that contains `SKILL.md` and `manifest.json`:

```sh
curl -fsSL https://<source>/skills/coordinator/scripts/install.sh | \
  sh -s -- --source https://<source>/skills/coordinator --project .
```

The wrapper checks `python3` and `git`, downloads the self-contained Python installer to the operating-system temporary directory, and lets that installer fetch and hash-verify the manifest-declared package.

### Remote package — Windows CMD

Run these commands in CMD.exe after replacing the placeholder. The wrapper cleans its downloaded Python installer; the final `del` cleans the wrapper itself even when exit code `10` correctly reports that a restart is required.

```bat
set "COORDINATOR_WRAPPER=%TEMP%\coordinator-install-%RANDOM%-%RANDOM%.cmd"
curl.exe -fsSL https://<source>/skills/coordinator/scripts/install.cmd -o "%COORDINATOR_WRAPPER%" && call "%COORDINATOR_WRAPPER%" --source https://<source>/skills/coordinator --project .
set "COORDINATOR_RC=%ERRORLEVEL%"
del /q "%COORDINATOR_WRAPPER%" 2>nul
echo Coordinator installer exit code: %COORDINATOR_RC%
```

This uses CMD.exe, `curl.exe`, `python`, and Git for Windows. Exit code `10` is an intentional hard stop: fully restart Codex before repeating the task. In a batch file that must propagate the result, add `exit /b %COORDINATOR_RC%` as the final line; do not add it when pasting into an interactive CMD window.

## Direct remote-skill invocation

Coordinator is designed to remain deployable when the only initial material is a remote instruction file:

```text
Use the skill https://example.com/skills/coordinator/SKILL.md and implement a chat bot
```

`SKILL.md` tells Codex to derive the package base URL by removing `/SKILL.md`, download `scripts/install.py` with Python's standard library, and run:

```text
install.py ensure --source-url <package-base> --project <target> --json
```

The installer then obtains `manifest.json`, downloads only manifest-declared files, verifies SHA-256 hashes, installs the project integration, commits it, and enforces the same restart gate. Hosting must preserve the package's relative paths and serve every file referenced by `manifest.json`.

## Files installed into a project

Coordinator owns only these paths:

```text
.agents/skills/coordinator/**/*
.codex/agents/researcher.toml
.codex/agents/designer.toml
.codex/agents/architect.toml
.codex/agents/documenter.toml
.codex/agents/implementer.toml
.codex/agents/reviewer.toml
.codex/agents/fixer.toml
.codex/agents/validator.toml
.codex/config.toml                 # Coordinator values are marker-delimited
.codex/hooks.json                  # One marker-identifiable SessionStart group
.codex/coordinator-install.json
```

The installation commit is isolated with a temporary Git index. Pre-existing staged changes outside managed paths remain staged. The installer refuses to continue if it cannot create the mandatory commit.

## Adaptive workflow behavior

After bootstrap readiness is proven, the Sol/max parent creates the smallest useful DAG and continuously modifies it from observed evidence. It autonomously chooses and revises graph shape, priorities, routes, concurrency, monitoring cadence, corrections, and integration order throughout the task. It can add or remove unstarted nodes, split broad work, supersede stale work, atomically rewire dependencies, reprioritize, reroute models and reasoning effort, change scope/contracts, interrupt obsolete children, vary concurrency, and add corrective paths after validation or review. Every mutation is locked, node-contract-checked, dependency-checked, path-overlap-checked for potentially concurrent work, cycle-checked, revisioned, and recorded.

Each node has a bounded output contract and validation command. A successful node cannot become `done` without concrete validation evidence. Final completion also requires a nonempty requirement ledger whose entries are all evidence-backed `satisfied` or `superseded` records.

Same-scope replacement and broad-node splitting are supported without freezing the initial graph. The coordinator temporarily serializes new replacement/split nodes behind stale future work, then uses atomic supersession/rewiring to remove the temporary ordering and expose the improved parallel graph.

Useful read-only status commands:

```sh
python3 .agents/skills/coordinator/scripts/install.py status --project . --json
python3 .agents/skills/coordinator/scripts/doctor.py --project-root . --json
python3 .agents/skills/coordinator/scripts/coordinator_state.py status --verbose --json
```

Useful graph commands:

```sh
python3 .agents/skills/coordinator/scripts/coordinator_state.py graph-validate --workflow-id <id> --json
python3 .agents/skills/coordinator/scripts/coordinator_state.py graph-replan --workflow-id <id> --plan-file <temporary-plan.json> --json
```

Use `python` on Windows. The state helper accepts `init --task-file <path>` and `graph-replan --plan-file <path>` (or `-` for stdin), avoiding CMD.exe quoting and command-length failures for multiline or shell-sensitive input. Keep those input files in the operating-system temporary directory.

See `SKILL.md`, `references/workflow-protocol.md`, and `references/state-schema.md` for the full lifecycle, mutability rules, recovery semantics, and blocker policy.
See `REVIEW_LOG.md` for the adversarial findings, fixes, and release-validation scope.

## Updates

Replace the global package with a newer complete package. The next invocation compares the source manifest against the project copy. Any file, config, hook, metadata, or stale managed-path mismatch triggers a project update commit and a mandatory restart before task execution.

The source resolver picks the highest semantic version among complete, manifest-verified copies of the currently loaded skill, the global package, and the project copy. A healthy external/global copy wins same-version ties so project drift is repaired. A declared newer but incomplete or hash-invalid local copy blocks automatic downgrade. Use `--source` or `--source-url` to make the intended source explicit.

## Temporary artifacts and recovery

Installer downloads, locks, backups, activation records, and resumable workflow state are stored below the operating-system temporary directory. Every installer or state-helper invocation removes Coordinator-owned temporary entries older than five days. No generated runtime artifact is written into the installed skill package.

Workflow state survives ordinary interruption and records graph revisions, node routes, attempts, agent identifiers, blockers, decisions, review findings, validation, Git snapshots, and completion evidence. Exact-task invocations can resume it until retention cleanup removes stale state.

## Validation

From the package root:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/model_router.py choose --stage architecture --complexity 5 --ambiguity 5 --criticality 5 --coupling 5 --novelty 5 --determinism 1 --budget-mode quality --json
```

The release tests cover manifest integrity, Python 3.8 syntax compatibility, standard-library-only imports, config/hook preservation and ambiguity blocking, isolated Git commits, restart activation, updates, source-downgrade prevention, dynamic DAG mutation, same-scope replacement, Windows case-insensitive scope collision detection, file-backed task/replan input, cycle rejection, resumability, recursive cleanup, routing freshness, evidence-backed node and requirement completion, remote HTTP installation, and packaging contracts.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Manual install/status action succeeded, or an execution bootstrap is ready. |
| `1` | Read-only status found installation drift. |
| `10` | Project integration was installed/updated and a full restart is mandatory. |
| `12` | Installation is current but this session lacks valid post-restart activation. |
| `20` | A dependency, source, Git, configuration, commit, or other hard blocker prevents continuation. |
