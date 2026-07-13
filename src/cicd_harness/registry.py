from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

from cicd_harness.command import CommandRunner
from cicd_harness.config import HarnessProfile, RegistryConfig
from cicd_harness.errors import HarnessError
from cicd_harness.image_ref import rewrite_image
from cicd_harness.kubectl import Kubectl
from cicd_harness.trust import (
    INSECURE_TLS_ENVIRONMENT,
    TRUST_CONFIG_MAP,
    TRUST_MOUNT_PATH,
    combined_ca_pem,
)


class RegistrySupport:
    """Image rewriting and credential plumbing for host and in-cluster pulls."""

    def __init__(self, profile: HarnessProfile, runner: CommandRunner) -> None:
        self.profile = profile
        self.config: RegistryConfig = profile.registry
        self.airgap = profile.airgap
        self.trust = profile.trust
        self.runner = runner
        self._auth_directory: Path | None = None
        self._installed_env: dict[str, str] = {}
        self._previous_env: dict[str, str | None] = {}

    @property
    def has_credentials(self) -> bool:
        return bool(self.config.credentials)

    @property
    def pull_secret_name(self) -> str | None:
        return self.config.pull_secret_name if self.has_credentials else None

    def redaction_values(self) -> tuple[str, ...]:
        """Return resolved secret values for diagnostics without exposing them."""

        values: list[str] = []
        for credential in self.config.credentials:
            for name in (credential.password_env,):
                value = os.getenv(name)
                if value:
                    values.append(value)
        return tuple(values)

    def image(self, image: str) -> str:
        effective = rewrite_image(image, self.config.rewrites)
        if self.airgap.enabled and effective != "auto":
            from cicd_harness.airgap import assert_allowed_image

            assert_allowed_image(effective, self.airgap, source=image)
        return effective

    def controlled_image_hosts(self) -> tuple[str, ...]:
        """Return registry hosts needed by profile-owned image operations."""

        from cicd_harness.airgap import audit_airgap

        return tuple(
            sorted(
                {
                    dependency.host
                    for dependency in audit_airgap(self.profile)
                    if dependency.kind == "image" and dependency.host
                }
            )
        )

    def runtime_tls_args(self, provider: str) -> tuple[str, ...]:
        """Return an explicit registry TLS bypass supported by the runtime CLI."""

        if self.trust.insecure_skip_tls_verify and provider == "podman":
            return ("--tls-verify=false",)
        return ()

    def manifest(self, manifest: str) -> str:
        if (
            not self.config.rewrites
            and self.pull_secret_name is None
            and not self.airgap.enabled
            and self.trust.ca_certificate is None
            and not self.trust.insecure_skip_tls_verify
        ):
            return manifest
        documents = list(yaml.safe_load_all(manifest))
        rewritten = [
            _rewrite_images(
                document,
                self.image,
                self.pull_secret_name,
                self.trust.ca_certificate is not None,
                self.trust.insecure_skip_tls_verify,
            )
            for document in documents
        ]
        return yaml.safe_dump_all(rewritten, explicit_start=True, sort_keys=False)

    def istio_image_values(self, version: str, *, modern: bool) -> tuple[str, ...]:
        pilot = self.image(f"docker.io/istio/pilot:{version}")
        proxy = self.image(f"docker.io/istio/proxyv2:{version}")
        values: list[str] = []
        if pilot != f"docker.io/istio/pilot:{version}":
            key = "image" if modern else "pilot.image"
            values.append(f"{key}={pilot}")
        if proxy != f"docker.io/istio/proxyv2:{version}":
            values.extend((f"global.hub={proxy.rsplit('/', 1)[0]}", f"global.tag={version}"))
        if self.pull_secret_name is not None:
            values.append(f"global.imagePullSecrets[0]={self.pull_secret_name}")
        return tuple(values)

    def gateway_image_values(self, version: str, *, modern: bool) -> tuple[str, ...]:
        proxy = self.image(f"docker.io/istio/proxyv2:{version}")
        values: list[str] = []
        if proxy != f"docker.io/istio/proxyv2:{version}":
            hub = proxy.rsplit("/", 1)[0]
            if modern:
                values.extend((f"hub={hub}", f"tag={version}"))
            else:
                values.extend((f"global.hub={hub}", f"global.tag={version}"))
        if self.pull_secret_name is not None:
            key = "imagePullSecrets[0]" if modern else "global.imagePullSecrets[0]"
            values.append(f"{key}={self.pull_secret_name}")
        return tuple(values)

    def install_runtime_auth(self, provider: str) -> None:
        self.runner.add_redactions(*self.redaction_values())
        runtime = self.runtime_env(provider)
        for key in runtime:
            if key not in self._previous_env:
                self._previous_env[key] = self.runner.base_env.get(key)
        self.runner.base_env.update(runtime)
        self._installed_env.update(runtime)

    def runtime_env(self, provider: str) -> dict[str, str]:
        if not self.has_credentials:
            return {}
        directory = self._ensure_auth_directory()
        if provider == "podman":
            return {"REGISTRY_AUTH_FILE": str(directory / "auth.json")}
        if provider == "docker":
            return {"DOCKER_CONFIG": str(directory)}
        raise HarnessError(f"unsupported container runtime provider: {provider}")

    def ensure_namespace(self, kubectl: Kubectl, namespace: str) -> None:
        kubectl.apply(
            f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
"""
        )
        if (
            self.trust.ca_certificate is not None
            or self.trust.insecure_skip_tls_verify
        ):
            kubectl.apply(self._trust_config_map_manifest(namespace))
        if not self.has_credentials:
            return
        kubectl.apply(self._pull_secret_manifest(namespace))
        deadline = time.monotonic() + 15
        service_account: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            with suppress(Exception):
                service_account = kubectl.get_json(
                    "serviceaccount/default",
                    "-n",
                    namespace,
                )
            if service_account is not None:
                break
            time.sleep(0.2)
        if service_account is None:
            raise HarnessError(f"default ServiceAccount was not created in namespace {namespace}")
        references = list(service_account.get("imagePullSecrets") or [])
        if not any(item.get("name") == self.config.pull_secret_name for item in references):
            references.append({"name": self.config.pull_secret_name})
            kubectl.runner.run(
                kubectl.command(
                    "patch",
                    "serviceaccount/default",
                    "-n",
                    namespace,
                    "--type=merge",
                    "-p",
                    json.dumps({"imagePullSecrets": references}),
                )
            )

    def close(self) -> None:
        for key, value in self._installed_env.items():
            if self.runner.base_env.get(key) == value:
                previous = self._previous_env.get(key)
                if previous is None:
                    self.runner.base_env.pop(key, None)
                else:
                    self.runner.base_env[key] = previous
        self._installed_env.clear()
        self._previous_env.clear()
        if self._auth_directory is not None:
            shutil.rmtree(self._auth_directory, ignore_errors=True)
            self._auth_directory = None

    def _ensure_auth_directory(self) -> Path:
        if self._auth_directory is not None:
            return self._auth_directory
        directory = Path(tempfile.mkdtemp(prefix="cicd-harness-registry-"))
        directory.chmod(0o700)
        contents = json.dumps(self._docker_config(), separators=(",", ":"))
        for name in ("auth.json", "config.json"):
            path = directory / name
            path.write_text(contents)
            path.chmod(0o600)
        self._auth_directory = directory
        return directory

    def _docker_config(self) -> dict[str, Any]:
        auths: dict[str, Any] = {}
        missing: list[str] = []
        for credential in self.config.credentials:
            username = os.getenv(credential.username_env)
            password = os.getenv(credential.password_env)
            if username is None:
                missing.append(credential.username_env)
            if password is None:
                missing.append(credential.password_env)
            email = os.getenv(credential.email_env) if credential.email_env else None
            if username is None or password is None:
                continue
            entry: dict[str, str] = {
                "auth": base64.b64encode(f"{username}:{password}".encode()).decode()
            }
            if email:
                entry["email"] = email
            auths[credential.server] = entry
        if missing:
            rendered = ", ".join(sorted(set(missing)))
            raise HarnessError(f"private registry credential environment is incomplete: {rendered}")
        return {"auths": auths}

    def _pull_secret_manifest(self, namespace: str) -> str:
        encoded = base64.b64encode(
            json.dumps(self._docker_config(), separators=(",", ":")).encode()
        ).decode()
        return f"""apiVersion: v1
kind: Secret
metadata:
  name: {self.config.pull_secret_name}
  namespace: {namespace}
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: {encoded}
"""

    def _trust_config_map_manifest(self, namespace: str) -> str:
        data: dict[str, str] = {}
        if self.trust.ca_certificate is not None:
            data.update(
                {
                    "ca.crt": self.trust.ca_certificate.read_text(),
                    "ca-bundle.crt": combined_ca_pem(self.trust.ca_certificate),
                }
            )
        if self.trust.insecure_skip_tls_verify:
            data.update(
                {
                    ".curlrc": "insecure\n",
                    "wgetrc": "check_certificate = off\n",
                }
            )
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": TRUST_CONFIG_MAP, "namespace": namespace},
            "data": data,
        }
        return yaml.safe_dump(manifest, sort_keys=False)


def _rewrite_images(
    value: Any,
    rewrite: Any,
    pull_secret_name: str | None,
    ca_enabled: bool,
    insecure_skip_tls_verify: bool,
) -> Any:
    if isinstance(value, dict):
        rewritten: dict[Any, Any] = {}
        contains_containers = False
        for key, item in value.items():
            if key in {"containers", "initContainers", "ephemeralContainers"} and isinstance(
                item,
                list,
            ):
                contains_containers = True
                rewritten[key] = [
                    _rewrite_container(
                        container,
                        rewrite,
                        pull_secret_name,
                        ca_enabled,
                        insecure_skip_tls_verify,
                    )
                    for container in item
                ]
            else:
                rewritten[key] = _rewrite_images(
                    item,
                    rewrite,
                    pull_secret_name,
                    ca_enabled,
                    insecure_skip_tls_verify,
                )
        if contains_containers and pull_secret_name is not None:
            references = list(rewritten.get("imagePullSecrets") or [])
            if not any(item.get("name") == pull_secret_name for item in references):
                references.append({"name": pull_secret_name})
            rewritten["imagePullSecrets"] = references
        if contains_containers and (ca_enabled or insecure_skip_tls_verify):
            volumes = list(rewritten.get("volumes") or [])
            if not any(item.get("name") == TRUST_CONFIG_MAP for item in volumes):
                volumes.append(
                    {
                        "name": TRUST_CONFIG_MAP,
                        "configMap": {"name": TRUST_CONFIG_MAP},
                    }
                )
            rewritten["volumes"] = volumes
        return rewritten
    if isinstance(value, list):
        return [
            _rewrite_images(
                item,
                rewrite,
                pull_secret_name,
                ca_enabled,
                insecure_skip_tls_verify,
            )
            for item in value
        ]
    return value


def _rewrite_container(
    value: Any,
    rewrite: Any,
    pull_secret_name: str | None,
    ca_enabled: bool,
    insecure_skip_tls_verify: bool,
) -> Any:
    if not isinstance(value, dict):
        return _rewrite_images(
            value,
            rewrite,
            pull_secret_name,
            ca_enabled,
            insecure_skip_tls_verify,
        )
    rewritten = {
        key: rewrite(item) if key == "image" and isinstance(item, str) else item
        for key, item in value.items()
    }
    if ca_enabled or insecure_skip_tls_verify:
        mounts = list(rewritten.get("volumeMounts") or [])
        if not any(item.get("name") == TRUST_CONFIG_MAP for item in mounts):
            mounts.append(
                {
                    "name": TRUST_CONFIG_MAP,
                    "mountPath": TRUST_MOUNT_PATH,
                    "readOnly": True,
                }
            )
        rewritten["volumeMounts"] = mounts
        environment = list(rewritten.get("env") or [])
        names = {item.get("name") for item in environment}
        trust_environment: dict[str, str] = {}
        if ca_enabled:
            trust_environment.update(
                {
                    "AWS_CA_BUNDLE": f"{TRUST_MOUNT_PATH}/ca-bundle.crt",
                    "CURL_CA_BUNDLE": f"{TRUST_MOUNT_PATH}/ca-bundle.crt",
                    "GIT_SSL_CAINFO": f"{TRUST_MOUNT_PATH}/ca-bundle.crt",
                    "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH": (
                        f"{TRUST_MOUNT_PATH}/ca-bundle.crt"
                    ),
                    "NODE_EXTRA_CA_CERTS": f"{TRUST_MOUNT_PATH}/ca.crt",
                    "PIP_CERT": f"{TRUST_MOUNT_PATH}/ca-bundle.crt",
                    "REQUESTS_CA_BUNDLE": f"{TRUST_MOUNT_PATH}/ca-bundle.crt",
                    "SSL_CERT_FILE": f"{TRUST_MOUNT_PATH}/ca-bundle.crt",
                }
            )
        if insecure_skip_tls_verify:
            trust_environment.update(INSECURE_TLS_ENVIRONMENT)
        environment.extend(
            {"name": name, "value": setting}
            for name, setting in trust_environment.items()
            if name not in names
        )
        rewritten["env"] = environment
    return _rewrite_images(
        rewritten,
        rewrite,
        pull_secret_name,
        ca_enabled,
        insecure_skip_tls_verify,
    )
