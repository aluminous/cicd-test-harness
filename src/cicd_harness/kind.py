from __future__ import annotations

from contextlib import suppress

from cicd_harness.command import CommandRunner
from cicd_harness.config import HarnessProfile
from cicd_harness.registry import RegistrySupport
from cicd_harness.tooling import ensure_kind_binary


class KindCluster:
    def __init__(
        self,
        profile: HarnessProfile,
        runner: CommandRunner,
        registry: RegistrySupport | None = None,
    ) -> None:
        self.profile = profile
        self.runner = runner
        self.registry = registry or RegistrySupport(profile, runner)
        self._owns_registry = registry is None
        self.registry.install_runtime_auth(profile.runtime.provider)

    @property
    def context(self) -> str:
        return f"kind-{self.profile.kind.cluster_name}"

    def exists(self) -> bool:
        ensure_kind_binary(self.profile.kind)
        result = self.runner.run(
            [self.profile.kind.binary, "get", "clusters"],
            env=self._provider_env(),
            check=False,
        )
        return self.profile.kind.cluster_name in result.stdout.splitlines()

    def create(self) -> None:
        self.registry.install_runtime_auth(self.profile.runtime.provider)
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
                    return
            # A failed Kind create can leave a named node without ever writing
            # kubeconfig, or a stopped node with a stale context. Clear either
            # orphan before retrying the same profile.
            self.delete()
            self.registry.install_runtime_auth(self.profile.runtime.provider)
        config = """kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
"""
        try:
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
        except Exception:
            with suppress(Exception):
                self.delete()
            raise

    def delete(self) -> None:
        try:
            ensure_kind_binary(self.profile.kind)
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
        finally:
            if self._owns_registry:
                self.registry.close()

    def _provider_env(self) -> dict[str, str]:
        provider = self.profile.runtime.provider
        if provider == "podman":
            return {"KIND_EXPERIMENTAL_PROVIDER": "podman"}
        return {}
