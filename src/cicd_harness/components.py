from __future__ import annotations

from dataclasses import dataclass

from cicd_harness.component import BaseEnvironmentComponent, EnvironmentContext
from cicd_harness.config import HarnessProfile
from cicd_harness.controllers import ControllerStack
from cicd_harness.endpoints import EndpointKind, HostEndpointSpec
from cicd_harness.errors import HarnessError
from cicd_harness.infra import InfraStack
from cicd_harness.jenkins import JenkinsStack
from cicd_harness.spinnaker import SpinnakerStack


@dataclass(frozen=True)
class KubernetesServiceRef:
    namespace: str
    name: str
    port: int

    @property
    def resource(self) -> str:
        return f"service/{self.name}"


class ArgoRolloutsComponent(BaseEnvironmentComponent):
    name = "argo-rollouts"

    def start(self, context: EnvironmentContext, *, timeout: int) -> None:
        ControllerStack(
            context.profile,
            context.kubectl,
            context.runner,
            context.registry,
        ).install_argo_rollouts(timeout=timeout)


class IstioComponent(BaseEnvironmentComponent):
    name = "istio"
    host_endpoints = (
        HostEndpointSpec(
            name="ingress",
            component=name,
            kind=EndpointKind.TRAFFIC,
            namespace="istio-system",
            service="istio-ingressgateway",
            port=80,
            description="Istio ingress gateway; send the VirtualService Host header",
            default=False,
        ),
    )

    def start(self, context: EnvironmentContext, *, timeout: int) -> None:
        ControllerStack(
            context.profile,
            context.kubectl,
            context.runner,
            context.registry,
        ).install_istio(timeout=timeout)


class WireMockComponent(BaseEnvironmentComponent):
    name = "wiremock"
    service = KubernetesServiceRef("harness-system", "wiremock", 8080)
    host_endpoints = (
        HostEndpointSpec(
            name="wiremock-admin",
            component=name,
            kind=EndpointKind.API,
            namespace=service.namespace,
            service=service.name,
            port=service.port,
            path="/__admin/",
            description="WireMock mappings and request-journal administration API",
        ),
    )

    def start(self, context: EnvironmentContext, *, timeout: int) -> None:
        InfraStack(
            context.profile,
            context.kubectl,
            context.workspace,
            context.registry,
        ).install_wiremock(timeout=timeout)


class GiteaComponent(BaseEnvironmentComponent):
    name = "gitea"
    service = KubernetesServiceRef("harness-system", "gitea", 3000)
    host_endpoints = (
        HostEndpointSpec(
            name="gitea",
            component=name,
            kind=EndpointKind.UI_API,
            namespace=service.namespace,
            service=service.name,
            port=service.port,
            description="Writable Git server UI and REST API",
            authentication="basic user harness (test credential)",
        ),
    )

    def start(self, context: EnvironmentContext, *, timeout: int) -> None:
        stack = InfraStack(
            context.profile,
            context.kubectl,
            context.workspace,
            context.registry,
        )
        stack.install_gitea(timeout=timeout)
        stack.bootstrap_gitea()


class JenkinsComponent(BaseEnvironmentComponent):
    name = "jenkins"
    service = KubernetesServiceRef("harness-system", "jenkins", 8080)
    host_endpoints = (
        HostEndpointSpec(
            name="jenkins",
            component=name,
            kind=EndpointKind.UI_API,
            namespace=service.namespace,
            service=service.name,
            port=service.port,
            description="Jenkins job, build, console, and REST interface",
        ),
    )

    def start(self, context: EnvironmentContext, *, timeout: int) -> None:
        JenkinsStack(
            context.profile,
            context.kubectl,
            context.registry,
        ).install(timeout=timeout)


class SpinnakerComponent(BaseEnvironmentComponent):
    name = "spinnaker"
    dependencies = frozenset({"gitea"})
    service = KubernetesServiceRef("spinnaker", "spin-gate", 8084)
    host_endpoints = (
        HostEndpointSpec(
            name="spinnaker-gate",
            component=name,
            kind=EndpointKind.API,
            namespace=service.namespace,
            service=service.name,
            port=service.port,
            description="Primary Spinnaker pipeline API",
        ),
        HostEndpointSpec(
            name="spinnaker-orca",
            component=name,
            kind=EndpointKind.API,
            namespace="spinnaker",
            service="spin-orca",
            port=8083,
            description="Orca orchestration API for deep pipeline troubleshooting",
            default=False,
        ),
        HostEndpointSpec(
            name="spinnaker-clouddriver",
            component=name,
            kind=EndpointKind.API,
            namespace="spinnaker",
            service="spin-clouddriver",
            port=7002,
            description="Clouddriver Kubernetes operations and cache API",
            default=False,
        ),
        HostEndpointSpec(
            name="spinnaker-rosco",
            component=name,
            kind=EndpointKind.API,
            namespace="spinnaker",
            service="spin-rosco",
            port=8087,
            description="Rosco manifest-baking API",
            default=False,
        ),
        HostEndpointSpec(
            name="spinnaker-front50",
            component=name,
            kind=EndpointKind.API,
            namespace="spinnaker",
            service="spin-front50",
            port=8080,
            description="Front50 pipeline-configuration storage API",
            default=False,
        ),
    )

    def start(self, context: EnvironmentContext, *, timeout: int) -> None:
        stack = SpinnakerStack(
            context.profile,
            context.cluster,
            context.kubectl,
            context.runner,
            context.registry,
        )
        stack.prepare_service_images()
        stack.install(timeout=timeout)


def default_components(
    profile: HarnessProfile,
    *,
    include_jenkins: bool = True,
    include_spinnaker: bool = True,
) -> list[BaseEnvironmentComponent]:
    components: list[BaseEnvironmentComponent] = []
    if profile.argo_rollouts is not None:
        components.append(ArgoRolloutsComponent())
    if profile.istio is not None:
        components.append(IstioComponent())
    if profile.infra is not None and profile.infra.wiremock is not None:
        components.append(WireMockComponent())
    if profile.infra is not None and profile.infra.gitea is not None:
        components.append(GiteaComponent())
    if (
        include_jenkins
        and profile.jenkins is not None
        and profile.jenkins.enabled
    ):
        components.append(JenkinsComponent())
    if (
        include_spinnaker
        and profile.spinnaker is not None
        and profile.spinnaker.enabled
    ):
        components.append(SpinnakerComponent())
    return components


def select_components(
    profile: HarnessProfile,
    names: set[str],
    *,
    include_jenkins: bool = True,
    include_spinnaker: bool = True,
) -> list[BaseEnvironmentComponent]:
    configured = default_components(
        profile,
        include_jenkins=include_jenkins,
        include_spinnaker=include_spinnaker,
    )
    by_name = {component.name: component for component in configured}
    unknown = names - set(by_name)
    if unknown:
        rendered = ", ".join(sorted(unknown))
        available = ", ".join(by_name) or "none"
        raise HarnessError(
            f"requested environment components are not configured: {rendered}; "
            f"available: {available}"
        )
    return [component for component in configured if component.name in names]
