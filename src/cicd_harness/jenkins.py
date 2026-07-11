from __future__ import annotations

import base64
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx

from cicd_harness.config import HarnessProfile
from cicd_harness.errors import ReadinessError
from cicd_harness.kubectl import Kubectl
from cicd_harness.registry import RegistrySupport


class JenkinsStack:
    namespace = "harness-system"

    def __init__(
        self,
        profile: HarnessProfile,
        kubectl: Kubectl,
        registry: RegistrySupport | None = None,
    ) -> None:
        self.profile = profile
        self.kubectl = kubectl
        self.registry = registry or RegistrySupport(profile, kubectl.runner)

    def manifest(self) -> str:
        rendered = self.profile.jenkins.manifest.read_text()
        return rendered.replace(
            "docker.io/jenkins/jenkins:2.426.1-lts-jdk17",
            self.registry.image(self.profile.jenkins.image),
        )

    def install(self, *, timeout: int = 600) -> None:
        self.registry.ensure_namespace(self.kubectl, self.namespace)
        self.prepare_image()
        self.kubectl.apply(self.manifest())
        self.kubectl.wait_available("jenkins", self.namespace, timeout=timeout)

    def prepare_image(self) -> None:
        provider = self.profile.runtime.provider
        image = self.registry.image(self.profile.jenkins.image)
        base_image = self.registry.image(self.profile.jenkins.base_image)
        fingerprint = self._build_fingerprint()
        inspect = self.kubectl.runner.run(
            [provider, "image", "inspect", image],
            check=False,
            timeout=30,
        )
        local_fingerprint: str | None = None
        if inspect.returncode == 0:
            payload = json.loads(inspect.stdout)[0]
            labels = payload.get("Labels") or payload.get("Config", {}).get("Labels") or {}
            local_fingerprint = labels.get("io.harness.jenkins.fingerprint")
        rebuilt = local_fingerprint != fingerprint
        if rebuilt:
            self.kubectl.runner.run(
                [
                    provider,
                    "build",
                    "--build-arg",
                    f"JENKINS_BASE_IMAGE={base_image}",
                    "--label",
                    f"io.harness.jenkins.fingerprint={fingerprint}",
                    "-t",
                    image,
                    "-f",
                    self.profile.jenkins.containerfile,
                    self.profile.jenkins.containerfile.parent,
                ],
                timeout=1200,
            )
        if not rebuilt and self._image_in_node():
            return
        if provider == "podman":
            digest = hashlib.sha256(image.encode()).hexdigest()[:16]
            archive = Path("/tmp") / f"cicd-harness-jenkins-{digest}.tar"
            try:
                self.kubectl.runner.run(
                    ["podman", "save", "-o", archive, image],
                    timeout=600,
                )
                self.kubectl.runner.run(
                    [
                        self.profile.kind.binary,
                        "load",
                        "image-archive",
                        archive,
                        "--name",
                        self.profile.kind.cluster_name,
                    ],
                    env={"KIND_EXPERIMENTAL_PROVIDER": "podman"},
                    timeout=600,
                )
            finally:
                archive.unlink(missing_ok=True)
        else:
            self.kubectl.runner.run(
                [
                    self.profile.kind.binary,
                    "load",
                    "docker-image",
                    image,
                    "--name",
                    self.profile.kind.cluster_name,
                ],
                timeout=600,
            )

    def _image_in_node(self) -> bool:
        provider = self.profile.runtime.provider
        result = self.kubectl.runner.run(
            [
                provider,
                "exec",
                f"{self.profile.kind.cluster_name}-control-plane",
                "crictl",
                "inspecti",
                self.registry.image(self.profile.jenkins.image),
            ],
            check=False,
            timeout=30,
        )
        return result.returncode == 0

    def _build_fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.registry.image(self.profile.jenkins.base_image).encode())
        digest.update(self.registry.image(self.profile.jenkins.image).encode())
        digest.update(self.profile.jenkins.containerfile.read_bytes())
        digest.update(self.profile.jenkins.plugins_file.read_bytes())
        return digest.hexdigest()


@dataclass(frozen=True)
class JenkinsJob:
    name: str
    full_name: str
    url: str
    kind: str
    color: str | None = None


@dataclass(frozen=True)
class JenkinsJobConfiguration:
    job: JenkinsJob
    xml: str
    root_type: str
    repository_urls: tuple[str, ...]
    script_path: str | None


@dataclass(frozen=True)
class JenkinsLibrary:
    name: str
    repository_url: str
    default_version: str
    implicit: bool
    allow_version_override: bool
    include_in_changesets: bool
    credentials_id: str | None = None


@dataclass(frozen=True)
class JenkinsArtifact:
    file_name: str
    relative_path: str


class JenkinsClient:
    def __init__(self, base_url: str, *, timeout: float = 30) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JenkinsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def list_jobs(self, *, recursive: bool = False) -> list[JenkinsJob]:
        depth = 8 if recursive else 1
        tree = _job_tree(depth)
        response = self._client.get("/api/json", params={"tree": tree})
        response.raise_for_status()
        jobs: list[JenkinsJob] = []

        def visit(items: list[dict[str, Any]], parent: str = "") -> None:
            for item in items:
                name = str(item["name"])
                full_name = f"{parent}/{name}" if parent else name
                jobs.append(
                    JenkinsJob(
                        name=name,
                        full_name=full_name,
                        url=str(item.get("url", "")),
                        kind=str(item.get("_class", "")),
                        color=item.get("color"),
                    )
                )
                if recursive:
                    visit(item.get("jobs", []), full_name)

        visit(response.json().get("jobs", []))
        return jobs

    def job(self, full_name: str) -> JenkinsJob:
        response = self._client.get(f"{_job_path(full_name)}/api/json")
        response.raise_for_status()
        payload = response.json()
        return JenkinsJob(
            name=str(payload.get("name", full_name.rsplit("/", 1)[-1])),
            full_name=str(payload.get("fullName", full_name)),
            url=str(payload.get("url", "")),
            kind=str(payload.get("_class", "")),
            color=payload.get("color"),
        )

    def wait_for_job(
        self,
        full_name: str,
        *,
        timeout: float = 120,
        interval: float = 1,
    ) -> JenkinsJob:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return self.job(full_name)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
            time.sleep(interval)
        raise ReadinessError(f"Jenkins job {full_name!r} did not appear in {timeout}s")

    def inspect_job(self, full_name: str) -> JenkinsJobConfiguration:
        job = self.job(full_name)
        response = self._client.get(f"{_job_path(full_name)}/config.xml")
        response.raise_for_status()
        xml = response.text
        root = ET.fromstring(xml)
        repository_urls = tuple(
            value
            for element in root.iter()
            if _local_name(element.tag) in {"remote", "url"}
            and (value := (element.text or "").strip())
        )
        script_path = next(
            (
                (element.text or "").strip()
                for element in root.iter()
                if _local_name(element.tag) == "scriptPath"
            ),
            None,
        )
        return JenkinsJobConfiguration(
            job=job,
            xml=xml,
            root_type=_local_name(root.tag),
            repository_urls=repository_urls,
            script_path=script_path,
        )

    def create_job(self, name: str, config_xml: str) -> JenkinsJobConfiguration:
        response = self._client.post(
            "/createItem",
            params={"name": name},
            content=config_xml,
            headers={"Content-Type": "application/xml"},
        )
        response.raise_for_status()
        return self.inspect_job(name)

    def scan_multibranch(self, job: str) -> None:
        response = self._client.post(f"{_job_path(job)}/build", params={"delay": "0sec"})
        response.raise_for_status()

    def configure_library(
        self,
        name: str,
        *,
        repository_url: str,
        default_version: str = "main",
        implicit: bool = False,
        allow_version_override: bool = True,
        include_in_changesets: bool = False,
        credentials_id: str | None = None,
    ) -> JenkinsLibrary:
        encoded = _encoded_json(
            {
                "name": name,
                "repositoryUrl": repository_url,
                "defaultVersion": default_version,
                "implicit": implicit,
                "allowVersionOverride": allow_version_override,
                "includeInChangesets": include_in_changesets,
                "credentialsId": credentials_id or "",
            }
        )
        self._script(
            f"""
import groovy.json.JsonSlurper
import java.util.Base64
import jenkins.model.Jenkins
import jenkins.plugins.git.GitSCMSource
import jenkins.plugins.git.traits.BranchDiscoveryTrait
import org.jenkinsci.plugins.workflow.libs.GlobalLibraries
import org.jenkinsci.plugins.workflow.libs.LibraryConfiguration
import org.jenkinsci.plugins.workflow.libs.SCMSourceRetriever

def config = new JsonSlurper().parseText(
    new String(Base64.getDecoder().decode('{encoded}'), 'UTF-8')
)
def source = new GitSCMSource(config.repositoryUrl as String)
source.traits = [new BranchDiscoveryTrait()]
if (config.credentialsId) {{
    source.credentialsId = config.credentialsId as String
}}
def library = new LibraryConfiguration(
    config.name as String,
    new SCMSourceRetriever(source)
)
library.defaultVersion = config.defaultVersion as String
library.implicit = config.implicit as boolean
library.allowVersionOverride = config.allowVersionOverride as boolean
library.includeInChangesets = config.includeInChangesets as boolean
def globals = Jenkins.get().getExtensionList(GlobalLibraries.class)[0]
def retained = globals.libraries.findAll {{ it.name != config.name }}
globals.libraries = retained + library
globals.save()
"""
        )
        return self.inspect_library(name)

    def list_libraries(self) -> list[JenkinsLibrary]:
        output = self._script(
            """
import groovy.json.JsonOutput
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.libs.GlobalLibraries

def globals = Jenkins.get().getExtensionList(GlobalLibraries.class)[0]
def result = globals.libraries.collect { library ->
    def scm = library.retriever.hasProperty('scm') ? library.retriever.scm : null
    [
        name: library.name,
        repositoryUrl: scm?.remote ?: '',
        defaultVersion: library.defaultVersion ?: '',
        implicit: library.implicit,
        allowVersionOverride: library.allowVersionOverride,
        includeInChangesets: library.includeInChangesets,
        credentialsId: scm?.credentialsId ?: ''
    ]
}
println(JsonOutput.toJson(result))
"""
        )
        last_line = output.strip().splitlines()[-1] if output.strip() else "[]"
        return [
            JenkinsLibrary(
                name=str(item["name"]),
                repository_url=str(item["repositoryUrl"]),
                default_version=str(item["defaultVersion"]),
                implicit=bool(item["implicit"]),
                allow_version_override=bool(item["allowVersionOverride"]),
                include_in_changesets=bool(item["includeInChangesets"]),
                credentials_id=str(item["credentialsId"]) or None,
            )
            for item in json.loads(last_line)
        ]

    def inspect_library(self, name: str) -> JenkinsLibrary:
        for library in self.list_libraries():
            if library.name == name:
                return library
        visible = [library.name for library in self.list_libraries()]
        raise ReadinessError(
            f"Jenkins shared library {name!r} was not found; configured libraries: {visible}"
        )

    def remove_library(self, name: str) -> None:
        encoded = _encoded_json({"name": name})
        self._script(
            f"""
import groovy.json.JsonSlurper
import java.util.Base64
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.libs.GlobalLibraries

def config = new JsonSlurper().parseText(
    new String(Base64.getDecoder().decode('{encoded}'), 'UTF-8')
)
def globals = Jenkins.get().getExtensionList(GlobalLibraries.class)[0]
globals.libraries = globals.libraries.findAll {{ it.name != config.name }}
globals.save()
"""
        )

    def trigger(
        self,
        job: str,
        *,
        parameters: dict[str, str] | None = None,
    ) -> str:
        path = (
            f"{_job_path(job)}/buildWithParameters"
            if parameters is not None
            else f"{_job_path(job)}/build"
        )
        response = self._client.post(path, data=parameters or {})
        response.raise_for_status()
        location = response.headers.get("location")
        if not location:
            raise ReadinessError("Jenkins did not return a queue item location")
        return location.rstrip("/").rsplit("/", 1)[-1]

    def wait_build(
        self,
        queue_id: str,
        *,
        job: str = "harness-poc",
        timeout: float = 300,
        interval: float = 1,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        build_number: int | None = None
        while time.monotonic() < deadline:
            if build_number is None:
                queue = self._json(f"/queue/item/{queue_id}/api/json")
                if queue.get("cancelled"):
                    raise ReadinessError(f"Jenkins queue item {queue_id} was cancelled")
                executable = queue.get("executable")
                if executable:
                    build_number = int(executable["number"])
            else:
                build = self._json(f"{_job_path(job)}/{build_number}/api/json")
                if not build["building"]:
                    return build
            time.sleep(interval)
        raise ReadinessError(f"Jenkins queue item {queue_id} did not finish in {timeout}s")

    def builds(self, job: str) -> list[dict[str, Any]]:
        response = self._client.get(
            f"{_job_path(job)}/api/json",
            params={
                "tree": (
                    "builds[number,result,building,timestamp,url,"
                    "artifacts[fileName,relativePath]]"
                )
            },
        )
        response.raise_for_status()
        return list(response.json().get("builds", []))

    def wait_for_new_build(
        self,
        job: str,
        *,
        after: int | None = None,
        timeout: float = 300,
        interval: float = 1,
    ) -> dict[str, Any]:
        """Observe a build triggered by the system under test, rather than trigger it."""

        deadline = time.monotonic() + timeout
        number: int | None = None
        while time.monotonic() < deadline:
            if number is None:
                candidates = [
                    int(build["number"])
                    for build in self.builds(job)
                    if after is None or int(build["number"]) > after
                ]
                if candidates:
                    number = max(candidates)
            if number is not None:
                build = self._json(f"{_job_path(job)}/{number}/api/json")
                if not build["building"]:
                    return build
            time.sleep(interval)
        boundary = "any build" if after is None else f"a build newer than #{after}"
        raise ReadinessError(f"Jenkins job {job!r} did not finish {boundary} in {timeout}s")

    def console(self, job: str, build_number: int) -> str:
        response = self._client.get(f"{_job_path(job)}/{build_number}/consoleText")
        response.raise_for_status()
        return response.text

    def list_artifacts(self, job: str, build_number: int) -> list[JenkinsArtifact]:
        payload = self._json(f"{_job_path(job)}/{build_number}/api/json")
        return [
            JenkinsArtifact(
                file_name=str(item["fileName"]),
                relative_path=str(item["relativePath"]),
            )
            for item in payload.get("artifacts", [])
        ]

    def artifact(self, job: str, build_number: int, path: str) -> bytes:
        artifact_path = PurePosixPath(path)
        if artifact_path.is_absolute() or ".." in artifact_path.parts:
            raise ValueError("Jenkins artifact path must be relative and may not contain '..'")
        response = self._client.get(
            f"{_job_path(job)}/{build_number}/artifact/{quote(path, safe='/')}"
        )
        response.raise_for_status()
        return response.content

    def _json(self, path: str) -> dict[str, Any]:
        response = self._client.get(path)
        response.raise_for_status()
        return response.json()

    def _script(self, script: str) -> str:
        response = self._client.post("/scriptText", data={"script": script})
        response.raise_for_status()
        return response.text


def multibranch_job_config(
    *,
    repository_url: str,
    source_id: str,
    script_path: str = "Jenkinsfile",
    description: str = "Harness-managed multibranch Pipeline",
) -> str:
    remote = escape(repository_url)
    source = escape(source_id)
    script = escape(script_path)
    rendered_description = escape(description)
    return f"""<?xml version='1.1' encoding='UTF-8'?>
<org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>
  <actions/>
  <description>{rendered_description}</description>
  <properties/>
  <folderViews class="jenkins.branch.MultiBranchProjectViewHolder">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject"
           reference="../.."/>
  </folderViews>
  <healthMetrics/>
  <icon class="jenkins.branch.MetadataActionFolderIcon">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject"
           reference="../.."/>
  </icon>
  <orphanedItemStrategy
      class="com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy">
    <pruneDeadBranches>true</pruneDeadBranches>
    <daysToKeep>-1</daysToKeep>
    <numToKeep>-1</numToKeep>
    <abortBuilds>false</abortBuilds>
  </orphanedItemStrategy>
  <triggers/>
  <disabled>false</disabled>
  <sources class="jenkins.branch.MultiBranchProject$BranchSourceList">
    <data>
      <jenkins.branch.BranchSource>
        <source class="jenkins.plugins.git.GitSCMSource">
          <id>{source}</id>
          <remote>{remote}</remote>
          <credentialsId></credentialsId>
          <traits>
            <jenkins.plugins.git.traits.BranchDiscoveryTrait/>
          </traits>
        </source>
        <strategy class="jenkins.branch.DefaultBranchPropertyStrategy">
          <properties class="empty-list"/>
        </strategy>
      </jenkins.branch.BranchSource>
    </data>
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject"
           reference="../.."/>
  </sources>
  <factory class="org.jenkinsci.plugins.workflow.multibranch.WorkflowBranchProjectFactory">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject"
           reference="../.."/>
    <scriptPath>{script}</scriptPath>
  </factory>
</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>
"""


def _job_path(full_name: str) -> str:
    parts = [part for part in full_name.split("/") if part]
    return "".join(f"/job/{quote(part, safe='')}" for part in parts)


def _job_tree(depth: int) -> str:
    fields = "name,url,color,_class"
    for _ in range(max(depth, 0)):
        fields = f"name,url,color,_class,jobs[{fields}]"
    return f"jobs[{fields}]"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _encoded_json(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, separators=(",", ":")).encode()
    return base64.b64encode(serialized).decode()
