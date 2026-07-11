from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from cicd_harness.testing import TestHarness

pytestmark = [
    pytest.mark.poc,
    pytest.mark.timeout(900),
    pytest.mark.skipif(not os.getenv("CICD_RUN_POC"), reason="PoC disabled"),
]


def test_high_level_fixture_observes_a_complete_canary(harness: TestHarness) -> None:
    """The author deals with the application rollout, not harness infrastructure."""

    source = Path("manifests/rollout-poc.yaml").read_text()
    name = harness.unique_name("payments")
    manifest = source.replace("rollout-poc", name)
    manifest = manifest.replace(f"namespace: {name}", f"namespace: {harness.namespace}")
    # The fixture already owns the namespace; omit the sample's Namespace document.
    manifest = "---".join(manifest.split("---")[1:])
    harness.resources.apply(manifest)

    rollout = harness.rollout(name)
    rollout.wait_healthy(timeout=300)
    rollout.update_template_annotations({"harness.test/revision": "v2"})

    rollout.wait_for_canary(weights=(50, 50), timeout=180)
    rollout.assert_replica_sets(stable=1, canary=1)
    rollout.assert_traffic_weights(50, 50)

    rollout.wait_healthy(timeout=120)
    rollout.assert_scale_down_pending(count=1)


def test_fixture_captures_live_diagnostics(harness: TestHarness) -> None:
    destination = harness.capture_diagnostics("live-smoke")
    summary = json.loads((destination / "summary.json").read_text())

    assert summary["metadata"]["test"].endswith("test_fixture_captures_live_diagnostics")
    assert (destination / "pods.txt").exists()
    assert (destination / "rollouts.yaml").exists()
    assert (destination / "wiremock.json").exists()
    assert (destination / "component-graph.json").exists()
    assert (destination / "host-endpoints.json").exists()


def test_host_endpoint_catalog_exposes_component_apis(harness: TestHarness) -> None:
    names = {endpoint.name for endpoint in harness.host.list()}
    assert {"gitea", "wiremock-admin"} <= names

    gitea = harness.host.expose("gitea")
    gitea_health = httpx.get(f"{gitea.url}/api/healthz")
    assert gitea_health.status_code == 200
    assert gitea_health.json()["status"] == "pass"

    wiremock = harness.host.expose("wiremock-admin")
    wiremock_mappings = httpx.get(f"{wiremock.url}mappings")
    assert wiremock_mappings.status_code == 200
    assert "mappings" in wiremock_mappings.json()


def test_graph_backed_spinnaker_deploys_git_manifest(harness: TestHarness) -> None:
    """Prove the composed Spinnaker node can execute, not merely become Ready."""

    name = harness.unique_name("spinnaker-raw")
    manifest = Path("fixtures/spinnaker-repo/raw/rollout.yaml").read_text()
    manifest = manifest.replace("spin-raw", name)
    manifest = manifest.replace("spinnaker-poc", harness.namespace)
    repository = harness.git.create_repository(
        files={"raw/rollout.yaml": manifest},
    )

    execution = harness.spinnaker.deploy_raw_manifest(
        repository,
        "raw/rollout.yaml",
        application=harness.unique_name("application").replace("-", ""),
        timeout=420,
    )

    assert execution.status == "SUCCEEDED"
    rollout = harness.rollout(name)
    result = rollout.wait_healthy(timeout=180)
    assert result.annotations["harness.cicd/revision"] == "raw-v3"
