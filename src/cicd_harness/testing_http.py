from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cicd_harness.components import WireMockComponent
from cicd_harness.naming import dns_name
from cicd_harness.wiremock import (
    Expectation,
    MockService,
    ProxyService,
    RequestRecord,
    ResponseSpec,
)

if TYPE_CHECKING:
    from cicd_harness.testing import TestHarness


class WireMockRouting:
    """Own per-test DNS aliases that route logical services to WireMock."""

    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness
        self._aliases: set[str] = set()

    def resolve(self, name: str, host: str | None) -> str:
        if host is not None:
            return host
        service_name = dns_name(name)
        if service_name not in self._aliases:
            self._create_alias(service_name)
            self._aliases.add(service_name)
        return f"{service_name}.{self.harness.namespace}.svc.cluster.local"

    def _create_alias(self, name: str) -> None:
        component = self.harness.runtime.environment.components.require(
            "wiremock",
            WireMockComponent,
        )
        service = component.service
        self.harness.runtime.environment.kubectl.apply(
            f"""apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {self.harness.namespace}
  labels:
    harness.cicd/managed: "true"
    harness.cicd/test: {self.harness.token}
spec:
  type: ExternalName
  externalName: {service.name}.{service.namespace}.svc.cluster.local
  ports:
    - name: http
      port: {service.port}
      protocol: TCP
"""
        )


class MockAPI:
    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness

    def reset(self) -> None:
        self.harness._services.wiremock().reset()

    def service(self, name: str, *, host: str | None = None) -> MockEndpoint:
        resolved_host = self.harness._wiremock_routing.resolve(name, host)
        raw = self.harness._services.wiremock().service(name, host=resolved_host)
        return MockEndpoint(
            name=name,
            host=resolved_host,
            url=f"http://{resolved_host}:8080",
            raw=raw,
        )

    def verify(self) -> None:
        self.harness._services.wiremock().verify()

    def requests(self, *, unmatched_only: bool = False) -> list[dict[str, Any]]:
        return self.harness._services.wiremock().requests(unmatched_only=unmatched_only)


class ProxyAPI:
    """Reverse proxies with pass-through defaults and optional fault injection."""

    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness

    def service(
        self,
        name: str,
        *,
        target: str,
        host: str | None = None,
        priority: int = 10,
    ) -> ProxyEndpoint:
        resolved_host = self.harness._wiremock_routing.resolve(name, host)
        raw = self.harness._services.wiremock().proxy(
            name,
            target=target,
            host=resolved_host,
            priority=priority,
        )
        return ProxyEndpoint(
            name=name,
            host=resolved_host,
            url=f"http://{resolved_host}:8080",
            target=raw.target,
            raw=raw,
        )


@dataclass(frozen=True)
class MockEndpoint:
    name: str
    host: str
    url: str
    raw: MockService

    def expect(
        self,
        *,
        method: str,
        path: str,
        response: ResponseSpec | dict[str, Any],
        headers: dict[str, str] | None = None,
        json_paths: dict[str, Any] | None = None,
        times: int = 1,
        name: str | None = None,
    ) -> Expectation:
        return self.raw.expect(
            method=method,
            path=path,
            response=response,
            headers=headers,
            json_paths=json_paths,
            times=times,
            name=name,
        )

    def requests(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Inspect requests received by this logical outbound service."""

        return self.raw.requests(method=method, path=path)

    def records(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> list[RequestRecord]:
        return self.raw.records(method=method, path=path)


@dataclass(frozen=True)
class ProxyEndpoint:
    name: str
    host: str
    url: str
    target: str
    raw: ProxyService

    def intercept(
        self,
        *,
        method: str,
        path: str,
        response: ResponseSpec | dict[str, Any],
        headers: dict[str, str] | None = None,
        json_paths: dict[str, Any] | None = None,
        times: int = 1,
        name: str | None = None,
    ) -> Expectation:
        return self.raw.intercept(
            method=method,
            path=path,
            response=response,
            headers=headers,
            json_paths=json_paths,
            times=times,
            name=name,
        )

    def requests(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.raw.requests(method=method, path=path)

    def records(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> list[RequestRecord]:
        return self.raw.records(method=method, path=path)

    def assert_called(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
        times: int = 1,
    ) -> list[RequestRecord]:
        return self.raw.assert_called(method=method, path=path, times=times)
