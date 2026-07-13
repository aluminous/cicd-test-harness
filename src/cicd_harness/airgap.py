from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from cicd_harness.config import AirgapConfig, HarnessProfile
from cicd_harness.errors import HarnessError
from cicd_harness.image_ref import image_registry_host, rewrite_image

JENKINS_PLUGIN_DEFAULTS = {
    "update center": "https://updates.jenkins.io",
    "experimental update center": "https://updates.jenkins.io/experimental",
    "plugin downloads": "https://updates.jenkins.io/download",
    "plugin metadata": "https://updates.jenkins.io/current/plugin-versions.json",
    "incrementals": "https://repo.jenkins-ci.org/incrementals",
}
_HTTP_URL = re.compile(r"https?://[^\s'\"<>]+")


@dataclass(frozen=True)
class AirgapDependency:
    name: str
    kind: str
    stage: str
    source: str
    effective: str
    host: str
    allowed: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def kind_download_url(profile: HarnessProfile, platform: str) -> str:
    return profile.kind.download_url_template.format(
        version=profile.kind.version,
        platform=platform,
    )


def host_allowed(host: str, config: AirgapConfig) -> bool:
    normalized = host.lower().rstrip(".")
    hostname = normalized.rsplit(":", 1)[0] if normalized.count(":") == 1 else normalized
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    if hostname.endswith((".svc", ".svc.cluster.local")):
        return True
    for entry in config.allowed_hosts:
        allowed = entry.lower().rstrip(".")
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if hostname.endswith(suffix) and hostname != suffix[1:]:
                return True
        elif ":" in allowed:
            if normalized == allowed:
                return True
        elif hostname == allowed:
            return True
    return False


def assert_allowed_image(image: str, config: AirgapConfig, *, source: str | None = None) -> None:
    host = image_registry_host(image)
    if not host_allowed(host, config):
        original = f" (from {source})" if source and source != image else ""
        raise HarnessError(
            f"air-gap mode rejected image registry {host!r}: {image}{original}; "
            "add an internal registry rewrite or explicitly allow the host"
        )


def audit_airgap(profile: HarnessProfile) -> tuple[AirgapDependency, ...]:
    """Return every host/build dependency controlled by a profile.

    The report intentionally includes build-time inputs (Jenkins plugins and the
    legacy Istio pilot source) as well as stack-up inputs. Test-authored manifests
    are checked later by RegistrySupport when they are rendered.
    """

    dependencies: list[AirgapDependency] = []

    def image(name: str, source: str, stage: str = "runtime") -> None:
        effective = rewrite_image(source, profile.registry.rewrites)
        host = image_registry_host(effective)
        dependencies.append(
            AirgapDependency(
                name=name,
                kind="image",
                stage=stage,
                source=source,
                effective=effective,
                host=host,
                allowed=host_allowed(host, profile.airgap),
            )
        )

    def url(name: str, source: str, stage: str) -> None:
        parsed = urlparse(source)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        host = f"{hostname}{port}"
        safe_source = parsed._replace(netloc=host).geturl()
        dependencies.append(
            AirgapDependency(
                name=name,
                kind="url",
                stage=stage,
                source=safe_source,
                effective=safe_source,
                host=host,
                allowed=bool(host) and host_allowed(host, profile.airgap),
            )
        )

    image("Kubernetes Kind node", profile.kind.node_image)
    url("Kind binary", kind_download_url(profile, "<host-platform>"), "bootstrap")

    if profile.argo_rollouts is not None:
        for index, reference in enumerate(_manifest_images(profile.argo_rollouts.install_manifest)):
            image(f"Argo Rollouts image {index + 1}", reference)
        if profile.argo_rollouts.notifications_manifest is not None:
            for index, reference in enumerate(
                _manifest_images(profile.argo_rollouts.notifications_manifest)
            ):
                image(f"Argo notifications image {index + 1}", reference)

    if profile.istio is not None:
        version = profile.istio.version
        image("Istio pilot", f"docker.io/istio/pilot:{version}")
        image("Istio proxy/gateway", f"docker.io/istio/proxyv2:{version}")
        native = profile.istio.arm64_pilot
        if native is not None:
            image("Legacy ARM pilot", native.image)
            image("Legacy ARM gateway stub", native.gateway_stub_image)
            for platform, reference in native.builder_images.items():
                image(f"Legacy ARM Go builder ({platform})", reference, "image-build")
            url("Legacy Istio source", native.source_url, "image-build")
            go_proxy = native.go_proxy or "https://proxy.golang.org"
            for index, endpoint in enumerate(
                part.strip() for part in re.split(r"[,|]", go_proxy)
            ):
                if endpoint in {"off", ""}:
                    continue
                if endpoint == "direct":
                    dependencies.append(
                        AirgapDependency(
                            name="Legacy Istio Go modules (direct fallback)",
                            kind="network-policy",
                            stage="image-build",
                            source=endpoint,
                            effective=endpoint,
                            host="internet",
                            allowed=False,
                        )
                    )
                else:
                    url(f"Legacy Istio Go module proxy {index + 1}", endpoint, "image-build")
            if native.go_sumdb != "off":
                sumdb = native.go_sumdb or "https://sum.golang.org"
                parts = sumdb.split()
                endpoints = [part for part in parts if "://" in part]
                endpoint = endpoints[-1] if endpoints else f"https://{parts[0]}"
                url("Legacy Istio Go checksum database", endpoint, "image-build")

    if profile.infra is not None:
        if profile.infra.gitea is not None:
            image("Gitea", profile.infra.gitea.image)
        if profile.infra.wiremock is not None:
            image("WireMock", profile.infra.wiremock.image)

    if profile.spinnaker is not None and profile.spinnaker.enabled:
        for name, reference in profile.spinnaker.images.model_dump().items():
            image(f"Spinnaker {name}", reference)
        for endpoint in _manifest_urls(profile.spinnaker.manifest):
            name = (
                "Spinnaker Front50 object storage (in-cluster MinIO)"
                if "spin-minio." in endpoint
                else "Spinnaker configured service endpoint"
            )
            url(name, endpoint, "runtime")

    if profile.jenkins is not None and profile.jenkins.enabled:
        image("Jenkins harness image", profile.jenkins.image)
        mirrors = profile.jenkins.plugin_mirrors
        image("Jenkins base image", profile.jenkins.base_image, "image-build")
        configured = {
            "update center": mirrors.update_center,
            "experimental update center": mirrors.experimental_update_center,
            "plugin downloads": mirrors.download,
            "plugin metadata": mirrors.plugin_info,
            "incrementals": mirrors.incrementals,
        }
        for name, default in JENKINS_PLUGIN_DEFAULTS.items():
            url(f"Jenkins {name}", configured[name] or default, "image-build")

    return tuple(dependencies)


def validate_airgap(profile: HarnessProfile) -> tuple[AirgapDependency, ...]:
    dependencies = audit_airgap(profile)
    if not profile.airgap.enabled:
        return dependencies
    rejected = [dependency for dependency in dependencies if not dependency.allowed]
    if rejected:
        lines = "\n".join(
            f"- {item.stage}/{item.name}: {item.effective} (host {item.host})"
            for item in rejected
        )
        raise HarnessError(
            "air-gap preflight found network dependencies outside airgap.allowed_hosts:\n"
            f"{lines}\n"
            "Mirror/rewrite these dependencies or configure an internally staged artifact."
        )
    return dependencies


def _manifest_images(path: Path) -> tuple[str, ...]:
    images: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "image" and isinstance(item, str):
                    images.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for document in yaml.safe_load_all(path.read_text()):
        visit(document)
    return tuple(dict.fromkeys(images))


def _manifest_urls(path: Path) -> tuple[str, ...]:
    urls: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            urls.extend(_HTTP_URL.findall(value))

    for document in yaml.safe_load_all(path.read_text()):
        visit(document)
    return tuple(dict.fromkeys(urls))
