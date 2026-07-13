import logging
from pathlib import Path

import pytest

from cicd_harness.command import CommandRunner
from cicd_harness.errors import CommandError


def test_command_logs_timing_and_redacts_commands_and_failure_output(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = CommandRunner(cwd=tmp_path)
    runner.add_redactions("top-secret-value")
    caplog.set_level(logging.DEBUG, logger="cicd_harness.command")

    with pytest.raises(CommandError) as failure:
        runner.run(
            [
                "sh",
                "-c",
                "echo top-secret-value; echo http://user:password@example.invalid >&2; exit 7",
            ]
        )

    combined = caplog.text + str(failure.value)
    assert "top-secret-value" not in combined
    assert "user:password" not in combined
    assert "[REDACTED]" in combined
    assert "exit 7 after" in caplog.text


def test_sensitive_flag_value_is_not_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="cicd_harness.command")
    CommandRunner(cwd=tmp_path).run(["sh", "-c", "exit 0", "--password", "secret"])

    assert "--password '[REDACTED]'" in caplog.text
    assert " secret" not in caplog.text
