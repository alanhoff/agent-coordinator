# Gates: remove retired surfaces and replace installation flow

Scope: Keep the current branch while removing every requested surface and orphan, then make `INSTALL.md` the sole end-user installation path.

- [x] G1: Work remains on the branch that was active when the task began (`codex/remove-update-release-gates`).
  CHECK: git branch --show-current
  EXPECT: codex/remove-update-release-gates
  EVIDENCE: codex/remove-update-release-gates

- [x] G2: Graphical workflow representation, webserver/dashboard, doctor, release surface, Codex hooks, restart checks, and `skill/scripts/install.*` are absent from the surviving product, tests, documentation, metadata, and automation.
  CHECK: python3.11 -c "from pathlib import Path; forbidden_paths=['.github/workflows/release.yml','.github/workflows/trusted-agentic-e2e.yml','CHANGELOG.md','VERSION','docs/assets','skill/references/dashboard.md','skill/scripts/dashboard.py','skill/scripts/doctor.py','src/coordinator/dashboard','src/coordinator/install','src/coordinator/cli/dashboard.py','src/coordinator/cli/doctor.py','tests/test_dashboard.py','tests/test_install.py','tests/test_package.py','tools/build_release.py']; existing=[p for p in forbidden_paths if Path(p).exists()]; banned=('dashboard','doctor','release','http.server','webbrowser','serve_forever','threadinghttpserver','127.0.0.1','localhost','--between-sessions','new_session_required','git-path hooks','restart','mermaid','webserver','codex_hooks','features.hooks','hooks.json'); files=[p for p in Path('.').rglob('*') if p.is_file() and '.git' not in p.parts and p.name not in {'GATES.md','.env'} and '__pycache__' not in p.parts and '.ruff_cache' not in p.parts]; hits=[f'{p}:{term}' for p in files for term in banned if term in p.read_text(errors='ignore').lower()]; assets=[p for p in Path('.').rglob('*') if p.is_file() and p.suffix.lower() in {'.png','.svg','.html','.zip'} and '.git' not in p.parts]; print('OK: retired surfaces absent' if not existing and not hits and not assets and not list(Path('skill/scripts').glob('install.*')) else f'FOUND: paths={existing}; text={hits}; assets={assets}; install={list(Path(\"skill/scripts\").glob(\"install.*\"))}')"
  EXPECT: OK: retired surfaces absent
  EVIDENCE: OK: retired surfaces absent

- [x] G3: `INSTALL.md` is self-contained and every surviving end-user install reference uses the exact prompt `Install https://github.com/alanhoff/agent-coordinator by following INSTALL.md` rather than a script, clone, package-manager, release, or manual-copy flow.
  CHECK: PYTHONDONTWRITEBYTECODE=1 python3.11 -m unittest tests.test_contract.PublicContractTests.test_end_user_install_surface_is_exact_prose tests.test_contract.PublicContractTests.test_agent_procedure_builds_a_complete_runnable_layout -v
  EXPECT: OK
  EVIDENCE: Ran 2 tests in 0.095s | OK

- [x] G4: Every repository path and workspace artifact has an explicit keep/remove disposition, all removal-created or pre-existing in-scope orphans have been pruned, and no obsolete references or generated debris remain.
  CHECK: python3.11 -c "from pathlib import Path; import re,subprocess; text=Path('GATES.md').read_text(); tracked=text.rsplit('\n## Tracked-path audit\n',1)[1].split('\n## Ignored workspace-artifact audit\n',1)[0]; rows=re.findall(r'^\| \x60([^\x60]+)\x60 \| (keep|remove) \|',tracked,re.M); audited={p for p,_ in rows}; baseline=set(subprocess.run(['git','ls-tree','-r','--name-only','6cf8d09'],check=True,text=True,capture_output=True).stdout.splitlines())|{'INSTALL.md'}; keep={p for p,d in rows if d=='keep'}; live={p.as_posix() for p in Path('.').rglob('*') if p.is_file() and '.git' not in p.parts and p.name!='.env'}; print('OK: orphan audit complete and clean' if len(rows)==len(audited) and audited==baseline and live==keep else f'FOUND: unaudited={sorted(baseline-audited)} extra_audit={sorted(audited-baseline)} unlisted_live={sorted(live-keep)} missing_live={sorted(keep-live)}')"
  EXPECT: OK: orphan audit complete and clean
  EVIDENCE: OK: orphan audit complete and clean

- [x] G5: The complete surviving validation suite passes after the removals.
  CHECK: PYTHONPYCACHEPREFIX=/tmp/agent-coordinator-gates-pycache python3.11 -m unittest discover -s tests -v && PYTHONPYCACHEPREFIX=/tmp/agent-coordinator-gates-pycache python3.11 -m compileall -q src skill/scripts tests && python -m ruff check --no-cache src skill/scripts tests && echo 'OK: full suite passed'
  EXPECT: OK: full suite passed
  EVIDENCE: Ran 24 tests in 0.886s | OK

- [x] G6: A final adversarial review of the diff and a second full-tree retired-surface/orphan scan find no missed removal, accidental deletion, stale asset, or unsupported installation path.
  EVIDENCE: Reviewed all 39 tracked diff entries plus new `INSTALL.md`; refuted the 61-path ledger against Git and the live tree (37 keep, 24 remove); read all 37 live files; parsed 14 Python files and 13 internal imports; found zero missing imports, unused non-dynamic definitions or methods, broken links, unreferenced skill references, unwired roles, placeholders, symlinks, empty directories, generated caches, or retired-surface hits. YAML, TOML, installed-layout execution, official Codex path/config contracts, and `git diff --check` also passed; ignored `.env` remained untouched as user-local data.

## Tracked-path audit

Baseline: commit `6cf8d09`, plus the new `INSTALL.md`. Every baseline path is classified once.

| Path | Disposition | Reason |
|---|---|---|
| `.github/workflows/ci.yml` | keep | Core lint and unit validation remain useful after pruning retired smoke steps. |
| `.github/workflows/release.yml` | remove | Dedicated release automation. |
| `.github/workflows/trusted-agentic-e2e.yml` | remove | Its setup and assertions depend on retired installation, dashboard, and hook surfaces. |
| `.gitignore` | keep | Retain user-secret and generated-cache exclusions; prune obsolete output exclusions. |
| `CHANGELOG.md` | remove | Versioned release-history surface. |
| `CONTRIBUTING.md` | keep | Rewrite around the surviving runtime and checks. |
| `GATES.md` | keep | Required hard-gate ledger and audit evidence. |
| `INSTALL.md` | keep | New agent-facing prose installation contract. |
| `LICENSE` | keep | License for source and installed copies. |
| `README.md` | keep | Rewrite to the sole end-user prose installation path and surviving features. |
| `SECURITY.md` | keep | Rewrite to surviving security boundaries. |
| `VERSION` | remove | Standalone release/version marker. |
| `docs/assets/agent-coordinator-hero.png` | remove | Graphical workflow representation. |
| `docs/assets/dashboard.png` | remove | Retired dashboard capture. |
| `pyproject.toml` | keep | Retain Ruff configuration; remove package/release metadata. |
| `skill/README.md` | keep | Reduce to surviving operator entry points. |
| `skill/SKILL.md` | keep | Remove retired startup, observation, and reference instructions. |
| `skill/agents/openai.yaml` | keep | Skill discovery metadata remains live. |
| `skill/agents/roles/architect.toml` | keep | Live routed specialist role. |
| `skill/agents/roles/designer.toml` | keep | Live routed specialist role, unrelated to workflow graphics. |
| `skill/agents/roles/documenter.toml` | keep | Live routed specialist role. |
| `skill/agents/roles/fixer.toml` | keep | Live routed specialist role. |
| `skill/agents/roles/implementer.toml` | keep | Live routed specialist role. |
| `skill/agents/roles/researcher.toml` | keep | Live routed specialist role. |
| `skill/agents/roles/reviewer.toml` | keep | Live routed specialist role. |
| `skill/agents/roles/validator.toml` | keep | Live routed specialist role. |
| `skill/references/dashboard.md` | remove | Dashboard-only documentation. |
| `skill/references/model-routing.md` | keep | Live routing contract. |
| `skill/references/state-schema.md` | keep | Live durable-state contract; remove the retired observer reference. |
| `skill/references/workflow-protocol.md` | keep | Live controller protocol; remove the retired diagnostic step. |
| `skill/scripts/coordinator_state.py` | keep | Live state CLI adapter. |
| `skill/scripts/dashboard.py` | remove | Dashboard adapter. |
| `skill/scripts/doctor.py` | remove | Doctor adapter. |
| `skill/scripts/install.cmd` | remove | End-user installer wrapper. |
| `skill/scripts/install.py` | remove | End-user installer adapter. |
| `skill/scripts/install.sh` | remove | End-user installer wrapper. |
| `skill/scripts/model_router.py` | keep | Live routing CLI adapter. |
| `src/coordinator/__init__.py` | keep | Runtime package marker; remove the release version constant. |
| `src/coordinator/cli/__init__.py` | keep | Live CLI package marker. |
| `src/coordinator/cli/dashboard.py` | remove | Dashboard CLI and browser/server entry point. |
| `src/coordinator/cli/doctor.py` | remove | Doctor CLI. |
| `src/coordinator/cli/outcome.py` | keep | Shared live CLI outcomes; remove restart-only output. |
| `src/coordinator/cli/routing.py` | keep | Live routing CLI. |
| `src/coordinator/cli/state.py` | keep | Live state CLI. |
| `src/coordinator/dashboard/__init__.py` | remove | Dashboard package marker. |
| `src/coordinator/dashboard/view.py` | remove | Graphical rendering and webserver implementation. |
| `src/coordinator/install/__init__.py` | remove | Installer package marker. |
| `src/coordinator/install/doctor.py` | remove | Doctor implementation coupled to installation. |
| `src/coordinator/install/standalone.py` | remove | Standalone installer, restart gate, recovery, and release acquisition. |
| `src/coordinator/routing/__init__.py` | keep | Live routing package marker. |
| `src/coordinator/routing/selector.py` | keep | Live route selection. |
| `src/coordinator/state/__init__.py` | keep | Live state package exports. |
| `src/coordinator/state/store.py` | keep | Live workflow-state owner; remove redundant release version data. |
| `tests/support.py` | remove | Used only by installer/package tests. |
| `tests/test_contract.py` | keep | Rewrite as the hard contract for the surviving surface. |
| `tests/test_dashboard.py` | remove | Dashboard-only tests. |
| `tests/test_install.py` | remove | Installer/doctor/restart-only tests. |
| `tests/test_package.py` | remove | Release-assembly-only tests. |
| `tests/test_routing.py` | keep | Live routing tests. |
| `tests/test_state.py` | keep | Live state tests. |
| `tools/build_release.py` | remove | Release asset builder. |

## Ignored workspace-artifact audit

| Path or class | Disposition | Reason |
|---|---|---|
| `.env` | keep | User-local ignored data; no surviving product path owns it. |
| `.ruff_cache/**` | remove | Generated linter cache. |
| `**/__pycache__/**` and `**/*.pyc` | remove | Generated bytecode, including stale files for already-deleted benchmark and verifier sources. |
