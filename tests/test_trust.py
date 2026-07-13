import ssl
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml

from cicd_harness.command import CommandResult, CommandRunner
from cicd_harness.config import TrustConfig, load_profile
from cicd_harness.errors import HarnessError
from cicd_harness.infra import InfraStack
from cicd_harness.registry import RegistrySupport
from cicd_harness.spinnaker import SpinnakerStack
from cicd_harness.trust import (
    INSECURE_TLS_ENVIRONMENT,
    TRUST_CONFIG_MAP,
    TrustSupport,
    ssl_context,
)


def _test_ca(path: Path) -> Path:
    roots = ssl.create_default_context().get_ca_certs(binary_form=True)
    assert roots, "Python's platform trust store is empty"
    path.write_text(ssl.DER_cert_to_PEM_cert(roots[0]))
    return path


def _profile(
    ca_certificate: Path | None = None,
    *,
    insecure_skip_tls_verify: bool = False,
):
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)
    return profile.model_copy(
        update={
            "trust": TrustConfig(
                ca_certificate=ca_certificate,
                insecure_skip_tls_verify=insecure_skip_tls_verify,
            )
        }
    )


def test_profile_resolves_a_relative_private_ca_path(tmp_path: Path) -> None:
    workspace = Path(__file__).parents[1]
    raw = yaml.safe_load((workspace / "profiles/modern.yaml").read_text())
    raw["trust"] = {"ca_certificate": "certificates/corporate-ca.pem"}
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(raw))

    profile = load_profile(profile_path, workspace=tmp_path)

    assert profile.trust.ca_certificate == tmp_path / "certificates/corporate-ca.pem"


def test_client_trust_is_additive_private_and_restores_runner_env(tmp_path: Path) -> None:
    ca_certificate = _test_ca(tmp_path / "corporate-ca.pem")
    runner = CommandRunner(cwd=tmp_path, base_env={"SSL_CERT_FILE": "/previous/bundle"})
    trust = TrustSupport(_profile(ca_certificate), runner)

    trust.install_client_env()

    bundle = Path(runner.base_env["SSL_CERT_FILE"])
    assert bundle.is_file()
    assert ca_certificate.read_text().strip() in bundle.read_text()
    assert runner.base_env["GIT_SSL_CAINFO"] == str(bundle)
    assert runner.base_env["NODE_EXTRA_CA_CERTS"] == str(ca_certificate)
    ssl_context(ca_certificate)

    trust.close()

    assert runner.base_env["SSL_CERT_FILE"] == "/previous/bundle"
    assert "GIT_SSL_CAINFO" not in runner.base_env
    assert not bundle.exists()


def test_invalid_private_ca_fails_before_network_use(tmp_path: Path) -> None:
    ca_certificate = tmp_path / "not-a-certificate.pem"
    ca_certificate.write_text("not a certificate\n")

    with pytest.raises(HarnessError, match="must contain a PEM certificate"):
        TrustSupport(
            _profile(ca_certificate),
            CommandRunner(cwd=tmp_path),
        ).install_client_env()


def test_insecure_ssl_context_disables_certificate_and_hostname_checks() -> None:
    context = ssl_context(None, insecure_skip_tls_verify=True)

    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_insecure_client_environment_is_process_scoped_and_restored(
    tmp_path: Path,
) -> None:
    runner = CommandRunner(
        cwd=tmp_path,
        base_env={"GIT_SSL_NO_VERIFY": "previous"},
    )
    trust = TrustSupport(_profile(insecure_skip_tls_verify=True), runner)

    trust.install_client_env()

    assert runner.base_env["CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY"] == "1"
    assert runner.base_env["GIT_SSL_NO_VERIFY"] == "true"
    curl_home = Path(runner.base_env["CURL_HOME"])
    wgetrc = Path(runner.base_env["WGETRC"])
    assert (curl_home / ".curlrc").read_text() == "insecure\n"
    assert wgetrc.read_text() == "check_certificate = off\n"
    assert stat.S_IMODE(curl_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(wgetrc.stat().st_mode) == 0o600

    trust.close()

    assert runner.base_env["GIT_SSL_NO_VERIFY"] == "previous"
    assert "CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY" not in runner.base_env
    assert not curl_home.exists()


def test_private_ca_file_rejects_private_key_material(tmp_path: Path) -> None:
    ca_certificate = _test_ca(tmp_path / "corporate-ca.pem")
    ca_certificate.write_text(
        ca_certificate.read_text()
        + "-----BEGIN PRIVATE KEY-----\nnever-copy-this\n-----END PRIVATE KEY-----\n"
    )

    with pytest.raises(HarnessError, match="must not contain a private key"):
        TrustSupport(
            _profile(ca_certificate),
            CommandRunner(cwd=tmp_path),
        ).install_client_env()


def test_rootful_podman_machine_receives_private_ca(tmp_path: Path) -> None:
    ca_certificate = _test_ca(tmp_path / "corporate-ca.pem")
    runner = Mock()
    runner.base_env = {}
    runner.run.return_value = CommandResult(("podman",), 0, "", "")
    trust = TrustSupport(_profile(ca_certificate), runner)

    with patch("cicd_harness.trust.platform.system", return_value="Darwin"):
        trust.prepare_runtime("podman")

    command = runner.run.call_args.args[0]
    assert command[:5] == ["podman", "machine", "ssh", "sudo", "sh"]
    assert "update-ca-trust extract" in command[-1]
    assert runner.run.call_args.kwargs["input_text"] == ca_certificate.read_text()
    trust.close()


def test_kind_node_receives_private_ca_and_recovers(tmp_path: Path) -> None:
    ca_certificate = _test_ca(tmp_path / "corporate-ca.pem")
    runner = Mock()
    runner.base_env = {}
    runner.run.side_effect = [
        CommandResult(("podman", "exec"), 0, "", ""),
        CommandResult(("kubectl", "get"), 0, "ok", ""),
    ]
    trust = TrustSupport(_profile(ca_certificate), runner)

    trust.install_kind_node("podman", "private-ca-test")

    install = runner.run.call_args_list[0]
    assert install.args[0][:5] == [
        "podman",
        "exec",
        "-i",
        "private-ca-test-control-plane",
        "sh",
    ]
    assert "update-ca-certificates" in install.args[0][-1]
    assert install.kwargs["input_text"] == ca_certificate.read_text()
    assert "kind-private-ca-test" in runner.run.call_args_list[1].args[0]


def test_kind_containerd_receives_host_scoped_insecure_registry_config() -> None:
    runner = Mock()
    runner.base_env = {}
    runner.run.side_effect = [
        CommandResult(("podman", "exec"), 0, "changed\n", ""),
        CommandResult(("podman", "exec"), 0, "", ""),
        CommandResult(("podman", "exec"), 0, "", ""),
        CommandResult(("podman", "exec"), 0, "", ""),
        CommandResult(("kubectl", "get"), 0, "ok", ""),
    ]
    trust = TrustSupport(_profile(insecure_skip_tls_verify=True), runner)

    trust.install_kind_insecure_registries(
        "podman",
        "insecure-test",
        ("registry.example:5443", "mirror.example", "docker.io"),
    )

    first = runner.run.call_args_list[0]
    assert first.args[0][-2:] == ["sh", "registry.example:5443"]
    assert 'server = "https://registry.example:5443"' in first.kwargs["input_text"]
    assert "skip_verify = true" in first.kwargs["input_text"]
    docker_hub = runner.run.call_args_list[2]
    assert 'server = "https://registry-1.docker.io"' in (
        docker_hub.kwargs["input_text"]
    )
    assert '[host."https://registry-1.docker.io"]' in (
        docker_hub.kwargs["input_text"]
    )
    assert runner.run.call_args_list[3].args[0] == [
        "podman",
        "exec",
        "insecure-test-control-plane",
        "systemctl",
        "restart",
        "containerd",
    ]
    assert "kind-insecure-test" in runner.run.call_args_list[4].args[0]


def test_kind_insecure_registry_rejects_unsafe_host_values() -> None:
    runner = Mock(base_env={})
    trust = TrustSupport(_profile(insecure_skip_tls_verify=True), runner)

    with pytest.raises(HarnessError, match="cannot configure insecure containerd"):
        trust.install_kind_insecure_registries(
            "podman",
            "insecure-test",
            ('registry.example".malformed',),
        )

    runner.run.assert_not_called()


def test_workload_manifests_mount_combined_trust_without_overriding_test_env(
    tmp_path: Path,
) -> None:
    ca_certificate = _test_ca(tmp_path / "corporate-ca.pem")
    runner = CommandRunner(cwd=tmp_path)
    registry = RegistrySupport(_profile(ca_certificate), runner)
    manifest = registry.manifest(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: sample
spec:
  template:
    spec:
      containers:
        - name: sample
          image: example/application:1
          env:
            - name: SSL_CERT_FILE
              value: /application/custom.pem
"""
    )

    pod_spec = yaml.safe_load(manifest)["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["SSL_CERT_FILE"] == "/application/custom.pem"
    assert environment["GIT_SSL_CAINFO"].endswith("/ca-bundle.crt")
    assert container["volumeMounts"][0]["name"] == TRUST_CONFIG_MAP
    assert pod_spec["volumes"][0]["configMap"]["name"] == TRUST_CONFIG_MAP

    kubectl = SimpleNamespace(apply=Mock())
    registry.ensure_namespace(kubectl, "application-test")
    config_map = yaml.safe_load(kubectl.apply.call_args_list[1].args[0])
    assert config_map["metadata"] == {
        "name": TRUST_CONFIG_MAP,
        "namespace": "application-test",
    }
    assert ca_certificate.read_text().strip() in config_map["data"]["ca-bundle.crt"]


def test_insecure_workload_manifests_receive_client_fallbacks(tmp_path: Path) -> None:
    registry = RegistrySupport(
        _profile(insecure_skip_tls_verify=True),
        CommandRunner(cwd=tmp_path),
    )
    manifest = registry.manifest(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: sample
spec:
  template:
    spec:
      containers:
        - name: sample
          image: example/application:1
          env:
            - name: NODE_TLS_REJECT_UNAUTHORIZED
              value: application-choice
"""
    )

    pod_spec = yaml.safe_load(manifest)["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["NODE_TLS_REJECT_UNAUTHORIZED"] == "application-choice"
    for name, value in INSECURE_TLS_ENVIRONMENT.items():
        if name != "NODE_TLS_REJECT_UNAUTHORIZED":
            assert environment[name] == value
    assert container["volumeMounts"][0]["name"] == TRUST_CONFIG_MAP

    kubectl = SimpleNamespace(apply=Mock())
    registry.ensure_namespace(kubectl, "application-test")
    config_map = yaml.safe_load(kubectl.apply.call_args_list[1].args[0])
    assert config_map["data"] == {
        ".curlrc": "insecure\n",
        "wgetrc": "check_certificate = off\n",
    }


def test_wiremock_and_gitea_receive_insecure_fallbacks(tmp_path: Path) -> None:
    profile = _profile(insecure_skip_tls_verify=True)
    runner = Mock(cwd=Path(__file__).parents[1])
    kubectl = SimpleNamespace(runner=runner)

    documents = list(
        yaml.safe_load_all(InfraStack(profile, kubectl, runner.cwd).manifest())
    )
    deployments = {
        document["metadata"]["name"]: document
        for document in documents
        if document is not None and document.get("kind") == "Deployment"
    }
    wiremock = deployments["wiremock"]["spec"]["template"]["spec"]["containers"][0]
    wiremock_environment = {
        item["name"]: item["value"] for item in wiremock["env"]
    }
    assert wiremock_environment["CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY"] == "1"
    assert "--trust-all-proxy-targets" not in wiremock["args"]
    gitea = deployments["gitea"]["spec"]["template"]["spec"]["containers"][0]
    gitea_environment = {
        item["name"]: item["value"] for item in gitea["env"]
    }
    assert gitea_environment["GITEA__webhook__SKIP_TLS_VERIFY"] == "true"


def test_wiremock_and_spinnaker_java_processes_receive_generated_trust_stores(
    tmp_path: Path,
) -> None:
    ca_certificate = _test_ca(tmp_path / "corporate-ca.pem")
    profile = _profile(ca_certificate)
    runner = Mock()
    runner.cwd = Path(__file__).parents[1]
    kubectl = SimpleNamespace(runner=runner)
    registry = RegistrySupport(profile, runner)

    infra_documents = list(
        yaml.safe_load_all(
            InfraStack(profile, kubectl, runner.cwd, registry).manifest()
        )
    )
    wiremock = next(
        document
        for document in infra_documents
        if document is not None
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == "wiremock"
    )
    wiremock_spec = wiremock["spec"]["template"]["spec"]
    assert wiremock_spec["initContainers"][0]["name"] == "harness-java-trust"
    wiremock_env = {
        item["name"]: item["value"]
        for item in wiremock_spec["containers"][0]["env"]
    }
    assert wiremock_env["JAVA_TOOL_OPTIONS"].endswith("/cacerts")

    spinnaker_documents = list(
        yaml.safe_load_all(
            SpinnakerStack(
                profile,
                SimpleNamespace(),
                kubectl,
                runner,
                registry,
            ).manifest()
        )
    )
    java_deployments = [
        document
        for document in spinnaker_documents
        if document is not None
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name")
        in {
            "spin-clouddriver",
            "spin-front50",
            "spin-gate",
            "spin-orca",
            "spin-rosco",
        }
    ]
    assert len(java_deployments) == 5
    assert all(
        deployment["spec"]["template"]["spec"]["initContainers"][0]["name"]
        == "harness-java-trust"
        for deployment in java_deployments
    )
