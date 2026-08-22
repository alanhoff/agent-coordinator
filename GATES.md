# Gates: simplify release and CI lifecycle

Scope: remove skill update detection and release manifest/hash gates, deduplicate documentation, and keep CI tests only on each platform's latest runner plus the minimum supported Python.

- [x] G1: No runtime, installer, CLI, test, or documentation path detects or advertises skill updates.
  CHECK: python -c "from pathlib import Path; roots=[Path('src'),Path('skill'),Path('.github'),Path('tools'),Path('README.md'),Path('CONTRIBUTING.md'),Path('CHANGELOG.md')]; files=[p for r in roots for p in ([r] if r.is_file() else r.rglob('*')) if p.is_file() and '__pycache__' not in p.parts]; terms=('check-updates','update_available','available_version','up_to_date'); hits=[str(p) for p in files if any(term in p.read_text(errors='ignore').lower() for term in terms)]; print('OK: no skill update detection' if not hits else 'FOUND: '+', '.join(hits))"
  EXPECT: OK: no skill update detection
  EVIDENCE: OK: no skill update detection

- [x] G2: Release creation and CI/CD contain no manifest/hash integrity gate.
  CHECK: python -c "from pathlib import Path; files=[*Path('.github/workflows').glob('*.yml'),*Path('tools').glob('*.py'),Path('src/coordinator/install/standalone.py'),Path('README.md'),Path('CONTRIBUTING.md'),Path('CHANGELOG.md'),Path('skill/README.md')]; banned=('manifest.json','sha256sums','verify_release','source-sha256','external_manifest','exact_sha256'); release_files=[*Path('.github/workflows').glob('*.yml'),*Path('tools').glob('*.py')]; hits=[str(p) for p in files if p.exists() and any(t in p.read_text(errors='ignore').lower() for t in banned)]+[str(p) for p in release_files if p.exists() and any(t in p.read_text(errors='ignore').lower() for t in ('hashlib','checksum','sha256'))]; print('OK: no manifest/hash release gate' if not hits else 'FOUND: '+', '.join(sorted(set(hits))))" && PYTHONDONTWRITEBYTECODE=1 python3.11 -m unittest tests.test_package.PackageTests.test_new_tracked_file_is_packaged_without_an_inventory_update -v
  EXPECT: OK: no manifest/hash release gate
  EVIDENCE: Ran 1 test in 0.127s | OK

- [x] G3: CI tests use only the latest runner for every supported platform and only the minimum supported Python version.
  CHECK: PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_contract.PublicContractTests.test_ci_uses_latest_platforms_with_only_minimum_python -v
  EXPECT: OK
  EVIDENCE: Ran 1 test in 0.001s | OK

- [x] G4: Duplicated explanatory text is removed while canonical documentation remains coherent.
  CHECK: python -c "from pathlib import Path; from collections import defaultdict; paths=[Path('README.md'),Path('CONTRIBUTING.md'),Path('CHANGELOG.md'),Path('SECURITY.md'),Path('skill/README.md'),Path('skill/SKILL.md'),*Path('skill/references').glob('*.md')]; seen=defaultdict(list); [(seen[' '.join(block.split())].append(str(p))) for p in paths for block in p.read_text().split('\n\n') if len(' '.join(block.split())) >= 80]; duplicates={text:names for text,names in seen.items() if len(set(names)) > 1}; print('OK: no duplicated documentation blocks' if not duplicates else 'FOUND: '+str(duplicates))"
  EXPECT: OK: no duplicated documentation blocks
  EVIDENCE: OK: no duplicated documentation blocks

- [x] G5: The complete automated test and static-check suite passes.
  CHECK: PYTHONPYCACHEPREFIX=/tmp/agent-coordinator-gates-pycache python3.11 -m unittest discover -s tests -v && PYTHONPYCACHEPREFIX=/tmp/agent-coordinator-gates-pycache python3.11 -m compileall -q src skill/scripts tools tests && python -m ruff check --no-cache src skill/scripts tools tests && echo 'OK: full suite passed'
  EXPECT: OK: full suite passed
  EVIDENCE: Ran 48 tests in 4.277s | OK

- [x] G6: A final repository-wide adversarial search finds no residual implementation or reference in the requested scope.
  EVIDENCE: Audited 54 text files out of 56 visible project files; production hits were 0 for update detection and 0 for release manifest/hash gates. The builder contains 0 exact-inventory symbols and its extension test passed. All 3 workflows parsed as YAML, exposed only Python 3.11, and CI listed ubuntu-latest, macos-latest, and windows-latest. Duplicate documentation blocks: 0. `git diff --check` passed.
