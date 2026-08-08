# Agent Coordinator

Agent Coordinator is a durable multi-agent workflow coordinator for Codex. Version 3 is a breaking, current-user global installation: its package, eight role definitions, Codex configuration, recovery data, and workflow state live under your home directory. Repositories being orchestrated receive only the task changes you authorize—no Coordinator hooks, state, lock, role, or configuration files.

The runtime supports Python 3.11+ on Linux, macOS, and Windows and uses only the Python standard library. Releases are deterministic, closed-inventory ZIPs published through public GitHub Releases.

## Install

Close running Codex sessions first. Download these public assets from the latest release:

- [`install.py`](https://github.com/alanhoff/agent-coordinator/releases/latest/download/install.py)
- [`manifest.json`](https://github.com/alanhoff/agent-coordinator/releases/latest/download/manifest.json)
- [`SHA256SUMS`](https://github.com/alanhoff/agent-coordinator/releases/latest/download/SHA256SUMS)

Verify the downloaded `install.py` against `SHA256SUMS`, then run:

```sh
python3 install.py ensure-global --between-sessions
```

On Windows use `python` when that is the Python 3.11+ launcher. The installer anonymously downloads and verifies `coordinator-latest.zip`, checks the standalone and ZIP-root manifests are byte-identical, validates the exact inventory and safe archive layout, then commits one recoverable current-user transaction.

A successful change reports that a new Codex session is required. Start that new session before orchestration.

### Installed locations

| Location | Coordinator ownership |
|---|---|
| `~/.agents/skills/coordinator` | Verified package |
| `~/.codex/agents/coordinator` | Eight role definitions |
| `~/.codex/config.toml` | Marked semantic agent/feature keys only |
| `~/.agent-coordinator` | Private metadata, locks, recovery, sessions, and workflow state |

Existing unrelated Codex configuration is preserved. An ambiguous file, symlink/reparse point, unknown target tree, or unsafe ownership condition is reported without adoption.

### Upgrading from version 2

Version 3 does not adopt, update, or silently delete a v2 project-local installation. First use version control to review and remove the old Coordinator-only project commit or files while preserving your own repository configuration and changes. Then install version 3 globally between sessions. Confirm `install.py status` and `doctor.py preflight --repo PATH` before orchestration; v3 will not create replacement files in that repository.

## Operate

The installed `SKILL.md` is the controller protocol. Useful read-only checks are:

```sh
python3 ~/.agents/skills/coordinator/scripts/install.py status
python3 ~/.agents/skills/coordinator/scripts/doctor.py check
python3 ~/.agents/skills/coordinator/scripts/dashboard.py watch --once
```

The eight roles are `architect`, `designer`, `documenter`, `fixer`, `implementer`, `researcher`, `reviewer`, and `validator`. Role files do not pin a model or reasoning effort; the controller routes each launch from validated task evidence.

Workflow mutations use a private controller session file, expected prior revision, and unique mutation ID. State records launch claims before provider calls, distinguishes uncertain responses that require reconciliation, fences old controller epochs, and atomically commits complete validated snapshots. Read-only status and dashboard commands never initialize, repair, lock, cache, or clean state.

### Dashboard

```sh
python3 ~/.agents/skills/coordinator/scripts/dashboard.py serve --open
python3 ~/.agents/skills/coordinator/scripts/dashboard.py render --workflow-id ID --out report.html
```

`serve` binds only `127.0.0.1`, protects requests with an in-memory capability, exposes no mutating endpoint, and retains bounded replay only in memory. `render` creates a self-contained report at the explicit output path. Stored workflow text is treated as untrusted in both views.

## Update and recovery

Between sessions, check and update anonymously:

```sh
python3 ~/.agents/skills/coordinator/scripts/install.py check-updates
python3 ~/.agents/skills/coordinator/scripts/install.py ensure-global --between-sessions
```

`check-updates` exits 1 when an update is available. Downgrades are rejected. For a verified development build, `ensure-global` also accepts `--source PATH` for a package directory or ZIP. A custom network ZIP requires both a credential-free HTTPS `--source-url` and its exact `--source-sha256`.

If an interruption leaves a journal, first perform the zero-write status pass:

```sh
python3 ~/.agents/skills/coordinator/scripts/install.py recovery-status --json
```

Run only the exact token-free rollback command it returns. Recovery stops unchanged if transaction or digest evidence differs.

## Build and verify a release

From a clean source commit:

```sh
python3 tools/build_release.py --output /empty/output/directory
python3 tools/verify_release.py --release-dir /empty/output/directory
```

Two builds from the same commit produce byte-identical ZIPs, manifests, installer assets, and checksums. The independent verifier reconstructs archive inventory, content modes, source commit/version binding, safe paths, manifest identity, and standalone-installer identity.

## Troubleshooting

- Exit 1 is a valid negative state such as drift or an available update.
- Exit 2 means invalid invocation or rejected semantic transition.
- Exit 20 means safety, concurrency, recovery, network, or I/O evidence needs operator attention.
- Run `doctor.py preflight --repo PATH --json` before a workflow to inspect Git, installation integrity, roles/config, Codex capability discovery, privacy, recovery, and state health.
- Never place bearer files in a repository. If a controller is replaced, use `controller-takeover`, then explicit `resume`.
- Do not delete unknown recovery or lock paths. Coordinator intentionally refuses ambiguous automatic takeover and cleanup.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [CHANGELOG.md](CHANGELOG.md). This project is available under the [MIT License](LICENSE).
