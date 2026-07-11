class HarnessError(RuntimeError):
    """Base error raised by the harness."""


class CommandError(HarnessError):
    """A managed child process returned a non-zero exit status."""


class ReadinessError(HarnessError):
    """A component did not become ready before its deadline."""


class VerificationError(HarnessError):
    """A test-facing interaction contract was not satisfied."""

