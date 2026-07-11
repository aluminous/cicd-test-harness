import json

import httpx
import pytest

from cicd_harness.errors import HarnessError
from cicd_harness.wiremock import (
    Expectation,
    RequestRecord,
    ResponseSpec,
    WireMockClient,
)


def test_response_spec_is_immutable() -> None:
    spec = ResponseSpec(status=202, json={"state": "accepted"})

    assert spec.status == 202
    assert spec.json == {"state": "accepted"}


def test_expectation_wait_returns_matching_request_journal() -> None:
    pattern = {"method": "POST", "urlPath": "/callback"}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/__admin/requests/find":
            return httpx.Response(
                200,
                json={
                    "requests": [
                        {
                            "method": "POST",
                            "url": "/callback",
                            "body": '{"revision":"abc"}',
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = WireMockClient("http://wiremock.invalid")
    client._client.close()  # noqa: SLF001 - deterministic API transport
    client._client = httpx.Client(  # noqa: SLF001
        base_url="http://wiremock.invalid",
        transport=httpx.MockTransport(handle),
    )
    expectation = Expectation(client, pattern, expected_calls=1, name="callback")
    try:
        requests = expectation.wait(timeout=0.1, interval=0)
    finally:
        client.close()

    assert requests[0]["body"] == '{"revision":"abc"}'


def test_proxy_installs_low_priority_fallback_and_high_priority_intercept() -> None:
    mappings: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/__admin/mappings":
            mappings.append(json.loads(request.content))
            return httpx.Response(201)
        if request.url.path == "/__admin/requests":
            return httpx.Response(200, json={"requests": []})
        if request.url.path == "/__admin/requests/count":
            return httpx.Response(200, json={"count": 0})
        return httpx.Response(404)

    client = WireMockClient("http://wiremock.invalid")
    client._client.close()  # noqa: SLF001 - deterministic API transport
    client._client = httpx.Client(  # noqa: SLF001
        base_url="http://wiremock.invalid",
        transport=httpx.MockTransport(handle),
    )
    try:
        proxy = client.proxy(
            "jenkins",
            host="jenkins.test.svc.cluster.local",
            target="http://jenkins-origin.harness-system.svc.cluster.local:8080/",
        )
        proxy.intercept(
            method="POST",
            path="/job/payments/build",
            response={"status": 503},
        )
    finally:
        client.close()

    assert proxy.target == "http://jenkins-origin.harness-system.svc.cluster.local:8080"
    assert mappings[0]["priority"] == 10
    assert mappings[0]["response"]["proxyBaseUrl"] == proxy.target
    assert mappings[0]["metadata"]["harnessRole"] == "proxyFallback"
    assert mappings[1]["priority"] == 1
    assert mappings[1]["response"]["status"] == 503
    assert mappings[1]["metadata"]["harnessRole"] == "intercept"
    assert mappings[0]["request"]["headers"]["Host"]["matches"].endswith(
        "(?::[0-9]+)?$"
    )


def test_proxy_rejects_unsafe_or_non_http_targets() -> None:
    client = WireMockClient("http://wiremock.invalid")
    try:
        with pytest.raises(HarnessError, match="absolute HTTP"):
            client.proxy("invalid", target="jenkins:8080")
        with pytest.raises(HarnessError, match="credentials"):
            client.proxy("invalid", target="http://user:password@jenkins:8080")
        with pytest.raises(HarnessError, match="query or fragment"):
            client.proxy("invalid", target="http://jenkins:8080?token=secret")
    finally:
        client.close()


def test_request_record_normalizes_proxied_serve_event() -> None:
    record = RequestRecord.from_wiremock(
        {
            "id": "request-1",
            "request": {
                "method": "POST",
                "url": "/job/payments/build",
                "absoluteUrl": "http://jenkins/job/payments/build",
                "headers": {"X-Request-Id": ["release-7"]},
                "body": "{}",
            },
            "response": {"status": 201},
            "responseDefinition": {"proxyBaseUrl": "http://jenkins-origin:8080"},
            "stubMapping": {"name": "jenkins:proxy"},
            "timing": {"actualTotalTime": 42},
            "wasMatched": True,
        }
    )

    assert record.request_id == "request-1"
    assert record.proxied
    assert record.response_status == 201
    assert record.duration_milliseconds == 42
    assert record.headers["X-Request-Id"] == ("release-7",)


def test_diagnostic_snapshot_redacts_sensitive_proxy_headers() -> None:
    journal = {
        "requests": [
            {
                "request": {
                    "method": "GET",
                    "url": "/private",
                    "headers": {
                        "Authorization": ["Bearer secret-token"],
                        "Cookie": ["session=secret"],
                        "X-Request-Id": ["release-7"],
                    },
                }
            }
        ]
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/__admin/mappings":
            return httpx.Response(200, json={"mappings": []})
        if request.url.path == "/__admin/requests/unmatched":
            return httpx.Response(200, json={"requests": []})
        if request.url.path == "/__admin/requests":
            return httpx.Response(200, json=journal)
        return httpx.Response(404)

    client = WireMockClient("http://wiremock.invalid")
    client._client.close()  # noqa: SLF001 - deterministic API transport
    client._client = httpx.Client(  # noqa: SLF001
        base_url="http://wiremock.invalid",
        transport=httpx.MockTransport(handle),
    )
    try:
        snapshot = client.snapshot()
    finally:
        client.close()

    headers = snapshot["requests"][0]["request"]["headers"]
    assert headers["Authorization"] == "***"
    assert headers["Cookie"] == "***"
    assert headers["X-Request-Id"] == ["release-7"]
