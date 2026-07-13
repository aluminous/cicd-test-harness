# CI/CD test harness

[English](https://github.com/aluminous/cicd-test-harness/blob/main/README.md) |
[한국어](https://github.com/aluminous/cicd-test-harness/blob/main/README.ko.md)

> **Alpha preview:** the tested workflows are functional, but public APIs and profile
> structure may change before `1.0`. The supplied infrastructure is intentionally
> disposable and is not suitable for production or shared clusters.

This repository provides an ephemeral CI/CD test environment built from
Kind, Argo Rollouts, Istio, Jenkins, Gitea, WireMock, and a minimal Spinnaker service
slice.

The harness follows a Testcontainers-style lifecycle while using the native component
interfaces:

- `kind` owns the Kubernetes cluster.
- `kubectl` and Helm own in-cluster resources.
- Python owns dependency ordering, readiness, diagnostics, and teardown.
- pytest tests consume typed fixtures rather than shell output.

## Requirements

- Python 3.11 or newer;
- Docker or Podman, with at least 8 GiB available to the complete stack;
- `kubectl`, Helm, and Git on `PATH`;
- rootful Podman or Docker/DinD for the Kubernetes 1.21 legacy profile.

The profile-specific Kind binary is downloaded on first use into `.tools/bin` and verified
against a platform-specific SHA-256 checksum. Kubernetes node images and component images
remain digest- or version-pinned by the selected profile.

## Installation

For source development:

```bash
git clone https://github.com/aluminous/cicd-test-harness.git
cd cicd-test-harness
uv sync --extra dev
uv run pytest
```

The built wheel is self-contained: it includes the built-in profiles, manifests, Helm
charts, image recipes, and repository fixtures. After the preview is published it can be
installed as a normal development dependency:

```bash
uv add --dev cicd-test-harness
uv run cicd-harness profile show modern
```

A project-owned `profiles/<name>.yaml` takes precedence over the bundled profile with the
same name, so teams can pin private registries or alternate component images without
modifying the installed package.

## Built-in profiles

| Profile | Runtime | Kubernetes | Argo Rollouts | Istio | PoC status |
|---|---|---:|---:|---:|---|
| `modern` | Podman or Docker | 1.31.14 | 1.8.3 | 1.25.5 | Validated on arm64 Podman |
| `legacy` | Rootful Podman or Docker/DinD | 1.21.14 | 1.4.1 | 1.10.6 | Validated on arm64 rootful Podman with documented Istio shim |

All image and tool references are intended to be pinned. Runtime-downloaded release
assets are cached under `.tools/`, which is deliberately not committed.

## Quick start

```bash
uv run cicd-harness profile show modern
uv run cicd-harness doctor modern --prepare
uv run cicd-harness stack-up modern
uv run cicd-harness endpoints modern
uv run cicd-harness expose modern gitea jenkins
uv run cicd-harness stack-down modern
uv run pytest
```

`stack-up` creates the Kind cluster, installs Rollouts, Istio plus one ingress gateway,
Gitea, WireMock, Jenkins, and the thin Spinnaker slice, and bootstraps Gitea without UI
interaction. Use `--without-spinnaker` or `--without-jenkins` for smaller tests.
Use `--components argo-rollouts,istio,wiremock` to select an exact component graph.
`endpoints` lists host-accessible UIs and APIs without starting anything. `expose`
attaches loopback-only `kubectl port-forward` processes to an existing stack and remains
in the foreground until Ctrl-C. This works through rootful Podman without Podman VM port
mapping and does not depend on Istio.

`doctor` checks the selected container runtime connection plus `kubectl`, Helm, Git, and
the profile memory budget. With `--prepare`, it also downloads and checksum-verifies the
pinned Kind binary without creating a cluster.

Infrastructure PoCs are opt-in because they create containers:

```bash
CICD_RUN_POC=1 uv run pytest -m poc -s
```

Application tests should request the `harness` pytest fixture instead of assembling
clusters and clients themselves. It provides high-level Git, outbound mock, Jenkins,
Spinnaker, and Rollout operations; owns an isolated namespace; verifies mocks; and
collects cluster/component diagnostics automatically on failure. See
[`docs/testing-api.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/testing-api.md)
for the author-facing API and escape hatch.
WireMock-backed reverse proxies can pass calls through to real services, record normalized
request evidence, and replace selected responses without coupling the harness to Istio.
Writable repositories can be seeded recursively from named fixture directories, and
`harness.jenkins.create_library()` both creates a unique library repository and registers
it dynamically—no manifest or UI change is needed for additional shared libraries.

Every configured image can be redirected through a private registry without editing
manifests. Registry credentials are named by environment variable, materialized in
private temporary Docker/Podman auth files, installed as Kubernetes pull secrets, and
removed at teardown. Application manifests applied through `harness.resources` inherit
the same rewrite and pull-secret behavior. See
[`docs/testing-api.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/testing-api.md#private-registries)
for configuration.

A single optional `trust.ca_certificate` profile path adds a PEM corporate root without
discarding public roots. The harness propagates it to host download/Git clients, a
rootful macOS Podman VM, Kind/containerd, managed workload TLS variables, Jenkins, and
the WireMock/Spinnaker Java trust stores. This supports private registries, Nexus, and
HTTPS proxy origins without disabling certificate verification. Docker/DinD still needs
its daemon trust configured before it can pull the Kind node image; see the
[`air-gapped operation guide`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/airgapped.md#private-ca-certificates).

For an isolated test network where that root still cannot be used, set
`trust.insecure_skip_tls_verify: true`. This emergency global fallback reaches
harness-owned clients, subprocesses, managed workloads, Podman registry operations,
Kind registries, WireMock proxy targets, and Gitea webhooks where a native bypass exists.
It is unsafe and intentionally does not weaken Kubernetes control-plane TLS. Docker
daemon pulls and application libraries that ignore the harness marker still require
native configuration; the
[`air-gap guide`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/airgapped.md#emergency-tls-verification-fallback)
has the exact boundary.

For disconnected CI, enforced air-gap profiles validate every controlled bootstrap,
image-build, and runtime destination before Kind starts, then reject public images added
through test-authored manifests. Example modern/legacy profiles cover an internal OCI
registry, Nexus-hosted Kind downloads, Jenkins plugin mirrors, and the legacy Istio
source/Go build. Spinnaker's S3-compatible Front50 storage is already the in-cluster
MinIO service and does not contact AWS. See the
[`air-gapped operation guide`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/airgapped.md).

Lifecycle commands emit timed component and subprocess progress at `INFO`. Add
`--log-level DEBUG` for captured command output or put
`--log-file artifacts/stack-up.log` before the subcommand to retain a redacted CI log.

Run one version profile explicitly with `CICD_PROFILE=modern` or
`CICD_PROFILE=legacy`. Kubernetes 1.21 cannot start under the current rootless Podman VM,
so the legacy profile requires a rootful Podman connection or Docker/DinD. On Apple
Silicon it builds an exact-source arm64 Istio 1.10.6 pilot shim automatically; see the
[`ARM64 compatibility guide`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/arm64-compatibility.md)
for the reproducible Docker/Podman build command, GHCR publication path, and gateway
fidelity boundary.

On macOS, the current PoC supports the existing Podman machine. CI will use a privileged
DinD harness container so all child processes share one 8 GiB cgroup.

## Mock API example

```python
scanner = mocks.service("scanner")
expectation = scanner.expect(
    method="POST",
    path="/v1/scans",
    response={"status": 202, "json": {"scanId": "scan-123"}},
    json_paths={"$.repository": "payments"},
    times=1,
)

# exercise the backend or Jenkins job

expectation.verify()
```

WireMock returns deterministic HTTP responses and keeps an in-memory request journal.
The harness wrapper resets mappings between tests, verifies call counts, reports
unmatched outbound calls, and exposes each expectation as a small Python object.

## Validated workflows

- Gitea repository creation, authenticated commit/push, and exact-commit raw fetch.
- WireMock host/path/header/JSONPath matching, deterministic responses, exact call-count
  verification, and unmatched-request reporting.
- Argo Rollouts canaries on Kubernetes 1.31/1.21 with Istio 1.25/1.10 50/50
  VirtualService weights, stable/canary ReplicaSet inspection, and delayed
  old-ReplicaSet scale-down evidence.
- Spinnaker 1.25.4 raw manifest pipeline: Gate -> Orca -> Clouddriver -> exact Gitea
  commit -> Kubernetes.
- Spinnaker Kustomize pipeline: Gate -> Orca -> Rosco -> embedded artifact ->
  Clouddriver -> Kubernetes.
- Jenkins 2.426.1 REST trigger -> shell job -> authenticated Gitea push -> verified
  WireMock callback.
- Jenkins multibranch job creation and configuration inspection -> Gitea branch
  discovery -> repository `Jenkinsfile` -> external `@Library('example')` checkout and
  execution.
- Exact component subsets: Jenkins/Gitea/WireMock callback flow without unrelated
  controllers, and Rollouts/Istio/Gitea/Spinnaker deployment into a fixture-owned
  namespace.
- Host endpoint discovery and loopback exposure through rootful Podman, plus retained
  failed-test namespaces that can be reattached and explicitly deleted from the CLI.

The minimal Spinnaker runtime is Gate, Orca, Clouddriver, Front50, Rosco, Redis, and
MinIO. Deck, Echo, Igor, Fiat, Kayenta, and Halyard are omitted. Before adding Pipeline
plugins, the complete modern node used 5.6-5.8 GiB during Spinnaker canaries and 6.01
GiB with Jenkins also running. With Pipeline Jenkins, all seven Spinnaker services, and a
completed multibranch/shared-library build, the node used 5.84 GB while services warmed
and 5.68 GB after the build, with zero pod restarts. The complete legacy node previously
used 6.05 GiB after its full raw and Kustomize pipeline run.

Spinnaker reports deploy-stage success independently of Argo Rollout health, so tests
must assert both the pipeline execution and `RolloutProbe.wait_healthy()`. See
[`docs/engineering-notes.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/engineering-notes.md)
for the compatibility traps,
memory tuning history, and refactoring guidance discovered during the PoCs.

The representative application-test coverage and known v1 DX boundaries are recorded
in [`docs/dx-coverage.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/dx-coverage.md).
The post-PoC structure, completed
cleanup, and next refactoring thresholds are in
[`docs/architecture-review.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/architecture-review.md).
The intentionally small
component extension contract is documented in
[`docs/components.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/components.md).
ARM64 native, emulated, and compatibility-image boundaries are summarized in
[`docs/arm64-compatibility.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/arm64-compatibility.md).

## Contributing and licensing

Contributions are welcome; see
[`CONTRIBUTING.md`](https://github.com/aluminous/cicd-test-harness/blob/main/CONTRIBUTING.md)
and [`SECURITY.md`](https://github.com/aluminous/cicd-test-harness/blob/main/SECURITY.md)
before opening a change or security report. The harness is available under the
[`MIT License`](https://github.com/aluminous/cicd-test-harness/blob/main/LICENSE).
Vendored upstream material is documented in
[`THIRD_PARTY_NOTICES.md`](https://github.com/aluminous/cicd-test-harness/blob/main/THIRD_PARTY_NOTICES.md).
Maintainers can use the
[`OSS preview release checklist`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/release-checklist.md)
when publishing an artifact.
