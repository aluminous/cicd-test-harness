from pathlib import Path

import pytest

from cicd_harness.config import load_profile
from cicd_harness.pytest_plugin import _session_cluster_name, _with_cluster_name


def test_pytest_profile_uses_unique_bounded_cluster_name() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)

    first = _session_cluster_name(profile)
    second = _session_cluster_name(profile)

    assert first.startswith("cicd-poc-modern-pytest-")
    assert first != second
    assert len(first) <= 63
    assert _with_cluster_name(profile, first).kind.cluster_name == first
    assert profile.kind.cluster_name == "cicd-poc-modern"


def test_failure_status_reaches_harness_finalizer(pytester: pytest.Pytester) -> None:
    finished = pytester.path / "finished.txt"
    pytester.makeconftest(
        f"""
import pytest
from pathlib import Path


class FakeCase:
    def start(self):
        pass

    def finish(self, *, failed):
        Path({str(finished)!r}).write_text(str(failed))


class FakeRuntime:
    def test_case(self, node_id, workdir):
        return FakeCase()


@pytest.fixture(scope="session")
def cicd_harness_runtime():
    return FakeRuntime()
"""
    )
    pytester.makepyfile(
        """
def test_application_failure(harness):
    assert False, "application did not converge"
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(failed=1)
    assert finished.read_text() == "True"
