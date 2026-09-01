from __future__ import annotations

import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "skill" / "scripts" / "lib"
sys.path.insert(0, str(RUNTIME))

from coordinator.state import proofs  # noqa: E402
from coordinator.state.proofs import ProofExecutionError, run_proof_command  # noqa: E402


def python_command(body: str) -> str:
    arguments = [sys.executable, "-c", body]
    return (
        subprocess.list2cmdline(arguments)
        if os.name == "nt"
        else shlex.join(arguments)
    )


class ProofRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = pathlib.Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_shell_uses_repository_cwd_inherited_environment_and_combined_output(self) -> None:
        command = python_command(
            "import os, pathlib, sys; "
            "print(pathlib.Path.cwd().name, flush=True); "
            "print(os.environ['COORDINATOR_PROOF_TEST'], flush=True); "
            "print('stderr-line', file=sys.stderr, flush=True)"
        )
        with mock.patch.dict(
            os.environ, {"COORDINATOR_PROOF_TEST": "inherited-value"}, clear=False
        ):
            result = run_proof_command(command, repository=str(self.repository))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            result.output.splitlines(),
            [self.repository.name, "inherited-value", "stderr-line"],
        )

        failed = run_proof_command(
            python_command("print('failure-output'); raise SystemExit(7)"),
            repository=str(self.repository),
        )
        self.assertEqual(
            (failed.exit_code, failed.output), (7, f"failure-output{os.linesep}")
        )

    def test_output_limit_invalid_utf8_timeout_and_launch_error_are_rejected(self) -> None:
        cases = (
            (
                "overflow",
                python_command("import os; os.write(1, b'x' * 33)"),
                {"output_limit": 32},
                "exceeds 32 bytes",
            ),
            (
                "invalid-utf8",
                python_command("import os; os.write(1, b'\\xff')"),
                {},
                "not valid UTF-8",
            ),
            (
                "timeout",
                python_command("import time; time.sleep(5)"),
                {"timeout": 0.05},
                "timeout",
            ),
        )
        for name, command, options, message in cases:
            with self.subTest(case=name), self.assertRaisesRegex(
                ProofExecutionError, message
            ):
                run_proof_command(
                    command, repository=str(self.repository), **options
                )

        for error in (OSError("launch failed"), ValueError("embedded null byte")):
            with self.subTest(launch_error=type(error).__name__), mock.patch.object(
                proofs.subprocess, "Popen", side_effect=error
            ), self.assertRaisesRegex(ProofExecutionError, "unable to start"):
                run_proof_command("anything", repository=str(self.repository))


if __name__ == "__main__":
    unittest.main()
