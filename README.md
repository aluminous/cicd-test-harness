# CI/CD test harness

This repository contains executable PoCs for an ephemeral CI/CD environment built from
Kind, Argo Rollouts, Istio, Jenkins, Gitea, WireMock, and a minimal Spinnaker service
slice.

The harness follows a Testcontainers-style lifecycle while using the native component
interfaces:

- `kind` owns the Kubernetes cluster.
- `kubectl` and Helm own in-cluster resources.
- Python owns dependency ordering, readiness, diagnostics, and teardown.
- pytest tests consume typed fixtures rather than shell output.

## Initial profiles

| Profile | Runtime | Kubernetes | Argo Rollouts | Istio | PoC status |
|---|---|---:|---:|---:|---|
| `modern` | Podman or Docker | 1.31.14 | 1.8.3 | 1.25.5 | Validated on arm64 Podman |
| `legacy` | Rootful Podman or Docker/DinD | 1.21.14 | 1.4.1 | 1.10.6 | Validated on arm64 rootful Podman with documented Istio shim |

All image and tool references are intended to be pinned. Runtime-downloaded release
assets are cached under `.tools/`, which is deliberately not committed.

## Local commands

```bash
uv sync --extra dev
uv run cicd-harness profile show modern
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

Infrastructure PoCs are opt-in because they create containers:

```bash
CICD_RUN_POC=1 uv run pytest -m poc -s
```

Application tests should request the `harness` pytest fixture instead of assembling
clusters and clients themselves. It provides high-level Git, outbound mock, Jenkins,
Spinnaker, and Rollout operations; owns an isolated namespace; verifies mocks; and
collects cluster/component diagnostics automatically on failure. See
[`docs/testing-api.md`](docs/testing-api.md) for the author-facing API and escape hatch.
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
[`docs/testing-api.md`](docs/testing-api.md#private-registries) for configuration.

Run one version profile explicitly with `CICD_PROFILE=modern` or
`CICD_PROFILE=legacy`. Kubernetes 1.21 cannot start under the current rootless Podman VM,
so the legacy profile requires a rootful Podman connection or Docker/DinD. On Apple
Silicon it builds an exact-source arm64 Istio 1.10.6 pilot shim automatically; see the
engineering notes for its gateway fidelity boundary.

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

## Validated PoCs

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
[`docs/engineering-notes.md`](docs/engineering-notes.md) for the compatibility traps,
memory tuning history, and refactoring guidance discovered during the PoCs.

The representative application-test coverage and known v1 DX boundaries are recorded
in [`docs/dx-coverage.md`](docs/dx-coverage.md). The post-PoC structure, completed
cleanup, and next refactoring thresholds are in
[`docs/architecture-review.md`](docs/architecture-review.md). The intentionally small
component extension contract is documented in
[`docs/components.md`](docs/components.md).
