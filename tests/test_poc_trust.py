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
    pytest.mark.skipif(
        not os.getenv("CICD_TEST_CA_CERTIFICATE"),
        reason="CICD_TEST_CA_CERTIFICATE is not set",
    ),
]


def test_private_ca_reaches_rootful_podman_kind_and_java() -> None:
    workspace = Path(__file__).parents[1]
    profile_name = os.getenv("CICD_PROFILE", "modern")
    ca_certificate = Path(os.environ["CICD_TEST_CA_CERTIFICATE"]).resolve()
    profile = load_profile(
        workspace / f"profiles/{profile_name}.yaml",
        workspace=workspace,
    )
    cluster_name = f"cicd-ca-{uuid4().hex[:8]}"
    profile = profile.model_copy(
        update={
            "trust": TrustConfig(ca_certificate=ca_certificate),
            "kind": profile.kind.model_copy(update={"cluster_name": cluster_name}),
        }
    )
    runner = CommandRunner(cwd=workspace)
    environment = HarnessEnvironment(
        profile,
        workspace=workspace,
        runner=runner,
        component_names={"wiremock"},
    )

    try:
        environment.up(timeout=600)
        runner.run(
            [
                "podman",
                "machine",
                "ssh",
                "sudo",
                "test",
                "-s",
                "/etc/pki/ca-trust/source/anchors/cicd-harness-private-ca.crt",
            ]
        )
        runner.run(
            [
                "podman",
                "exec",
                f"{cluster_name}-control-plane",
                "test",
                "-s",
                "/usr/local/share/ca-certificates/cicd-harness-private-ca.crt",
            ]
        )
        config_map = environment.kubectl.get_json(
            "configmap/harness-trust-bundle",
            "-n",
            "harness-system",
        )
        assert {"ca.crt", "ca-bundle.crt"} <= set(config_map["data"])

        pod = environment.kubectl.first_pod(
            "harness-system",
            "app.kubernetes.io/name=wiremock",
        )
        environment.kubectl.exec(
            "harness-system",
            pod,
            "keytool",
            "-list",
            "-keystore",
            "/var/run/cicd-harness-java-trust/cacerts",
            "-storepass",
            "changeit",
            "-alias",
            "cicd-harness-private-ca",
        )
    finally:
        environment.down()
