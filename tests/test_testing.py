from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cicd_harness.command import CommandResult
from cicd_harness.components import WireMockComponent
from cicd_harness.errors import HarnessError, VerificationError
from cicd_harness.gitea import GiteaRepository, GitWorkspace
from cicd_harness.jenkins import JenkinsClient, JenkinsLibrary
from cicd_harness.rollouts import ReplicaSetState
from cicd_harness.spinnaker import SpinnakerClient
from cicd_harness.testing import (
    GitAPI,
    JenkinsAPI,
    RolloutHandle,
    SpinnakerAPI,
    TestHarness,
    TestRepository,
)
from cicd_harness.wiremock import WireMockClient


class RolloutKubectl:
    def get_json(self, resource: str, *_: str):
        if resource.startswith("rollout"):
            return {
                "metadata": {
                    "name": "payments",
                    "namespace": "apps",
                    "annotations": {"harness.cicd/revision": "raw-v3"},
                },
                "spec": {
                    "template": {
                        "metadata": {"annotations": {"release": "candidate"}}
                    },
                    "strategy": {
                        "canary": {
                            "trafficRouting": {
                                "istio": {
                                    "virtualService": {
                                        "name": "payments",
                                        "routes": ["primary"],
                                    }
                                }
                            }
                        }
                    }
                },
                "status": {
                    "phase": "Progressing",
                    "stableRS": "stable-hash",
                    "currentPodHash": "canary-hash",
                    "pauseConditions": [{"reason": "CanaryPauseStep"}],
                },
            }
        if resource == "replicasets":
            return {
                "items": [
                    self._replica_set("payments-stable", "stable-hash", 2),
                    self._replica_set("payments-canary", "canary-hash", 2),
                    self._replica_set(
                        "payments-old",
                        "old-hash",
                        1,
                        deadline="2030-01-01T00:00:00Z",
                    ),
                ]
            }
        if resource.startswith("virtualservice"):
            return {
                "spec": {
                    "http": [
                        {
                            "name": "primary",
                            "route": [
                                {"destination": {"host": "payments-stable"}, "weight": 50},
                                {"destination": {"host": "payments-canary"}, "weight": 50},
                            ],
                        }
                    ]
                }
            }
        raise AssertionError(resource)

    @staticmethod
    def _replica_set(name: str, pod_hash: str, desired: int, *, deadline: str | None = None):
        annotations = {"scale-down-deadline": deadline} if deadline else {}
        return {
            "metadata": {
                "name": name,
                "labels": {"rollouts-pod-template-hash": pod_hash},
                "annotations": annotations,
                "ownerReferences": [{"kind": "Rollout", "name": "payments"}],
            },
            "spec": {"replicas": desired},
            "status": {"readyReplicas": desired},
        }


def test_git_workspace_inherits_harness_tls_environment(tmp_path: Path) -> None:
    git = GitWorkspace(
        tmp_path,
        base_env={
            "CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY": "1",
            "GIT_SSL_NO_VERIFY": "true",
        },
    )

    assert git.runner.base_env["CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY"] == "1"
    assert git.runner.base_env["GIT_SSL_NO_VERIFY"] == "true"


def test_rollout_handle_exposes_canary_and_scale_down_assertions() -> None:
    rollout = RolloutHandle(RolloutKubectl(), namespace="apps", name="payments")  # type: ignore[arg-type]

    snapshot = rollout.wait_for_canary(weights=(50, 50), timeout=0.1)

    assert snapshot.paused
    assert snapshot.annotations["harness.cicd/revision"] == "raw-v3"
    assert snapshot.template_annotations["release"] == "candidate"
    assert tuple(item.weight for item in snapshot.traffic) == (50, 50)
    rollout.assert_replica_sets(stable=1, canary=1, old=1)
    rollout.assert_traffic_weights(50, 50)
    pending = rollout.assert_scale_down_pending(count=1)
    assert isinstance(pending.replica_sets[0], ReplicaSetState)


def test_rollout_assertion_error_contains_observed_state() -> None:
    rollout = RolloutHandle(RolloutKubectl(), namespace="apps", name="payments")  # type: ignore[arg-type]

    with pytest.raises(VerificationError, match="Observed state"):
        rollout.assert_traffic_weights(100, 0)


def test_jenkins_api_asserts_success_and_tracks_console() -> None:
    client = Mock(spec=JenkinsClient)
    client.trigger.return_value = "queue-4"
    client.wait_build.return_value = {"number": 12, "result": "SUCCESS", "building": False}
    client.console.return_value = "Finished: SUCCESS"
    harness = SimpleNamespace(
        _services=SimpleNamespace(jenkins=lambda: client),
        _jenkins_runs=[],
    )

    build = JenkinsAPI(harness).run("deploy", parameters={"REVISION": "abc"})  # type: ignore[arg-type]

    assert build.number == 12
    assert build.result == "SUCCESS"
    assert harness._jenkins_runs[0]["console"] == "Finished: SUCCESS"


def test_jenkins_api_observes_build_triggered_by_application() -> None:
    client = Mock(spec=JenkinsClient)
    client.builds.return_value = [{"number": 7}]
    client.wait_for_new_build.return_value = {
        "number": 8,
        "result": "SUCCESS",
        "building": False,
    }
    client.console.return_value = "Application-triggered build"
    harness = SimpleNamespace(
        _services=SimpleNamespace(jenkins=lambda: client),
        _jenkins_runs=[],
    )
    api = JenkinsAPI(harness)  # type: ignore[arg-type]

    baseline = api.latest_build_number("payments/main")
    build = api.wait_for_build("payments/main", after=baseline)

    assert baseline == 7
    assert build.number == 8
    assert build.queue_id is None
    assert harness._jenkins_runs[0]["observedExternally"] is True


def test_jenkins_api_creates_seeded_library_repository_and_configures_it(
    tmp_path: Path,
) -> None:
    repository = TestRepository(
        remote=GiteaRepository(owner="harness", name="example-library"),
        path=tmp_path,
        git=Mock(spec=GitWorkspace),
        revision="library-commit",
    )
    git_api = Mock()
    git_api.create_repository.return_value = repository
    client = Mock(spec=JenkinsClient)
    client.configure_library.return_value = JenkinsLibrary(
        name="example",
        repository_url=repository.clone_url,
        default_version="main",
        implicit=False,
        allow_version_override=True,
        include_in_changesets=False,
    )
    harness = SimpleNamespace(
        git=git_api,
        unique_name=lambda prefix: f"{prefix}-unique",
        _services=SimpleNamespace(jenkins=lambda: client),
        _jenkins_libraries=set(),
        _owned_jenkins_libraries=set(),
    )

    fixture = JenkinsAPI(harness).create_library(  # type: ignore[arg-type]
        "example",
        template="jenkins/library",
    )

    assert fixture.repository is repository
    assert fixture.configuration.name == "example"
    git_api.create_repository.assert_called_once_with(
        name="example-library-unique",
        template="jenkins/library",
        files=None,
        variables=None,
        message="Create Jenkins shared library example",
    )
    assert harness._owned_jenkins_libraries == {"example"}


def test_spinnaker_api_asserts_terminal_status_and_tracks_execution() -> None:
    client = Mock(spec=SpinnakerClient)
    client.trigger.return_value = "execution-9"
    client.wait_execution.return_value = {"id": "execution-9", "status": "SUCCEEDED"}
    harness = SimpleNamespace(
        _services=SimpleNamespace(spinnaker=lambda: client),
        _spinnaker_runs=[],
    )
    pipeline = {"application": "payments", "name": "deploy", "stages": []}

    execution = SpinnakerAPI(harness).run(pipeline)  # type: ignore[arg-type]

    assert execution.status == "SUCCEEDED"
    assert harness._spinnaker_runs[0]["executionId"] == "execution-9"


def test_spinnaker_api_observes_execution_triggered_by_application() -> None:
    client = Mock(spec=SpinnakerClient)
    client.executions.side_effect = [
        [{"id": "old", "name": "deploy", "startTime": 1}],
        [
            {"id": "new", "name": "deploy", "startTime": 2},
            {"id": "old", "name": "deploy", "startTime": 1},
        ],
    ]
    client.wait_execution.return_value = {
        "id": "new",
        "name": "deploy",
        "status": "SUCCEEDED",
        "stages": [],
    }
    harness = SimpleNamespace(
        _services=SimpleNamespace(spinnaker=lambda: client),
        _spinnaker_runs=[],
    )
    api = SpinnakerAPI(harness)  # type: ignore[arg-type]

    baseline = api.execution_ids("payments", pipeline="deploy")
    execution = api.wait_for_execution(
        "payments",
        pipeline="deploy",
        excluding=baseline,
        timeout=1,
    )

    assert baseline == {"old"}
    assert execution.execution_id == "new"
    assert harness._spinnaker_runs[0]["observedExternally"] is True


def test_repository_rejects_files_outside_its_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "repository"
    worktree.mkdir()
    repository = TestRepository(
        remote=GiteaRepository(owner="harness", name="test"),
        path=worktree,
        git=Mock(spec=GitWorkspace),
    )

    with pytest.raises(HarnessError, match="escapes its worktree"):
        repository.update({"../secret": "nope"}, message="unsafe")


def test_repository_refreshes_revision_after_an_external_push(tmp_path: Path) -> None:
    runner = Mock()
    runner.run.side_effect = [
        CommandResult(("git", "fetch"), 0, "", ""),
        CommandResult(("git", "rev-parse"), 0, "new-commit\n", ""),
    ]
    repository = TestRepository(
        remote=GiteaRepository(owner="harness", name="test"),
        path=tmp_path,
        git=SimpleNamespace(runner=runner),  # type: ignore[arg-type]
        revision="old-commit",
    )

    assert repository.refresh() == "new-commit"
    assert repository.revision == "new-commit"


def test_repository_creates_branch_and_returns_to_main(tmp_path: Path) -> None:
    runner = Mock()
    git = SimpleNamespace(
        runner=runner,
        commit=Mock(return_value="release-commit"),
        push=Mock(),
    )
    repository = TestRepository(
        remote=GiteaRepository(owner="harness", name="test"),
        path=tmp_path,
        git=git,  # type: ignore[arg-type]
        revision="main-commit",
    )

    revision = repository.create_branch(
        "release",
        files={"branch.txt": "release\n"},
    )

    assert revision == "release-commit"
    git.push.assert_called_once_with(branch="release")
    assert runner.run.call_args_list[-1].args[0] == ["git", "switch", "main"]
    assert repository.revision == "main-commit"


def test_repository_seed_copies_tree_renders_templates_and_preserves_executable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "template"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "README.md.tmpl").write_text("# $application\n")
    (source / "nested/config.yaml").write_text("enabled: true\n")
    (source / "asset.bin").write_bytes(b"\x00\xfffixture")
    script = source / "run.sh"
    script.write_text("#!/bin/sh\necho seeded\n")
    script.chmod(0o755)
    destination = tmp_path / "repository"
    destination.mkdir()
    git = Mock(spec=GitWorkspace)
    git.commit.return_value = "seed-commit"
    repository = TestRepository(
        remote=GiteaRepository(owner="harness", name="seeded"),
        path=destination,
        git=git,
    )

    revision = repository.update_from(
        source,
        message="Seed from fixture",
        variables={"application": "payments"},
        files={"nested/config.yaml": "enabled: false\n"},
    )

    assert revision == "seed-commit"
    assert (destination / "README.md").read_text() == "# payments\n"
    assert not (destination / "README.md.tmpl").exists()
    assert (destination / "nested/config.yaml").read_text() == "enabled: false\n"
    assert (destination / "asset.bin").read_bytes() == b"\x00\xfffixture"
    assert (destination / "run.sh").stat().st_mode & 0o111
    git.push.assert_called_once_with()


def test_repository_seed_rejects_missing_variables_and_symlinks(tmp_path: Path) -> None:
    destination = tmp_path / "repository"
    destination.mkdir()
    repository = TestRepository(
        remote=GiteaRepository(owner="harness", name="seeded"),
        path=destination,
        git=Mock(spec=GitWorkspace),
    )
    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "config.tmpl").write_text("value=$required\n")

    with pytest.raises(HarnessError, match="could not render"):
        repository.update_from(missing, message="Missing variable")

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "outside").symlink_to(missing / "config.tmpl")
    with pytest.raises(HarnessError, match="must not contain symlinks"):
        repository.update_from(linked, message="Unsafe link")


def test_named_repository_template_resolves_under_workspace_fixtures() -> None:
    workspace = Path(__file__).parents[1]
    harness = SimpleNamespace(runtime=SimpleNamespace(workspace=workspace))

    resolved = GitAPI(harness).resolve_template("jenkins/library")  # type: ignore[arg-type]

    assert resolved == workspace / "fixtures/jenkins/library"


def test_generic_wait_until_returns_last_matching_application_state() -> None:
    states = iter([{"state": "queued"}, {"state": "deploying"}, {"state": "ready"}])

    result = TestHarness.wait_until(
        SimpleNamespace(),  # type: ignore[arg-type]
        lambda: next(states),
        predicate=lambda item: item["state"] == "ready",
        description="release to become ready",
        timeout=0.1,
        interval=0,
    )

    assert result == {"state": "ready"}


def test_failed_test_can_preserve_namespace_and_owned_state(tmp_path: Path) -> None:
    runtime = SimpleNamespace(
        preserve_environment_on_failure=True,
        preserve_failure=Mock(),
        host=Mock(),
    )
    harness = TestHarness(
        runtime,  # type: ignore[arg-type]
        node_id="tests/test_release.py::test_failure",
        workdir=tmp_path,
    )
    harness.capture_diagnostics = Mock()  # type: ignore[method-assign]
    harness._services = Mock()  # noqa: SLF001
    harness._remove_owned_jenkins_libraries = Mock()  # type: ignore[method-assign]
    harness._delete_namespace = Mock()  # type: ignore[method-assign]

    harness.finish(failed=True)

    runtime.preserve_failure.assert_called_once_with(
        node_id="tests/test_release.py::test_failure",
        namespace=harness.namespace,
    )
    harness._remove_owned_jenkins_libraries.assert_not_called()  # type: ignore[attr-defined]
    harness._delete_namespace.assert_not_called()  # type: ignore[attr-defined]
    harness._services.close.assert_called_once()  # noqa: SLF001


def test_proxy_api_creates_a_test_owned_external_name_alias(tmp_path: Path) -> None:
    kubectl = Mock()
    client = Mock(spec=WireMockClient)
    client.proxy.return_value = SimpleNamespace(target="http://jenkins-origin:8080")
    components = SimpleNamespace(require=lambda *_: WireMockComponent())
    runtime = SimpleNamespace(
        environment=SimpleNamespace(kubectl=kubectl, components=components)
    )
    harness = TestHarness(runtime, node_id="tests/test_release.py::test_proxy", workdir=tmp_path)
    harness._services = SimpleNamespace(wiremock=lambda: client)  # noqa: SLF001

    endpoint = harness.proxies.service(
        "jenkins",
        target="http://jenkins-origin:8080",
    )

    manifest = kubectl.apply.call_args.args[0]
    assert f"namespace: {harness.namespace}" in manifest
    assert "type: ExternalName" in manifest
    assert "externalName: wiremock.harness-system.svc.cluster.local" in manifest
    assert endpoint.host == f"jenkins.{harness.namespace}.svc.cluster.local"
    client.proxy.assert_called_once_with(
        "jenkins",
        target="http://jenkins-origin:8080",
        host=endpoint.host,
        priority=10,
    )


def test_harness_without_wiremock_skips_mock_lifecycle(tmp_path: Path) -> None:
    kubectl = SimpleNamespace(
        apply=Mock(),
        command=Mock(return_value=["kubectl", "delete", "namespace"]),
    )
    environment = SimpleNamespace(
        kubectl=kubectl,
        runner=Mock(),
        registry=SimpleNamespace(ensure_namespace=Mock()),
        components=SimpleNamespace(has=lambda name: False),
    )
    runtime = SimpleNamespace(
        environment=environment,
        preserve_environment_on_failure=False,
    )
    harness = TestHarness(
        runtime,
        node_id="tests/test_release.py::test_without_wiremock",
        workdir=tmp_path,
    )

    harness.start()
    harness.finish(failed=False)

    assert harness._mocks is None  # noqa: SLF001 - proves optional component isolation
