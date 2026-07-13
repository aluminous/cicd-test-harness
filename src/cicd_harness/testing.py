from __future__ import annotations

import hashlib
import json
import ssl
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from string import Template
from typing import Any

import httpx

from cicd_harness.assets import bundled_workspace
from cicd_harness.command import CommandRunner
from cicd_harness.component import EnvironmentComponent
from cicd_harness.components import (
    GiteaComponent,
    JenkinsComponent,
    SpinnakerComponent,
    WireMockComponent,
)
from cicd_harness.config import HarnessProfile
from cicd_harness.diagnostics import DiagnosticCollector, DiagnosticSource
from cicd_harness.endpoints import EndpointCatalog, HostEndpointManager
from cicd_harness.environment import HarnessEnvironment
from cicd_harness.errors import HarnessError, ReadinessError, VerificationError
from cicd_harness.gitea import GiteaClient, GiteaRepository, GitWorkspace
from cicd_harness.jenkins import (
    JenkinsArtifact,
    JenkinsClient,
    JenkinsJob,
    JenkinsJobConfiguration,
    JenkinsLibrary,
    multibranch_job_config,
)
from cicd_harness.kubectl import Kubectl, PortForward
from cicd_harness.naming import dns_name_with_suffix as _dns_name_with_suffix
from cicd_harness.rollouts import ReplicaSetState, RolloutProbe
from cicd_harness.spinnaker import (
    SpinnakerClient,
    http_file_artifact,
    kustomize_pipeline,
    raw_manifest_pipeline,
)
from cicd_harness.testing_http import (
    MockAPI,
    ProxyAPI,
    WireMockRouting,
)
from cicd_harness.trust import ssl_context
from cicd_harness.wiremock import WireMockClient


class HarnessRuntime:
    """Session-owned infrastructure used by function-scoped test cases."""

    def __init__(
        self,
        profile: HarnessProfile,
        *,
        workspace: Path,
        artifact_root: Path,
        include_spinnaker: bool = True,
        include_jenkins: bool = True,
        keep: bool = False,
        preserve_environment_on_failure: bool = False,
        reporter: Any | None = None,
        runner: CommandRunner | None = None,
        components: list[EnvironmentComponent] | None = None,
        component_names: set[str] | None = None,
    ) -> None:
        self.profile = profile
        self.workspace = workspace
        self.artifact_root = artifact_root
        self.include_spinnaker = include_spinnaker
        self.include_jenkins = include_jenkins
        self.keep = keep
        self.preserve_environment_on_failure = preserve_environment_on_failure
        self.reporter = reporter
        self.environment = HarnessEnvironment(
            profile,
            workspace=workspace,
            runner=runner,
            include_spinnaker=include_spinnaker,
            include_jenkins=include_jenkins,
            components=components,
            component_names=component_names,
        )
        self.diagnostics = DiagnosticCollector(
            self.environment.kubectl,
            artifact_root,
            secrets=(
                "harness-password",
                *self.environment.registry.redaction_values(),
            ),
        )
        self.host = HostEndpointManager(
            EndpointCatalog(self.environment.components),
            self.environment.kubectl,
        )
        self.artifacts: list[Path] = []
        self.preserved_failures: list[dict[str, str]] = []
        self.started = False

    def start(self, *, timeout: int = 900) -> None:
        try:
            self.environment.up(timeout=timeout)
            self.started = True
        except Exception:
            self.capture("session-startup", metadata={"phase": "startup"})
            if self.preserve_environment_on_failure:
                self.preserve_failure(node_id="<session-startup>", namespace="")
            elif not self.keep:
                with suppress(Exception):
                    self.environment.down()
            raise

    def test_case(self, node_id: str, workdir: Path) -> TestHarness:
        if not self.started:
            raise HarnessError("the CI/CD harness runtime has not been started")
        return TestHarness(self, node_id=node_id, workdir=workdir)

    def stop(self) -> None:
        self.host.close()
        if not self.started or self.keep or self.preserved_failures:
            return
        try:
            self.environment.down()
        except Exception:
            self.capture("session-teardown", metadata={"phase": "teardown"})
            raise
        finally:
            self.started = False

    def preserve_failure(self, *, node_id: str, namespace: str) -> None:
        preserved = {"test": node_id, "namespace": namespace}
        if preserved not in self.preserved_failures:
            self.preserved_failures.append(preserved)
        if self.reporter is not None:
            namespace_detail = f", namespace={namespace}" if namespace else ""
            self.reporter(
                f"CI/CD harness preserved failed environment: "
                f"context={self.environment.cluster.context}{namespace_detail}"
            )

    def capture(
        self,
        label: str,
        *,
        metadata: dict[str, Any] | None = None,
        sources: dict[str, DiagnosticSource] | None = None,
    ) -> Path:
        resolved_sources = {
            "component-graph": self.environment.components.snapshot,
            "host-endpoints": self.host.snapshot,
            **(sources or {}),
        }
        artifact = self.diagnostics.collect(
            label,
            metadata={"profile": self.profile.name, **(metadata or {})},
            sources=resolved_sources,
        )
        self.artifacts.append(artifact)
        if self.reporter is not None:
            self.reporter(f"CI/CD harness diagnostics: {artifact}")
        return artifact


class TestHarness:
    """High-level, application-focused interface exposed as the pytest fixture."""

    __test__ = False

    def __init__(self, runtime: HarnessRuntime, *, node_id: str, workdir: Path) -> None:
        self.runtime = runtime
        self.node_id = node_id
        self.workdir = workdir
        self.token = hashlib.sha256(node_id.encode()).hexdigest()[:8]
        self.namespace = _dns_name_with_suffix(
            f"test-{node_id.rsplit('::', 1)[-1]}",
            self.token,
        )
        self._counter = 0
        self._services = _ServiceConnections(self)
        self._git: GitAPI | None = None
        self._mocks: MockAPI | None = None
        self._proxies: ProxyAPI | None = None
        self._wiremock_routing = WireMockRouting(self)
        self._jenkins: JenkinsAPI | None = None
        self._spinnaker: SpinnakerAPI | None = None
        self._resources = ApplicationResources(self)
        self._images = ImageAPI(self)
        self._advanced = AdvancedAccess(self)
        self._rollouts: set[tuple[str, str]] = set()
        self._repositories: list[TestRepository] = []
        self._jenkins_runs: list[dict[str, Any]] = []
        self._jenkins_jobs: set[str] = set()
        self._jenkins_libraries: set[str] = set()
        self._owned_jenkins_libraries: set[str] = set()
        self._spinnaker_runs: list[dict[str, Any]] = []
        self._closed = False

    @property
    def profile(self) -> HarnessProfile:
        return self.runtime.profile

    @property
    def git(self) -> GitAPI:
        if self._git is None:
            self._git = GitAPI(self)
        return self._git

    @property
    def mocks(self) -> MockAPI:
        if self._mocks is None:
            self._mocks = MockAPI(self)
        return self._mocks

    @property
    def proxies(self) -> ProxyAPI:
        if self._proxies is None:
            self._proxies = ProxyAPI(self)
        return self._proxies

    @property
    def jenkins(self) -> JenkinsAPI:
        if not self.runtime.environment.components.has("jenkins"):
            raise HarnessError("Jenkins was disabled for this harness session")
        if self._jenkins is None:
            self._jenkins = JenkinsAPI(self)
        return self._jenkins

    @property
    def spinnaker(self) -> SpinnakerAPI:
        if not self.runtime.environment.components.has("spinnaker"):
            raise HarnessError("Spinnaker was disabled for this harness session")
        if self._spinnaker is None:
            self._spinnaker = SpinnakerAPI(self)
        return self._spinnaker

    @property
    def resources(self) -> ApplicationResources:
        return self._resources

    @property
    def images(self) -> ImageAPI:
        """Resolve private-registry images and prepare additional namespaces."""

        return self._images

    @property
    def advanced(self) -> AdvancedAccess:
        return self._advanced

    @property
    def host(self) -> HostEndpointManager:
        """Discover or expose selected component UIs and APIs on host loopback."""

        return self.runtime.host

    def start(self) -> None:
        self.runtime.environment.kubectl.apply(
            f"""apiVersion: v1
kind: Namespace
metadata:
  name: {self.namespace}
  labels:
    harness.cicd/managed: "true"
    harness.cicd/test: {self.token}
"""
        )
        self.runtime.environment.registry.ensure_namespace(
            self.runtime.environment.kubectl,
            self.namespace,
        )
        if self.runtime.environment.components.has("wiremock"):
            self.mocks.reset()

    def unique_name(self, prefix: str) -> str:
        self._counter += 1
        return _dns_name_with_suffix(prefix, f"{self.token}-{self._counter}")

    def service(
        self,
        name: str,
        *,
        port: int,
        namespace: str | None = None,
    ) -> ApplicationService:
        """Connect to an HTTP Service without exposing port-forward lifecycle."""

        resolved_namespace = namespace or self.namespace
        return self._services.application(name, resolved_namespace, port)

    def wait_until(
        self,
        probe: Callable[[], Any],
        *,
        predicate: Callable[[Any], bool] | None = None,
        description: str = "condition to become true",
        timeout: float = 60,
        interval: float = 0.5,
    ) -> Any:
        """Poll application state with a useful last-value timeout error."""

        matches = predicate or bool
        deadline = time.monotonic() + timeout
        last: Any = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                last = probe()
                last_error = None
                if matches(last):
                    return last
            except Exception as exc:
                last_error = exc
            time.sleep(interval)
        detail = f"last value: {last!r}"
        if last_error is not None:
            detail = f"last error: {type(last_error).__name__}: {last_error}"
        raise ReadinessError(f"Timed out waiting for {description} in {timeout}s; {detail}")

    def rollout(self, name: str, *, namespace: str | None = None) -> RolloutHandle:
        resolved_namespace = namespace or self.namespace
        self._rollouts.add((resolved_namespace, name))
        return RolloutHandle(
            self.runtime.environment.kubectl,
            namespace=resolved_namespace,
            name=name,
        )

    def capture_diagnostics(self, label: str = "manual") -> Path:
        return self.runtime.capture(
            f"{self.token}-{label}",
            metadata={"test": self.node_id, "namespace": self.namespace},
            sources=self._diagnostic_sources(),
        )

    def finish(self, *, failed: bool) -> None:
        if self._closed:
            return
        verification_error: Exception | None = None
        try:
            if failed:
                self.capture_diagnostics("failure")
            elif self.runtime.environment.components.has("wiremock"):
                try:
                    self.mocks.verify()
                except Exception as exc:
                    verification_error = exc
                    self.capture_diagnostics("mock-verification-failure")
        finally:
            preserve = self.runtime.preserve_environment_on_failure and (
                failed or verification_error is not None
            )
            if preserve:
                self.runtime.preserve_failure(
                    node_id=self.node_id,
                    namespace=self.namespace,
                )
            else:
                self._remove_owned_jenkins_libraries()
                self._delete_namespace()
            self._services.close()
            self._closed = True
        if verification_error is not None:
            raise verification_error

    def _delete_namespace(self) -> None:
        self.runtime.environment.runner.run(
            self.runtime.environment.kubectl.command(
                "delete",
                "namespace",
                self.namespace,
                "--ignore-not-found",
                "--wait=true",
                "--timeout=120s",
            ),
            check=False,
            timeout=130,
        )

    def _diagnostic_sources(self) -> dict[str, DiagnosticSource]:
        sources: dict[str, DiagnosticSource] = {
            "test-context": lambda: {
                "nodeId": self.node_id,
                "namespace": self.namespace,
                "trackedRollouts": sorted(self._rollouts),
                "repositories": [repository.as_dict() for repository in self._repositories],
            },
            "jenkins": self._jenkins_diagnostics,
            "spinnaker-executions": self._spinnaker_diagnostics,
        }
        if self._services.has("wiremock"):
            sources["wiremock"] = self._services.wiremock().snapshot
        return sources

    def _jenkins_diagnostics(self) -> dict[str, Any]:
        result: dict[str, Any] = {"builds": self._jenkins_runs, "jobs": []}
        if not self._services.has("jenkins"):
            return result
        client = self._services.jenkins()
        try:
            result["jobs"] = [asdict(job) for job in client.list_jobs(recursive=True)]
        except Exception as exc:
            result["listError"] = str(exc)
        configurations: dict[str, Any] = {}
        for name in sorted(self._jenkins_jobs):
            try:
                config = client.inspect_job(name)
                configurations[name] = asdict(config)
            except Exception as exc:
                configurations[name] = {"diagnosticError": str(exc)}
        result["configurations"] = configurations
        try:
            result["libraries"] = [asdict(item) for item in client.list_libraries()]
        except Exception as exc:
            result["libraryListError"] = str(exc)
        return result

    def _remove_owned_jenkins_libraries(self) -> None:
        if not self._owned_jenkins_libraries or not self._services.has("jenkins"):
            return
        client = self._services.jenkins()
        for name in sorted(self._owned_jenkins_libraries):
            with suppress(Exception):
                client.remove_library(name)

    def _spinnaker_diagnostics(self) -> list[dict[str, Any]]:
        if not self._services.has("spinnaker"):
            return self._spinnaker_runs
        client = self._services.spinnaker()
        result: list[dict[str, Any]] = []
        for tracked in self._spinnaker_runs:
            item = dict(tracked)
            execution_id = item.get("executionId")
            if execution_id:
                try:
                    item["execution"] = client.execution(str(execution_id))
                except Exception as exc:
                    item["diagnosticError"] = str(exc)
            result.append(item)
        return result


@dataclass(frozen=True)
class ApplicationService:
    """Managed HTTP connection to a Kubernetes Service in the test environment."""

    name: str
    namespace: str
    port: int
    url: str
    _client: httpx.Client = field(repr=False, compare=False)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.request(method, path, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.post(path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.put(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.delete(path, **kwargs)


class ApplicationResources:
    """Deploy and inspect test application resources in the isolated namespace."""

    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness

    def apply(self, manifest: str, *, namespace: str | None = None) -> None:
        manifest = self.harness.runtime.environment.registry.manifest(manifest)
        self.harness.runtime.environment.kubectl.apply(
            manifest,
            namespace=namespace or self.harness.namespace,
        )

    def apply_file(self, path: Path, *, namespace: str | None = None) -> None:
        self.apply(
            path.read_text(),
            namespace=namespace,
        )

    def get(self, resource: str, *, namespace: str | None = None) -> Any:
        return self.harness.runtime.environment.kubectl.get_json(
            resource,
            "-n",
            namespace or self.harness.namespace,
        )

    def list(self, resource: str, *, namespace: str | None = None) -> list[Any]:
        payload = self.get(resource, namespace=namespace)
        return list(payload.get("items", []))

    def wait_available(
        self,
        deployment: str,
        *,
        namespace: str | None = None,
        timeout: int = 180,
    ) -> None:
        self.harness.runtime.environment.kubectl.wait_available(
            deployment,
            namespace or self.harness.namespace,
            timeout=timeout,
        )


class ImageAPI:
    """Private-registry behavior exposed without leaking registry credentials."""

    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness

    def resolve(self, image: str) -> str:
        """Return the effective image reference for this profile."""

        return self.harness.runtime.environment.registry.image(image)

    def rewrite_manifest(self, manifest: str) -> str:
        """Rewrite all Kubernetes container image fields in YAML."""

        return self.harness.runtime.environment.registry.manifest(manifest)

    def ensure_namespace(self, namespace: str) -> None:
        """Create a namespace and attach the configured pull secret."""

        self.harness.runtime.environment.registry.ensure_namespace(
            self.harness.runtime.environment.kubectl,
            namespace,
        )


class GitAPI:
    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness

    def create_repository(
        self,
        *,
        files: Mapping[str, str | bytes] | None = None,
        template: str | Path | None = None,
        variables: Mapping[str, str] | None = None,
        name: str | None = None,
        message: str = "Seed test repository",
        private: bool = False,
    ) -> TestRepository:
        repository_name = name or self.harness.unique_name("manifests")
        remote = self.harness._services.gitea().create_repository(
            repository_name,
            private=private,
        )
        path = self.harness.workdir / "repositories" / repository_name
        path.mkdir(parents=True)
        git = GitWorkspace(
            path,
            base_env=self.harness.runtime.environment.runner.base_env,
        )
        git.initialize()
        git.add_remote(self.harness._services.gitea().host_clone_url(remote))
        repository = TestRepository(remote=remote, path=path, git=git)
        self.harness._repositories.append(repository)
        if template is not None or files:
            if template is not None:
                repository._copy_seed_tree(  # noqa: SLF001 - GitAPI owns repository setup
                    self.resolve_template(template),
                    variables=variables,
                )
            repository._write_files(files or {})  # noqa: SLF001 - one initial commit
            repository.revision = repository.git.commit(message)
            repository.git.push()
        return repository

    def create_repository_from(
        self,
        source: str | Path,
        *,
        files: Mapping[str, str | bytes] | None = None,
        variables: Mapping[str, str] | None = None,
        name: str | None = None,
        message: str = "Seed test repository",
        private: bool = False,
    ) -> TestRepository:
        return self.create_repository(
            files=files,
            template=source,
            variables=variables,
            name=name,
            message=message,
            private=private,
        )

    def resolve_template(self, template: str | Path) -> Path:
        requested = Path(template)
        candidates = (
            (requested,) if requested.is_absolute() else ()
        ) + (
            self.harness.runtime.workspace / requested,
            self.harness.runtime.workspace / "fixtures" / requested,
            bundled_workspace() / "fixtures" / requested,
        )
        for candidate in candidates:
            if candidate.is_dir():
                return candidate.resolve()
        rendered = ", ".join(str(candidate) for candidate in candidates)
        raise HarnessError(
            f"repository template {str(template)!r} was not found; searched: {rendered}"
        )


@dataclass
class TestRepository:
    __test__ = False

    remote: GiteaRepository
    path: Path
    git: GitWorkspace
    revision: str | None = None

    @property
    def clone_url(self) -> str:
        return self.remote.clone_url

    def update(self, files: Mapping[str, str | bytes], *, message: str) -> str:
        self._write_files(files)
        self.revision = self.git.commit(message)
        self.git.push()
        return self.revision

    def update_from(
        self,
        source: Path,
        *,
        message: str,
        files: Mapping[str, str | bytes] | None = None,
        variables: Mapping[str, str] | None = None,
    ) -> str:
        self._copy_seed_tree(source, variables=variables)
        self._write_files(files or {})
        self.revision = self.git.commit(message)
        self.git.push()
        return self.revision

    def create_branch(
        self,
        name: str,
        *,
        files: Mapping[str, str | bytes],
        message: str | None = None,
    ) -> str:
        """Create and push another branch while keeping the worktree on main."""

        self.git.runner.run(["git", "check-ref-format", "--branch", name])
        self.git.runner.run(["git", "switch", "-c", name])
        try:
            self._write_files(files)
            revision = self.git.commit(message or f"Create {name} branch")
            self.git.push(branch=name)
            return revision
        finally:
            self.git.runner.run(["git", "switch", "main"])

    def _write_files(self, files: Mapping[str, str | bytes]) -> None:
        for relative, contents in files.items():
            target = _safe_child(self.path, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(contents, bytes):
                target.write_bytes(contents)
            else:
                target.write_text(contents)

    def _copy_seed_tree(
        self,
        source: Path,
        *,
        variables: Mapping[str, str] | None = None,
    ) -> None:
        source = source.resolve()
        if not source.is_dir():
            raise HarnessError(f"repository seed source is not a directory: {source}")
        for item in sorted(source.rglob("*")):
            relative = item.relative_to(source)
            if ".git" in relative.parts:
                raise HarnessError(f"repository seed must not contain .git metadata: {item}")
            if item.is_symlink():
                raise HarnessError(f"repository seed must not contain symlinks: {item}")
            if item.is_dir():
                continue
            if not item.is_file():
                raise HarnessError(f"unsupported repository seed entry: {item}")
            rendered_relative = relative
            contents = item.read_bytes()
            if item.name.endswith(".tmpl"):
                rendered_relative = relative.with_name(item.name.removesuffix(".tmpl"))
                try:
                    template = Template(contents.decode("utf-8"))
                    contents = template.substitute(dict(variables or {})).encode()
                except (KeyError, UnicodeDecodeError, ValueError) as exc:
                    raise HarnessError(
                        f"could not render repository template file {item}: {exc}"
                    ) from exc
            target = _safe_child(self.path, str(rendered_relative))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
            executable_bits = item.stat().st_mode & 0o111
            if executable_bits:
                target.chmod(target.stat().st_mode | executable_bits)

    def refresh(self, *, branch: str = "main") -> str:
        """Refresh the exact revision after Jenkins or another service pushes."""

        self.git.runner.run(["git", "fetch", "origin", branch])
        self.revision = self.git.runner.run(
            ["git", "rev-parse", f"origin/{branch}"]
        ).stdout.strip()
        return self.revision

    def read(self, path: str, *, revision: str | None = None) -> str:
        """Read a file from an exact local or externally-pushed Git revision."""

        _safe_child(self.path, path)
        resolved_revision = revision or self.revision
        if resolved_revision is None:
            raise HarnessError("the repository has no commit yet")
        return self.git.runner.run(
            ["git", "show", f"{resolved_revision}:{path}"]
        ).stdout

    def raw_url(self, path: str, *, revision: str | None = None) -> str:
        resolved_revision = revision or self.revision
        if resolved_revision is None:
            raise HarnessError("the repository has no commit yet")
        return self.remote.raw_commit_url(resolved_revision, path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner": self.remote.owner,
            "name": self.remote.name,
            "cloneUrl": self.remote.clone_url,
            "revision": self.revision,
            "worktree": str(self.path),
        }


class JenkinsAPI:
    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness

    def run(
        self,
        job: str,
        *,
        parameters: dict[str, str] | None = None,
        timeout: float = 300,
        expected_result: str = "SUCCESS",
    ) -> JenkinsBuild:
        client = self.harness._services.jenkins()
        queue_id = client.trigger(job, parameters=parameters)
        tracked: dict[str, Any] = {"job": job, "queueId": queue_id}
        self.harness._jenkins_runs.append(tracked)
        try:
            payload = client.wait_build(queue_id, job=job, timeout=timeout)
            number = int(payload["number"])
            console = client.console(job, number)
            result = str(payload["result"])
            tracked.update(
                {
                    "number": number,
                    "result": result,
                    "payload": payload,
                    "console": console,
                }
            )
        except Exception as exc:
            tracked["error"] = str(exc)
            raise
        build = JenkinsBuild(
            job=job,
            queue_id=queue_id,
            number=number,
            result=result,
            console=console,
            payload=payload,
            _client=client,
        )
        if result != expected_result:
            raise VerificationError(
                f"Jenkins {job} #{number} finished as {result}, expected "
                f"{expected_result}\n\n{console}"
            )
        return build

    def list_jobs(self, *, recursive: bool = True) -> list[JenkinsJob]:
        return self.harness._services.jenkins().list_jobs(recursive=recursive)

    def wait_for_job(self, name: str, *, timeout: float = 180) -> JenkinsJob:
        """Wait for an asynchronously created Jenkins job to appear."""

        self.harness._jenkins_jobs.add(name)
        return self.harness._services.jenkins().wait_for_job(name, timeout=timeout)

    def latest_build_number(self, job: str) -> int | None:
        """Capture a baseline before triggering the system under test."""

        builds = self.harness._services.jenkins().builds(job)
        return max((int(build["number"]) for build in builds), default=None)

    def wait_for_build(
        self,
        job: str,
        *,
        after: int | None = None,
        timeout: float = 300,
        expected_result: str = "SUCCESS",
    ) -> JenkinsBuild:
        """Wait for a build initiated by the application and assert its result."""

        client = self.harness._services.jenkins()
        tracked: dict[str, Any] = {"job": job, "observedExternally": True, "after": after}
        self.harness._jenkins_runs.append(tracked)
        try:
            payload = client.wait_for_new_build(job, after=after, timeout=timeout)
            number = int(payload["number"])
            result = str(payload["result"])
            console = client.console(job, number)
            tracked.update(
                {
                    "number": number,
                    "result": result,
                    "payload": payload,
                    "console": console,
                }
            )
        except Exception as exc:
            tracked["error"] = str(exc)
            raise
        build = JenkinsBuild(
            job=job,
            queue_id=None,
            number=number,
            result=result,
            console=console,
            payload=payload,
            _client=client,
        )
        if result != expected_result:
            raise VerificationError(
                f"Jenkins {job} #{number} finished as {result}, expected "
                f"{expected_result}\n\n{console}"
            )
        return build

    def inspect_job(self, name: str) -> JenkinsJobConfiguration:
        self.harness._jenkins_jobs.add(name)
        return self.harness._services.jenkins().inspect_job(name)

    def assert_job(
        self,
        name: str,
        *,
        kind_contains: str | None = None,
        repository_url: str | None = None,
        script_path: str | None = None,
    ) -> JenkinsJobConfiguration:
        try:
            configuration = self.inspect_job(name)
        except Exception as exc:
            visible = [job.full_name for job in self.list_jobs()]
            raise VerificationError(
                f"Jenkins job {name!r} was not found; visible jobs: {visible}"
            ) from exc
        mismatches: list[str] = []
        if kind_contains is not None and kind_contains not in configuration.job.kind:
            mismatches.append(
                f"kind was {configuration.job.kind!r}, expected it to contain {kind_contains!r}"
            )
        if (
            repository_url is not None
            and repository_url not in configuration.repository_urls
        ):
            mismatches.append(
                f"repository URLs were {configuration.repository_urls!r}, "
                f"expected {repository_url!r}"
            )
        if script_path is not None and configuration.script_path != script_path:
            mismatches.append(
                f"script path was {configuration.script_path!r}, expected {script_path!r}"
            )
        if mismatches:
            raise VerificationError(
                f"Jenkins job {name!r} configuration mismatch: " + "; ".join(mismatches)
            )
        return configuration

    def create_multibranch_job(
        self,
        repository: TestRepository,
        *,
        name: str | None = None,
        script_path: str = "Jenkinsfile",
        scan: bool = True,
    ) -> JenkinsJobConfiguration:
        job_name = name or self.harness.unique_name("pipeline")
        source_id = self.harness.unique_name("git-source")
        client = self.harness._services.jenkins()
        configuration = client.create_job(
            job_name,
            multibranch_job_config(
                repository_url=repository.clone_url,
                source_id=source_id,
                script_path=script_path,
            ),
        )
        self.harness._jenkins_jobs.add(job_name)
        if scan:
            client.scan_multibranch(job_name)
        return configuration

    def wait_for_branch(
        self,
        job: str,
        branch: str,
        *,
        timeout: float = 180,
    ) -> JenkinsJob:
        full_name = f"{job}/{branch}"
        self.harness._jenkins_jobs.add(job)
        return self.harness._services.jenkins().wait_for_job(full_name, timeout=timeout)

    def run_branch(
        self,
        job: str,
        branch: str = "main",
        *,
        timeout: float = 300,
        expected_result: str = "SUCCESS",
    ) -> JenkinsBuild:
        self.wait_for_branch(job, branch, timeout=timeout)
        return self.run(
            f"{job}/{branch}",
            timeout=timeout,
            expected_result=expected_result,
        )

    def configure_library(
        self,
        name: str,
        repository: TestRepository | str,
        *,
        default_version: str = "main",
        implicit: bool = False,
        allow_version_override: bool = True,
        include_in_changesets: bool = False,
        credentials_id: str | None = None,
    ) -> JenkinsLibrary:
        repository_url = (
            repository.clone_url if isinstance(repository, TestRepository) else repository
        )
        configured = self.harness._services.jenkins().configure_library(
            name,
            repository_url=repository_url,
            default_version=default_version,
            implicit=implicit,
            allow_version_override=allow_version_override,
            include_in_changesets=include_in_changesets,
            credentials_id=credentials_id,
        )
        self.harness._jenkins_libraries.add(name)
        self.harness._owned_jenkins_libraries.add(name)
        return configured

    def create_library(
        self,
        name: str,
        *,
        template: str | Path | None = None,
        files: Mapping[str, str | bytes] | None = None,
        variables: Mapping[str, str] | None = None,
        repository_name: str | None = None,
        default_version: str = "main",
        implicit: bool = False,
        allow_version_override: bool = True,
        include_in_changesets: bool = False,
    ) -> JenkinsLibraryFixture:
        repository = self.harness.git.create_repository(
            name=repository_name or self.harness.unique_name(f"{name}-library"),
            template=template,
            files=files,
            variables=variables,
            message=f"Create Jenkins shared library {name}",
        )
        configuration = self.configure_library(
            name,
            repository,
            default_version=default_version,
            implicit=implicit,
            allow_version_override=allow_version_override,
            include_in_changesets=include_in_changesets,
        )
        return JenkinsLibraryFixture(
            name=name,
            repository=repository,
            configuration=configuration,
        )

    def list_libraries(self) -> list[JenkinsLibrary]:
        return self.harness._services.jenkins().list_libraries()

    def inspect_library(self, name: str) -> JenkinsLibrary:
        self.harness._jenkins_libraries.add(name)
        return self.harness._services.jenkins().inspect_library(name)

    def assert_library(
        self,
        name: str,
        *,
        repository_url: str | None = None,
        default_version: str | None = None,
        implicit: bool | None = None,
    ) -> JenkinsLibrary:
        try:
            library = self.inspect_library(name)
        except Exception as exc:
            visible = [item.name for item in self.list_libraries()]
            raise VerificationError(
                f"Jenkins shared library {name!r} was not found; visible libraries: {visible}"
            ) from exc
        mismatches: list[str] = []
        if repository_url is not None and library.repository_url != repository_url:
            mismatches.append(
                f"repository was {library.repository_url!r}, expected {repository_url!r}"
            )
        if default_version is not None and library.default_version != default_version:
            mismatches.append(
                f"default version was {library.default_version!r}, "
                f"expected {default_version!r}"
            )
        if implicit is not None and library.implicit != implicit:
            mismatches.append(f"implicit was {library.implicit!r}, expected {implicit!r}")
        if mismatches:
            raise VerificationError(
                f"Jenkins shared library {name!r} configuration mismatch: "
                + "; ".join(mismatches)
            )
        return library


@dataclass(frozen=True)
class JenkinsBuild:
    job: str
    queue_id: str | None
    number: int
    result: str
    console: str
    payload: dict[str, Any]
    _client: JenkinsClient = field(repr=False, compare=False)

    def artifacts(self) -> list[JenkinsArtifact]:
        return self._client.list_artifacts(self.job, self.number)

    def artifact(self, path: str) -> bytes:
        return self._client.artifact(self.job, self.number, path)


@dataclass(frozen=True)
class JenkinsLibraryFixture:
    name: str
    repository: TestRepository
    configuration: JenkinsLibrary


class SpinnakerAPI:
    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness

    def run(
        self,
        pipeline: dict[str, Any],
        *,
        timeout: float = 600,
        expected_status: str = "SUCCEEDED",
    ) -> SpinnakerExecution:
        application = str(pipeline["application"])
        name = str(pipeline["name"])
        client = self.harness._services.spinnaker()
        client.save_pipeline(pipeline)
        execution_id = client.trigger(application, name)
        tracked: dict[str, Any] = {
            "application": application,
            "pipeline": name,
            "executionId": execution_id,
        }
        self.harness._spinnaker_runs.append(tracked)
        try:
            payload = client.wait_execution(execution_id, timeout=timeout)
            status = str(payload["status"])
            tracked.update({"status": status, "execution": payload})
        except Exception as exc:
            tracked["error"] = str(exc)
            raise
        execution = SpinnakerExecution(
            application=application,
            pipeline=name,
            execution_id=execution_id,
            status=status,
            payload=payload,
        )
        if status != expected_status:
            raise VerificationError(
                f"Spinnaker pipeline {application}/{name} finished as {status}, "
                f"expected {expected_status}:\n{json.dumps(payload, indent=2)}"
            )
        return execution

    def execution_ids(
        self,
        application: str,
        *,
        pipeline: str | None = None,
    ) -> set[str]:
        """Capture a baseline before the application triggers a pipeline."""

        return {
            str(item["id"])
            for item in self.harness._services.spinnaker().executions(application)
            if pipeline is None or item.get("name") == pipeline
        }

    def wait_for_execution(
        self,
        application: str,
        *,
        pipeline: str | None = None,
        excluding: set[str] | None = None,
        timeout: float = 600,
        expected_status: str = "SUCCEEDED",
    ) -> SpinnakerExecution:
        """Wait for a pipeline execution initiated by the application."""

        client = self.harness._services.spinnaker()
        ignored = excluding or set()
        deadline = time.monotonic() + timeout
        execution_id: str | None = None
        while time.monotonic() < deadline:
            candidates = [
                item
                for item in client.executions(application)
                if str(item.get("id")) not in ignored
                and (pipeline is None or item.get("name") == pipeline)
            ]
            if candidates:
                candidates.sort(key=lambda item: int(item.get("startTime") or 0), reverse=True)
                execution_id = str(candidates[0]["id"])
                break
            time.sleep(1)
        if execution_id is None:
            rendered = pipeline or "any pipeline"
            raise ReadinessError(
                f"Spinnaker did not create a new {application}/{rendered} execution "
                f"in {timeout}s"
            )
        remaining = max(1.0, deadline - time.monotonic())
        tracked = {
            "application": application,
            "pipeline": pipeline or "unknown",
            "executionId": execution_id,
            "observedExternally": True,
        }
        self.harness._spinnaker_runs.append(tracked)
        try:
            payload = client.wait_execution(execution_id, timeout=remaining)
            name = str(payload.get("name") or pipeline or "unknown")
            status = str(payload["status"])
            tracked.update(
                {
                    "pipeline": name,
                    "status": status,
                    "execution": payload,
                }
            )
        except Exception as exc:
            tracked["error"] = str(exc)
            raise
        execution = SpinnakerExecution(
            application=application,
            pipeline=name,
            execution_id=execution_id,
            status=status,
            payload=payload,
        )
        if status != expected_status:
            raise VerificationError(
                f"Spinnaker pipeline {application}/{name} finished as {status}, "
                f"expected {expected_status}:\n{json.dumps(payload, indent=2)}"
            )
        return execution

    def deploy_raw_manifest(
        self,
        repository: TestRepository,
        path: str,
        *,
        application: str,
        pipeline_name: str | None = None,
        timeout: float = 600,
    ) -> SpinnakerExecution:
        revision = _require_revision(repository)
        name = pipeline_name or self.harness.unique_name("raw-manifest")
        artifact = http_file_artifact(
            url=repository.raw_url(path, revision=revision),
            commit=revision,
            name=Path(path).name,
        )
        return self.run(
            raw_manifest_pipeline(
                application=application,
                name=name,
                artifact=artifact,
            ),
            timeout=timeout,
        )

    def deploy_kustomize(
        self,
        repository: TestRepository,
        kustomization_path: str,
        *,
        application: str,
        pipeline_name: str | None = None,
        timeout: float = 600,
    ) -> SpinnakerExecution:
        revision = _require_revision(repository)
        name = pipeline_name or self.harness.unique_name("kustomize")
        artifact = {
            "type": "git/repo",
            "name": repository.remote.name,
            "reference": repository.clone_url,
            "version": revision,
            "artifactAccount": "gitea",
        }
        return self.run(
            kustomize_pipeline(
                application=application,
                name=name,
                repo_artifact=artifact,
                kustomization_path=kustomization_path,
            ),
            timeout=timeout,
        )


@dataclass(frozen=True)
class SpinnakerExecution:
    application: str
    pipeline: str
    execution_id: str
    status: str
    payload: dict[str, Any]

    def stages(
        self,
        *,
        name: str | None = None,
        type: str | None = None,
    ) -> list[dict[str, Any]]:
        stages = list(self.payload.get("stages", []))
        return [
            stage
            for stage in stages
            if (name is None or stage.get("name") == name)
            and (type is None or stage.get("type") == type)
        ]

    def assert_stage(
        self,
        name: str,
        *,
        status: str = "SUCCEEDED",
    ) -> dict[str, Any]:
        matches = self.stages(name=name)
        if len(matches) != 1:
            visible = [stage.get("name") for stage in self.payload.get("stages", [])]
            raise VerificationError(
                f"Spinnaker stage {name!r} matched {len(matches)} stages; visible: {visible}"
            )
        stage = matches[0]
        if stage.get("status") != status:
            raise VerificationError(
                f"Spinnaker stage {name!r} finished as {stage.get('status')!r}, "
                f"expected {status!r}:\n{json.dumps(stage, indent=2)}"
            )
        return stage


@dataclass(frozen=True)
class TrafficWeight:
    destination: str
    weight: int


@dataclass(frozen=True)
class RolloutSnapshot:
    namespace: str
    name: str
    phase: str | None
    paused: bool
    stable_hash: str | None
    current_hash: str | None
    annotations: Mapping[str, str]
    template_annotations: Mapping[str, str]
    replica_sets: tuple[ReplicaSetState, ...]
    traffic: tuple[TrafficWeight, ...]


class RolloutHandle:
    def __init__(self, kubectl: Kubectl, *, namespace: str, name: str) -> None:
        self.kubectl = kubectl
        self.namespace = namespace
        self.name = name
        self.probe = RolloutProbe(kubectl, namespace, name)

    def snapshot(self) -> RolloutSnapshot:
        raw = self.probe.get()
        status = raw.get("status", {})
        return RolloutSnapshot(
            namespace=self.namespace,
            name=self.name,
            phase=status.get("phase"),
            paused=bool(status.get("pauseConditions")),
            stable_hash=status.get("stableRS"),
            current_hash=status.get("currentPodHash"),
            annotations=_string_mapping(raw.get("metadata", {}).get("annotations")),
            template_annotations=_string_mapping(
                raw.get("spec", {})
                .get("template", {})
                .get("metadata", {})
                .get("annotations")
            ),
            replica_sets=tuple(self.probe.replica_sets()),
            traffic=self._traffic(raw),
        )

    def update_template_annotations(self, annotations: dict[str, str]) -> None:
        """Start a new revision without exposing kubectl patch mechanics to the test."""

        patch = {"spec": {"template": {"metadata": {"annotations": annotations}}}}
        self.kubectl.runner.run(
            self.kubectl.command(
                "patch",
                "rollout",
                self.name,
                "-n",
                self.namespace,
                "--type=merge",
                "-p",
                json.dumps(patch),
            )
        )

    def wait_healthy(self, *, timeout: float = 180) -> RolloutSnapshot:
        return self._wait(
            lambda snapshot: snapshot.phase == "Healthy",
            description="to become Healthy",
            timeout=timeout,
        )

    def wait_for_canary(
        self,
        *,
        stable_sets: int = 1,
        canary_sets: int = 1,
        weights: tuple[int, ...] | None = None,
        require_paused: bool = True,
        timeout: float = 180,
    ) -> RolloutSnapshot:
        def ready(snapshot: RolloutSnapshot) -> bool:
            active = [item for item in snapshot.replica_sets if item.desired > 0]
            counts = {
                role: len([item for item in active if item.role == role])
                for role in ("stable", "canary")
            }
            traffic = tuple(item.weight for item in snapshot.traffic)
            return (
                counts["stable"] == stable_sets
                and counts["canary"] == canary_sets
                and (not require_paused or snapshot.paused)
                and (weights is None or traffic == weights)
            )

        return self._wait(
            ready,
            description=(
                f"to expose {stable_sets} stable and {canary_sets} canary ReplicaSets"
            ),
            timeout=timeout,
        )

    def assert_replica_sets(
        self,
        *,
        stable: int | None = None,
        canary: int | None = None,
        old: int | None = None,
        active_only: bool = True,
    ) -> RolloutSnapshot:
        snapshot = self.snapshot()
        states = [
            item for item in snapshot.replica_sets if not active_only or item.desired > 0
        ]
        expected = {"stable": stable, "canary": canary, "old": old}
        actual = {
            role: len([item for item in states if item.role == role]) for role in expected
        }
        mismatches = [
            f"{role}={actual[role]} (expected {count})"
            for role, count in expected.items()
            if count is not None and actual[role] != count
        ]
        if mismatches:
            self._fail("unexpected ReplicaSets: " + ", ".join(mismatches), snapshot)
        return snapshot

    def assert_traffic_weights(self, *weights: int) -> RolloutSnapshot:
        snapshot = self.snapshot()
        actual = tuple(item.weight for item in snapshot.traffic)
        if actual != weights:
            self._fail(f"traffic weights were {actual}, expected {weights}", snapshot)
        return snapshot

    def assert_scale_down_pending(self, *, count: int = 1) -> RolloutSnapshot:
        snapshot = self.snapshot()
        pending = [
            item
            for item in snapshot.replica_sets
            if item.role == "old" and item.desired > 0 and item.scale_down_deadline is not None
        ]
        if len(pending) != count:
            self._fail(
                f"found {len(pending)} scale-down-pending old ReplicaSets, expected {count}",
                snapshot,
            )
        return snapshot

    def _wait(
        self,
        predicate: Any,
        *,
        description: str,
        timeout: float,
        interval: float = 0.5,
    ) -> RolloutSnapshot:
        deadline = time.monotonic() + timeout
        last: RolloutSnapshot | None = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                last = self.snapshot()
            except Exception as exc:
                last_error = exc
                time.sleep(interval)
                continue
            if predicate(last):
                return last
            time.sleep(interval)
        rendered = self._render(last) if last is not None else "no snapshot available"
        if last_error is not None:
            rendered += f"\nLast inspection error: {type(last_error).__name__}: {last_error}"
        raise ReadinessError(
            f"Rollout {self.namespace}/{self.name} did not {description} in {timeout}s.\n"
            f"Last state:\n{rendered}"
        )

    def _traffic(self, rollout: dict[str, Any]) -> tuple[TrafficWeight, ...]:
        canary = rollout.get("spec", {}).get("strategy", {}).get("canary", {})
        virtual_service = (
            canary.get("trafficRouting", {}).get("istio", {}).get("virtualService", {})
        )
        name = virtual_service.get("name")
        if not name:
            return ()
        routes = set(virtual_service.get("routes") or [])
        payload = self.kubectl.get_json(
            f"virtualservice.networking.istio.io/{name}",
            "-n",
            self.namespace,
        )
        weights: list[TrafficWeight] = []
        for route in payload.get("spec", {}).get("http", []):
            if routes and route.get("name") not in routes:
                continue
            for target in route.get("route", []):
                destination = target.get("destination", {}).get("host", "unknown")
                weights.append(
                    TrafficWeight(
                        destination=str(destination),
                        weight=int(target.get("weight", 0)),
                    )
                )
        return tuple(weights)

    def _fail(self, message: str, snapshot: RolloutSnapshot) -> None:
        raise VerificationError(
            f"Rollout {self.namespace}/{self.name}: {message}.\n"
            f"Observed state:\n{self._render(snapshot)}"
        )

    @staticmethod
    def _render(snapshot: RolloutSnapshot) -> str:
        return json.dumps(asdict(snapshot), indent=2, default=str)


class AdvancedAccess:
    """Explicit escape hatch for tests that genuinely need infrastructure details."""

    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness

    @property
    def profile(self) -> HarnessProfile:
        return self.harness.profile

    @property
    def environment(self) -> HarnessEnvironment:
        return self.harness.runtime.environment

    @property
    def kubectl(self) -> Kubectl:
        return self.harness.runtime.environment.kubectl

    @property
    def runner(self) -> CommandRunner:
        return self.harness.runtime.environment.runner

    @property
    def gitea(self) -> GiteaClient:
        return self.harness._services.gitea()

    @property
    def wiremock(self) -> WireMockClient:
        return self.harness._services.wiremock()

    @property
    def jenkins(self) -> JenkinsClient:
        return self.harness._services.jenkins()

    @property
    def spinnaker(self) -> SpinnakerClient:
        return self.harness._services.spinnaker()

    def port_forward(
        self,
        namespace: str,
        resource: str,
        remote_port: int,
    ) -> PortForward:
        return self.harness._services.forward(
            f"advanced:{namespace}:{resource}:{remote_port}",
            namespace,
            resource,
            remote_port,
        )


class _ServiceConnections:
    def __init__(self, harness: TestHarness) -> None:
        self.harness = harness
        self._stack = ExitStack()
        self._forwards: dict[str, PortForward] = {}
        self._clients: dict[str, Any] = {}

    def _verify(self) -> ssl.SSLContext:
        trust = self.harness.runtime.profile.trust
        return ssl_context(
            trust.ca_certificate,
            insecure_skip_tls_verify=trust.insecure_skip_tls_verify,
        )

    def has(self, name: str) -> bool:
        return name in self._clients

    def forward(
        self,
        key: str,
        namespace: str,
        resource: str,
        remote_port: int,
    ) -> PortForward:
        if key not in self._forwards:
            forward = self.harness.runtime.environment.kubectl.port_forward(
                namespace,
                resource,
                remote_port,
            )
            self._forwards[key] = self._stack.enter_context(forward)
        return self._forwards[key]

    def wiremock(self) -> WireMockClient:
        component = self.harness.runtime.environment.components.require(
            "wiremock",
            WireMockComponent,
        )
        if "wiremock" not in self._clients:
            service = component.service
            url = self.forward(
                "wiremock",
                service.namespace,
                service.resource,
                service.port,
            ).url
            self._clients["wiremock"] = self._stack.enter_context(
                WireMockClient(url, verify=self._verify())
            )
        return self._clients["wiremock"]

    def gitea(self) -> GiteaClient:
        component = self.harness.runtime.environment.components.require(
            "gitea",
            GiteaComponent,
        )
        if "gitea" not in self._clients:
            service = component.service
            url = self.forward(
                "gitea",
                service.namespace,
                service.resource,
                service.port,
            ).url
            self._clients["gitea"] = self._stack.enter_context(
                GiteaClient(url, verify=self._verify())
            )
        return self._clients["gitea"]

    def jenkins(self) -> JenkinsClient:
        component = self.harness.runtime.environment.components.require(
            "jenkins",
            JenkinsComponent,
        )
        if "jenkins" not in self._clients:
            service = component.service
            url = self.forward(
                "jenkins",
                service.namespace,
                service.resource,
                service.port,
            ).url
            self._clients["jenkins"] = self._stack.enter_context(
                JenkinsClient(url, verify=self._verify())
            )
        return self._clients["jenkins"]

    def spinnaker(self) -> SpinnakerClient:
        component = self.harness.runtime.environment.components.require(
            "spinnaker",
            SpinnakerComponent,
        )
        if "spinnaker" not in self._clients:
            service = component.service
            url = self.forward(
                "spinnaker",
                service.namespace,
                service.resource,
                service.port,
            ).url
            self._clients["spinnaker"] = self._stack.enter_context(
                SpinnakerClient(url, verify=self._verify())
            )
        return self._clients["spinnaker"]

    def application(
        self,
        name: str,
        namespace: str,
        port: int,
    ) -> ApplicationService:
        key = f"application:{namespace}:{name}:{port}"
        if key not in self._clients:
            url = self.forward(
                key,
                namespace,
                f"service/{name}",
                port,
            ).url
            self._clients[key] = self._stack.enter_context(
                httpx.Client(base_url=url, timeout=30, verify=self._verify())
            )
        return ApplicationService(
            name=name,
            namespace=namespace,
            port=port,
            url=str(self._clients[key].base_url).rstrip("/"),
            _client=self._clients[key],
        )

    def close(self) -> None:
        self._stack.close()


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate == resolved_root or resolved_root not in candidate.parents:
        raise HarnessError(f"repository path escapes its worktree: {relative}")
    return candidate


def _require_revision(repository: TestRepository) -> str:
    if repository.revision is None:
        raise HarnessError("Spinnaker requires a committed repository revision")
    return repository.revision
