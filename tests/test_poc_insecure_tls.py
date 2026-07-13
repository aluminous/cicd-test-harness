from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from cicd_harness.command import CommandRunner
from cicd_harness.config import TrustConfig, load_profile
from cicd_harness.environment import HarnessEnvironment

pytestmark = [
    pytest.mark.poc,
    pytest.mark.timeout(900),
    pytest.mark.skipif(not os.getenv("CICD_RUN_POC"), reason="PoC disabled"),
]


def test_insecure_tls_fallback_reaches_rootful_podman_kind_and_services() -> None:
    workspace = Path(__file__).parents[1]
    profile_name = os.getenv("CICD_PROFILE", "modern")
    profile = load_profile(
        workspace / f"profiles/{profile_name}.yaml",
        workspace=workspace,
    )
    cluster_name = f"cicd-insecure-{uuid4().hex[:8]}"
    profile = profile.model_copy(
        update={
            "trust": TrustConfig(insecure_skip_tls_verify=True),
            "kind": profile.kind.model_copy(update={"cluster_name": cluster_name}),
        }
    )
    runner = CommandRunner(cwd=workspace)
    environment = HarnessEnvironment(
        profile,
        workspace=workspace,
        runner=runner,
        component_names={"gitea", "wiremock"},
    )

    try:
        environment.up(timeout=600)
        node = f"{cluster_name}-control-plane"
        containerd_config = runner.run(
            ["podman", "exec", node, "containerd", "config", "dump"]
        ).stdout
        if profile.kind.version == "0.17.0":
            assert "insecure_skip_verify = true" in containerd_config
        else:
            assert "/etc/containerd/certs.d" in containerd_config
            docker_hub = runner.run(
                [
                    "podman",
                    "exec",
                    node,
                    "cat",
                    "/etc/containerd/certs.d/docker.io/hosts.toml",
                ]
            ).stdout
            assert "skip_verify = true" in docker_hub

        config_map = environment.kubectl.get_json(
            "configmap/harness-trust-bundle",
            "-n",
            "harness-system",
        )
        assert config_map["data"][".curlrc"] == "insecure\n"
        assert config_map["data"]["wgetrc"] == "check_certificate = off\n"

        wiremock = environment.kubectl.get_json(
            "deployment/wiremock",
            "-n",
            "harness-system",
        )["spec"]["template"]["spec"]["containers"][0]
        wiremock_env = {item["name"]: item["value"] for item in wiremock["env"]}
        assert wiremock_env["CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY"] == "1"

        gitea = environment.kubectl.get_json(
            "deployment/gitea",
            "-n",
            "harness-system",
        )["spec"]["template"]["spec"]["containers"][0]
        gitea_env = {item["name"]: item["value"] for item in gitea["env"]}
        assert gitea_env["GITEA__webhook__SKIP_TLS_VERIFY"] == "true"
        assert gitea_env["GIT_SSL_NO_VERIFY"] == "true"
    finally:
        environment.down()
