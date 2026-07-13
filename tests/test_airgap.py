from pathlib import Path

import pytest

from cicd_harness.airgap import audit_airgap, host_allowed, validate_airgap
from cicd_harness.command import CommandRunner
from cicd_harness.config import AirgapConfig, RegistryConfig, load_profile
from cicd_harness.errors import HarnessError
from cicd_harness.registry import RegistrySupport


def test_bundled_airgap_examples_route_all_controlled_dependencies_internally() -> None:
    workspace = Path(__file__).parents[1]

    for name in ("airgap-modern.example", "airgap-legacy.example"):
        profile = load_profile(workspace / f"profiles/{name}.yaml", workspace=workspace)
        dependencies = validate_airgap(profile)

        assert dependencies
        assert all(item.allowed for item in dependencies)
        assert any(item.name == "Kind binary" for item in dependencies)
        assert any(item.name == "Jenkins plugin downloads" for item in dependencies)

    legacy = load_profile(
        workspace / "profiles/airgap-legacy.example.yaml",
        workspace=workspace,
    )
    legacy_dependencies = audit_airgap(legacy)
    assert any(item.name == "Legacy Istio Go module proxy 1" for item in legacy_dependencies)
    assert not any("checksum database" in item.name for item in legacy_dependencies)


def test_airgap_preflight_reports_every_unmirrored_dependency() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)
    profile = profile.model_copy(
        update={
            "airgap": AirgapConfig(
                enabled=True,
                allowed_hosts=("registry.corp.example", "nexus.corp.example"),
            ),
            "registry": RegistryConfig(
                rewrites={"docker.io": "registry.corp.example/dockerhub"}
            ),
            "kind": profile.kind.model_copy(
                update={
                    "download_url_template": (
                        "https://nexus.corp.example/kind/v{version}/kind-{platform}"
                    )
                }
            ),
        }
    )

    with pytest.raises(HarnessError) as failure:
        validate_airgap(profile)

    message = str(failure.value)
    assert "quay.io" in message
    assert "us-docker.pkg.dev" in message
    assert "updates.jenkins.io" in message


def test_allowed_hosts_distinguish_exact_ports_and_wildcard_subdomains() -> None:
    config = AirgapConfig(
        enabled=True,
        allowed_hosts=("registry.corp.example:5443", "*.services.corp.example"),
    )

    assert host_allowed("registry.corp.example:5443", config)
    assert not host_allowed("registry.corp.example:443", config)
    assert host_allowed("nexus.services.corp.example.", config)
    assert not host_allowed("services.corp.example", config)
    assert host_allowed("wiremock.harness-system.svc.cluster.local", config)
    assert host_allowed("127.0.0.1", config)


def test_airgap_preflight_catches_an_external_spinnaker_s3_endpoint(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(
        workspace / "profiles/airgap-modern.example.yaml",
        workspace=workspace,
    )
    manifest = tmp_path / "spinnaker.yaml"
    manifest.write_text(
        profile.spinnaker.manifest.read_text().replace(
            "http://spin-minio.spinnaker.svc.cluster.local:9000",
            "https://s3.amazonaws.com/harness-front50",
        )
    )
    profile = profile.model_copy(
        update={
            "spinnaker": profile.spinnaker.model_copy(update={"manifest": manifest})
        }
    )

    with pytest.raises(HarnessError, match="s3.amazonaws.com"):
        validate_airgap(profile)


def test_airgap_preflight_rejects_a_go_direct_fallback() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(
        workspace / "profiles/airgap-legacy.example.yaml",
        workspace=workspace,
    )
    native = profile.istio.arm64_pilot.model_copy(
        update={
            "go_proxy": "https://nexus.airgap.example/repository/go-proxy|direct"
        }
    )
    profile = profile.model_copy(
        update={
            "istio": profile.istio.model_copy(update={"arm64_pilot": native})
        }
    )

    with pytest.raises(HarnessError, match="direct fallback"):
        validate_airgap(profile)


def test_airgap_manifest_rejects_a_test_authored_public_image(tmp_path: Path) -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(
        workspace / "profiles/airgap-modern.example.yaml",
        workspace=workspace,
    )
    registry = RegistrySupport(profile, CommandRunner(cwd=tmp_path))

    with pytest.raises(HarnessError, match="air-gap mode rejected image registry"):
        registry.manifest(
            """apiVersion: v1
kind: Pod
metadata:
  name: accidental-public-image
spec:
  containers:
    - name: application
      image: ghcr.io/example/application:latest
"""
        )


def test_airgap_manifest_validation_runs_even_without_rewrites(tmp_path: Path) -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)
    profile = profile.model_copy(
        update={
            "airgap": AirgapConfig(enabled=True, allowed_hosts=("registry.corp.example",)),
            "registry": RegistryConfig(),
        }
    )

    with pytest.raises(HarnessError, match="docker.io"):
        RegistrySupport(profile, CommandRunner(cwd=tmp_path)).manifest(
            """apiVersion: v1
kind: Pod
metadata:
  name: public
spec:
  containers:
    - name: public
      image: busybox:1
"""
        )
