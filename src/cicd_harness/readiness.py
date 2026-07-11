from __future__ import annotations

import time
from collections.abc import Collection

import httpx

from cicd_harness.errors import ReadinessError


def wait_for_http(
    url: str,
    *,
    expected_statuses: Collection[int] = (200,),
    timeout: float = 15,
    interval: float = 0.25,
    auth: tuple[str, str] | None = None,
) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last_status: int | None = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, auth=auth, follow_redirects=True)
            last_status = response.status_code
            if response.status_code in expected_statuses:
                return response
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(interval)
    detail = (
        f"last status was {last_status}"
        if last_status is not None
        else f"last error: {last_error}"
    )
    raise ReadinessError(f"HTTP endpoint did not become ready: {url}; {detail}")
