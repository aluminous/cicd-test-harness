from __future__ import annotations

import json
from pathlib import Path

from cicd_harness.command import CommandRunner
from cicd_harness.config import HarnessProfile
from cicd_harness.kubectl import Kubectl
from cicd_harness.native_pilot import NativePilotBuilder
from cicd_harness.registry import RegistrySupport


class ControllerStack:
    def __init__(
        self,
        profile: HarnessProfile,
        kubectl: Kubectl,
        runner: CommandRunner,
        registry: RegistrySupport | None = None,
    ) -> None:
        self.profile = profile
        self.kubectl = kubectl
        self.runner = runner
        self.registry = registry or RegistrySupport(profile, runner)

    def install_argo_rollouts(self, *, timeout: int = 300) -> None:
        self.registry.ensure_namespace(self.kubectl, "argo-rollouts")
        self.kubectl.apply(
            self.registry.manifest(self.profile.argo_rollouts.install_manifest.read_text()),
            namespace="argo-rollouts",
        )
        if self.profile.argo_rollouts.notifications_manifest is not None:
            self.kubectl.apply(
                self.registry.manifest(
                    self.profile.argo_rollouts.notifications_manifest.read_text()
                ),
                namespace="argo-rollouts",
            )
        self.kubectl.wait_available("argo-rollouts", "argo-rollouts", timeout=timeout)

    def install_istio(self, *, timeout: int = 300) -> None:
        self.registry.ensure_namespace(self.kubectl, "istio-system")
        self.registry.ensure_namespace(
            self.kubectl,
            self.profile.istio.gateway.namespace,
        )
        chart_root = self.profile.istio.chart_directory
        self._helm_upgrade("istio-base", chart_root / "base", "istio-system", timeout=timeout)
        pilot_values = [
            "pilot.autoscaleEnabled=false",
            "pilot.replicaCount=1",
            "pilot.resources.requests.cpu=50m",
            "pilot.resources.requests.memory=128Mi",
            "pilot.resources.limits.memory=512Mi",
            # Istio probes metadata.google.internal to detect GCE. A closed
            # loopback endpoint preserves non-GCP behavior without external DNS.
            "pilot.env.GCE_METADATA_HOST=127.0.0.1:9",
        ]
        modern_istio = not self.profile.istio.version.startswith("1.10.")
        pilot_values.extend(
            self.registry.istio_image_values(
                self.profile.istio.version,
                modern=modern_istio,
            )
        )
        if self.profile.trust.insecure_skip_tls_verify:
            pilot_values.append(
                "pilot.env.CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY=1"
            )
        native_pilot = self.profile.istio.arm64_pilot
        use_arm64_shim = native_pilot is not None and NativePilotBuilder.host_needs_shim()
        if use_arm64_shim:
            assert native_pilot is not None
            builder = NativePilotBuilder(
                self.profile,
                native_pilot,
                self.runner,
                self.registry,
            )
            builder.prepare()
            pilot_values.extend(builder.helm_values())
        self._helm_upgrade(
            "istiod",
            chart_root / "istiod",
            "istio-system",
            timeout=timeout,
            values=tuple(pilot_values),
        )
        if not modern_istio:
            gateway_values = [
                "gateways.istio-ingressgateway.type=ClusterIP",
                "gateways.istio-ingressgateway.autoscaleEnabled=false",
                "gateways.istio-ingressgateway.replicaCount=1",
                "gateways.istio-ingressgateway.resources.requests.cpu=25m",
                "gateways.istio-ingressgateway.resources.requests.memory=96Mi",
                "gateways.istio-ingressgateway.resources.limits.memory=256Mi",
                "gateways.istio-ingressgateway.env.GCE_METADATA_HOST=127.0.0.1:9",
            ]
        else:
            gateway_values = [
                "service.type=ClusterIP",
                "autoscaling.enabled=false",
                "replicaCount=1",
                "resources.requests.cpu=25m",
                "resources.requests.memory=96Mi",
                "resources.limits.memory=256Mi",
                "env.GCE_METADATA_HOST=127.0.0.1:9",
            ]
        gateway_values.extend(
            self.registry.gateway_image_values(
                self.profile.istio.version,
                modern=modern_istio,
            )
        )
        if self.profile.trust.insecure_skip_tls_verify:
            gateway_env = (
                "env"
                if modern_istio
                else "gateways.istio-ingressgateway.env"
            )
            gateway_values.append(
                f"{gateway_env}.CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY=1"
            )
        if use_arm64_shim:
            assert native_pilot is not None
            self.runner.run(
                self.kubectl.command(
                    "-n",
                    "istio-system",
                    "delete",
                    "deployment/istio-ingressgateway",
                    "--ignore-not-found",
                    "--wait=true",
                ),
                timeout=timeout,
            )
            self.kubectl.apply(
                self.registry.manifest(
                    self.gateway_stub_manifest(
                        self.registry.image(native_pilot.gateway_stub_image)
                    )
                )
            )
            self.kubectl.wait_available("istio-ingressgateway", "istio-system", timeout=timeout)
        else:
            self._helm_upgrade(
                "istio-ingressgateway",
                chart_root / "gateway",
                "istio-system",
                timeout=timeout,
                values=tuple(gateway_values),
            )
        self.kubectl.apply(self.gateway_manifest())

    @staticmethod
    def gateway_stub_manifest(image: str) -> str:
        """Provide the Gateway selector on arm64 without emulating old proxyv2."""

        return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: istio-ingressgateway
  namespace: istio-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: istio-ingressgateway
      istio: ingressgateway
  template:
    metadata:
      annotations:
        sidecar.istio.io/inject: "false"
      labels:
        app: istio-ingressgateway
        istio: ingressgateway
    spec:
      containers:
        - name: gateway-placeholder
          image: {image}
          resources:
            requests:
              cpu: 5m
              memory: 8Mi
            limits:
              memory: 32Mi
---
apiVersion: v1
kind: Service
metadata:
  name: istio-ingressgateway
  namespace: istio-system
spec:
  type: ClusterIP
  selector:
    app: istio-ingressgateway
    istio: ingressgateway
  ports:
    - name: http2
      port: 80
      targetPort: 8080
"""

    def gateway_manifest(self) -> str:
        gateway = self.profile.istio.gateway
        hosts = "\n".join(f"        - {json.dumps(host)}" for host in gateway.hosts)
        return f"""apiVersion: networking.istio.io/v1beta1
kind: Gateway
metadata:
  name: {gateway.name}
  namespace: {gateway.namespace}
spec:
  selector:
    istio: ingressgateway
  servers:
    - port:
        number: 80
        name: http
        protocol: HTTP
      hosts:
{hosts}
"""

    def _helm_upgrade(
        self,
        release: str,
        chart: Path,
        namespace: str,
        *,
        timeout: int,
        values: tuple[str, ...] = (),
    ) -> None:
        command: list[str | Path] = [
            "helm",
            "upgrade",
            "--install",
            release,
            chart,
            "--namespace",
            namespace,
            "--wait",
            "--timeout",
            f"{timeout}s",
            "--kube-context",
            self.kubectl.context,
        ]
        helm_version = self.runner.run(["helm", "version", "--short"]).stdout.strip()
        if helm_version.removeprefix("v").startswith("4."):
            # Helm 4 defaults to server-side apply. Istiod intentionally edits the
            # validator installed by the base chart, which otherwise creates an
            # ownership conflict on an idempotent upgrade.
            command.append("--server-side=false")
        for value in values:
            command.extend(("--set", value))
        self.runner.run(command, timeout=timeout + 30)


def _namespace(name: str) -> str:
    return f"""apiVersion: v1
kind: Namespace
metadata:
  name: {name}
"""
