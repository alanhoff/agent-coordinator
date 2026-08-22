# Coordinator operator entry points

`SKILL.md` is the workflow protocol. The installed commands are:

- `scripts/doctor.py preflight --repo PATH` for readiness checks.
- `scripts/dashboard.py watch --once`, `serve`, or `render --out PATH` for read-only observation.
- `scripts/install.py status` for installation state.
- `scripts/install.py ensure-global --between-sessions` for an explicitly requested installation.
- `scripts/install.py recovery-status` for the exact follow-up after an interrupted installation.

Documentation and releases: <https://github.com/alanhoff/agent-coordinator>
