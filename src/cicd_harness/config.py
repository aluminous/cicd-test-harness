from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeConfig(StrictModel):
    provider: str = "docker"
    memory_budget_mib: int = Field(default=8192, ge=1024, le=16384)


class RegistryCredentialConfig(StrictModel):
    server: str
    username_env: str
    password_env: str
    email_env: str | None = None

    @model_validator(mode="after")
    def validate_server(self) -> RegistryCredentialConfig:
        if "://" in self.server or "/" in self.server.strip("/"):
            raise ValueError("registry credential server must be a hostname with optional port")
        return self


class RegistryConfig(StrictModel):
    rewrites: dict[str, str] = Field(default_factory=dict)
    credentials: tuple[RegistryCredentialConfig, ...] = ()
    pull_secret_name: str = "harness-registry"

    @model_validator(mode="after")
    def validate_rewrites(self) -> RegistryConfig:
        for source, destination in self.rewrites.items():
            if not source.strip("/") or not destination.strip("/"):
                raise ValueError("registry rewrite prefixes must not be empty")
            if "://" in source or "://" in destination:
                raise ValueError("registry rewrite prefixes must not include a URL scheme")
        return self


class KindConfig(StrictModel):
    version: str
    binary: Path
    node_image: str
    cluster_name: str
    wait_seconds: int = Field(default=180, ge=30)

    @model_validator(mode="after")
    def require_digest(self) -> KindConfig:
        if "@sha256:" not in self.node_image:
            raise ValueError("Kind node image must be pinned by sha256 digest")
        return self


class ArgoRolloutsConfig(StrictModel):
    version: str
    install_manifest: Path
    notifications_manifest: Path | None = None


class GatewayConfig(StrictModel):
    name: str = "harness-gateway"
    namespace: str = "harness-system"
    hosts: tuple[str, ...] = ("*.harness.test",)


class NativePilotConfig(StrictModel):
    image: str
    builder_image: str
    source_url: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    git_revision: str
    containerfile: Path
    gateway_stub_image: str = "registry.k8s.io/pause:3.9"


class IstioConfig(StrictModel):
    version: str
    chart_directory: Path
    gateway: GatewayConfig = GatewayConfig()
    arm64_pilot: NativePilotConfig | None = None


class ServiceImageConfig(StrictModel):
    image: str


class WireMockConfig(ServiceImageConfig):
    max_request_journal_entries: int = Field(default=1000, ge=1)
    logged_response_body_size_limit: int = Field(default=65536, ge=0)
    proxy_timeout_milliseconds: int = Field(default=30000, ge=100)


class InfraConfig(StrictModel):
    gitea: ServiceImageConfig | None = None
    wiremock: WireMockConfig | None = None


class SpinnakerImagesConfig(StrictModel):
    gate: str
    front50: str
    orca: str
    clouddriver: str
    rosco: str
    redis: str = "redis:7.2.7-alpine"
    minio: str = "docker.io/minio/minio:RELEASE.2024-10-29T16-01-48Z"


class SpinnakerConfig(StrictModel):
    version: str = "1.25.4"
    enabled: bool = True
    rosco_enabled: bool = True
    manifest: Path
    images: SpinnakerImagesConfig


class JenkinsConfig(StrictModel):
    version: str = "2.426.1"
    enabled: bool = True
    manifest: Path
    image: str
    base_image: str
    containerfile: Path
    plugins_file: Path


class HarnessProfile(StrictModel):
    name: str
    runtime: RuntimeConfig
    registry: RegistryConfig = RegistryConfig()
    kind: KindConfig
    argo_rollouts: ArgoRolloutsConfig | None = None
    istio: IstioConfig | None = None
    infra: InfraConfig | None = None
    spinnaker: SpinnakerConfig | None = None
    jenkins: JenkinsConfig | None = None


def _resolve_paths(profile: HarnessProfile, root: Path) -> HarnessProfile:
    data = profile.model_dump()
    for section, key in (
        ("kind", "binary"),
        ("argo_rollouts", "install_manifest"),
        ("argo_rollouts", "notifications_manifest"),
        ("istio", "chart_directory"),
        ("istio", "arm64_pilot.containerfile"),
        ("spinnaker", "manifest"),
        ("jenkins", "manifest"),
        ("jenkins", "containerfile"),
        ("jenkins", "plugins_file"),
    ):
        section_data = data.get(section)
        if section_data is None:
            continue
        if "." in key:
            parent, key = key.split(".", 1)
            section_data = section_data.get(parent) or {}
        value = section_data.get(key)
        if value is not None:
            path = Path(value)
            section_data[key] = path if path.is_absolute() else root / path
    return HarnessProfile.model_validate(data)


def load_profile(path: Path, *, workspace: Path | None = None) -> HarnessProfile:
    raw = yaml.safe_load(path.read_text())
    profile = HarnessProfile.model_validate(raw)
    return _resolve_paths(profile, (workspace or path.parent).resolve())
