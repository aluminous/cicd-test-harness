import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError

from cicd_harness.command import CommandResult, CommandRunner
from cicd_harness.config import (
    RegistryConfig,
    RegistryCredentialConfig,
    TrustConfig,
    load_profile,
)
from cicd_harness.controllers import ControllerStack
from cicd_harness.errors import HarnessError
from cicd_harness.infra import InfraStack
from cicd_harness.jenkins import JenkinsStack
from cicd_harness.kind import KindCluster
from cicd_harness.registry import RegistrySupport
from cicd_harness.spinnaker import SpinnakerStack


def _profile(registry: RegistryConfig):
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)
    return profile.model_copy(update={"registry": registry})


def test_image_rewrites_are_canonical_and_use_the_longest_prefix(tmp_path: Path) -> None:
    profile = _profile(
        RegistryConfig(
            rewrites={
                "docker.io": "registry.example/mirror",
                "docker.io/jenkins": "registry.example/ci/jenkins",
                "registry.k8s.io": "registry.example/kubernetes",
            }
        )
    )
    registry = RegistrySupport(profile, CommandRunner(cwd=tmp_path))

    assert registry.image("redis:7") == "registry.example/mirror/library/redis:7"
    assert registry.image("wiremock/wiremock:3") == (
        "registry.example/mirror/wiremock/wiremock:3"
    )
    assert registry.image("jenkins/jenkins:2") == (
        "registry.example/ci/jenkins/jenkins:2"
    )
    assert registry.image("registry.k8s.io/pause:3.9") == (
        "registry.example/kubernetes/pause:3.9"
    )
    assert registry.image("quay.io/argoproj/rollouts:v1") == (
        "quay.io/argoproj/rollouts:v1"
    )


def test_manifest_only_rewrites_actual_container_images(tmp_path: Path) -> None:
    registry = RegistrySupport(
        _profile(RegistryConfig(rewrites={"docker.io": "registry.example/cache"})),
        CommandRunner(cwd=tmp_path),
    )
    manifest = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: sample
spec:
  template:
    spec:
      initContainers:
        - name: setup
          image: busybox:1
      containers:
        - name: app
          image: example/application:2
---
apiVersion: example.io/v1
kind: ImagePolicy
spec:
  image: string
"""

    deployment, policy = list(yaml.safe_load_all(registry.manifest(manifest)))

    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["initContainers"][0]["image"] == (
        "registry.example/cache/library/busybox:1"
    )
    assert pod_spec["containers"][0]["image"] == (
        "registry.example/cache/example/application:2"
    )
    assert policy["spec"]["image"] == "string"


def test_istio_values_cover_modern_and_legacy_chart_shapes(tmp_path: Path) -> None:
    config = RegistryConfig(
        rewrites={"docker.io": "registry.example/cache/docker.io"},
        credentials=(
            RegistryCredentialConfig(
                server="registry.example",
                username_env="REGISTRY_USER",
                password_env="REGISTRY_PASSWORD",
            ),
        ),
    )
    modern = RegistrySupport(_profile(config), CommandRunner(cwd=tmp_path))
    workspace = Path(__file__).parents[1]
    legacy_profile = load_profile(
        workspace / "profiles/legacy.yaml",
        workspace=workspace,
    ).model_copy(update={"registry": config})
    legacy = RegistrySupport(legacy_profile, CommandRunner(cwd=tmp_path))

    assert "image=registry.example/cache/docker.io/istio/pilot:1.25.5" in (
        modern.istio_image_values("1.25.5", modern=True)
    )
    assert "imagePullSecrets[0]=harness-registry" in (
        modern.gateway_image_values("1.25.5", modern=True)
    )
    assert "pilot.image=registry.example/cache/docker.io/istio/pilot:1.10.6" in (
        legacy.istio_image_values("1.10.6", modern=False)
    )
    assert "global.imagePullSecrets[0]=harness-registry" in (
        legacy.gateway_image_values("1.10.6", modern=False)
    )


def test_kind_uses_the_effective_private_node_image() -> None:
    profile = _profile(
        RegistryConfig(rewrites={"docker.io": "registry.example/cache/docker.io"})
    )
    runner = Mock()
    runner.base_env = {}
    runner.run.side_effect = [
        CommandResult(("kind", "get", "clusters"), 0, "", ""),
        CommandResult(("kind", "create", "cluster"), 0, "", ""),
    ]

    KindCluster(profile, runner).create()

    create_command = runner.run.call_args_list[1].args[0]
    image_index = create_command.index("--image") + 1
    assert create_command[image_index].startswith(
        "registry.example/cache/docker.io/kindest/node:v1.31.14@sha256:"
    )


def test_insecure_tls_uses_podman_cli_and_kind_containerd_switches(
    tmp_path: Path,
) -> None:
    profile = _profile(RegistryConfig()).model_copy(
        update={"trust": TrustConfig(insecure_skip_tls_verify=True)}
    )
    runner = Mock(cwd=tmp_path)
    runner.base_env = {}
    registry = RegistrySupport(profile, runner)
    cluster = KindCluster(profile, runner, registry=registry)

    try:
        assert registry.runtime_tls_args("podman") == ("--tls-verify=false",)
        assert registry.runtime_tls_args("docker") == ()
        config = cluster._cluster_config()
        assert "containerdConfigPatches" not in config
    finally:
        cluster.trust.close()
        registry.close()

    workspace = Path(__file__).parents[1]
    legacy = load_profile(
        workspace / "profiles/legacy.yaml",
        workspace=workspace,
    ).model_copy(update={"trust": TrustConfig(insecure_skip_tls_verify=True)})
    legacy_runner = Mock(cwd=tmp_path)
    legacy_runner.base_env = {}
    legacy_registry = RegistrySupport(legacy, legacy_runner)
    legacy_cluster = KindCluster(legacy, legacy_runner, registry=legacy_registry)
    try:
        legacy_config = legacy_cluster._cluster_config()
        assert "insecure_skip_verify = true" in legacy_config
        assert 'registry.configs."docker.io".tls' in legacy_config
        assert "io.containerd.cri.v1.images" not in legacy_config
    finally:
        legacy_cluster.trust.close()
        legacy_registry.close()


def test_runtime_auth_files_are_private_and_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REGISTRY_USER", "ci-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "do-not-log-this")
    profile = _profile(
        RegistryConfig(
            credentials=(
                RegistryCredentialConfig(
                    server="registry.example:5000",
                    username_env="REGISTRY_USER",
                    password_env="REGISTRY_PASSWORD",
                ),
            )
        )
    )
    runner = CommandRunner(cwd=tmp_path, base_env={"REGISTRY_AUTH_FILE": "/previous/auth"})
    registry = RegistrySupport(profile, runner)

    registry.install_runtime_auth("podman")
    auth_path = Path(runner.base_env["REGISTRY_AUTH_FILE"])
    auth_directory = auth_path.parent
    contents = json.loads(auth_path.read_text())

    assert stat.S_IMODE(auth_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    assert contents["auths"]["registry.example:5000"]["auth"]
    assert registry.redaction_values() == ("do-not-log-this",)

    registry.close()

    assert runner.base_env["REGISTRY_AUTH_FILE"] == "/previous/auth"
    assert not auth_directory.exists()


def test_credentials_inject_pull_secret_into_named_service_account_workloads(
    tmp_path: Path,
) -> None:
    profile = _profile(
        RegistryConfig(
            credentials=(
                RegistryCredentialConfig(
                    server="registry.example",
                    username_env="REGISTRY_USER",
                    password_env="REGISTRY_PASSWORD",
                ),
            )
        )
    )
    registry = RegistrySupport(profile, CommandRunner(cwd=tmp_path))
    rendered = registry.manifest(
        """apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      serviceAccountName: rollout-controller
      imagePullSecrets:
        - name: existing
      containers:
        - name: controller
          image: quay.io/argoproj/rollouts:v1
"""
    )

    pod_spec = yaml.safe_load(rendered)["spec"]["template"]["spec"]

    assert pod_spec["imagePullSecrets"] == [
        {"name": "existing"},
        {"name": "harness-registry"},
    ]


def test_missing_registry_environment_names_are_reported_without_values(
    tmp_path: Path,
) -> None:
    profile = _profile(
        RegistryConfig(
            credentials=(
                RegistryCredentialConfig(
                    server="registry.example",
                    username_env="MISSING_REGISTRY_USER",
                    password_env="MISSING_REGISTRY_PASSWORD",
                ),
            )
        )
    )

    with pytest.raises(HarnessError, match="MISSING_REGISTRY_PASSWORD"):
        RegistrySupport(profile, CommandRunner(cwd=tmp_path)).runtime_env("docker")


def test_namespace_gets_pull_secret_and_default_service_account(tmp_path: Path) -> None:
    profile = _profile(
        RegistryConfig(
            credentials=(
                RegistryCredentialConfig(
                    server="registry.example",
                    username_env="REGISTRY_USER",
                    password_env="REGISTRY_PASSWORD",
                ),
            ),
            pull_secret_name="private-pull",
        )
    )
    runner = Mock()
    runner.base_env = {}
    kubectl = SimpleNamespace(
        apply=Mock(),
        get_json=Mock(return_value={"imagePullSecrets": [{"name": "existing"}]}),
        runner=runner,
        command=lambda *args: list(args),
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("REGISTRY_USER", "user")
        patch.setenv("REGISTRY_PASSWORD", "password")
        RegistrySupport(profile, runner).ensure_namespace(kubectl, "application-test")

    applied = "\n".join(call.args[0] for call in kubectl.apply.call_args_list)
    assert "name: application-test" in applied
    assert "name: private-pull" in applied
    patch_payload = json.loads(runner.run.call_args.args[0][-1])
    assert patch_payload["imagePullSecrets"] == [
        {"name": "existing"},
        {"name": "private-pull"},
    ]


def test_all_component_manifests_use_effective_registry_images(tmp_path: Path) -> None:
    workspace = Path(__file__).parents[1]
    profile = _profile(
        RegistryConfig(
            rewrites={
                "docker.io": "registry.example/docker",
                "docker.gitea.com": "registry.example/gitea",
                "us-docker.pkg.dev": "registry.example/google",
                "localhost": "registry.example/local",
            }
        )
    )
    runner = Mock()
    runner.cwd = workspace
    kubectl = SimpleNamespace(runner=runner)
    registry = RegistrySupport(profile, runner)

    infra = InfraStack(profile, kubectl, workspace, registry).manifest()
    jenkins = JenkinsStack(profile, kubectl, registry).manifest()
    spinnaker = SpinnakerStack(
        profile,
        SimpleNamespace(),
        kubectl,
        runner,
        registry,
    ).manifest()

    assert "registry.example/docker/wiremock/wiremock:3.13.1" in infra
    assert "__WIREMOCK_" not in infra
    assert "--max-request-journal-entries" in infra
    assert "--logged-response-body-size-limit" in infra
    assert "--proxy-timeout" in infra
    assert "registry.example/gitea/gitea:1.26.4-rootless" in infra
    assert "registry.example/local/cicd-harness/jenkins" in jenkins
    assert "registry.example/google/spinnaker-community/docker/gate" in spinnaker
    assert "registry.example/docker/library/redis:7.2.7-alpine" in spinnaker


def test_component_manifests_disable_nonessential_runtime_egress(tmp_path: Path) -> None:
    profile = _profile(RegistryConfig())
    runner = Mock(cwd=tmp_path)
    kubectl = SimpleNamespace(runner=runner)

    jenkins_documents = list(
        yaml.safe_load_all(JenkinsStack(profile, kubectl).manifest())
    )
    jenkins = next(
        document
        for document in jenkins_documents
        if document.get("kind") == "Deployment"
        and document["metadata"]["name"] == "jenkins"
    )
    jenkins_opts = next(
        item["value"]
        for item in jenkins["spec"]["template"]["spec"]["containers"][0]["env"]
        if item["name"] == "JAVA_OPTS"
    )
    assert "-Dhudson.model.UpdateCenter.never=true" in jenkins_opts
    assert "DownloadService$Downloadable.defaultInterval=9223372036854775807" in (
        jenkins_opts
    )

    spinnaker_documents = list(
        yaml.safe_load_all(
            SpinnakerStack(profile, SimpleNamespace(), kubectl, runner).manifest()
        )
    )
    for service in ("gate", "front50", "orca", "clouddriver", "rosco"):
        deployment = next(
            document
            for document in spinnaker_documents
            if document.get("kind") == "Deployment"
            and document["metadata"]["name"] == f"spin-{service}"
        )
        host_aliases = deployment["spec"]["template"]["spec"]["hostAliases"]
        assert host_aliases == [
            {"ip": "127.0.0.1", "hostnames": ["raw.githubusercontent.com"]}
        ]
    minio = next(
        document
        for document in spinnaker_documents
        if document.get("kind") == "Deployment"
        and document["metadata"]["name"] == "spin-minio"
    )
    minio_env = {
        item["name"]: item["value"]
        for item in minio["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert minio_env["MINIO_UPDATE"] == "off"
    assert minio_env["MINIO_CALLHOME_ENABLE"] == "off"
    assert minio_env["MINIO_BROWSER"] == "off"


def test_istio_helm_values_keep_cloud_metadata_probe_on_loopback(tmp_path: Path) -> None:
    profile = _profile(RegistryConfig())
    runner = Mock(cwd=tmp_path)
    runner.run.return_value = CommandResult(("helm", "version"), 0, "v3.17.0", "")
    kubectl = Mock(context="kind-test", runner=runner)
    registry = Mock()
    registry.istio_image_values.return_value = ()
    registry.gateway_image_values.return_value = ()
    stack = ControllerStack(profile, kubectl, runner, registry)
    stack._helm_upgrade = Mock()  # type: ignore[method-assign]

    stack.install_istio()

    upgrades = {call.args[0]: call for call in stack._helm_upgrade.call_args_list}
    assert "pilot.env.GCE_METADATA_HOST=127.0.0.1:9" in (
        upgrades["istiod"].kwargs["values"]
    )
    assert "env.GCE_METADATA_HOST=127.0.0.1:9" in (
        upgrades["istio-ingressgateway"].kwargs["values"]
    )


def test_istio_processes_receive_global_insecure_tls_marker(tmp_path: Path) -> None:
    profile = _profile(RegistryConfig()).model_copy(
        update={"trust": TrustConfig(insecure_skip_tls_verify=True)}
    )
    runner = Mock(cwd=tmp_path)
    runner.run.return_value = CommandResult(("helm", "version"), 0, "v3.17.0", "")
    kubectl = Mock(context="kind-test", runner=runner)
    registry = Mock()
    registry.istio_image_values.return_value = ()
    registry.gateway_image_values.return_value = ()
    stack = ControllerStack(profile, kubectl, runner, registry)
    stack._helm_upgrade = Mock()  # type: ignore[method-assign]

    stack.install_istio()

    upgrades = {call.args[0]: call for call in stack._helm_upgrade.call_args_list}
    assert "pilot.env.CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY=1" in (
        upgrades["istiod"].kwargs["values"]
    )
    assert "env.CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY=1" in (
        upgrades["istio-ingressgateway"].kwargs["values"]
    )


def test_registry_configuration_rejects_urls_and_server_paths() -> None:
    with pytest.raises(ValidationError, match="URL scheme"):
        RegistryConfig(rewrites={"https://docker.io": "registry.example"})
    with pytest.raises(ValidationError, match="hostname"):
        RegistryCredentialConfig(
            server="registry.example/team",
            username_env="USER_ENV",
            password_env="PASSWORD_ENV",
        )
