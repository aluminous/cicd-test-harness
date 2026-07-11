from pathlib import Path

import yaml

from cicd_harness.command import CommandRunner
from cicd_harness.config import load_profile
from cicd_harness.controllers import ControllerStack
from cicd_harness.native_pilot import NativePilotBuilder


def test_legacy_pilot_shim_pins_source_and_only_overrides_pilot_image() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/legacy.yaml", workspace=workspace)
    config = profile.istio.arm64_pilot
    assert config is not None
    builder = NativePilotBuilder(profile, config, CommandRunner(cwd=workspace))

    assert config.source_sha256 == (
        "c737648a6dc6b4bb3a5ac1dfc202469ced73e54c83cc591db917120c5590aae4"
    )
    assert config.git_revision == "fd053c6165d21105d66dac6e3d0649db2dde5b86"
    assert config.containerfile == workspace / "images/istio-pilot-arm64/Containerfile"
    assert builder.helm_values() == (
        "pilot.image=localhost/istio/pilot:1.10.6-arm64-poc",
    )


def test_arm_gateway_placeholder_cannot_be_sidecar_injected() -> None:
    deployment, service = list(
        yaml.safe_load_all(ControllerStack.gateway_stub_manifest("registry.k8s.io/pause:3.9"))
    )

    assert deployment["spec"]["template"]["metadata"]["annotations"] == {
        "sidecar.istio.io/inject": "false"
    }
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert [container["name"] for container in containers] == ["gateway-placeholder"]
    assert service["spec"]["selector"]["istio"] == "ingressgateway"
