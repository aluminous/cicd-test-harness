from __future__ import annotations

import os

import pytest

from cicd_harness.testing import TestHarness

pytestmark = [
    pytest.mark.poc,
    pytest.mark.timeout(1200),
    pytest.mark.skipif(not os.getenv("CICD_RUN_POC"), reason="PoC disabled"),
]


def test_freestyle_boundary_pushes_git_and_calls_mock_backend(
    harness: TestHarness,
) -> None:
    revision = harness.unique_name("jenkins")
    repository = harness.git.create_repository(files={"README.md": "# Jenkins PoC\n"})
    callback = harness.mocks.service("backend-callback")
    callback.expect(
        method="POST",
        path="/jenkins/callback",
        response={"status": 202, "json": {"accepted": True}},
        json_paths={"$.revision": revision, "$.job": "harness-poc"},
    )

    harness.jenkins.run(
        "harness-poc",
        parameters={
            "REPO_URL": repository.clone_url,
            "REVISION": revision,
            "CALLBACK_URL": f"{callback.url}/jenkins/callback",
        },
    )

    repository.refresh()
    assert repository.read("jenkins-proof.txt").strip() == revision


def test_multibranch_job_executes_jenkinsfile_and_external_library(
    harness: TestHarness,
) -> None:
    library = harness.jenkins.create_library(
        "example",
        template="jenkins/library",
    )
    harness.jenkins.assert_library(
        "example",
        repository_url=library.repository.clone_url,
        default_version="main",
        implicit=False,
    )
    application = harness.git.create_repository(
        template="jenkins/application",
        message="Create multibranch Jenkins application",
    )
    application.create_branch(
        "release",
        files={"branch.txt": "release branch discovered by Jenkins\n"},
    )
    job_name = harness.unique_name("application-pipeline")

    created = harness.jenkins.create_multibranch_job(application, name=job_name)
    assert created.script_path == "Jenkinsfile"
    inspected = harness.jenkins.assert_job(
        job_name,
        kind_contains="WorkflowMultiBranchProject",
        repository_url=application.clone_url,
        script_path="Jenkinsfile",
    )
    assert "BranchDiscoveryTrait" in inspected.xml

    harness.jenkins.wait_for_branch(job_name, "main", timeout=300)
    harness.jenkins.wait_for_branch(job_name, "release", timeout=300)
    visible = {job.full_name for job in harness.jenkins.list_jobs()}
    assert job_name in visible
    assert f"{job_name}/main" in visible
    assert f"{job_name}/release" in visible

    build = harness.jenkins.run_branch(job_name, "main", timeout=300)
    assert "JENKINSFILE_EXECUTED" in build.console
    assert "EXAMPLE_LIBRARY_EXECUTED" in build.console
    assert "Loading library example@main" in build.console
