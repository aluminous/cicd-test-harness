from __future__ import annotations

import os

import pytest

from cicd_harness.controllers import ControllerStack
from cicd_harness.rollouts import RolloutProbe

pytestmark = [
    pytest.mark.poc,
    pytest.mark.skipif(not os.getenv("CICD_RUN_POC"), reason="PoC disabled"),
]


def test_argo_rollout_updates_istio_virtual_service(poc_cluster) -> None:
    workspace, profile, runner, _cluster, kubectl = poc_cluster
    controllers = ControllerStack(profile, kubectl, runner)
    controllers.install_argo_rollouts()
    controllers.install_istio()

    runner.run(
        kubectl.command(
            "delete",
            "namespace",
            "rollout-poc",
            "--ignore-not-found",
            "--wait=true",
        )
    )
    kubectl.apply_file(workspace / "manifests/rollout-poc.yaml")
    rollout = RolloutProbe(kubectl, "rollout-poc", "rollout-poc")
    rollout.wait_healthy(timeout=300)

    runner.run(
        kubectl.command(
            "-n",
            "rollout-poc",
            "patch",
            "rollout",
            "rollout-poc",
            "--type=merge",
            "-p",
            '{"spec":{"template":{"metadata":{"annotations":'
            '{"harness.test/revision":"v2"}}}}}',
        )
    )
    rollout.wait_paused(timeout=180)
    states = rollout.replica_sets()
    assert len([state for state in states if state.role == "stable" and state.desired > 0]) == 1
    assert len([state for state in states if state.role == "canary" and state.desired > 0]) == 1

    virtual_service = kubectl.get_json(
        "virtualservice/rollout-poc",
        "-n",
        "rollout-poc",
    )
    weights = [route["weight"] for route in virtual_service["spec"]["http"][0]["route"]]
    assert weights == [50, 50]

    rollout.wait_healthy(timeout=90)
    final_states = rollout.replica_sets()
    active_stable = [
        state for state in final_states if state.role == "stable" and state.desired > 0
    ]
    assert len(active_stable) == 1
    assert any(state.role == "old" for state in final_states)
