from __future__ import annotations

import re
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from cicd_harness.errors import HarnessError, ReadinessError, VerificationError

_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}


@dataclass(frozen=True)
class ResponseSpec:
    status: int = 200
    json: Any | None = None
    body: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    delay_milliseconds: int | None = None


@dataclass(frozen=True)
class RequestRecord:
    """Normalized view of a WireMock request or proxy serve event."""

    request_id: str | None
    method: str
    url: str
    absolute_url: str | None
    headers: dict[str, tuple[str, ...]]
    body: str | None
    response_status: int | None
    duration_milliseconds: int | None
    matched: bool | None
    proxied: bool
    mapping_name: str | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_wiremock(cls, event: dict[str, Any]) -> RequestRecord:
        request = event.get("request") if isinstance(event.get("request"), dict) else event
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        mapping = (
            event.get("stubMapping") if isinstance(event.get("stubMapping"), dict) else {}
        )
        definition = (
            event.get("responseDefinition")
            if isinstance(event.get("responseDefinition"), dict)
            else mapping.get("response", {})
        )
        timing = event.get("timing") if isinstance(event.get("timing"), dict) else {}
        raw_headers = request.get("headers") or {}
        headers = {
            str(name): _header_values(value)
            for name, value in raw_headers.items()
        }
        status = response.get("status", definition.get("status"))
        duration = timing.get("actualTotalTime", timing.get("totalTime"))
        return cls(
            request_id=_optional_string(event.get("id") or request.get("id")),
            method=str(request.get("method", "UNKNOWN")),
            url=str(request.get("url", "")),
            absolute_url=_optional_string(request.get("absoluteUrl")),
            headers=headers,
            body=_optional_string(request.get("body")),
            response_status=int(status) if status is not None else None,
            duration_milliseconds=int(duration) if duration is not None else None,
            matched=bool(event["wasMatched"]) if "wasMatched" in event else None,
            proxied=bool(definition.get("proxyBaseUrl")),
            mapping_name=_optional_string(mapping.get("name")),
            raw=event,
        )


class Expectation:
    def __init__(
        self,
        client: WireMockClient,
        request_pattern: dict[str, Any],
        expected_calls: int,
        name: str,
        baseline_count: int = 0,
    ) -> None:
        self.client = client
        self.request_pattern = request_pattern
        self.expected_calls = expected_calls
        self.name = name
        self.baseline_count = baseline_count

    def count(self) -> int:
        return max(0, self.client.count_requests(self.request_pattern) - self.baseline_count)

    def verify(self) -> None:
        actual = self.count()
        if actual != self.expected_calls:
            raise VerificationError(
                f"mock expectation {self.name!r} expected {self.expected_calls} calls, got {actual}"
            )

    def requests(self) -> list[dict[str, Any]]:
        requests = self.client.find_requests(self.request_pattern)
        observed = max(0, len(requests) - self.baseline_count)
        return requests[:observed]

    def records(self) -> list[RequestRecord]:
        matching = self.requests()
        identifiers = {
            str(item.get("id"))
            for item in matching
            if item.get("id") is not None
        }
        if not identifiers:
            return [RequestRecord.from_wiremock(item) for item in matching]
        return [
            record
            for record in self.client.records()
            if record.request_id in identifiers
        ]

    def wait(
        self,
        *,
        calls: int | None = None,
        timeout: float = 60,
        interval: float = 0.2,
    ) -> list[dict[str, Any]]:
        """Wait until at least the requested calls arrive; teardown still checks exactness."""

        target = self.expected_calls if calls is None else calls
        deadline = time.monotonic() + timeout
        requests: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            requests = self.requests()
            if len(requests) >= target:
                return requests
            time.sleep(interval)
        raise ReadinessError(
            f"mock expectation {self.name!r} received {len(requests)} calls, "
            f"waiting for at least {target} in {timeout}s"
        )


class MockService:
    def __init__(self, client: WireMockClient, name: str, host: str) -> None:
        self.client = client
        self.name = name
        self.host = host

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
        response_spec = response if isinstance(response, ResponseSpec) else ResponseSpec(**response)
        request = self.request_pattern(
            method=method,
            path=path,
            headers=headers,
            json_paths=json_paths,
        )
        wire_response: dict[str, Any] = {
            "status": response_spec.status,
            "headers": dict(response_spec.headers),
        }
        if response_spec.json is not None:
            wire_response["jsonBody"] = response_spec.json
            wire_response["headers"].setdefault("Content-Type", "application/json")
        if response_spec.body is not None:
            wire_response["body"] = response_spec.body
        if response_spec.delay_milliseconds is not None:
            wire_response["fixedDelayMilliseconds"] = response_spec.delay_milliseconds
        mapping_name = name or f"{self.name}:{method.upper()} {path}"
        baseline_count = self.client.count_requests(request)
        self.client.register_mapping(
            {
                "name": mapping_name,
                "priority": 1,
                "request": request,
                "response": wire_response,
                "metadata": {"service": self.name, "harnessRole": "intercept"},
            }
        )
        expectation = Expectation(
            client=self.client,
            request_pattern=request,
            expected_calls=times,
            name=mapping_name,
            baseline_count=baseline_count,
        )
        self.client.expectations.append(expectation)
        return expectation

    def request_pattern(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
        headers: dict[str, str] | None = None,
        json_paths: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "headers": {"Host": {"matches": _host_expression(self.host)}},
        }
        if method is not None:
            request["method"] = method.upper()
        if path is not None:
            request["urlPath"] = path
        for header, value in (headers or {}).items():
            request["headers"][header] = {"equalTo": value}
        if json_paths:
            request["bodyPatterns"] = [
                {
                    "matchesJsonPath": {
                        "expression": expression,
                        "equalTo": str(value),
                    }
                }
                for expression, value in json_paths.items()
            ]
        return request

    def requests(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.client.find_requests(self.request_pattern(method=method, path=path))

    def records(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> list[RequestRecord]:
        return self.client.records(host=self.host, method=method, path=path)


class ProxyService(MockService):
    def __init__(
        self,
        client: WireMockClient,
        name: str,
        host: str,
        target: str,
    ) -> None:
        super().__init__(client, name, host)
        self.target = _validate_proxy_target(target)

    def install(self, *, priority: int = 10) -> None:
        self.client.register_mapping(
            {
                "name": f"{self.name}:proxy -> {self.target}",
                "priority": priority,
                "request": self.request_pattern(),
                "response": {"proxyBaseUrl": self.target},
                "metadata": {
                    "service": self.name,
                    "harnessRole": "proxyFallback",
                    "target": self.target,
                },
            }
        )

    def intercept(self, **kwargs: Any) -> Expectation:
        """Install a higher-priority stub in front of the pass-through target."""

        return self.expect(**kwargs)

    def assert_called(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
        times: int = 1,
    ) -> list[RequestRecord]:
        records = self.records(method=method, path=path)
        if len(records) != times:
            rendered = f"{method.upper()} " if method is not None else ""
            rendered += path or "any path"
            raise VerificationError(
                f"proxy {self.name!r} expected {times} calls matching {rendered}, "
                f"got {len(records)}"
            )
        return records


class WireMockClient:
    def __init__(
        self,
        admin_url: str,
        *,
        timeout: float = 10,
        verify: bool | ssl.SSLContext = True,
    ) -> None:
        self._client = httpx.Client(base_url=admin_url, timeout=timeout, verify=verify)
        self.expectations: list[Expectation] = []

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WireMockClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def service(self, name: str, *, host: str | None = None) -> MockService:
        return MockService(self, name=name, host=host or name)

    def proxy(
        self,
        name: str,
        *,
        target: str,
        host: str | None = None,
        priority: int = 10,
    ) -> ProxyService:
        service = ProxyService(self, name=name, host=host or name, target=target)
        service.install(priority=priority)
        return service

    def register_mapping(self, mapping: dict[str, Any]) -> None:
        response = self._client.post("/__admin/mappings", json=mapping)
        response.raise_for_status()

    def reset(self) -> None:
        response = self._client.post("/__admin/reset")
        response.raise_for_status()
        self.expectations.clear()

    def count_requests(self, pattern: dict[str, Any]) -> int:
        response = self._client.post("/__admin/requests/count", json=pattern)
        response.raise_for_status()
        return int(response.json()["count"])

    def requests(self, *, unmatched_only: bool = False) -> list[dict[str, Any]]:
        path = "/__admin/requests/unmatched" if unmatched_only else "/__admin/requests"
        response = self._client.get(path)
        response.raise_for_status()
        return list(response.json().get("requests", []))

    def find_requests(self, pattern: dict[str, Any]) -> list[dict[str, Any]]:
        response = self._client.post("/__admin/requests/find", json=pattern)
        response.raise_for_status()
        return list(response.json().get("requests", []))

    def records(
        self,
        *,
        host: str | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> list[RequestRecord]:
        records = [RequestRecord.from_wiremock(item) for item in self.requests()]
        return [
            record
            for record in records
            if (host is None or _record_host(record) == host)
            and (method is None or record.method == method.upper())
            and (path is None or record.url.partition("?")[0] == path)
        ]

    def mappings(self) -> list[dict[str, Any]]:
        response = self._client.get("/__admin/mappings")
        response.raise_for_status()
        return list(response.json().get("mappings", []))

    def snapshot(self) -> dict[str, Any]:
        """Return redacted mappings and request journals for diagnostics."""

        return _redact_sensitive_headers(
            {
                "expectations": [
                    {"name": item.name, "expectedCalls": item.expected_calls}
                    for item in self.expectations
                ],
                "mappings": self.mappings(),
                "requests": self.requests(),
                "unmatchedRequests": self.requests(unmatched_only=True),
            }
        )

    def verify(self) -> None:
        failures: list[str] = []
        for expectation in self.expectations:
            try:
                expectation.verify()
            except VerificationError as exc:
                failures.append(str(exc))
        unmatched_requests = self.requests(unmatched_only=True)
        if unmatched_requests:
            rendered = [_request_summary(item) for item in unmatched_requests]
            failures.append(f"unmatched outbound requests: {rendered}")
        if failures:
            raise VerificationError("\n".join(failures))


def _host_expression(host: str) -> str:
    return rf"^{re.escape(host)}(?::[0-9]+)?$"


def _validate_proxy_target(target: str) -> str:
    value = target.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HarnessError("WireMock proxy target must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise HarnessError("WireMock proxy target must not contain credentials")
    if parsed.query or parsed.fragment:
        raise HarnessError("WireMock proxy target must not contain a query or fragment")
    return value


def _header_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, dict) and isinstance(value.get("values"), list):
        return tuple(str(item) for item in value["values"])
    return (str(value),)


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _request_summary(event: dict[str, Any]) -> str:
    record = RequestRecord.from_wiremock(event)
    return f"{record.method} {record.url}"


def _record_host(record: RequestRecord) -> str | None:
    for name, values in record.headers.items():
        if name.lower() == "host" and values:
            return values[0].partition(":")[0]
    return None


def _redact_sensitive_headers(value: Any, *, in_headers: bool = False) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if (in_headers and key_text in _SENSITIVE_HEADERS) or key_text in {
                "authorization",
                "proxy-authorization",
            }:
                redacted[key] = "***"
            else:
                redacted[key] = _redact_sensitive_headers(
                    item,
                    in_headers=key_text == "headers",
                )
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_headers(item, in_headers=in_headers) for item in value]
    return value
