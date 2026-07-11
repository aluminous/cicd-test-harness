from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from cicd_harness.command import CommandRunner
from cicd_harness.config import load_profile
from cicd_harness.controllers import ControllerStack
from cicd_harness.gitea import GiteaClient, GitWorkspace
from cicd_harness.infra import InfraStack
from cicd_harness.kind import KindCluster
from cicd_harness.kubectl import Kubectl
from cicd_harness.rollouts import RolloutProbe
from cicd_harness.spinnaker import (
    SpinnakerClient,
    SpinnakerStack,
    git_repo_artifact,
    http_file_artifact,
    kustomize_pipeline,
    raw_manifest_pipeline,
)

pytestmark = [
    pytest.mark.poc,
    pytest.mark.timeout(1200),
    pytest.mark.skipif(not os.getenv("CICD_RUN_POC"), reason="PoC disabled"),
]


def test_raw_and_kustomize_pipelines_drive_argo_rollouts(tmp_path: Path) -> None:
    """Exercise exact-commit Git artifacts through the minimal Spinnaker slice.

    The raw pipeline is deployed twice so the second execution exposes both the
    stable and canary ReplicaSets. The Kustomize path must pass through Rosco and
    deploy its embedded artifact; calling Rosco directly would miss Orca's stage
    wiring and artifact propagation.
    """

    workspace = Path(__file__).parents[1]
    profile_name = os.getenv("CICD_PROFILE", "modern")
    profile = load_profile(workspace / f"profiles/{profile_name}.yaml", workspace=workspace)
    runner = CommandRunner(cwd=workspace)
    cluster = KindCluster(profile, runner)
    cluster.create()
    kubectl = Kubectl(cluster.context, runner)

    controllers = ControllerStack(profile, kubectl, runner)
    controllers.install_argo_rollouts(timeout=600)
    controllers.install_istio(timeout=600)

    infra = InfraStack(profile, kubectl, workspace)
    infra.install(timeout=600)
    infra.bootstrap_gitea()

    spinnaker = SpinnakerStack(profile, cluster, kubectl, runner)
    spinnaker.prepare_service_images()
    spinnaker.install(timeout=900)

    suffix = uuid4().hex[:8]
    repository_name = f"spinnaker-poc-{suffix}"
    raw_name = f"raw-{suffix}"
    kustomize_name = f"kustomize-{suffix}"
    application = f"poc{suffix}"
    repository_path = tmp_path / "repository"
    shutil.copytree(workspace / "fixtures/spinnaker-repo", repository_path)
    _replace(repository_path / "raw/rollout.yaml", "spin-raw", raw_name)
    _replace(repository_path / "raw/rollout.yaml", "raw-v3", "raw-v1")
    _replace(repository_path / "kustomize/base/rollout.yaml", "spin-kustomize", kustomize_name)

    with (
        kubectl.port_forward("harness-system", "service/gitea", 3000) as gitea_forward,
        kubectl.port_forward("spinnaker", "service/spin-gate", 8084) as gate_forward,
        GiteaClient(gitea_forward.url) as gitea,
    ):
        repository = gitea.create_repository(repository_name)
        git = GitWorkspace(repository_path)
        git.initialize()
        git.add_remote(gitea.host_clone_url(repository))
        first_commit = git.commit("Initial manifests")
        git.push()

        raw_pipeline_name = f"raw-{suffix}"
        first_artifact = http_file_artifact(
            url=repository.raw_commit_url(first_commit, "raw/rollout.yaml"),
            commit=first_commit,
            name="rollout.yaml",
        )
        with SpinnakerClient(gate_forward.url) as client:
            client.save_pipeline(
                raw_manifest_pipeline(
                    application=application,
                    name=raw_pipeline_name,
                    artifact=first_artifact,
                )
            )
            execution_id = client.trigger(application, raw_pipeline_name)
            execution = client.wait_execution(execution_id, timeout=420)
            assert execution["status"] == "SUCCEEDED", execution

            rollout = RolloutProbe(kubectl, "spinnaker-poc", raw_name)
            rollout.wait_healthy(timeout=180)

            _replace(repository_path / "raw/rollout.yaml", "raw-v1", "raw-v2")
            second_commit = git.commit("Start routed canary")
            git.push()
            second_artifact = http_file_artifact(
                url=repository.raw_commit_url(second_commit, "raw/rollout.yaml"),
                commit=second_commit,
                name="rollout.yaml",
            )
            client.save_pipeline(
                raw_manifest_pipeline(
                    application=application,
                    name=raw_pipeline_name,
                    artifact=second_artifact,
                )
            )
            execution_id = client.trigger(application, raw_pipeline_name)

            rollout.wait_paused(timeout=180)
            states = rollout.replica_sets()
            assert len(
                [state for state in states if state.role == "stable" and state.desired > 0]
            ) == 1
            assert len(
                [state for state in states if state.role == "canary" and state.desired > 0]
            ) == 1
            virtual_service = kubectl.get_json(
                f"virtualservice/{raw_name}",
                "-n",
                "spinnaker-poc",
            )
            weights = [
                route["weight"] for route in virtual_service["spec"]["http"][0]["route"]
            ]
            assert weights == [50, 50]

            execution = client.wait_execution(execution_id, timeout=420)
            assert execution["status"] == "SUCCEEDED", execution
            rollout.wait_healthy(timeout=120)
            final_states = rollout.replica_sets()
            retained_old = [
                state for state in final_states if state.role == "old" and state.desired > 0
            ]
            assert len(retained_old) == 1
            assert retained_old[0].scale_down_deadline is not None

            kustomize_pipeline_name = f"kustomize-{suffix}"
            repository_artifact = git_repo_artifact(
                repo_url=repository.clone_url,
                commit=second_commit,
            )
            client.save_pipeline(
                kustomize_pipeline(
                    application=application,
                    name=kustomize_pipeline_name,
                    repo_artifact=repository_artifact,
                    kustomization_path="kustomize/overlays/test/kustomization.yaml",
                )
            )
            execution_id = client.trigger(application, kustomize_pipeline_name)
            execution = client.wait_execution(execution_id, timeout=600)
            assert execution["status"] == "SUCCEEDED", execution

    baked_rollout = RolloutProbe(kubectl, "spinnaker-poc", kustomize_name)
    baked = baked_rollout.wait_healthy(timeout=180)
    assert baked["metadata"]["annotations"]["harness.cicd/revision"] == "kustomize-v1"
    assert baked["metadata"]["labels"]["harness.cicd/baked-by"] == "rosco"


def _replace(path: Path, old: str, new: str) -> None:
    contents = path.read_text()
    assert old in contents
    path.write_text(contents.replace(old, new))
