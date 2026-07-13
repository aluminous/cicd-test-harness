from __future__ import annotations

import logging
from contextlib import suppress

from cicd_harness.command import CommandRunner
from cicd_harness.config import HarnessProfile
from cicd_harness.registry import RegistrySupport
from cicd_harness.tooling import ensure_kind_binary
from cicd_harness.trust import TrustSupport, ssl_context, validate_registry_hosts

logger = logging.getLogger(__name__)


class KindCluster:
    def __init__(
        self,
        profile: HarnessProfile,
        runner: CommandRunner,
        registry: RegistrySupport | None = None,
        trust: TrustSupport | None = None,
    ) -> None:
        self.profile = profile
        self.runner = runner
        self.registry = registry or RegistrySupport(profile, runner)
        self._owns_registry = registry is None
        self.trust = trust or TrustSupport(profile, runner)
        self._owns_trust = trust is None
        self.trust.install_client_env()
        self.registry.install_runtime_auth(profile.runtime.provider)

    @property
    def context(self) -> str:
        return f"kind-{self.profile.kind.cluster_name}"

    def exists(self) -> bool:
        ensure_kind_binary(
            self.profile.kind,
            verify=ssl_context(
                self.profile.trust.ca_certificate,
                insecure_skip_tls_verify=self.profile.trust.insecure_skip_tls_verify,
            ),
        )
        result = self.runner.run(
            [self.profile.kind.binary, "get", "clusters"],
            env=self._provider_env(),
            check=False,
        )
        return self.profile.kind.cluster_name in result.stdout.splitlines()

    def create(self) -> None:
        self.trust.prepare_runtime(self.profile.runtime.provider)
        self.registry.install_runtime_auth(self.profile.runtime.provider)
        self._prepare_insecure_node_image()
        if self.exists():
            context = self.runner.run(
                ["kubectl", "config", "get-contexts", self.context],
                check=False,
                timeout=15,
            )
            if context.returncode == 0:
                ready = self.runner.run(
                    [
                        "kubectl",
                        "--context",
                        self.context,
                        "get",
                        "--raw=/readyz",
                    ],
                    check=False,
                    timeout=15,
                )
                if ready.returncode == 0:
                    self._configure_node_trust()
                    logger.info("reusing ready Kind cluster %s", self.profile.kind.cluster_name)
                    return
            # A failed Kind create can leave a named node without ever writing
            # kubeconfig, or a stopped node with a stale context. Clear either
            # orphan before retrying the same profile.
            self.delete()
            self.trust.install_client_env()
            self.registry.install_runtime_auth(self.profile.runtime.provider)
        config = self._cluster_config()
        try:
            logger.info(
                "creating Kind cluster %s with %s",
                self.profile.kind.cluster_name,
                self.registry.image(self.profile.kind.node_image),
            )
            self.runner.run(
                [
                    self.profile.kind.binary,
                    "create",
                    "cluster",
                    "--name",
                    self.profile.kind.cluster_name,
                    "--image",
                    self.registry.image(self.profile.kind.node_image),
                    "--wait",
                    f"{self.profile.kind.wait_seconds}s",
                    "--config",
                    "-",
                ],
                input_text=config,
                env=self._provider_env(),
                timeout=self.profile.kind.wait_seconds + 120,
            )
            self._configure_node_trust()
            logger.info("Kind cluster %s is ready", self.profile.kind.cluster_name)
        except Exception:
            with suppress(Exception):
                self.delete()
            raise

    def delete(self) -> None:
        try:
            ensure_kind_binary(
                self.profile.kind,
                verify=ssl_context(
                    self.profile.trust.ca_certificate,
                    insecure_skip_tls_verify=self.profile.trust.insecure_skip_tls_verify,
                ),
            )
            logger.info("deleting Kind cluster %s", self.profile.kind.cluster_name)
            self.runner.run(
                [
                    self.profile.kind.binary,
                    "delete",
                    "cluster",
                    "--name",
                    self.profile.kind.cluster_name,
                ],
                env=self._provider_env(),
                check=False,
                timeout=60,
            )
            logger.info("Kind cluster %s deleted", self.profile.kind.cluster_name)
        finally:
            if self._owns_registry:
                self.registry.close()
            if self._owns_trust:
                self.trust.close()

    def _provider_env(self) -> dict[str, str]:
        provider = self.profile.runtime.provider
        if provider == "podman":
            return {"KIND_EXPERIMENTAL_PROVIDER": "podman"}
        return {}

    def _cluster_config(self) -> str:
        patches = ""
        if (
            self.profile.trust.insecure_skip_tls_verify
            and not self._kind_has_registry_config_path()
        ):
            hosts = validate_registry_hosts(
                self.registry.controlled_image_hosts()
            )
            registry_tls = "\n".join(
                (
                    "  [plugins.\"io.containerd.grpc.v1.cri\".registry.configs."
                    f"\"{host}\".tls]\n"
                    "    insecure_skip_verify = true"
                )
                for host in hosts
            )
            patches = f"""containerdConfigPatches:
- |-
{registry_tls}
"""
        return f"""kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
{patches}nodes:
- role: control-plane
"""

    def _kind_has_registry_config_path(self) -> bool:
        mode = self.profile.kind.containerd_registry_mode
        if mode != "auto":
            return mode == "hosts"
        try:
            major, minor, *_ = (
                int(part)
                for part in self.profile.kind.version.removeprefix("v").split(".")
            )
        except ValueError as exc:
            raise ValueError(
                f"invalid Kind version: {self.profile.kind.version}"
            ) from exc
        return (major, minor) >= (0, 27)

    def _configure_node_trust(self) -> None:
        provider = self.profile.runtime.provider
        cluster_name = self.profile.kind.cluster_name
        self.trust.install_kind_node(provider, cluster_name)
        if not self.profile.trust.insecure_skip_tls_verify:
            return
        if self._kind_has_registry_config_path():
            self.trust.install_kind_insecure_registries(
                provider,
                cluster_name,
                self.registry.controlled_image_hosts(),
            )
        else:
            logger.warning(
                "Kind containerd TLS verification uses legacy registry config for %s",
                cluster_name,
            )

    def _prepare_insecure_node_image(self) -> None:
        if not self.profile.trust.insecure_skip_tls_verify:
            return
        provider = self.profile.runtime.provider
        image = self.registry.image(self.profile.kind.node_image)
        present = self.runner.run(
            [provider, "image", "inspect", image],
            check=False,
            timeout=30,
        )
        if present.returncode == 0:
            return
        if provider == "podman":
            self.runner.run(
                ["podman", "pull", "--tls-verify=false", image],
                timeout=900,
            )
        else:
            logger.warning(
                "Docker has no per-pull TLS bypass; its daemon must mark %s insecure",
                image.partition("/")[0],
            )
