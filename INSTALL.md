# Agent installation procedure

These instructions are for the agent handling the user's installation request. Complete the work on
the user's behalf; do not turn this file into a list of commands for the user to run.

## Safety and ownership

- Use the repository at `https://github.com/alanhoff/agent-coordinator` as the source. Work from a
  fresh temporary checkout and record the exact source commit in the completion report.
- Require Python 3.11 or newer and a current-user home directory. Stop with a precise explanation if
  either prerequisite is unavailable.
- Do not write Coordinator files into the user's active project. The only Coordinator-owned paths are
  `~/.agents/skills/coordinator` and the eight `~/.codex/agents/coordinator-<role>.toml` files described
  below. The only owned configuration is the three keys described below in `~/.codex/config.toml`.
- Treat symlinks, filesystem aliases, unexpected file types, and an existing destination that cannot
  be identified as Coordinator-owned as conflicts. Do not follow, replace, or delete ambiguous paths.
- Identify an existing skill root as Coordinator-owned only when it is a real directory whose
  `SKILL.md` declares `name: coordinator`, whose `README.md` names this repository as its source, and
  whose two surviving adapters are present. Otherwise leave it unchanged and report a conflict.
- Preserve unrelated Codex configuration byte-for-byte where its syntax permits a safe semantic edit.
  Back up every destination that will change and restore all of them if any placement or validation
  step fails.

## Build the installed layout

Stage the complete installation beside its destination before replacing an existing owned copy:

1. Copy the full `skill/` tree to the staged skill root.
2. Copy the repository's `LICENSE` to `LICENSE` at the staged skill root.
3. Copy `src/coordinator/` to `scripts/lib/coordinator/` inside the staged skill root. Do not copy
   bytecode, cache directories, temporary files, or any file outside those declared source paths.
4. Confirm that the staged skill contains `SKILL.md`, `agents/openai.yaml`, both Python adapters in
   `scripts/`, the reference documents `model-routing.md`, `state-schema.md`, and
   `workflow-protocol.md`, and exactly these eight role files under `agents/roles/`: `architect`,
   `designer`, `documenter`, `fixer`, `implementer`, `researcher`, `reviewer`, and `validator`.
5. Stage the same eight role TOML files without editing their contents. Their final standalone paths
   must be `~/.codex/agents/coordinator-architect.toml`, `coordinator-designer.toml`,
   `coordinator-documenter.toml`, `coordinator-fixer.toml`, `coordinator-implementer.toml`,
   `coordinator-researcher.toml`, `coordinator-reviewer.toml`, and `coordinator-validator.toml`.
   Keep each TOML's `name` field unchanged.

Before placement, parse every existing standalone TOML in `~/.codex/agents/`. If a file outside those
eight destinations declares any of the eight Coordinator role names, stop without changing anything
and report the collision. Treat an existing destination as Coordinator-owned only when its complete
contents match the corresponding role from the current checkout or a commit in that checkout's Git
history; otherwise report a conflict rather than replacing it.

Semantically merge these required values into `~/.codex/config.toml` without duplicating TOML tables
or keys:

```toml
[agents]
enabled = true
max_concurrent_threads_per_session = 8

[features]
multi_agent = true
```

Keep every unrelated table, key, comment, and newline style. If the existing file cannot be parsed or
merged unambiguously, leave it and every destination unchanged and report the conflict. An absent
required key may be inserted and an exact existing value may be retained; a different existing value
is user-owned and must be reported as a conflict rather than overwritten.

## Place and validate

Replace only destinations proven to be Coordinator-owned. Use a same-filesystem rename for each path
so no individual file or tree is partial, and roll back every already-placed path if the sequence fails.
Apply current-user-only write permissions to the installed role files and normal read permissions to
the skill files. Do not alter permissions on a pre-existing parent directory.

Before reporting success:

1. Re-read the installed trees and confirm their file contents match the staged source.
2. Parse the resulting TOML and confirm the three required settings have the exact values above.
3. With bytecode generation disabled, invoke each installed adapter with an invalid command and JSON
   output enabled. `coordinator_state.py` and `model_router.py` must each exit with status 2 and return
   valid JSON whose `code` is `invalid_invocation`.
4. Confirm that no temporary checkout, staging directory, backup, or generated cache remains after a
   successful placement. On failure, restore the backups first, then clean temporary data.

Report the source commit, changed destinations, configuration keys established, and validation results.
Do not claim success if any required step was skipped or only partially completed.
