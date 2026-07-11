from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from cicd_harness.component import ComponentGraph, ComponentState, EnvironmentContext
from cicd_harness.components import default_components, select_components
from cicd_harness.config import HarnessProfile, load_profile
from cicd_harness.environment import HarnessEnvironment
from cicd_harness.errors import HarnessError
from cicd_harness.testing import HarnessRuntime


@dataclass
class FakeComponent:
    name: str
    dependencies: frozenset[str] = frozenset()
    events: list[str] = field(default_factory=list)
    fail_start: bool = False
    fail_stop: bool = False

    def start(self, context: EnvironmentContext, *, timeout: int) -> None:
        self.events.append(f"start:{self.name}:{timeout}")
        if self.fail_start:
            raise RuntimeError("start failure")

    def stop(self, context: EnvironmentContext) -> None:
        self.events.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError("stop failure")


def _context() -> Any:
    return SimpleNamespace()


def test_graph_starts_dependencies_first_and_stops_in_reverse() -> None:
    events: list[str] = []
    application = FakeComponent("application", frozenset({"database"}), events)
    database = FakeComponent("database", events=events)
    graph = ComponentGraph([application, database])

    graph.start(_context(), timeout=42)

    assert graph.order == ("database", "application")
    assert graph.snapshot() == {"application": "ready", "database": "ready"}
    graph.stop(_context())
    assert events == [
        "start:database:42",
        "start:application:42",
        "stop:application",
        "stop:database",
    ]
    assert graph.snapshot() == {"application": "stopped", "database": "stopped"}
    graph.stop(_context())
    assert len(events) == 4


def test_graph_preserves_failed_state_for_diagnostics_then_stops_attempted_components() -> None:
    events: list[str] = []
    ready = FakeComponent("ready", events=events)
    failed = FakeComponent(
        "failed",
        frozenset({"ready"}),
        events,
        fail_start=True,
    )
    pending = FakeComponent("pending", frozenset({"failed"}), events)
    graph = ComponentGraph([ready, failed, pending])

    with pytest.raises(HarnessError, match="component 'failed' failed to start"):
        graph.start(_context(), timeout=10)

    assert graph.state("ready") == ComponentState.READY
    assert graph.state("failed") == ComponentState.FAILED
    assert graph.state("pending") == ComponentState.PENDING
    graph.stop(_context())
    assert events[-2:] == ["stop:failed", "stop:ready"]


def test_graph_rejects_duplicate_missing_and_cyclic_components() -> None:
    with pytest.raises(HarnessError, match="duplicate"):
        ComponentGraph([FakeComponent("same"), FakeComponent("same")])
    with pytest.raises(HarnessError, match="missing dependencies: absent"):
        ComponentGraph([FakeComponent("child", frozenset({"absent"}))])
    with pytest.raises(HarnessError, match="dependency cycle"):
        ComponentGraph(
            [
                FakeComponent("first", frozenset({"second"})),
                FakeComponent("second", frozenset({"first"})),
            ]
        )


def test_default_component_selection_preserves_concrete_stack() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)

    complete = default_components(profile)
    reduced = default_components(profile, include_jenkins=False, include_spinnaker=False)

    assert [component.name for component in complete] == [
        "argo-rollouts",
        "istio",
        "wiremock",
        "gitea",
        "jenkins",
        "spinnaker",
    ]
    assert [component.name for component in reduced] == [
        "argo-rollouts",
        "istio",
        "wiremock",
        "gitea",
    ]

    selected = select_components(profile, {"wiremock", "jenkins"})
    assert [component.name for component in selected] == ["wiremock", "jenkins"]
    with pytest.raises(HarnessError, match="not configured: unknown"):
        select_components(profile, {"unknown"})


def test_explicit_selection_still_validates_component_dependencies() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)

    with pytest.raises(HarnessError, match="missing dependencies: gitea"):
        ComponentGraph(select_components(profile, {"spinnaker"}))


def test_environment_accepts_an_explicit_empty_component_graph() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)

    environment = HarnessEnvironment(profile, workspace=workspace, components=[])

    assert environment.components.order == ()


def test_profile_can_describe_only_the_reusable_cluster_substrate() -> None:
    profile = HarnessProfile.model_validate(
        {
            "name": "substrate-only",
            "runtime": {"provider": "docker"},
            "kind": {
                "version": "0.31.0",
                "binary": "kind",
                "node_image": "kindest/node:v1.31.14@sha256:" + "a" * 64,
                "cluster_name": "substrate-only",
            },
        }
    )

    assert default_components(profile) == []
    assert profile.argo_rollouts is None
    assert profile.istio is None
    assert profile.infra is None
    assert profile.jenkins is None
    assert profile.spinnaker is None


def test_profile_can_select_wiremock_without_gitea() -> None:
    profile = HarnessProfile.model_validate(
        {
            "name": "http-mocking",
            "runtime": {"provider": "docker"},
            "kind": {
                "version": "0.31.0",
                "binary": "kind",
                "node_image": "kindest/node:v1.31.14@sha256:" + "a" * 64,
                "cluster_name": "http-mocking",
            },
            "infra": {"wiremock": {"image": "wiremock/wiremock:3.13.1"}},
        }
    )

    assert [component.name for component in default_components(profile)] == [
        "wiremock"
    ]
    assert profile.infra is not None
    assert profile.infra.gitea is None


def test_runtime_accepts_a_concrete_custom_component_list(tmp_path: Path) -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)
    custom = FakeComponent("custom-controller")

    runtime = HarnessRuntime(
        profile,
        workspace=workspace,
        artifact_root=tmp_path / "artifacts",
        components=[custom],
    )

    assert runtime.environment.components.names == ("custom-controller",)


def test_runtime_preserved_failure_closes_forwards_but_keeps_cluster() -> None:
    runtime = object.__new__(HarnessRuntime)
    runtime.host = Mock()
    runtime.started = True
    runtime.keep = False
    runtime.preserved_failures = [
        {"test": "tests/test_release.py::test_failure", "namespace": "test-failure"}
    ]
    runtime.environment = SimpleNamespace(down=Mock())

    runtime.stop()

    runtime.host.close.assert_called_once_with()
    runtime.environment.down.assert_not_called()


def test_runtime_can_preserve_partial_environment_after_startup_failure() -> None:
    runtime = object.__new__(HarnessRuntime)
    runtime.environment = SimpleNamespace(
        up=Mock(side_effect=RuntimeError("not ready")),
        down=Mock(),
    )
    runtime.capture = Mock()
    runtime.preserve_failure = Mock()
    runtime.preserve_environment_on_failure = True
    runtime.keep = False
    runtime.started = False

    with pytest.raises(RuntimeError, match="not ready"):
        runtime.start(timeout=12)

    runtime.capture.assert_called_once_with(
        "session-startup",
        metadata={"phase": "startup"},
    )
    runtime.preserve_failure.assert_called_once_with(
        node_id="<session-startup>",
        namespace="",
    )
    runtime.environment.down.assert_not_called()
