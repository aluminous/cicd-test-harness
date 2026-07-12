import json
from pathlib import Path

import pytest
import yaml

from cicd_harness.command import CommandRunner
from cicd_harness.config import load_profile
from cicd_harness.controllers import ControllerStack
from cicd_harness.errors import HarnessError
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
    assert config.pull_before_build is False


def test_pilot_image_build_is_explicitly_linux_arm64_and_runtime_portable() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/legacy.yaml", workspace=workspace)
    profile = profile.model_copy(
        update={"runtime": profile.runtime.model_copy(update={"provider": "docker"})}
    )
    config = profile.istio.arm64_pilot
    assert config is not None
    builder = NativePilotBuilder(profile, config, CommandRunner(cwd=workspace))

    assert builder.build_command(Path("/tmp/context"))[:6] == [
        "docker",
        "build",
        "--no-cache",
        "--platform",
        "linux/arm64",
        "-t",
    ]


def test_pilot_build_stages_license_and_machine_readable_provenance(tmp_path: Path) -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/legacy.yaml", workspace=workspace)
    config = profile.istio.arm64_pilot
    assert config is not None
    builder = NativePilotBuilder(profile, config, CommandRunner(cwd=workspace))
    source = tmp_path / "istio"
    (source / "out").mkdir(parents=True)
    (source / "LICENSE").write_text("Apache License 2.0\n")

    builder._stage_metadata(source)

    assert (source / "out/LICENSE").read_text() == "Apache License 2.0\n"
    metadata = json.loads((source / "out/BUILD-METADATA.json").read_text())
    assert metadata["target"] == "linux/arm64"
    assert metadata["version"] == "1.10.6"
    assert metadata["git_revision"] == config.git_revision
    assert "no proxyv2" in metadata["fidelity"]


def test_pilot_builder_rejects_non_arm64_binary(tmp_path: Path) -> None:
    binary = tmp_path / "pilot-discovery"
    header = bytearray(20)
    header[:4] = b"\x7fELF"
    header[5] = 1
    header[18:20] = (62).to_bytes(2, "little")
    binary.write_bytes(header)

    with pytest.raises(HarnessError, match="expected AArch64"):
        NativePilotBuilder._validate_arm64_elf(binary)


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
