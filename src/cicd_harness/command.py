from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cicd_harness.errors import CommandError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def __init__(self, *, cwd: Path, base_env: Mapping[str, str] | None = None) -> None:
        self.cwd = cwd
        self.base_env = dict(base_env or {})

    def run(
        self,
        argv: Sequence[str | Path],
        *,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> CommandResult:
        normalized = tuple(str(part) for part in argv)
        process_env = os.environ.copy()
        process_env.update(self.base_env)
        process_env.update(env or {})
        try:
            completed = subprocess.run(
                normalized,
                cwd=self.cwd,
                env=process_env,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CommandError(f"required executable was not found: {normalized[0]}") from exc
        result = CommandResult(
            argv=normalized,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            rendered = " ".join(normalized)
            raise CommandError(
                f"command failed ({result.returncode}): {rendered}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def popen(
        self,
        argv: Sequence[str | Path],
        *,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        normalized = tuple(str(part) for part in argv)
        process_env = os.environ.copy()
        process_env.update(self.base_env)
        process_env.update(env or {})
        try:
            return subprocess.Popen(
                normalized,
                cwd=self.cwd,
                env=process_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise CommandError(f"required executable was not found: {normalized[0]}") from exc
