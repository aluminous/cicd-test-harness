import hashlib
import stat
from pathlib import Path

import httpx
import pytest

from cicd_harness.command import CommandRunner
from cicd_harness.config import KindConfig
from cicd_harness.errors import CommandError, HarnessError
from cicd_harness.tooling import ensure_kind_binary, host_platform


def _kind_config(path: Path, checksum: str) -> KindConfig:
    return KindConfig(
        version="0.31.0",
        binary=path,
        node_image="kindest/node:v1.31.14@sha256:" + "a" * 64,
        cluster_name="tool-test",
        download_sha256={host_platform(): checksum},
    )


def test_missing_kind_binary_is_downloaded_verified_and_made_executable(
    tmp_path: Path,
) -> None:
    payload = b"preview-kind-binary"
    checksum = hashlib.sha256(payload).hexdigest()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=payload)

    target = tmp_path / ".tools/bin/kind-v0.31.0"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = ensure_kind_binary(_kind_config(target, checksum), client=client)

    assert result == target
    assert target.read_bytes() == payload
    assert stat.S_IMODE(target.stat().st_mode) & stat.S_IXUSR
    assert requests[0].url.path.endswith(f"/kind-{host_platform()}")


def test_kind_binary_can_be_downloaded_from_an_internal_template(tmp_path: Path) -> None:
    payload = b"internal-kind-binary"
    checksum = hashlib.sha256(payload).hexdigest()
    config = _kind_config(tmp_path / "kind", checksum).model_copy(
        update={
            "download_url_template": (
                "https://nexus.corp.example/repository/kind/v{version}/kind-{platform}"
            )
        }
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        ensure_kind_binary(config, client=client)

    assert requests[0].url.host == "nexus.corp.example"
    assert requests[0].url.path.endswith(f"/v0.31.0/kind-{host_platform()}")


def test_existing_kind_binary_must_match_profile_checksum(tmp_path: Path) -> None:
    target = tmp_path / "kind"
    target.write_bytes(b"unexpected")

    with pytest.raises(HarnessError, match="checksum mismatch"):
        ensure_kind_binary(_kind_config(target, "0" * 64))


def test_missing_executable_has_actionable_managed_error(tmp_path: Path) -> None:
    runner = CommandRunner(cwd=tmp_path)

    with pytest.raises(CommandError, match="required executable was not found"):
        runner.run(["definitely-not-a-real-harness-command"])
