#!/usr/bin/env python3
"""Installed adapter for Coordinator diagnostics."""
import pathlib
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "lib"))
from coordinator.cli.doctor import main  # noqa: E402

raise SystemExit(main())
