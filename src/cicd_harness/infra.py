from __future__ import annotations

from pathlib import Path

import yaml

from cicd_harness.config import HarnessProfile
from cicd_harness.errors import HarnessError
from cicd_harness.kubectl import Kubectl
from cicd_harness.registry import RegistrySupport


class InfraStack:
    namespace = "harness-system"

    def __init__(
        self,
        profile: HarnessProfile,
        kubectl: Kubectl,
        workspace: Path,
        registry: RegistrySupport | None = None,
    ) -> None:
        self.profile = profile
        self.kubectl = kubectl
        self.manifest_path = workspace / "manifests/infra.yaml"
        self.registry = registry or RegistrySupport(profile, kubectl.runner)

    def manifest(self) -> str:
        if self.profile.infra is None:
            raise HarnessError("infrastructure configuration is not present in this profile")
        infra = self.profile.infra
        rendered = self.manifest_path.read_text()
        if infra.wiremock is not None:
            rendered = rendered.replace(
                "wiremock/wiremock:3.13.1",
                self.registry.image(infra.wiremock.image),
            )
            rendered = rendered.replace(
                "__WIREMOCK_MAX_REQUEST_JOURNAL_ENTRIES__",
                str(infra.wiremock.max_request_journal_entries),
            )
            rendered = rendered.replace(
                "__WIREMOCK_LOGGED_RESPONSE_BODY_SIZE_LIMIT__",
                str(infra.wiremock.logged_response_body_size_limit),
            )
            rendered = rendered.replace(
                "__WIREMOCK_PROXY_TIMEOUT_MILLISECONDS__",
                str(infra.wiremock.proxy_timeout_milliseconds),
            )
        if infra.gitea is not None:
            rendered = rendered.replace(
                "docker.gitea.com/gitea:1.26.4-rootless",
                self.registry.image(infra.gitea.image),
            )
        return rendered

    def install(self, *, timeout: int = 300) -> None:
        if self.profile.infra is None:
            raise HarnessError("infrastructure configuration is not present in this profile")
        installed = False
        if self.profile.infra.wiremock is not None:
            self.install_wiremock(timeout=timeout)
            installed = True
        if self.profile.infra.gitea is not None:
            self.install_gitea(timeout=timeout)
            installed = True
        if not installed:
            raise HarnessError("infrastructure configuration contains no services")

    def install_wiremock(self, *, timeout: int = 300) -> None:
        if self.profile.infra is None or self.profile.infra.wiremock is None:
            raise HarnessError("WireMock configuration is not present in this profile")
        self.registry.ensure_namespace(self.kubectl, self.namespace)
        self.kubectl.apply(self._selected_manifest({"wiremock", "scanner-mock"}))
        self.kubectl.wait_available("wiremock", self.namespace, timeout=timeout)

    def install_gitea(self, *, timeout: int = 300) -> None:
        if self.profile.infra is None or self.profile.infra.gitea is None:
            raise HarnessError("Gitea configuration is not present in this profile")
        self.registry.ensure_namespace(self.kubectl, self.namespace)
        self.kubectl.apply(self._selected_manifest({"gitea"}))
        self.kubectl.wait_available("gitea", self.namespace, timeout=timeout)

    def _selected_manifest(self, names: set[str]) -> str:
        documents = [
            document
            for document in yaml.safe_load_all(self.manifest())
            if document is not None
            and document.get("kind") != "Namespace"
            and document.get("metadata", {}).get("name") in names
        ]
        return yaml.safe_dump_all(documents, explicit_start=True, sort_keys=False)

    def bootstrap_gitea(
        self,
        *,
        username: str = "harness",
        password: str = "harness-password",
    ) -> None:
        pod = self.kubectl.first_pod(self.namespace, "app.kubernetes.io/name=gitea")
        existing = self.kubectl.exec(
            self.namespace,
            pod,
            "gitea",
            "admin",
            "user",
            "list",
            "--config",
            "/etc/gitea/app.ini",
        )
        if username in existing:
            return
        self.kubectl.exec(
            self.namespace,
            pod,
            "gitea",
            "admin",
            "user",
            "create",
            "--username",
            username,
            "--password",
            password,
            "--email",
            "harness@example.invalid",
            "--admin",
            "--must-change-password=false",
            "--config",
            "/etc/gitea/app.ini",
        )
