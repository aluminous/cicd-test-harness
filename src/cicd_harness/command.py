from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cicd_harness.errors import CommandError

logger = logging.getLogger(__name__)
_SENSITIVE_FLAGS = {
    "--api-key",
    "--auth",
    "--client-secret",
    "--password",
    "--secret",
    "--token",
}
_URL_USERINFO = re.compile(r"(https?://)([^\s/@:]+):([^\s/@]+)@", re.IGNORECASE)


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
        self._redactions: set[str] = set()

    def add_redactions(self, *values: str) -> None:
        self._redactions.update(value for value in values if value)

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
        rendered = self._render_command(normalized)
        started = time.monotonic()
        logger.info("run: %s", rendered)
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
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            logger.error("timed out after %.1fs: %s", elapsed, rendered)
            raise CommandError(f"command timed out after {elapsed:.1f}s: {rendered}") from exc
        result = CommandResult(
            argv=normalized,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        elapsed = time.monotonic() - started
        log = logger.error if check and result.returncode != 0 else logger.info
        log("exit %d after %.1fs: %s", result.returncode, elapsed, rendered)
        if result.stdout:
            logger.debug("stdout from %s:\n%s", rendered, self._redact(result.stdout).rstrip())
        if result.stderr:
            logger.debug("stderr from %s:\n%s", rendered, self._redact(result.stderr).rstrip())
        if check and result.returncode != 0:
            raise CommandError(
                f"command failed ({result.returncode}): {rendered}\n"
                f"stdout:\n{self._redact(result.stdout)}\n"
                f"stderr:\n{self._redact(result.stderr)}"
            )
        return result

    def popen(
        self,
        argv: Sequence[str | Path],
        *,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        normalized = tuple(str(part) for part in argv)
        rendered = self._render_command(normalized)
        logger.info("start process: %s", rendered)
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

    def _render_command(self, argv: Sequence[str]) -> str:
        rendered: list[str] = []
        hide_next = False
        for part in argv:
            if hide_next:
                rendered.append("[REDACTED]")
                hide_next = False
                continue
            name, separator, _ = part.partition("=")
            if name.lower() in _SENSITIVE_FLAGS:
                rendered.append(f"{name}=[REDACTED]" if separator else part)
                hide_next = not separator
                continue
            rendered.append(self._redact(part))
        return shlex.join(rendered)

    def _redact(self, value: str) -> str:
        redacted = _URL_USERINFO.sub(r"\1[REDACTED]@", value)
        for secret in sorted(self._redactions, key=len, reverse=True):
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
