# Application-focused pytest API

Tests request one fixture, `harness`. The fixture owns cluster startup, readiness,
per-test isolation, service connections, mock verification, failure evidence, and final
teardown. A test author should not create Kind clusters, invoke `kubectl`, manage
port-forwards, or poll infrastructure APIs.

```python
from cicd_harness.testing import TestHarness


def test_payments_release(harness: TestHarness) -> None:
    callback = harness.mocks.service("backend-callback")
    callback.expect(
        method="POST",
        path="/build-complete",
        json_paths={"$.revision": "release-42"},
        response={"status": 202, "json": {"accepted": True}},
    )

    repository = harness.git.create_repository(
        files={"deploy/rollout.yaml": ROLLOUT_YAML},
    )
    harness.jenkins.run(
        "harness-poc",
        parameters={
            "REPO_URL": repository.clone_url,
            "REVISION": "release-42",
            "CALLBACK_URL": f"{callback.url}/build-complete",
        },
    )
    repository.refresh()  # Jenkins pushed; pin the pipeline to the new exact SHA.
    harness.spinnaker.deploy_raw_manifest(
        repository,
        "deploy/rollout.yaml",
        application="payments",
    )

    rollout = harness.rollout("payments", namespace="payments")
    rollout.wait_for_canary(weights=(50, 50))
    rollout.assert_replica_sets(stable=1, canary=1)
    rollout.wait_healthy()
    rollout.assert_scale_down_pending(count=1)
```

Registered mock expectations are verified automatically after a passing test. An
unexpected outbound request also fails the test, so authors do not need a final
`mocks.verify()` call.

## High-level primitives

| Interface | Purpose |
|---|---|
| `harness.namespace` | Unique namespace owned and deleted for the current test |
| `harness.unique_name(prefix)` | Collision-free Kubernetes/Git/pipeline name |
| `harness.service(name, port=...)` | Managed HTTP client for the frontend/backend under test |
| `harness.wait_until(...)` | Poll asynchronous application state with last-state errors |
| `harness.git.create_repository(...)` | Create, seed, commit, and push writable Git content |
| `harness.git.create_repository_from(...)` | Seed a repository recursively from a fixture tree |
| `harness.mocks.service(name)` | Create a WireMock-backed cluster DNS endpoint and expectations |
| `harness.proxies.service(name, target=...)` | Front a real HTTP service with pass-through and intercepts |
| `harness.jenkins.run(...)` | Trigger a build, wait, assert success, and retain its console |
| `harness.jenkins.list_jobs()` | Recursively list folders, multibranch projects, and branch jobs |
| `harness.jenkins.inspect_job(name)` | Read and parse the job's saved `config.xml` |
| `harness.jenkins.create_multibranch_job(...)` | Create and scan a Git-backed Jenkinsfile job |
| `harness.jenkins.run_branch(...)` | Execute a discovered branch and assert build success |
| `harness.jenkins.wait_for_build(...)` | Observe a build initiated through the application |
| `harness.jenkins.create_library(...)` | Seed and register a writable external shared library |
| `harness.jenkins.list_libraries()` | Inspect configured global Pipeline libraries |
| `harness.spinnaker.deploy_raw_manifest(...)` | Save and run an exact-commit raw pipeline |
| `harness.spinnaker.deploy_kustomize(...)` | Save and run an exact-commit Rosco pipeline |
| `harness.spinnaker.wait_for_execution(...)` | Observe a pipeline initiated through the application |
| `harness.rollout(name)` | Wait for and assert metadata, canary, traffic, health, and ReplicaSet state |
| `harness.resources` | Apply or inspect application manifests in the isolated namespace |
| `harness.images` | Resolve effective images or prepare another pull-secret namespace |
| `harness.host.list()` | Discover component UIs, APIs, traffic endpoints, and auth hints |
| `harness.host.expose(name)` | Open a managed host-loopback endpoint and return its URL |
| `harness.capture_diagnostics(label)` | Take a diagnostic snapshot during a passing test |

`TestRepository.update()` writes another commit and pushes it. Artifact URLs always use
an exact commit SHA. `MockEndpoint.url` is directly usable by pods and Jenkins jobs; the
fixture creates the matching Kubernetes Service alias automatically.

## Testing through the application

The harness can connect to any Service owned by the system under test. Its port-forward
and HTTP client are reused and closed with the test:

```python
backend = harness.service("release-api", port=8080)
response = backend.post("/releases", json={"repository": repository.clone_url})
response.raise_for_status()

release = harness.wait_until(
    lambda: backend.get(f"/releases/{response.json()['id']}").json(),
    predicate=lambda item: item["state"] == "ready",
    description="release to become ready",
    timeout=600,
)
```

When the application, rather than the test, triggers Jenkins or Spinnaker, take a
baseline first so an old execution cannot satisfy the assertion:

```python
jenkins_after = harness.jenkins.latest_build_number("payments/main")
spinnaker_before = harness.spinnaker.execution_ids("payments", pipeline="deploy")

backend.post("/releases", json={"service": "payments"}).raise_for_status()

build = harness.jenkins.wait_for_build("payments/main", after=jenkins_after)
execution = harness.spinnaker.wait_for_execution(
    "payments",
    pipeline="deploy",
    excluding=spinnaker_before,
)
execution.assert_stage("Deploy committed manifest")
```

`build.artifacts()` lists archived Jenkins artifacts and `build.artifact(path)` returns
their bytes. For jobs created asynchronously by the application, use
`wait_for_job(name)` before `assert_job(name, ...)`.

Mock expectations also act as callback wait handles. `expectation.wait()` returns the
matching WireMock journal entries, including request bodies, while teardown still
enforces the exact configured call count. `MockEndpoint.requests()` filters the journal
to one logical mocked service.

## Reverse proxies

`harness.proxies` puts the pinned WireMock instance in front of a real HTTP service. A
low-priority mapping passes unmatched requests to the target; test intercepts use a
higher priority and can return errors, latency, headers, or deterministic bodies.

```python
jenkins = harness.proxies.service(
    "jenkins-api",
    target="http://jenkins.harness-system.svc.cluster.local:8080",
)

# Configure the backend under test with JENKINS_URL=jenkins.url.
backend.post("/releases", json={"service": "payments"}).raise_for_status()

records = jenkins.assert_called(
    method="POST",
    path="/job/payments/job/main/build",
    times=1,
)
assert records[0].proxied
assert records[0].response_status == 201
```

An intercept can be installed before the test action or midway through a scenario. Its
call count starts when it is registered, so earlier pass-through calls to the same route
do not satisfy it:

```python
failure = jenkins.intercept(
    method="POST",
    path="/job/payments/job/main/build",
    response={"status": 503, "json": {"message": "maintenance"}},
    times=1,
)

backend.post("/releases", json={"service": "payments"})
failure.verify()
```

The returned URL uses a test-owned Kubernetes `ExternalName` Service and is deleted with
the test namespace. To make proxying transparent to the application, give the real
service an origin name (for example `jenkins-origin`) and configure the application with
the proxy's normal logical name. The harness's inspection client can continue using the
origin, so an injected outage does not disable diagnostics.

`ProxyEndpoint.records()` returns normalized `RequestRecord` values with method, URL,
headers, body, response status, duration, mapping name, and whether the response was
proxied. Raw events remain available for unusual assertions. Authorization, cookies,
proxy authorization, API keys, and set-cookie headers are removed from diagnostic
snapshots. The profile controls WireMock's request-journal length, logged response-body
limit, and upstream proxy timeout:

```yaml
infra:
  wiremock:
    image: wiremock/wiremock:3.13.1
    max_request_journal_entries: 1000
    logged_response_body_size_limit: 65536
    proxy_timeout_milliseconds: 30000
```

This is an HTTP reverse proxy, not network-level redirection. Traffic is observed only
when the application calls the proxy URL. HTTPS upstream targets work because WireMock
creates its own HTTPS connection; preserving an HTTPS URL from the application to
WireMock would require a test CA and is outside v1.

## Repository fixtures and templates

Seed a complete repository from any directory under the harness workspace, or use a
short name resolved beneath `fixtures/`:

```python
application = harness.git.create_repository(
    template="jenkins/application",
    files={"environment/test.yaml": "replicas: 2\n"},  # optional overlay
)

manifests = harness.git.create_repository_from(
    Path("fixtures/spinnaker-repo"),
)
```

The directory is copied recursively before the initial commit. Binary files are copied
unchanged, executable bits are preserved, dotfiles are supported, and the `files` map is
applied last as an overlay. Git metadata, symlinks, and paths escaping the worktree are
rejected.

Files ending in `.tmpl` are explicitly rendered with Python `$name` placeholders and
written without the suffix. Other files—including Jenkinsfiles and YAML—are always
literal:

```python
repository = harness.git.create_repository(
    template="repository-templates/service",
    variables={"application": "payments", "namespace": harness.namespace},
)
```

For later commits, use `repository.update_from(path, message=..., variables=...)`.
`repository.update()`, `create_branch()`, `refresh()`, and `read()` cover smaller changes,
branch fixtures, externally pushed revisions, and exact-revision assertions.

## Jenkins jobs and Jenkinsfiles

The common shared-library path is one call:

```python
library = harness.jenkins.create_library(
    "example",
    template="jenkins/library",
    default_version="main",
    implicit=False,
)

harness.jenkins.assert_library(
    "example",
    repository_url=library.repository.clone_url,
    default_version="main",
    implicit=False,
)
```

This creates a uniquely named writable Gitea repository from the fixture tree and
registers it in Jenkins. For an existing repository, use
`configure_library(name, repository, ...)`. Multiple libraries may be configured with
independent default versions, implicit loading, version overrides, change-set inclusion,
and optional credentials IDs. `list_libraries()` and `inspect_library()` provide
configuration-level assertions. Libraries owned by a test are removed during teardown.

Job inspection is independent of build execution. Tests can assert that another service
created the expected Jenkins configuration without triggering it:

```python
jobs = harness.jenkins.list_jobs()
assert "payments/main" in {job.full_name for job in jobs}

config = harness.jenkins.assert_job(
    "payments",
    kind_contains="WorkflowMultiBranchProject",
    repository_url=repository.clone_url,
    script_path="Jenkinsfile",
)
assert "BranchDiscoveryTrait" in config.xml
```

For tests that own job creation, the high-level API creates a native multibranch project,
asks Jenkins to scan Git, and waits for the branch job:

```python
job = harness.jenkins.create_multibranch_job(repository, name="payments")
harness.jenkins.wait_for_branch("payments", "main")
build = harness.jenkins.run_branch("payments", "main")
assert "JENKINSFILE_EXECUTED" in build.console
```

The acceptance fixture at `fixtures/jenkins/application/Jenkinsfile` contains
`@Library('example')`. The test creates and configures that library entirely through the
high-level API. Jenkins resolves it from the resulting separate writable Gitea repository
and executes `vars/exampleBuild.groovy`; neither repository is mounted into Jenkins. The
PoC pushes both `main` and `release` and asserts that Jenkins creates both branch jobs.
This exercises real Git SCM, branch indexing, Pipeline CPS, and shared library loading.

## Failure diagnostics

Diagnostics are collected while the cluster and port-forwards are still alive. A failed
test produces a directory under
`artifacts/cicd-harness/<session-cluster>/<test>-failure/` containing:

- the configured component graph and each component's lifecycle state;
- nodes, pods, workloads, events, and pod descriptions;
- current and previous logs for every container, capped per file;
- all Rollouts, ReplicaSets, and VirtualServices;
- WireMock mappings, expectations, request journal, and unmatched requests;
- the Jenkins job tree, tracked job XML, builds, and consoles;
- tracked Spinnaker executions and stage payloads; and
- test namespace, repository revisions, profile, and cluster context.

Collection is best effort: an unavailable component creates an error artifact instead
of hiding the original test failure. Disposable credentials and URL user-info are
redacted. Diagnostics are also captured if environment startup, mock verification, or
environment teardown fails.

The diagnostic bundle also includes `host-endpoints.json`. It records every endpoint in
the selected component graph and the ephemeral URL of any endpoint exposed during the
session. Passwords are not included.

## Lifecycle and options

The pytest plugin is registered when `cicd-test-harness` is installed. Infrastructure is
created lazily only when a test requests `harness`. One uniquely named cluster is shared
for that pytest session and is deleted even after test failures. Each test gets a fresh
namespace, mock journal, Git names, and pipeline names.

```bash
uv run pytest tests/application \
  --cicd-profile modern \
  --cicd-artifacts artifacts/integration
```

Useful options include:

- `--cicd-profile modern|legacy|path/to/profile.yaml`
- `--cicd-components wiremock,gitea,jenkins` to run an exact configured subset
- `--cicd-without-spinnaker`
- `--cicd-without-jenkins`
- `--cicd-startup-timeout 900`
- `--cicd-keep` to retain the unique cluster for interactive debugging
- `--cicd-cluster-name NAME` for an explicitly named session cluster
- `--expose-endpoints default|all|gitea,jenkins,...` for local host access
- `--preserve-environment-on-failure` to retain the failed namespace and cluster

`CICD_COMPONENTS` provides the same comma-separated selection in CI. Component
dependencies are validated rather than implicitly included, so a selection such as
`spinnaker` must also contain its configured `gitea` dependency. See
[`components.md`](components.md) for custom components and profile composition.

`--preserve-environment-on-failure` deliberately has no `cicd` prefix: it describes the
developer outcome rather than an implementation detail. Use it with `--maxfail=1`, since
later tests can reset session-global WireMock or Jenkins state. At session exit, pytest
closes its port-forward processes but preserves Kubernetes and component state and prints
an attach command such as:

```bash
uv run cicd-harness expose modern \
  --context kind-cicd-poc-modern-pytest-12345-abcdef \
  --components wiremock,gitea,jenkins

uv run cicd-harness stack-down modern \
  --cluster-name cicd-poc-modern-pytest-12345-abcdef
```

The programmatic catalog uses the same manager:

```python
jenkins = harness.host.expose("jenkins")
print(jenkins.url)

for endpoint in harness.host.list():
    print(endpoint.name, endpoint.kind, endpoint.description)
```

For interactive pytest sessions, exposed endpoints remain available for the whole test
session rather than being recreated for each function.

Run profiles sequentially under the 8 GiB budget. Separate pytest processes receive
separate cluster names, but tests inside one session should not use thread-level
parallelism because WireMock's journal is reset per test.

## Private registries

Image rewriting is profile-wide and uses the longest matching canonical prefix.
Unqualified Docker images are canonicalized first, so `redis:7` is treated as
`docker.io/library/redis:7`.

```yaml
registry:
  rewrites:
    docker.io: registry.example.internal/cache/docker.io
    quay.io: registry.example.internal/cache/quay.io
    registry.k8s.io: registry.example.internal/cache/registry.k8s.io
    us-docker.pkg.dev: registry.example.internal/cache/us-docker.pkg.dev
    docker.gitea.com: registry.example.internal/cache/docker.gitea.com
    localhost: registry.example.internal/harness-built
  pull_secret_name: harness-registry
  credentials:
    - server: registry.example.internal
      username_env: HARNESS_REGISTRY_USERNAME
      password_env: HARNESS_REGISTRY_PASSWORD
```

This covers the Kind node, Rollouts manifests, Istio pilot/proxy/gateway chart values,
Gitea, WireMock, Jenkins base and built images, all Spinnaker services, the legacy pilot
builder/stub, and manifests applied with `harness.resources`. Docker receives a private
`DOCKER_CONFIG`; Podman receives a private `REGISTRY_AUTH_FILE`. Only environment-variable
names live in the profile. Secret values are added to diagnostic redaction, and auth
files are mode `0600` beneath a mode `0700` directory that is deleted at teardown.

The per-test namespace and infrastructure namespaces receive the pull secret. The
default ServiceAccount is wired for manifests deployed directly by Spinnaker, and
manifests rendered by the harness also receive workload-level `imagePullSecrets`, which
covers named ServiceAccounts. For an additional namespace use
`harness.images.ensure_namespace(name)`. `harness.images.resolve(image)` is available
when generating a Git manifest that Spinnaker will later fetch without passing through
`harness.resources`.

v1 assumes the registry certificate is already trusted by the host runtime and Kind
node. An insecure registry or private CA requires runtime/Kind trust configuration and
is not yet modeled by the profile.

## Low-level escape hatch

Tests with a genuine infrastructure-specific assertion can use `harness.advanced`:

```python
def test_controller_annotation(harness: TestHarness) -> None:
    rollout = harness.advanced.kubectl.get_json(
        "rollout/payments", "-n", harness.namespace
    )
    assert rollout["metadata"]["annotations"]["example.com/controller"] == "ready"
```

It exposes the profile, environment, command runner, `Kubectl`, raw Gitea/WireMock/
Jenkins/Spinnaker clients, and managed port-forwards. Prefer the high-level APIs so
tests remain focused on application behavior and receive richer failure messages.
