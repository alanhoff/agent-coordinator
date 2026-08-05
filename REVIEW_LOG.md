# Coordinator 2.2.1 — adversarial review and repair log

This log records release-blocking findings and repairs applied after the portability redesign. A round was closed only after its findings were fixed and the automated suite passed before the next round began.

## Review 1 — current Codex configuration and bootstrap authority

**Attack focus:** stale Codex assumptions, ambiguous package authority, and execution before prerequisites.

Findings and repairs:

- Replaced the obsolete Multi-Agent V2 feature-table assumption with the current project configuration under `[agents]`, while keeping `[features].multi_agent = true` and hooks enabled.
- Corrected concurrency semantics so `max_concurrent_threads_per_session = 8` represents eight spawned workers in addition to the Sol/max parent.
- Made the global or explicitly loaded complete package win same-version ties over a drifted project copy. A declared newer but incomplete or hash-invalid package now blocks instead of silently downgrading.
- Added dispatch enforcement: a node cannot start until every dependency is terminal, its blockers are resolved, and its route is fresh for the current attempt.

## Review 2 — self-installation, restart proof, and cross-platform hard stops

**Attack focus:** first-run behavior, remote-only invocation, partial installation, unsafe continuation, and platform assumptions.

Findings and repairs:

- Made the project installation a full self-deploying copy under `.agents/skills/coordinator`, not a reduced runtime subset.
- Added local and remote installers for POSIX shells and Windows CMD.exe. Remote installation begins from `install.sh` or `install.cmd`, fetches the standard-library Python installer, then downloads and verifies every manifest-declared file.
- Added strict runtime checks for Python 3.8+, Git, repository resolution/initialization, Git author identity, source completeness, and manifest hashes. Missing dependencies are hard blockers.
- Added project `SessionStart` hooks with both `command` and `commandWindows`. Installation or update commits the managed integration and returns exit code `10`; task execution remains forbidden until a newly loaded Codex session supplies a valid project-, generation-, session-, model-, and time-bound activation token.
- Made config and hook editing refuse ambiguous target shapes instead of guessing. Unrelated TOML tables, hook groups, and staged Git changes are preserved.
- Added operating-system temporary directories, recursive five-day cleanup, atomic writes, and a cross-platform installation lock.

## Review 3 — workflow mutability, evidence integrity, and recovery

**Attack focus:** a static initial plan, unsafe graph surgery, false completion, stale routing, and weak interruption recovery.

Findings and repairs:

- Added atomic graph operations for node creation, patching, reprioritization, rerouting, dependency insertion/removal, supersession with dependent rewiring, safe removal of unstarted work, and batch replanning.
- Added cycle, missing-edge, duplicate-node, output-contract, dependency, and potentially concurrent write-scope checks. Windows scope comparison is case-insensitive.
- Required the parent to run an adaptive control loop after every material event and autonomously revise graph shape, routing, concurrency, monitoring, correction paths, and integration order.
- Made running-node plans immutable until the child is interrupted. Terminal evidence is immutable.
- Required fresh per-attempt routes, concrete output contracts, validation commands, validation evidence for `done`, and an evidence-backed requirement ledger for final completion.
- Strengthened Git completion checks to compare exact dirty-content fingerprints, require a new final commit, require recorded commit equality with `HEAD`, and reject newly introduced uncommitted task changes.
- Added exact-task resume, abort/resume, node-scoped blockers, workflow blockers, status reporting, and recoverable state below the operating-system temporary directory.

## Review 4 — installer commit completeness and exit-code consistency

**Attack focus:** changes that appear installed but are absent from the bootstrap commit, and wrappers that accidentally allow continuation.

Findings and repairs:

- Found that deletion of a formerly managed file could be applied to the worktree without entering the isolated bootstrap commit. Managed deletions are now included in the temporary Git index, and a regression test verifies a clean post-install worktree.
- Found that the manual installer returned success after installing instead of the documented restart-required code. A changed installation now returns `10` consistently through the Python entry point and both wrappers; an already-current manual installation returns `0` without claiming task readiness.
- Added an actual local-HTTP wrapper test for `install.sh`, including source URLs with trailing slashes and propagation of exit code `10`.

## Review 5 — Windows command transport and path semantics

**Attack focus:** CMD.exe quoting and length limits, case-only path collisions, temporary-file assumptions, and Unix-only cleanup behavior.

Findings and repairs:

- Added `init --task-file` and `graph-replan --plan-file`, each supporting `-` for UTF-8 stdin, so multiline or shell-sensitive payloads do not need to survive CMD.exe argument parsing.
- Case-folded write scopes on Windows so paths that differ only by case cannot be assigned to potentially concurrent nodes.
- Reworked POSIX and CMD wrappers to allocate temporary installer paths through Python's `tempfile` module and clean them while preserving the installer return code.
- Normalized remote base URLs and rejected query/fragment forms that cannot preserve manifest-relative package paths.
- Documented direct ZIP extraction, local update commands, POSIX `curl | sh`, CMD.exe remote installation, restart/trust steps, owned project paths, and exit-code propagation.

## Final release verification

The finalized package is verified from a clean extraction, not from the working directory. The release gate covers:

- manifest completeness and SHA-256 integrity;
- direct-root archive layout suitable for extraction into `~/.agents/skills/coordinator` or `%USERPROFILE%\.agents\skills\coordinator`;
- Python 3.8 grammar compatibility and standard-library-only imports;
- POSIX `sh`, `dash`, and BusyBox `sh` syntax checks;
- 18 model/reasoning routing combinations and quality-first Sol/max selection;
- real Git bootstrap, isolated installation commit, restart-required result, simulated fresh `SessionStart` activation, activation-required doctor check, resumable state lifecycle, evidence gates, final task commit, and workflow completion;
- unit and contract tests for Windows-specific command strings, path semantics, and file-backed input.

The release environment did not contain Windows CMD.exe/Wine or a live Codex executable. Therefore, the Windows launcher was validated by contract/unit tests rather than execution on a Windows kernel, and Codex child spawning was validated through current configuration contracts rather than a fabricated live spawn. The POSIX wrappers, BusyBox/dash syntax, remote HTTP installation, Git integration, activation protocol, router, and state engine were executed directly.
