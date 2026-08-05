# Codex and GPT-5.6 research baseline

Research date: **2026-08-05**. Recheck these official sources before changing configuration or model policy because Codex's multi-agent and hook surfaces evolve quickly.

## Official sources

- [Codex subagents](https://developers.openai.com/codex/agent-configuration/subagents)
- [Build Codex skills](https://developers.openai.com/codex/build-skills)
- [Codex hooks](https://developers.openai.com/codex/hooks)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference)
- [OpenAI model catalog](https://developers.openai.com/api/docs/models)
- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [Current Codex configuration schema](https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/config.schema.json)
- [Codex releases](https://github.com/openai/codex/releases)

## Applied configuration model

The parent is pinned at the project root to:

```toml
model = "gpt-5.6-sol"
model_reasoning_effort = "max"
web_search = "live"
```

Current documented subagent controls live under `[agents]`. The current public configuration reference no longer documents the earlier `features.multi_agent_v2` compatibility table, so the installer deliberately writes only stable, documented settings:

```toml
[agents]
enabled = true
interrupt_message = true
max_concurrent_threads_per_session = 8

[features]
hooks = true
multi_agent = true
```

The `[agents]` limit counts spawned workers and excludes the primary thread, preserving no more than eight delegated workers. Explicit spawn model and effort values take precedence over `[agents]` defaults; Coordinator omits child defaults entirely and requires a fresh explicit selection for every attempt.

The skill does not rely on configuration alone. Before its first child spawn, it inspects the live subagent tool interface and blocks unless a child can be given explicit `model` and `reasoning_effort` values. Custom role TOMLs intentionally omit both settings so no project default can silently defeat live ROI routing.

## Project hooks and restart proof

Codex project hooks live in `.codex/hooks.json` and are loaded from the project configuration layer. Project command hooks may require user trust. Coordinator installs a marker-identifiable `SessionStart` command hook with matcher `startup|resume`, plus both POSIX `command` and Windows `commandWindows` forms.

A newly installed hook cannot prove that the same already-running process loaded it. The hook therefore issues a temporary activation token only during a later startup/resume event. The token binds the project path, installation generation, Codex session ID, reported model, event source, and issue time. The installer refuses execution readiness without a matching token and requires another restart if hook trust was not granted.

## Portable skill/package layout

Official skill discovery supports `.agents/skills/<name>/SKILL.md` with optional `scripts/`, `references/`, `assets/`, and `agents/openai.yaml`. The distributable ZIP uses those contents directly at archive root so it can be extracted into `~/.agents/skills/coordinator` or `%USERPROFILE%\.agents\skills\coordinator`.

The project bootstrap copies the same self-deploying package into `.agents/skills/coordinator` and copies custom agent definitions from `assets/project/.codex/agents` into `.codex/agents`. A manifest declares every project file and SHA-256 digest, allowing identical local and HTTP(S) installation behavior without a package manager.

## Model family, effort, and pricing

The parent uses `gpt-5.6-sol`/`max`. Children can use Sol, Terra, or Luna and any advertised effort in `none`, `low`, `medium`, `high`, `xhigh`, or `max`. The model router covers all eighteen combinations and chooses at runtime from complexity, ambiguity, criticality, coupling, novelty, determinism, token estimates, cache estimates, and budget mode.

OpenAI prices token categories rather than reasoning labels. `references/model-routing.md` therefore keeps official token rates separate from clearly marked planning factors. Every retry is rerouted from current evidence; a higher-priced route must beat the closest cheaper viable route in expected value.

## Backend identity compatibility

Codex subagent backends can expose different stable identifiers. Coordinator state accepts and retains `agent_id`, `task_name`, another stable agent name/path, and nickname. Resume reconciliation matches whatever identifiers the active backend actually returns rather than assuming one backend-specific field.

## Upgrade policy

Do not pin a Codex CLI release number in the skill. On upgrade:

1. recheck official docs and the current schema;
2. update config keys only from primary sources;
3. test whether the live spawn interface still exposes explicit child model/effort overrides;
4. update `VERSION` and regenerate `manifest.json`;
5. run the complete release suite from a clean extraction;
6. force project update/restart through normal manifest drift detection.
