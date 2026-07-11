from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import httpx

from cicd_harness.command import CommandRunner
from cicd_harness.config import HarnessProfile
from cicd_harness.errors import ReadinessError
from cicd_harness.kind import KindCluster
from cicd_harness.kubectl import Kubectl
from cicd_harness.registry import RegistrySupport

TERMINAL_EXECUTION_STATUSES = {
    "CANCELED",
    "SKIPPED",
    "STOPPED",
    "SUCCEEDED",
    "TERMINAL",
}


class SpinnakerStack:
    namespace = "spinnaker"
    deployments = (
        "spin-redis",
        "spin-minio",
        "spin-front50",
        "spin-rosco",
        "spin-clouddriver",
        "spin-orca",
        "spin-gate",
    )

    def __init__(
        self,
        profile: HarnessProfile,
        cluster: KindCluster,
        kubectl: Kubectl,
        runner: CommandRunner,
        registry: RegistrySupport | None = None,
    ) -> None:
        self.profile = profile
        self.cluster = cluster
        self.kubectl = kubectl
        self.runner = runner
        self.registry = registry or RegistrySupport(profile, runner)

    @property
    def service_images(self) -> tuple[str, ...]:
        images = self.profile.spinnaker.images
        return tuple(
            self.registry.image(image)
            for image in (
                images.gate,
                images.front50,
                images.orca,
                images.clouddriver,
                images.rosco,
            )
        )

    def prepare_service_images(self) -> None:
        """Pull and preload the amd64-only 1.25.4 images into Kind.

        Podman on an arm64 Mac can execute these images through binfmt/QEMU, but
        Kind's direct `load docker-image` lookup does not see the foreign-arch
        image. Exporting an archive is reliable on both arm64 development Macs
        and amd64 CI hosts.
        """

        provider = self.profile.runtime.provider
        for image in self.service_images:
            if self._image_in_node(image):
                continue
            if provider == "podman":
                self.runner.run(["podman", "pull", image], timeout=900)
                digest = hashlib.sha256(image.encode()).hexdigest()[:16]
                archive = Path("/tmp") / f"cicd-harness-{digest}.tar"
                try:
                    self.runner.run(["podman", "save", "-o", archive, image], timeout=900)
                    self.runner.run(
                        [
                            self.profile.kind.binary,
                            "load",
                            "image-archive",
                            archive,
                            "--name",
                            self.profile.kind.cluster_name,
                        ],
                        env={"KIND_EXPERIMENTAL_PROVIDER": "podman"},
                        timeout=900,
                    )
                finally:
                    archive.unlink(missing_ok=True)
            else:
                self.runner.run(["docker", "pull", image], timeout=900)
                self.runner.run(
                    [
                        self.profile.kind.binary,
                        "load",
                        "docker-image",
                        image,
                        "--name",
                        self.profile.kind.cluster_name,
                    ],
                    timeout=900,
                )

    def _image_in_node(self, image: str) -> bool:
        provider = self.profile.runtime.provider
        result = self.runner.run(
            [
                provider,
                "exec",
                f"{self.profile.kind.cluster_name}-control-plane",
                "crictl",
                "inspecti",
                image,
            ],
            check=False,
            timeout=30,
        )
        return result.returncode == 0

    def manifest(self) -> str:
        rendered = self.profile.spinnaker.manifest.read_text()
        defaults = {
            "us-docker.pkg.dev/spinnaker-community/docker/gate:1.21.0-20210215200018": (
                self.registry.image(self.profile.spinnaker.images.gate)
            ),
            "us-docker.pkg.dev/spinnaker-community/docker/front50:0.26.2-20210216140019": (
                self.registry.image(self.profile.spinnaker.images.front50)
            ),
            "us-docker.pkg.dev/spinnaker-community/docker/orca:2.19.0-20210209140018": (
                self.registry.image(self.profile.spinnaker.images.orca)
            ),
            "us-docker.pkg.dev/spinnaker-community/docker/clouddriver:7.3.3-20210322155326": (
                self.registry.image(self.profile.spinnaker.images.clouddriver)
            ),
            "us-docker.pkg.dev/spinnaker-community/docker/rosco:0.24.0-20210210110018": (
                self.registry.image(self.profile.spinnaker.images.rosco)
            ),
            "redis:7.2.7-alpine": self.registry.image(
                self.profile.spinnaker.images.redis
            ),
            "docker.io/minio/minio:RELEASE.2024-10-29T16-01-48Z": (
                self.registry.image(self.profile.spinnaker.images.minio)
            ),
        }
        for original, configured in defaults.items():
            rendered = rendered.replace(original, configured)
        return rendered

    def install(self, *, timeout: int = 900) -> None:
        self.registry.ensure_namespace(self.kubectl, self.namespace)
        self.kubectl.apply(self.manifest())
        for deployment in self.deployments:
            self.kubectl.wait_available(deployment, self.namespace, timeout=timeout)


class SpinnakerClient:
    def __init__(self, base_url: str, *, timeout: float = 120) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SpinnakerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save_pipeline(self, pipeline: dict[str, Any]) -> None:
        response = self._client.post("/pipelines", json=pipeline)
        response.raise_for_status()

    def trigger(
        self,
        application: str,
        pipeline_name_or_id: str,
        *,
        trigger: dict[str, Any] | None = None,
    ) -> str:
        payload = trigger or {"type": "manual", "user": "harness"}
        response = self._client.post(
            f"/pipelines/{application}/{pipeline_name_or_id}",
            json=payload,
        )
        response.raise_for_status()
        ref = response.json()["ref"]
        return str(ref).rsplit("/", 1)[-1]

    def execution(self, execution_id: str) -> dict[str, Any]:
        response = self._client.get(f"/pipelines/{execution_id}")
        response.raise_for_status()
        return response.json()

    def executions(
        self,
        application: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        response = self._client.get(
            f"/applications/{application}/pipelines",
            params={"limit": limit},
        )
        response.raise_for_status()
        return list(response.json())

    def wait_execution(
        self,
        execution_id: str,
        *,
        timeout: float = 300,
        interval: float = 1,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            execution = self.execution(execution_id)
            if execution["status"] in TERMINAL_EXECUTION_STATUSES:
                return execution
            time.sleep(interval)
        raise ReadinessError(f"Spinnaker execution {execution_id} did not finish in {timeout}s")


class RoscoClient:
    def __init__(self, base_url: str, *, timeout: float = 120) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def bake_kustomize(
        self,
        *,
        repo_url: str,
        commit: str,
        kustomization_path: str,
        artifact_account: str = "gitea",
        output_name: str = "baked-manifest",
    ) -> dict[str, Any]:
        response = self._client.post(
            "/api/v2/manifest/bake/KUSTOMIZE",
            json={
                "templateRenderer": "KUSTOMIZE",
                "outputName": output_name,
                "outputArtifactName": output_name,
                "inputArtifact": git_repo_artifact(
                    repo_url=repo_url,
                    commit=commit,
                    artifact_account=artifact_account,
                ),
                "kustomizeFilePath": kustomization_path,
            },
        )
        response.raise_for_status()
        return response.json()


def http_file_artifact(*, url: str, commit: str, name: str) -> dict[str, Any]:
    return {
        "type": "http/file",
        "name": name,
        "reference": url,
        "version": commit,
        "artifactAccount": "no-auth-http-account",
    }


def git_repo_artifact(
    *,
    repo_url: str,
    commit: str,
    artifact_account: str = "gitea",
) -> dict[str, Any]:
    return {
        "type": "git/repo",
        "name": repo_url.rsplit("/", 1)[-1].removesuffix(".git"),
        "reference": repo_url,
        "version": commit,
        "artifactAccount": artifact_account,
    }


def raw_manifest_pipeline(
    *,
    application: str,
    name: str,
    artifact: dict[str, Any],
    account: str = "poc",
) -> dict[str, Any]:
    return {
        "application": application,
        "name": name,
        "id": name,
        "keepWaitingPipelines": False,
        "limitConcurrent": True,
        "parameterConfig": [],
        "triggers": [],
        "stages": [
            {
                "refId": "1",
                "requisiteStageRefIds": [],
                "type": "deployManifest",
                "name": "Deploy committed manifest",
                "account": account,
                "cloudProvider": "kubernetes",
                "moniker": {"app": application},
                "source": "artifact",
                "manifestArtifactAccount": artifact["artifactAccount"],
                "manifestArtifact": artifact,
                "skipExpressionEvaluation": True,
                "trafficManagement": {
                    "enabled": False,
                    "options": {"enableTraffic": False, "services": []},
                },
            }
        ],
    }


def kustomize_pipeline(
    *,
    application: str,
    name: str,
    repo_artifact: dict[str, Any],
    kustomization_path: str,
    account: str = "poc",
) -> dict[str, Any]:
    baked_artifact_id = "baked-manifest"
    baked_artifact = {
        "type": "embedded/base64",
        "name": baked_artifact_id,
        "customKind": False,
        "artifactAccount": "embedded-artifact",
    }
    return {
        "application": application,
        "name": name,
        "id": name,
        "keepWaitingPipelines": False,
        "limitConcurrent": True,
        "parameterConfig": [],
        "triggers": [],
        "stages": [
            {
                "refId": "1",
                "requisiteStageRefIds": [],
                "type": "bakeManifest",
                "name": "Bake committed Kustomize overlay",
                "templateRenderer": "KUSTOMIZE",
                "inputArtifact": {
                    "account": repo_artifact["artifactAccount"],
                    "artifact": repo_artifact,
                },
                "kustomizeFilePath": kustomization_path,
                "outputName": baked_artifact_id,
                "expectedArtifacts": [
                    {
                        "id": baked_artifact_id,
                        "matchArtifact": baked_artifact,
                        "useDefaultArtifact": False,
                    }
                ],
            },
            {
                "refId": "2",
                "requisiteStageRefIds": ["1"],
                "type": "deployManifest",
                "name": "Deploy baked manifest",
                "account": account,
                "cloudProvider": "kubernetes",
                "moniker": {"app": application},
                "source": "artifact",
                "manifestArtifactId": baked_artifact_id,
                "manifestArtifactAccount": "embedded-artifact",
                "skipExpressionEvaluation": True,
                "trafficManagement": {
                    "enabled": False,
                    "options": {"enableTraffic": False, "services": []},
                },
            },
        ],
    }
