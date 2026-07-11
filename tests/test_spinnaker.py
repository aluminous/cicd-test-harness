from pathlib import Path
from unittest.mock import Mock

from cicd_harness.config import load_profile
from cicd_harness.spinnaker import (
    SpinnakerClient,
    git_repo_artifact,
    http_file_artifact,
    kustomize_pipeline,
    raw_manifest_pipeline,
)
from cicd_harness.testing import SpinnakerExecution


def test_profile_pins_exact_spinnaker_bom_images() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)

    assert profile.spinnaker.version == "1.25.4"
    assert profile.spinnaker.images.clouddriver.endswith("7.3.3-20210322155326")
    assert profile.spinnaker.images.rosco.endswith("0.24.0-20210210110018")
    assert profile.spinnaker.manifest == workspace / "manifests/spinnaker-poc.yaml"


def test_spinnaker_account_accepts_fixture_owned_namespaces() -> None:
    workspace = Path(__file__).parents[1]
    manifest = (workspace / "manifests/spinnaker-poc.yaml").read_text()

    assert "\n          namespaces:\n" not in manifest


def test_raw_pipeline_pins_git_commit_and_supplies_moniker() -> None:
    artifact = http_file_artifact(
        url="http://gitea/repo/raw/commit/abc/rollout.yaml",
        commit="abc",
        name="rollout.yaml",
    )
    pipeline = raw_manifest_pipeline(
        application="payments",
        name="raw-manifest",
        artifact=artifact,
    )

    stage = pipeline["stages"][0]
    assert stage["source"] == "artifact"
    assert stage["moniker"] == {"app": "payments"}
    assert stage["manifestArtifact"]["version"] == "abc"
    assert stage["manifestArtifactAccount"] == "no-auth-http-account"


def test_kustomize_pipeline_bakes_exact_git_repo_before_deploy() -> None:
    artifact = git_repo_artifact(
        repo_url="http://gitea/harness/manifests.git",
        commit="deadbeef",
    )
    pipeline = kustomize_pipeline(
        application="payments",
        name="kustomize-manifest",
        repo_artifact=artifact,
        kustomization_path="overlays/test/kustomization.yaml",
    )

    bake, deploy = pipeline["stages"]
    assert bake["templateRenderer"] == "KUSTOMIZE"
    assert bake["inputArtifact"]["artifact"]["version"] == "deadbeef"
    assert bake["kustomizeFilePath"] == "overlays/test/kustomization.yaml"
    assert deploy["requisiteStageRefIds"] == ["1"]
    assert deploy["manifestArtifactId"] == "baked-manifest"


def test_wait_execution_does_not_treat_not_started_as_terminal() -> None:
    client = SpinnakerClient("http://spinnaker.invalid")
    client.execution = Mock(  # type: ignore[method-assign]
        side_effect=[
            {"status": "NOT_STARTED"},
            {"status": "RUNNING"},
            {"status": "SUCCEEDED"},
        ]
    )
    try:
        execution = client.wait_execution("execution-id", timeout=1, interval=0)
    finally:
        client.close()

    assert execution["status"] == "SUCCEEDED"
    assert client.execution.call_count == 3


def test_execution_exposes_stage_level_assertions() -> None:
    execution = SpinnakerExecution(
        application="payments",
        pipeline="deploy",
        execution_id="execution-1",
        status="SUCCEEDED",
        payload={
            "stages": [
                {"name": "Bake manifest", "type": "bakeManifest", "status": "SUCCEEDED"},
                {"name": "Deploy", "type": "deployManifest", "status": "SUCCEEDED"},
            ]
        },
    )

    assert execution.stages(type="deployManifest")[0]["name"] == "Deploy"
    assert execution.assert_stage("Bake manifest")["status"] == "SUCCEEDED"
