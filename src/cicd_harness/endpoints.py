from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from cicd_harness.component import ComponentGraph
from cicd_harness.errors import HarnessError
from cicd_harness.kubectl import Kubectl, PortForward


class EndpointKind(StrEnum):
    UI = "ui"
    API = "api"
    UI_API = "ui+api"
    TRAFFIC = "traffic"


@dataclass(frozen=True)
class HostEndpointSpec:
    name: str
    component: str
    kind: EndpointKind
    namespace: str
    service: str
    port: int
    description: str
    path: str = ""
    authentication: str = "none"
    default: bool = True

    @property
    def resource(self) -> str:
        return f"service/{self.service}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HostEndpoint:
    spec: HostEndpointSpec
    url: str

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def kind(self) -> EndpointKind:
        return self.spec.kind

    @property
    def description(self) -> str:
        return self.spec.description


class EndpointCatalog:
    """Discover optional host endpoints advertised by concrete components."""

    def __init__(self, components: ComponentGraph) -> None:
        endpoints: dict[str, HostEndpointSpec] = {}
        for component_name in components.names:
            component = components.configured(component_name)
            advertised = getattr(component, "host_endpoints", ())
            for endpoint in advertised:
                if not isinstance(endpoint, HostEndpointSpec):
                    raise HarnessError(
                        f"component {component_name!r} advertised an invalid host endpoint"
                    )
                if endpoint.component != component_name:
                    raise HarnessError(
                        f"host endpoint {endpoint.name!r} belongs to {endpoint.component!r}, "
                        f"not {component_name!r}"
                    )
                if endpoint.name in endpoints:
                    raise HarnessError(f"duplicate host endpoint: {endpoint.name}")
                endpoints[endpoint.name] = endpoint
        self._endpoints = endpoints

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._endpoints)

    @property
    def default_names(self) -> tuple[str, ...]:
        return tuple(name for name, endpoint in self._endpoints.items() if endpoint.default)

    def get(self, name: str) -> HostEndpointSpec:
        endpoint = self._endpoints.get(name)
        if endpoint is None:
            visible = ", ".join(self._endpoints) or "none"
            raise HarnessError(f"host endpoint {name!r} is not configured; available: {visible}")
        return endpoint

    def list(self) -> tuple[HostEndpointSpec, ...]:
        return tuple(self._endpoints.values())

    def snapshot(self) -> list[dict[str, Any]]:
        return [endpoint.as_dict() for endpoint in self._endpoints.values()]


class HostEndpointManager:
    """Own host-loopback port-forwards for catalog endpoints."""

    def __init__(self, catalog: EndpointCatalog, kubectl: Kubectl) -> None:
        self.catalog = catalog
        self.kubectl = kubectl
        self._forwards: dict[str, PortForward] = {}
        self._exposed: dict[str, HostEndpoint] = {}

    def list(self) -> tuple[HostEndpointSpec, ...]:
        return self.catalog.list()

    def expose(self, name: str) -> HostEndpoint:
        if name in self._exposed:
            return self._exposed[name]
        spec = self.catalog.get(name)
        forward = self.kubectl.port_forward(
            spec.namespace,
            spec.resource,
            spec.port,
        )
        endpoint = HostEndpoint(spec=spec, url=f"{forward.url}{spec.path}")
        self._forwards[name] = forward
        self._exposed[name] = endpoint
        return endpoint

    def expose_many(
        self,
        names: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> tuple[HostEndpoint, ...]:
        selected = tuple(names) if names is not None else self.catalog.default_names
        return tuple(self.expose(name) for name in selected)

    def exposed(self) -> tuple[HostEndpoint, ...]:
        return tuple(self._exposed.values())

    def snapshot(self) -> list[dict[str, Any]]:
        exposed = {name: endpoint.url for name, endpoint in self._exposed.items()}
        return [
            {**spec.as_dict(), "url": exposed.get(spec.name)}
            for spec in self.catalog.list()
        ]

    def close(self) -> None:
        for forward in reversed(tuple(self._forwards.values())):
            forward.close()
        self._forwards.clear()
        self._exposed.clear()
