from __future__ import annotations

import os
import shlex
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from cicd_harness.components import default_components
from cicd_harness.config import HarnessProfile, load_profile_argument
from cicd_harness.testing import HarnessRuntime, TestHarness


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("cicd-harness", "ephemeral CI/CD integration environment")
    group.addoption(
        "--cicd-profile",
        default=os.getenv("CICD_PROFILE", "modern"),
        help="profile name or YAML path (default: CICD_PROFILE or modern)",
    )
    group.addoption(
        "--cicd-workspace",
        default=None,
        help="harness workspace containing profiles/ (default: pytest root directory)",
    )
    group.addoption(
        "--cicd-artifacts",
        default=None,
        help="failure artifact directory (default: artifacts/cicd-harness/<cluster>)",
    )
    group.addoption(
        "--cicd-cluster-name",
        default=None,
        help="explicit Kind cluster name (default: a unique session-owned name)",
    )
    group.addoption(
        "--cicd-startup-timeout",
        type=int,
        default=900,
        help="per-component startup timeout in seconds (default: 900)",
    )
    group.addoption(
        "--cicd-components",
        default=os.getenv("CICD_COMPONENTS"),
        help=(
            "comma-separated component names; default is every component configured "
            "by the profile"
        ),
    )
    group.addoption(
        "--cicd-without-spinnaker",
        action="store_true",
        help="do not install Spinnaker for tests that only need controllers and infrastructure",
    )
    group.addoption(
        "--cicd-without-jenkins",
        action="store_true",
        help="do not install Jenkins for tests that do not exercise builds",
    )
    group.addoption(
        "--cicd-keep",
        action="store_true",
        default=os.getenv("CICD_KEEP", "").lower() in {"1", "true", "yes"},
        help="leave the unique cluster running after pytest for interactive debugging",
    )
    group.addoption(
        "--expose-endpoints",
        default=os.getenv("HARNESS_EXPOSE_ENDPOINTS"),
        help=(
            "comma-separated host endpoints to expose, or 'default'/'all'; "
            "for local interactive runs"
        ),
    )
    group.addoption(
        "--preserve-environment-on-failure",
        action="store_true",
        default=os.getenv("PRESERVE_ENVIRONMENT_ON_FAILURE", "").lower()
        in {"1", "true", "yes"},
        help="retain a failed test's namespace and cluster for host-side debugging",
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture(scope="session")
def cicd_harness_runtime(pytestconfig: pytest.Config) -> Iterator[HarnessRuntime]:
    workspace_option = pytestconfig.getoption("--cicd-workspace")
    workspace = Path(workspace_option or pytestconfig.rootpath).resolve()
    profile_option = str(pytestconfig.getoption("--cicd-profile"))
    profile = load_profile_argument(profile_option, workspace=workspace)

    explicit_name = pytestconfig.getoption("--cicd-cluster-name")
    cluster_name = explicit_name or _session_cluster_name(profile)
    profile = _with_cluster_name(profile, cluster_name)
    artifact_option = pytestconfig.getoption("--cicd-artifacts")
    artifact_root = Path(
        artifact_option or workspace / "artifacts" / "cicd-harness" / cluster_name
    ).resolve()
    terminal = pytestconfig.pluginmanager.get_plugin("terminalreporter")

    def report(message: str) -> None:
        if terminal is not None:
            terminal.write_line(message, yellow=True)

    runtime = HarnessRuntime(
        profile,
        workspace=workspace,
        artifact_root=artifact_root,
        component_names=_selected_component_names(pytestconfig, profile),
        keep=bool(pytestconfig.getoption("--cicd-keep")),
        preserve_environment_on_failure=bool(
            pytestconfig.getoption("--preserve-environment-on-failure")
        ),
        reporter=report,
    )
    runtime.start(timeout=int(pytestconfig.getoption("--cicd-startup-timeout")))
    requested_endpoints = _requested_endpoint_names(pytestconfig, runtime)
    if requested_endpoints is not None:
        try:
            exposed = runtime.host.expose_many(requested_endpoints)
        except Exception:
            runtime.stop()
            raise
        for endpoint in exposed:
            report(
                f"CI/CD harness endpoint: {endpoint.name}={endpoint.url} "
                f"({endpoint.spec.description})"
            )
    report(
        f"CI/CD harness ready: profile={profile.name}, "
        f"context={runtime.environment.cluster.context}, "
        f"components={','.join(runtime.environment.components.names) or 'none'}"
    )
    try:
        yield runtime
    finally:
        runtime.stop()
        if runtime.preserved_failures:
            namespaces = ",".join(
                item["namespace"] for item in runtime.preserved_failures
            )
            report(
                f"CI/CD harness preserved failure: context="
                f"{runtime.environment.cluster.context}, namespaces={namespaces}"
            )
            report(
                "Attach host endpoints: uv run cicd-harness expose "
                f"{shlex.quote(profile_option)} --context "
                f"{shlex.quote(runtime.environment.cluster.context)} --components "
                f"{shlex.quote(','.join(runtime.environment.components.names))}"
            )
            report(
                "Delete preserved environment: uv run cicd-harness stack-down "
                f"{shlex.quote(profile_option)} --cluster-name "
                f"{shlex.quote(runtime.profile.kind.cluster_name)}"
            )
            report("Use --maxfail=1 to prevent later tests from changing shared state.")
        elif runtime.keep:
            report(f"CI/CD harness kept running: {runtime.environment.cluster.context}")


@pytest.fixture
def harness(
    cicd_harness_runtime: HarnessRuntime,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterator[TestHarness]:
    test = cicd_harness_runtime.test_case(request.node.nodeid, tmp_path)
    try:
        test.start()
    except Exception:
        test.finish(failed=True)
        raise
    try:
        yield test
    finally:
        failed = any(
            getattr(getattr(request.node, f"rep_{phase}", None), "failed", False)
            for phase in ("setup", "call")
        )
        test.finish(failed=failed)


def _session_cluster_name(profile: HarnessProfile) -> str:
    suffix = f"pytest-{os.getpid()}-{uuid4().hex[:6]}"
    base = profile.kind.cluster_name[: 63 - len(suffix) - 1].rstrip("-")
    return f"{base}-{suffix}"


def _with_cluster_name(profile: HarnessProfile, name: str) -> HarnessProfile:
    return profile.model_copy(
        update={"kind": profile.kind.model_copy(update={"cluster_name": name})}
    )


def _selected_component_names(
    pytestconfig: pytest.Config,
    profile: HarnessProfile,
) -> set[str] | None:
    raw = pytestconfig.getoption("--cicd-components")
    names = (
        {item.strip() for item in str(raw).split(",") if item.strip()}
        if raw
        else None
    )
    if names is None and not any(
        pytestconfig.getoption(option)
        for option in ("--cicd-without-spinnaker", "--cicd-without-jenkins")
    ):
        return None
    if names is None:
        names = {component.name for component in default_components(profile)}
    if pytestconfig.getoption("--cicd-without-spinnaker"):
        names.discard("spinnaker")
    if pytestconfig.getoption("--cicd-without-jenkins"):
        names.discard("jenkins")
    return names


def _requested_endpoint_names(
    pytestconfig: pytest.Config,
    runtime: HarnessRuntime,
) -> tuple[str, ...] | None:
    raw = pytestconfig.getoption("--expose-endpoints")
    if not raw:
        return None
    value = str(raw).strip().lower()
    if value == "default":
        return runtime.host.catalog.default_names
    if value == "all":
        return runtime.host.catalog.names
    return tuple(item.strip() for item in str(raw).split(",") if item.strip())
