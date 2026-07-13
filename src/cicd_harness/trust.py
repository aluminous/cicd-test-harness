from __future__ import annotations

import hashlib
import logging
import platform
import re
import shutil
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any

from cicd_harness.command import CommandRunner
from cicd_harness.config import HarnessProfile, TrustConfig
from cicd_harness.errors import HarnessError

logger = logging.getLogger(__name__)
_REGISTRY_HOST = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]+)?$")

TRUST_CONFIG_MAP = "harness-trust-bundle"
TRUST_MOUNT_PATH = "/etc/cicd-harness/trust"
JAVA_TRUST_VOLUME = "harness-java-trust"
JAVA_TRUST_STORE = "/var/run/cicd-harness-java-trust/cacerts"
INSECURE_TLS_ENVIRONMENT = {
    # This harness-specific marker lets application fixtures opt into their
    # client library's native verification switch when no standard one exists.
    "CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY": "1",
    "CURL_HOME": TRUST_MOUNT_PATH,
    "GIT_SSL_NO_VERIFY": "true",
    "GOINSECURE": "*",
    "NODE_TLS_REJECT_UNAUTHORIZED": "0",
    "PYTHONHTTPSVERIFY": "0",
    "WGETRC": f"{TRUST_MOUNT_PATH}/wgetrc",
}


def validate_registry_hosts(hosts: tuple[str, ...]) -> tuple[str, ...]:
    """Reject host values that cannot safely become paths or TOML table keys."""

    invalid = [host for host in hosts if not _REGISTRY_HOST.fullmatch(host)]
    if invalid:
        raise HarnessError(
            "cannot configure insecure containerd registry hosts: "
            + ", ".join(invalid)
        )
    return hosts


def ssl_context(
    ca_certificate: Path | None,
    *,
    insecure_skip_tls_verify: bool = False,
) -> ssl.SSLContext:
    """Return configured TLS verification for harness-owned Python clients."""

    context = ssl.create_default_context()
    if ca_certificate is not None:
        _require_ca(ca_certificate)
        try:
            context.load_verify_locations(cafile=str(ca_certificate))
        except ssl.SSLError as exc:
            raise HarnessError(
                f"private CA certificate is not a valid PEM trust anchor: "
                f"{ca_certificate}: {exc}"
            ) from exc
    if insecure_skip_tls_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def combined_ca_pem(ca_certificate: Path) -> str:
    """Build a portable PEM bundle without replacing the platform's public roots."""

    _require_ca(ca_certificate)
    context = ssl.create_default_context()
    public_roots = "".join(
        ssl.DER_cert_to_PEM_cert(certificate)
        for certificate in context.get_ca_certs(binary_form=True)
    )
    private_roots = ca_certificate.read_text()
    try:
        context.load_verify_locations(cafile=str(ca_certificate))
    except ssl.SSLError as exc:
        raise HarnessError(
            f"private CA certificate is not a valid PEM trust anchor: {ca_certificate}: {exc}"
        ) from exc
    separator = "" if public_roots.endswith("\n") else "\n"
    return f"{public_roots}{separator}{private_roots.rstrip()}\n"


def stage_ca_bundle(config: TrustConfig, directory: Path) -> Path | None:
    """Stage a combined bundle where a remote container runtime can mount it."""

    if config.ca_certificate is None:
        return None
    contents = combined_ca_pem(config.ca_certificate)
    digest = hashlib.sha256(contents.encode()).hexdigest()[:16]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"ca-bundle-{digest}.crt"
    if not target.is_file() or target.read_text() != contents:
        target.write_text(contents)
        target.chmod(0o600)
    return target


def inject_java_trust(
    pod_spec: dict[str, Any],
    *,
    init_image: str,
    target_containers: set[str],
) -> None:
    """Build a Java trust store from the image defaults plus the private CA."""

    volumes = list(pod_spec.get("volumes") or [])
    if not any(item.get("name") == TRUST_CONFIG_MAP for item in volumes):
        volumes.append(
            {"name": TRUST_CONFIG_MAP, "configMap": {"name": TRUST_CONFIG_MAP}}
        )
    if not any(item.get("name") == JAVA_TRUST_VOLUME for item in volumes):
        volumes.append({"name": JAVA_TRUST_VOLUME, "emptyDir": {}})
    pod_spec["volumes"] = volumes

    init_containers = list(pod_spec.get("initContainers") or [])
    if not any(item.get("name") == "harness-java-trust" for item in init_containers):
        init_containers.append(
            {
                "name": "harness-java-trust",
                "image": init_image,
                "command": ["/bin/sh", "-ec"],
                "args": [
                    "cp \"${JAVA_HOME}/lib/security/cacerts\" /java-trust/cacerts; "
                    "chmod u+w /java-trust/cacerts; "
                    "keytool -importcert -noprompt -trustcacerts -storepass changeit "
                    "-alias cicd-harness-private-ca "
                    f"-file {TRUST_MOUNT_PATH}/ca.crt -keystore /java-trust/cacerts"
                ],
                "volumeMounts": [
                    {
                        "name": TRUST_CONFIG_MAP,
                        "mountPath": TRUST_MOUNT_PATH,
                        "readOnly": True,
                    },
                    {"name": JAVA_TRUST_VOLUME, "mountPath": "/java-trust"},
                ],
            }
        )
    pod_spec["initContainers"] = init_containers

    for container in pod_spec.get("containers") or []:
        if container.get("name") not in target_containers:
            continue
        mounts = list(container.get("volumeMounts") or [])
        if not any(item.get("name") == JAVA_TRUST_VOLUME for item in mounts):
            mounts.append(
                {
                    "name": JAVA_TRUST_VOLUME,
                    "mountPath": "/var/run/cicd-harness-java-trust",
                    "readOnly": True,
                }
            )
        container["volumeMounts"] = mounts
        environment = list(container.get("env") or [])
        java_options = next(
            (item for item in environment if item.get("name") == "JAVA_TOOL_OPTIONS"),
            None,
        )
        trust_option = f"-Djavax.net.ssl.trustStore={JAVA_TRUST_STORE}"
        if java_options is None:
            environment.append({"name": "JAVA_TOOL_OPTIONS", "value": trust_option})
        elif "value" in java_options and trust_option not in java_options["value"]:
            java_options["value"] = f"{java_options['value']} {trust_option}".strip()
        container["env"] = environment


class TrustSupport:
    """Propagate private trust or the emergency TLS fallback across boundaries."""

    def __init__(self, profile: HarnessProfile, runner: CommandRunner) -> None:
        self.config = profile.trust
        self.runner = runner
        self._directory: Path | None = None
        self._installed_env: dict[str, str] = {}
        self._previous_env: dict[str, str | None] = {}
        self._runtime_prepared = False

    @property
    def enabled(self) -> bool:
        return (
            self.config.ca_certificate is not None
            or self.config.insecure_skip_tls_verify
        )

    @property
    def ca_enabled(self) -> bool:
        return self.config.ca_certificate is not None

    @property
    def insecure_skip_tls_verify(self) -> bool:
        return self.config.insecure_skip_tls_verify

    @property
    def ca_certificate(self) -> Path | None:
        return self.config.ca_certificate

    def install_client_env(self) -> None:
        """Configure subprocess TLS clients while retaining the normal public roots."""

        if not self.enabled or self._installed_env:
            return
        values: dict[str, str] = {}
        if self.ca_enabled:
            assert self.ca_certificate is not None
            contents = combined_ca_pem(self.ca_certificate)
            directory = Path(tempfile.mkdtemp(prefix="cicd-harness-trust-"))
            directory.chmod(0o700)
            bundle = directory / "ca-bundle.crt"
            bundle.write_text(contents)
            bundle.chmod(0o600)
            self._directory = directory
            values.update(
                {
                    "AWS_CA_BUNDLE": str(bundle),
                    "CURL_CA_BUNDLE": str(bundle),
                    "GIT_SSL_CAINFO": str(bundle),
                    "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH": str(bundle),
                    "PIP_CERT": str(bundle),
                    "REQUESTS_CA_BUNDLE": str(bundle),
                    "SSL_CERT_FILE": str(bundle),
                    # Node adds this file to its default roots instead of replacing them.
                    "NODE_EXTRA_CA_CERTS": str(self.ca_certificate),
                }
            )
        if self.insecure_skip_tls_verify:
            values.update(INSECURE_TLS_ENVIRONMENT)
            if self._directory is None:
                self._directory = Path(
                    tempfile.mkdtemp(prefix="cicd-harness-trust-")
                )
                self._directory.chmod(0o700)
            curl_config = self._directory / ".curlrc"
            wget_config = self._directory / "wgetrc"
            curl_config.write_text("insecure\n")
            wget_config.write_text("check_certificate = off\n")
            curl_config.chmod(0o600)
            wget_config.chmod(0o600)
            values["CURL_HOME"] = str(self._directory)
            values["WGETRC"] = str(wget_config)
        for key, value in values.items():
            self._previous_env[key] = self.runner.base_env.get(key)
            self.runner.base_env[key] = value
        self._installed_env = values
        if self.ca_enabled:
            logger.info("configured additive private CA trust from %s", self.ca_certificate)
        if self.insecure_skip_tls_verify:
            logger.warning(
                "TLS certificate and hostname verification is disabled where supported"
            )

    def prepare_runtime(self, provider: str) -> None:
        """Install the CA into a macOS-hosted Podman VM before image operations."""

        if not self.enabled or self._runtime_prepared:
            return
        self.install_client_env()
        if self.ca_enabled and provider == "podman" and platform.system() == "Darwin":
            assert self.ca_certificate is not None
            script = (
                "set -eu; "
                "target=/etc/pki/ca-trust/source/anchors/cicd-harness-private-ca.crt; "
                "temporary=${target}.tmp; cat > ${temporary}; "
                "if test -f ${target} && cmp -s ${temporary} ${target}; then "
                "rm -f ${temporary}; else "
                "mv ${temporary} ${target}; chmod 0644 ${target}; update-ca-trust extract; fi"
            )
            try:
                self.runner.run(
                    ["podman", "machine", "ssh", "sudo", "sh", "-c", script],
                    input_text=self.ca_certificate.read_text(),
                    timeout=60,
                )
            except Exception as exc:
                raise HarnessError(
                    "could not install the private CA in the Podman machine; "
                    "ensure the selected rootful machine is running"
                ) from exc
            logger.info("installed private CA in the Podman machine trust store")
        self._runtime_prepared = True

    def install_kind_node(self, provider: str, cluster_name: str) -> None:
        """Trust the CA in the Kind OS and restart containerd only when it changes."""

        if not self.ca_enabled:
            return
        assert self.ca_certificate is not None
        node = f"{cluster_name}-control-plane"
        script = (
            "set -eu; "
            "target=/usr/local/share/ca-certificates/cicd-harness-private-ca.crt; "
            "temporary=${target}.tmp; cat > ${temporary}; "
            "if test -f ${target} && cmp -s ${temporary} ${target}; then "
            "rm -f ${temporary}; else "
            "mv ${temporary} ${target}; chmod 0644 ${target}; "
            "update-ca-certificates >/dev/null; systemctl restart containerd; fi"
        )
        self.runner.run(
            [provider, "exec", "-i", node, "sh", "-c", script],
            input_text=self.ca_certificate.read_text(),
            timeout=60,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = self.runner.run(
                [
                    "kubectl",
                    "--context",
                    f"kind-{cluster_name}",
                    "get",
                    "--raw=/readyz",
                ],
                check=False,
                timeout=10,
            )
            if ready.returncode == 0:
                logger.info("installed private CA in Kind node %s", node)
                return
            time.sleep(1)
        raise HarnessError(f"Kind cluster {cluster_name} did not recover after CA installation")

    def install_kind_insecure_registries(
        self,
        provider: str,
        cluster_name: str,
        hosts: tuple[str, ...],
    ) -> None:
        """Configure Kind containerd to skip TLS checks for controlled registries."""

        if not self.insecure_skip_tls_verify:
            return
        validate_registry_hosts(hosts)
        node = f"{cluster_name}-control-plane"
        changed = False
        script = (
            "set -eu; host=$1; directory=/etc/containerd/certs.d/${host}; "
            "target=${directory}/hosts.toml; temporary=${target}.tmp; "
            "mkdir -p ${directory}; cat > ${temporary}; "
            "if test -f ${target} && cmp -s ${temporary} ${target}; then "
            "rm -f ${temporary}; else mv ${temporary} ${target}; echo changed; fi"
        )
        for host in hosts:
            server = (
                "registry-1.docker.io"
                if host.lower() == "docker.io"
                else host
            )
            contents = (
                f'server = "https://{server}"\n\n'
                f'[host."https://{server}"]\n'
                '  capabilities = ["pull", "resolve", "push"]\n'
                "  skip_verify = true\n"
            )
            result = self.runner.run(
                [provider, "exec", "-i", node, "sh", "-c", script, "sh", host],
                input_text=contents,
                timeout=30,
            )
            changed = changed or "changed" in result.stdout.splitlines()
        if changed:
            self.runner.run(
                [provider, "exec", node, "systemctl", "restart", "containerd"],
                timeout=60,
            )
            self._wait_for_kind(cluster_name)
        logger.warning(
            "Kind containerd TLS verification is disabled for %d registry host(s)",
            len(hosts),
        )

    def _wait_for_kind(self, cluster_name: str) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = self.runner.run(
                [
                    "kubectl",
                    "--context",
                    f"kind-{cluster_name}",
                    "get",
                    "--raw=/readyz",
                ],
                check=False,
                timeout=10,
            )
            if ready.returncode == 0:
                return
            time.sleep(1)
        raise HarnessError(
            f"Kind cluster {cluster_name} did not recover after insecure registry setup"
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
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None


def _require_ca(path: Path) -> None:
    if not path.is_file():
        raise HarnessError(f"private CA certificate does not exist: {path}")
    try:
        contents = path.read_text()
    except UnicodeDecodeError as exc:
        raise HarnessError(f"private CA certificate must be PEM text: {path}") from exc
    if "-----BEGIN CERTIFICATE-----" not in contents:
        raise HarnessError(f"private CA certificate must contain a PEM certificate: {path}")
    if "PRIVATE KEY-----" in contents:
        raise HarnessError(f"private CA certificate must not contain a private key: {path}")
