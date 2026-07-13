import base64
import json
import re
import ssl
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx
import pytest

from cicd_harness.command import CommandResult
from cicd_harness.config import TrustConfig, load_profile
from cicd_harness.errors import HarnessError
from cicd_harness.jenkins import JenkinsClient, JenkinsStack, multibranch_job_config


def test_multibranch_config_declares_repository_discovery_and_jenkinsfile() -> None:
    xml = multibranch_job_config(
        repository_url="http://gitea/harness/application.git?x=1&y=2",
        source_id="application-source",
    )
    root = ET.fromstring(xml)

    assert root.tag.endswith("WorkflowMultiBranchProject")
    assert root.findtext(".//remote") == "http://gitea/harness/application.git?x=1&y=2"
    assert root.findtext(".//scriptPath") == "Jenkinsfile"
    assert root.find(".//jenkins.plugins.git.traits.BranchDiscoveryTrait") is not None


def test_client_lists_nested_jobs_and_inspects_configuration() -> None:
    config = multibranch_job_config(
        repository_url="http://gitea/harness/application.git",
        source_id="source-id",
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/json":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "name": "application",
                            "url": "http://jenkins/job/application/",
                            "_class": (
                                "org.jenkinsci.plugins.workflow.multibranch."
                                "WorkflowMultiBranchProject"
                            ),
                            "jobs": [
                                {
                                    "name": "main",
                                    "url": "http://jenkins/job/application/job/main/",
                                    "color": "blue",
                                    "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                                }
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/job/application/api/json":
            return httpx.Response(
                200,
                json={
                    "name": "application",
                    "fullName": "application",
                    "url": "http://jenkins/job/application/",
                    "_class": (
                        "org.jenkinsci.plugins.workflow.multibranch."
                        "WorkflowMultiBranchProject"
                    ),
                },
            )
        if request.url.path == "/job/application/config.xml":
            return httpx.Response(200, text=config)
        return httpx.Response(404)

    client = JenkinsClient("http://jenkins.invalid")
    client._client.close()
    client._client = httpx.Client(  # noqa: SLF001 - inject deterministic API transport
        base_url="http://jenkins.invalid",
        transport=httpx.MockTransport(handle),
    )
    try:
        jobs = client.list_jobs(recursive=True)
        inspected = client.inspect_job("application")
    finally:
        client.close()

    assert [job.full_name for job in jobs] == ["application", "application/main"]
    assert inspected.script_path == "Jenkinsfile"
    assert inspected.repository_urls == ("http://gitea/harness/application.git",)
    assert inspected.root_type.endswith("WorkflowMultiBranchProject")


def test_pipeline_plugin_lock_and_image_fingerprint_cover_inputs() -> None:
    profile_workspace = Path(__file__).parents[1]
    profile = load_profile(
        profile_workspace / "profiles/modern.yaml",
        workspace=profile_workspace,
    )
    plugins = profile.jenkins.plugins_file.read_text()
    fake_kubectl = Mock()
    stack = JenkinsStack(profile, fake_kubectl)

    assert "workflow-multibranch:756.v891d88f2cd46" in plugins
    assert "pipeline-groovy-lib:689.veec561a_dee13" in plugins
    assert "pipeline-stage-step:305.ve96d0205c1c6" in plugins
    assert len(stack._build_fingerprint()) == 64  # noqa: SLF001


def test_jenkins_image_build_passes_configured_plugin_mirrors() -> None:
    profile_workspace = Path(__file__).parents[1]
    profile = load_profile(
        profile_workspace / "profiles/airgap-modern.example.yaml",
        workspace=profile_workspace,
    )
    runner = Mock()
    runner.cwd = profile_workspace
    runner.run.return_value = CommandResult(("podman", "build"), 0, "", "")
    stack = JenkinsStack(profile, Mock(runner=runner))

    stack.build_image(force=True)

    command = runner.run.call_args.args[0]
    assert "JENKINS_UC=https://nexus.airgap.example/repository/jenkins-updates" in command
    assert "JENKINS_UC_DOWNLOAD=https://nexus.airgap.example/repository/jenkins-download" in command
    assert not any("updates.jenkins.io" in str(part) for part in command)


def test_jenkins_image_bakes_private_ca_and_fingerprints_it(tmp_path: Path) -> None:
    profile_workspace = Path(__file__).parents[1]
    profile = load_profile(
        profile_workspace / "profiles/modern.yaml",
        workspace=profile_workspace,
    )
    roots = ssl.create_default_context().get_ca_certs(binary_form=True)
    ca_certificate = tmp_path / "corporate-ca.pem"
    ca_certificate.write_text(ssl.DER_cert_to_PEM_cert(roots[0]))
    trusted_profile = profile.model_copy(
        update={"trust": TrustConfig(ca_certificate=ca_certificate)}
    )
    runner = Mock()
    runner.cwd = profile_workspace

    def run(command, **_kwargs):
        context = Path(command[-1])
        assert (context / "corporate-ca.crt").read_text() == ca_certificate.read_text()
        assert "update-ca-certificates" in (context / "Containerfile").read_text()
        return CommandResult(tuple(str(part) for part in command), 0, "", "")

    runner.run.side_effect = run
    untrusted = JenkinsStack(profile, Mock(runner=runner))
    trusted = JenkinsStack(trusted_profile, Mock(runner=runner))

    trusted.build_image(force=True)

    assert trusted._build_fingerprint() != untrusted._build_fingerprint()  # noqa: SLF001


def test_pull_only_jenkins_image_never_falls_back_to_a_build() -> None:
    profile_workspace = Path(__file__).parents[1]
    profile = load_profile(
        profile_workspace / "profiles/airgap-modern.example.yaml",
        workspace=profile_workspace,
    )
    runner = Mock()
    runner.cwd = profile_workspace
    runner.run.return_value = CommandResult(("podman",), 1, "", "not found")
    stack = JenkinsStack(profile, Mock(runner=runner))

    with pytest.raises(HarnessError, match="prebuilt Jenkins image"):
        stack.prepare_image()

    commands = [call.args[0] for call in runner.run.call_args_list]
    assert any(command[:2] == ["podman", "pull"] for command in commands)
    assert not any("build" in command for command in commands)


def test_client_configures_lists_and_removes_shared_library() -> None:
    configured: dict[str, dict] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/scriptText"
        script = parse_qs(request.content.decode())["script"][0]
        if "new LibraryConfiguration" in script:
            encoded = re.search(r"decode\('([^']+)'\)", script)
            assert encoded is not None
            payload = json.loads(base64.b64decode(encoded.group(1)))
            configured[payload["name"]] = payload
            return httpx.Response(200, text="")
        if "JsonOutput.toJson" in script:
            payload = [
                {
                    "name": item["name"],
                    "repositoryUrl": item["repositoryUrl"],
                    "defaultVersion": item["defaultVersion"],
                    "implicit": item["implicit"],
                    "allowVersionOverride": item["allowVersionOverride"],
                    "includeInChangesets": item["includeInChangesets"],
                    "credentialsId": item["credentialsId"],
                }
                for item in configured.values()
            ]
            return httpx.Response(200, text=json.dumps(payload) + "\n")
        encoded = re.search(r"decode\('([^']+)'\)", script)
        assert encoded is not None
        payload = json.loads(base64.b64decode(encoded.group(1)))
        configured.pop(payload["name"], None)
        return httpx.Response(200, text="")

    client = JenkinsClient("http://jenkins.invalid")
    client._client.close()
    client._client = httpx.Client(  # noqa: SLF001 - inject deterministic API transport
        base_url="http://jenkins.invalid",
        transport=httpx.MockTransport(handle),
    )
    try:
        library = client.configure_library(
            "example's-library",
            repository_url="http://gitea/harness/library.git",
            default_version="release",
            implicit=True,
        )
        listed = client.list_libraries()
        client.remove_library("example's-library")
        remaining = client.list_libraries()
    finally:
        client.close()

    assert library.name == "example's-library"
    assert library.default_version == "release"
    assert library.implicit
    assert listed == [library]
    assert remaining == []


def test_client_lists_and_downloads_build_artifacts() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/json"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {"fileName": "result.json", "relativePath": "reports/result.json"}
                    ]
                },
            )
        if request.url.path.endswith("/artifact/reports/result.json"):
            return httpx.Response(200, content=b'{"status":"ok"}')
        return httpx.Response(404)

    client = JenkinsClient("http://jenkins.invalid")
    client._client.close()  # noqa: SLF001 - inject deterministic API transport
    client._client = httpx.Client(  # noqa: SLF001
        base_url="http://jenkins.invalid",
        transport=httpx.MockTransport(handle),
    )
    try:
        artifacts = client.list_artifacts("payments/main", 7)
        contents = client.artifact("payments/main", 7, artifacts[0].relative_path)
    finally:
        client.close()

    assert artifacts[0].file_name == "result.json"
    assert contents == b'{"status":"ok"}'
