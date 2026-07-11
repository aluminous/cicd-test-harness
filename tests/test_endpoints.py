from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cicd_harness.component import ComponentGraph
from cicd_harness.components import default_components
from cicd_harness.config import load_profile
from cicd_harness.endpoints import (
    EndpointCatalog,
    EndpointKind,
    HostEndpointManager,
    HostEndpointSpec,
)
from cicd_harness.errors import HarnessError


class FakeForward:
    def __init__(self, port: int) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_standard_endpoint_catalog_distinguishes_primary_and_deep_debug_apis() -> None:
    workspace = Path(__file__).parents[1]
    profile = load_profile(workspace / "profiles/modern.yaml", workspace=workspace)
    catalog = EndpointCatalog(ComponentGraph(default_components(profile)))

    assert catalog.names == (
        "ingress",
        "wiremock-admin",
        "gitea",
        "jenkins",
        "spinnaker-gate",
        "spinnaker-orca",
        "spinnaker-clouddriver",
        "spinnaker-rosco",
        "spinnaker-front50",
    )
    assert catalog.default_names == (
        "wiremock-admin",
        "gitea",
        "jenkins",
        "spinnaker-gate",
    )
    assert catalog.get("gitea").kind == EndpointKind.UI_API
    assert catalog.get("ingress").kind == EndpointKind.TRAFFIC


def test_endpoint_manager_exposes_loopback_url_once_and_closes_forward() -> None:
    spec = HostEndpointSpec(
        name="example",
        component="example",
        kind=EndpointKind.API,
        namespace="example-system",
        service="example-api",
        port=8080,
        path="/admin",
        description="Example API",
    )
    graph = ComponentGraph(
        [
            SimpleNamespace(
                name="example",
                dependencies=frozenset(),
                host_endpoints=(spec,),
            )
        ]
    )
    forward = FakeForward(49152)
    kubectl = Mock()
    kubectl.port_forward.return_value = forward
    manager = HostEndpointManager(EndpointCatalog(graph), kubectl)

    first = manager.expose("example")
    second = manager.expose("example")

    assert first is second
    assert first.url == "http://127.0.0.1:49152/admin"
    kubectl.port_forward.assert_called_once_with(
        "example-system",
        "service/example-api",
        8080,
    )
    manager.close()
    assert forward.closed
    assert manager.exposed() == ()


def test_endpoint_catalog_rejects_wrong_component_and_unknown_endpoint() -> None:
    spec = HostEndpointSpec(
        name="api",
        component="other",
        kind=EndpointKind.API,
        namespace="example",
        service="api",
        port=80,
        description="Wrong owner",
    )
    graph = ComponentGraph(
        [
            SimpleNamespace(
                name="example",
                dependencies=frozenset(),
                host_endpoints=(spec,),
            )
        ]
    )

    with pytest.raises(HarnessError, match="belongs to 'other'"):
        EndpointCatalog(graph)

    empty = EndpointCatalog(
        ComponentGraph([SimpleNamespace(name="example", dependencies=frozenset())])
    )
    with pytest.raises(HarnessError, match="available: none"):
        empty.get("missing")
