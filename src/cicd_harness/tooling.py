from __future__ import annotations

import hashlib
import logging
import platform
import ssl
import stat
from pathlib import Path

import httpx

from cicd_harness.config import KindConfig
from cicd_harness.errors import HarnessError

logger = logging.getLogger(__name__)


def host_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "amd64",
        "x86_64": "amd64",
    }.get(machine)
    if system not in {"darwin", "linux"} or architecture is None:
        raise HarnessError(
            f"automatic Kind installation is not supported on {system}/{machine}; "
            "set kind.binary to an installed executable"
        )
    return f"{system}-{architecture}"


def ensure_kind_binary(
    config: KindConfig,
    *,
    client: httpx.Client | None = None,
    verify: ssl.SSLContext | bool = True,
) -> Path:
    """Install and verify the profile-pinned Kind binary when it is absent."""

    target = config.binary
    platform_key = host_platform()
    expected = config.download_sha256.get(platform_key)
    if target.is_file():
        if expected is not None:
            actual = _sha256(target)
            if actual != expected:
                raise HarnessError(
                    f"Kind binary checksum mismatch at {target}: "
                    f"expected {expected}, got {actual}"
                )
        return target
    if expected is None:
        raise HarnessError(
            f"Kind binary is missing at {target} and profile {config.version} "
            f"has no checksum for {platform_key}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.download")
    url = config.download_url_template.format(
        version=config.version,
        platform=platform_key,
    )
    logger.info("downloading Kind v%s for %s from %s", config.version, platform_key, url)
    owns_client = client is None
    resolved_client = client or httpx.Client(
        follow_redirects=True,
        timeout=120,
        verify=verify,
    )
    try:
        with resolved_client.stream("GET", url) as response:
            response.raise_for_status()
            with temporary.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        actual = _sha256(temporary)
        if actual != expected:
            raise HarnessError(
                f"downloaded Kind checksum mismatch for {url}: "
                f"expected {expected}, got {actual}"
            )
        temporary.chmod(
            temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        temporary.replace(target)
        logger.info("installed verified Kind binary at %s", target)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, HarnessError):
            raise
        raise HarnessError(f"could not download Kind {config.version} from {url}: {exc}") from exc
    finally:
        if owns_client:
            resolved_client.close()
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
