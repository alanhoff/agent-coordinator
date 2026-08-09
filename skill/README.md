# Coordinator operator guide

Coordinator is a current-user Codex skill for durable multi-agent workflows. It keeps the installed package, eight roles, Codex configuration, locks, and workflow state under your home directory—not in repositories you orchestrate.

Run `scripts/doctor.py preflight --repo PATH` before starting. Follow `SKILL.md` for session, workflow, graph, launch-reconciliation, and completion commands. Use `scripts/dashboard.py watch --once` for a terminal summary, `serve` for a loopback interactive view, or `render --out PATH` for a self-contained report.

Install and update only between Codex sessions:

```sh
python scripts/install.py ensure-global --between-sessions
python scripts/install.py status
python scripts/install.py check-updates
```

If an interrupted install is detected, run `recovery-status` and execute only the exact token-free follow-up command it returns. `doctor.py check` verifies package integrity, roles, owned configuration, privacy, state health, and available Codex capabilities.

Public documentation and releases: <https://github.com/alanhoff/agent-coordinator>
