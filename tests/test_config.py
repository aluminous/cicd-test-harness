from pathlib import Path

import pytest
from pydantic import ValidationError

from cicd_harness.config import HarnessProfile, load_profile


def test_load_modern_profile() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)

    assert profile.name == "modern"
    assert profile.kind.version == "0.31.0"
    assert profile.kind.node_image.endswith("55aac2b")
    assert profile.istio.gateway.name == "harness-gateway"
    assert profile.spinnaker.rosco_enabled is True
    assert profile.jenkins.version == "2.426.1"
    assert profile.jenkins.manifest == workspace / "manifests/jenkins-poc.yaml"
    assert profile.jenkins.containerfile == workspace / "images/jenkins/Containerfile"
    assert profile.jenkins.plugins_file == workspace / "images/jenkins/plugins.txt"
    assert "pipeline" in profile.jenkins.image
    assert profile.infra.wiremock.max_request_journal_entries == 1000
    assert profile.infra.wiremock.logged_response_body_size_limit == 65536
    assert profile.infra.wiremock.proxy_timeout_milliseconds == 30000


def test_kind_image_requires_digest() -> None:
    with pytest.raises(ValidationError, match="sha256"):
        HarnessProfile.model_validate(
            {
                "name": "invalid",
                "runtime": {},
                "kind": {
                    "version": "0.1.0",
                    "binary": "kind",
                    "node_image": "kindest/node:v1.31.14",
                    "cluster_name": "invalid",
                },
                "argo_rollouts": {
                    "version": "1.8.3",
                    "install_manifest": "install.yaml",
                },
                "istio": {"version": "1.25.5", "chart_directory": "charts"},
                "infra": {
                    "gitea": {"image": "gitea"},
                    "wiremock": {"image": "wiremock"},
                },
            }
        )
