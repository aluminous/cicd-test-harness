# Engineering notes and compatibility gotchas

This file records operational discoveries from the executable PoCs. Keep it current when
changing component versions or replacing a service; these details are expensive to
rediscover.

## Spinnaker 1.25.4 minimal service boundary

The smallest slice that retained the required fidelity was:

- Gate for the supported API entry point.
- Orca for pipeline/stage execution.
- Clouddriver for Git artifact fetches and Kubernetes deploy operations.
- Front50 for pipeline persistence.
- Rosco for the Kustomize pipeline.
- Redis for Orca and Clouddriver state.
- MinIO for Front50 object storage.

Deck, Echo, Igor, Fiat, Kayenta, and Halyard are unnecessary for manually triggered
manifest pipelines. Front50 0.26.2 cannot use its old Redis DAOs as its active
`StorageService`, so a small S3-compatible MinIO is cheaper and simpler than reviving an
unsupported storage path.

Running Orca by itself is not a useful pipeline replacement. Its deploy stage calls
Clouddriver, pipeline definitions come from Front50, Gate is the stable automation API,
and Kustomize baking calls Rosco. Removing those services would mean reimplementing the
very integration contracts this harness is intended to test.

## Old Clouddriver on new Kubernetes

Clouddriver 7.3.3 bundles kubectl 1.18.10. It can deploy to Kubernetes 1.31 for this PoC;
matching kubectl versions was not necessary. Its default startup cache queries resource
kinds removed from modern Kubernetes, including `podSecurityPolicy` and `podPreset`.
The explicit allowlist below avoids those queries.

An empty `kinds` list means "cache the default set," not "cache nothing." Under repeated
pipelines that made the disposable account enumerate cluster roles, webhooks, CRDs,
events, and other unrelated objects, and Clouddriver was OOM-killed at 1 GiB. Whitelist
`namespace`, `pod`, `replicaSet`, `service`, `Rollout.argoproj.io`, and
`VirtualService.networking.istio.io` for this PoC. In 7.3.3 the list controls both caching
and deploy validation, so every resource kind a pipeline submits must be present.
Clouddriver 7.3.3 rejects an account that sets both `kinds` and `omitKinds`; use the
whitelist alone, which also avoids querying the removed APIs.

The PoC uses cluster-admin for Clouddriver because the disposable account's final
resource inventory is not known yet. Narrow this to namespaced RBAC after recording all
resources used by the backend and pipelines.

## Argo Rollout health is a separate assertion

An unregistered Rollout CRD receives Clouddriver's generic custom-resource stability
handling. A Spinnaker deploy stage can therefore succeed before Argo Rollouts reaches
`Healthy`. Tests must wait for both conditions independently:

1. the Spinnaker execution reaches `SUCCEEDED`; and
2. `RolloutProbe.wait_healthy()` observes Argo's status.

This is useful fidelity for the real architecture, where the backend consumes Rollout
progress events. If Spinnaker itself must block on Rollout health later, add a
Clouddriver custom-resource status mapping and treat it as a separate compatibility PoC.

Argo Rollouts 1.8 writes the old ReplicaSet scale-down time to the unqualified
`scale-down-deadline` annotation. The probe also accepts the older qualified annotation
so one assertion can cover both profiles.

## Pipeline schema traps

Clouddriver 7.3.3's deploy-manifest stage throws a null-pointer error if the pipeline
stage lacks `moniker.app`, even though a hand-authored API payload might appear otherwise
complete. Always populate the moniker.

For a Kustomize pipeline, Rosco returns an `embedded/base64` artifact. The bake stage's
expected artifact and deploy stage must both use the `embedded-artifact` account. A
direct Rosco API call proves the renderer but does not prove Orca's artifact propagation;
the acceptance PoC exercises the full Gate -> Orca -> Rosco -> Clouddriver path.

Use exact Git commit SHAs in both raw HTTP artifacts and `git/repo` artifacts. Branch URLs
make tests race with pushes and weaken the test's audit trail.

Orca exposes a newly triggered execution as `NOT_STARTED` before its first task begins.
That status is transient, not terminal; polling must continue until a success, failure,
stop, cancellation, or skip state is observed.

## Memory and architecture

Spinnaker's Java processes need native headroom in addition to their configured heaps.
Front50's default cache warmers create roughly 200 threads; disabling cache warming and
using two workers per object type materially reduced its thread-stack footprint. Rosco
needs room for the Kustomize child process, and Orca needs transient headroom while
serializing/executing a multi-document deploy.

The validated per-service memory limits are encoded in `manifests/spinnaker-poc.yaml`.
The complete modern node used approximately 5.6-5.8 GiB of an 8 GiB Podman machine under
the raw canary workload, and 6.01 GiB with Jenkins also running. Do not set JVM `-Xmx`
equal to the container limit.

Spinnaker 1.25.4 service images are amd64-only. On an arm64 Mac, Podman plus binfmt/QEMU
can run them, but startup is slow and CPU usage is high. Kind cannot reliably import the
foreign-architecture image using its normal lookup, so the harness exports an OCI/Docker
archive and loads that archive. It deletes the large temporary archive afterward. Use
amd64 CI as the authoritative performance and memory environment.

## Istio and mocks

Install one ClusterIP ingress gateway even when tests do not send ingress traffic. The
Gateway object gives VirtualServices a real reference and allows Argo's Istio traffic
routing reconciliation to behave normally.

WireMock is an HTTP server controlled entirely by an API. The Python wrapper creates
per-service expectations, matches headers and JSONPath values, returns deterministic
responses, verifies call counts, and reports unmatched requests. It also installs
host-matched reverse-proxy fallbacks so a test can observe a real upstream and override
selected calls without relying on Istio. Network-level redirection and client-side HTTPS
interception remain out of scope; configure dependencies to call the test-owned proxy
Service or reserve a logical Service name in front of a separately named origin.

Request-search results in WireMock 3.13 contain request objects rather than the complete
serve event. Use the full journal when normalizing response status, timing, mapping
metadata, and whether a call was proxied. Expectations capture the matching count when
they are registered, which allows a scenario to pass through a route and later inject a
fault on that same route without the earlier request inflating verification.

## Legacy Kind profile

Kind 0.17 refuses to generate a Kubernetes 1.21.14 cluster when its provider is rootless
Podman and explicitly recommends the 0.11 line. Kind 0.11.1 gets through kubeadm
configuration on the current Podman VM, but its Kubernetes 1.21 kubelet never becomes
healthy because rootless cgroup delegation is unavailable. Rootful Podman with Kind
0.17.0 starts the exact Kubernetes 1.21.14 node successfully; Docker/DinD remains the CI
path. The modern profile remains supported on rootless Podman with Kind 0.31.0.

The ARM/rootful lane passed the controller, Gitea/WireMock, Jenkins, raw Spinnaker, and
Rosco/Kustomize acceptance tests. The full raw-plus-Kustomize Spinnaker test took 724
seconds and the full node used 6.05 GiB. The official amd64 Istio pilot/gateway images
must still be exercised in privileged amd64 DinD before claiming that packaging path;
do not silently substitute a newer Kubernetes or Istio version.

On Apple Silicon, the native arm64 Kind 1.21 node can start under rootful Podman, but old
amd64-only Istio 1.10.6 binaries run through nested QEMU user-mode emulation. Pilot's old
Go runtime then crashes with `lfstack.push invalid packing`, a known high-address failure
under qemu-x86_64 on aarch64. Forcing the entire Kind node to its amd64 manifest does not
help: `podman exec` still takes the QEMU path and kubeadm itself hits the same crash.
Either build pilot natively from the exact 1.10.6 source for this development lane or run
the official amd64 image on the authoritative amd64 CI lane.

The ARM development shim builds only `pilot-discovery` using the tag's declared Go 1.16
toolchain, verifies the source archive checksum, embeds the official release tag commit,
and loads the resulting image into Kind. Its version output must report 1.10.6, revision
`fd053c6165d21105d66dac6e3d0649db2dde5b86`, and `Clean`.

Missing-image bootstrap was also exercised from source through controller readiness and
the canary assertion. With the Go builder image already present but empty Istio source
and module caches, it completed in 117 seconds. The downloaded source and Go caches live
under `.tools/`; keep that directory out of lint, packaging, and source control.

Set the 1.10 chart's `pilot.image` to the full local image reference. Do not override the
global hub/tag: those values also rewrite sidecar injection to a nonexistent local
proxyv2 image. The Rollout fixture does not enable workload injection; ingress routing to
its Kubernetes Services does not require destination sidecars, and the assertion is
about VirtualService weight reconciliation.

Istio 1.10's ingress chart excludes arm64, and proxyv2 contains both an amd64 Go
pilot-agent and amd64 Envoy. The ARM development lane therefore deploys a tiny
selector-compatible gateway placeholder: Gateway and VirtualService resources are real,
istiod validation is real, and Argo traffic weights are reconciled, but ingress traffic
is not proxied. The amd64 CI lane must use and validate the official 1.10.6 gateway.

Testing the two official `proxyv2:1.10.6` binaries independently under rootful Podman
showed that `pilot-agent version` crashes with the same Go-runtime `lfstack.push`
failure, while the packaged Envoy starts and reports the expected Istio 1.18.5 build.
Consequently, the official proxyv2 container cannot run intact, but rebuilding only
`pilot-agent` natively and retaining the exact official amd64 Envoy, WASM filters, and
bootstrap assets is a promising intermediate PoC. It must pass a real request-routing
test before replacing the placeholder; starting Envoy with `--version` alone does not
prove xDS connectivity or gateway behavior.

A failed or interrupted Kind create can leave its named node behind, sometimes with a
stale kubeconfig context whose API port no longer answers. `KindCluster.create()` now
requires `/readyz` before reusing a cluster, tries bounded cleanup before recreating an
orphan, and performs best-effort cleanup when creation raises.

## Jenkins boundary

The exact `jenkins/jenkins:2.426.1-lts-jdk17` tag is multi-architecture, so it runs
natively on both arm64 development machines and amd64 CI. Jenkinsfile semantics are now
part of the v1 boundary. The harness derives a local image with the exact Git, Pipeline
CPS, multibranch, stage, durable-task, and Groovy-library plugin dependency closure from
the official Jenkins 2.426.x plugin BOM. `images/jenkins/plugins.txt` pins every direct
and transitive plugin version; do not let the plugin manager select current releases,
because many now require a newer Jenkins core.

The image builder labels the result with a hash of the base image, Containerfile, and
plugin lock file. A changed plugin set therefore rebuilds and reloads the image even when
the human-readable tag is unchanged. This check was added after a missing
`pipeline-stage-step` plugin demonstrated that a locally cached tag could otherwise hide
an updated lock file.

Tests can create a real `WorkflowMultiBranchProject` through Jenkins's XML API, list the
recursive job tree, inspect the persisted `config.xml`, trigger branch indexing, and run
the discovered branch job. The acceptance test proves that Jenkins fetches the
application repository's `Jenkinsfile`, checks out `main`, resolves `@Library('example')`
from a second Gitea repository, and executes the library's global step. The final PoC
also pushes a `release` branch and requires both `main` and `release` branch jobs to
appear after indexing before triggering `main`.

The job is copied into a fresh `JENKINS_HOME` from a ConfigMap and the setup wizard is
disabled with JVM configuration. Shared libraries are not hardcoded into that manifest.
The high-level client uses Jenkins's isolated `/scriptText` automation endpoint to update
`GlobalLibraries`, reads the configuration back, and removes test-owned entries during
teardown; multibranch jobs use the XML API. User values are passed to the Groovy script
as base64-encoded JSON rather than interpolated into source. No UI bootstrap is required.
The original freestyle boundary test remains because it performs a real authenticated
commit/push to Gitea and sends a JSON callback that WireMock verifies by host, path,
JSONPath values, and call count.

The first multibranch run failed at the deliberately omitted `stage` step, while branch
indexing, Jenkinsfile retrieval, and the external library clone had already succeeded.
Automatic failure diagnostics retained that console. Adding the BOM-matched stage plugin
made the focused acceptance pass in 147 seconds including cluster lifecycle.

After removing the fixed `example` bootstrap, the migrated acceptance uses
`create_library("example", template="jenkins/library")` and
`create_repository(template="jenkins/application")`. It passed in 134 seconds, proving
that repository seeding, dynamic library registration, configuration read-back, branch
indexing, and library execution work without manifest-level library state.

The complete modern stack acceptance—with all seven Spinnaker services and the
multibranch/shared-library build—passed in 412 seconds. Whole-node usage sampled 5.84 GB
during service warmup and 5.68 GB immediately after the build out of the 8.29 GB Podman
VM; all pods were ready with zero restarts. This is not a synthetic simultaneous
Spinnaker-pipeline peak, so retain the earlier 6.01 GiB canary observation as the more
conservative planning number.

Jenkins 2.426.1 and compatible historical plugins have published security advisories.
They are retained only because version fidelity is an explicit requirement. Keep this
runtime isolated, disposable, unauthenticated only inside the test cluster, and never
expose its service outside the local/CI container boundary.

Credentials are deliberately fixed disposable-cluster credentials in this PoC. Before
reusing the pattern outside an isolated test cluster, mount a Kubernetes Secret and use
Jenkins credentials binding so passwords cannot appear in job parameters or console
output.

## Pytest lifecycle and diagnostics

The pytest plugin uses a uniquely named cluster per test session instead of reusing the
fixed CLI cluster name. This prevents a test run from deleting a developer-owned cluster
and makes teardown ownership unambiguous. Infrastructure is session-scoped because
controller and Spinnaker startup dominate test time; application namespaces, mock
journals, repositories, and pipeline names are isolated per test.

Failure evidence must be collected before deleting the test namespace, closing lazy
port-forwards, or tearing down Kind. Collection is deliberately best effort: each failed
probe writes its own error evidence and the original application failure remains
primary. Do not turn diagnostics into another readiness dependency. Current/previous
container logs are capped, URL credentials are redacted, and Kubernetes Secrets are not
collected.

Mock expectations are verified automatically only after an otherwise passing test. If
the application assertion already failed, the complete WireMock journal is captured but
verification does not raise a second error that could obscure the first. Because the
WireMock server and request journal are session-global, tests sharing one cluster must
run sequentially; process-level parallelism is safe because each process receives a
different cluster.

Git and service clients are connected lazily so tests that only inspect Rollouts do not
pay for unrelated port-forwards. When Jenkins or another in-cluster service pushes to a
repository, call `TestRepository.refresh()` before constructing a Spinnaker artifact;
otherwise the high-level API correctly continues to reference the prior exact commit.

The modern controllers-plus-infrastructure acceptance run passed two fixture-managed
tests in 134 seconds. It exercised a full 50/50 canary, two isolated namespaces, live
diagnostic collection with zero probe errors, and automatic cluster teardown.

## Registry plumbing

Private-registry support is an end-to-end concern, not a manifest search-and-replace.
The host runtime must authenticate before Kind pulls its node image or the harness builds
Jenkins/legacy-pilot images; Kubernetes must authenticate independently; named
ServiceAccounts do not inherit the default ServiceAccount's `imagePullSecrets`; and
Istio images are assembled by Helm values instead of ordinary manifest fields.

The implementation canonicalizes Docker references, applies the longest registry-prefix
rewrite, gives Docker and Podman isolated auth-file locations, injects workload-level
pull secrets while preserving existing ones, and also patches each namespace's default
ServiceAccount for manifests that Spinnaker applies directly. Rollouts install YAML is
rendered through the same path. Istio uses version-specific pilot/gateway values because
1.10 and 1.25 use different keys. Do not recursively replace every YAML key named
`image`: CRD schemas and application configuration may legitimately contain an unrelated
field with that name. Rewriting is restricted to Kubernetes container lists.

Credentials are resolved only from named environment variables. Auth files are private
and ephemeral, error messages mention missing variable names rather than values, and
resolved passwords are passed to diagnostic redaction. The v1 trust boundary is a
registry whose TLS certificate is already trusted by both the runtime and Kind node;
private-CA and insecure-registry configuration needs a separate design, especially for
remote Podman where a host path is not automatically a VM path.

## Application-flow DX audit

The initial high-level API could trigger Jenkins and Spinnaker directly, which was useful
for infrastructure acceptance but bypassed the production-shaped custom backend. The DX
audit therefore added baseline-and-observe primitives: capture the latest Jenkins build
number or current Spinnaker execution IDs, call the application, and wait for a newer
build/execution. This prevents an old success from satisfying an asynchronous test.

The same audit added a managed HTTP Service client, generic last-state polling, callback
journal waiting, Jenkins artifact downloads, and Spinnaker stage assertions. These small
composition points cover ordinary frontend/backend tests without introducing a domain-
specific release model into the harness. The full representative matrix and intentional
v1 gaps live in `docs/dx-coverage.md`.

After the registry and DX changes, the full modern high-level canary plus live-diagnostic
fixture passed again under rootful arm64 Podman in 514.88 seconds. All infrastructure and
Spinnaker pods reached Ready with zero startup failures, both tests passed, and the Kind
node was removed at session teardown.

## Component graph lessons

The useful common abstraction is lifecycle, not a universal Git/build/delivery provider.
An `EnvironmentComponent` needs only a stable name, explicit dependencies, and start/stop
hooks over shared environment context. Concrete Jenkins, Spinnaker, Git, Rollouts, and
WireMock APIs remain separate because their observable models do not line up cleanly.

Keep Kind outside the component graph as its substrate. This makes an empty graph useful,
keeps cluster ownership unambiguous, and prevents every component from declaring a
synthetic Kubernetes dependency. Validate the complete graph before cluster creation;
especially do not auto-add missing dependencies, because doing so can unexpectedly turn
a low-memory focused test into a full-stack run.

Preserve a failed startup state long enough to collect diagnostics, then call `stop()` on
the failed component as well as previously ready components. Installers can fail after
creating external or in-cluster resources, so only stopping fully ready nodes leaks
partial state. Teardown errors should be aggregated and must never prevent Kind deletion.

The old infrastructure installer coupled WireMock and Gitea accidentally. Splitting the
component graph exposed this because selecting either node still rendered and waited for
both. Component boundaries must extend through configuration, manifest filtering,
readiness, and service discovery; wrapping a monolithic installer alone does not create
real composability. Likewise, service endpoints now belong to concrete components so
test facades do not maintain a second hard-coded topology.

The first graph-backed Spinnaker acceptance test found another boundary hidden by the
direct PoC: Clouddriver's account allowed only the fixed `spinnaker-poc` namespace, while
the pytest API correctly deploys into a unique per-test namespace. Readiness was green,
but the real deploy failed with `wrongNamespace`. The harness account now permits dynamic
namespaces; its cluster-wide cache remains bounded by the explicit `kinds` list. This is
why component acceptance should execute one representative operation rather than stop at
pod readiness.

After removing that allowlist, the exact graph `argo-rollouts,istio,gitea,spinnaker`
passed the graph-backed raw-manifest acceptance test under rootful arm64 Podman in
383.44 seconds. Spinnaker fetched the exact Gitea commit, deployed into the fixture-owned
namespace, and the high-level Rollout snapshot observed `Healthy` plus the expected
revision annotation. Teardown left no Podman containers running. A separate exact graph
of `wiremock,gitea,jenkins` passed its push-and-callback boundary test in 95.10 seconds.

## Host endpoint visibility

Host access uses `kubectl port-forward` bound to `127.0.0.1` rather than Kind
`extraPortMappings`. This works consistently through Docker, DinD, and a rootful remote
Podman VM; static Kind mappings can bind inside the VM, collide across parallel sessions,
and require deciding ports before components are selected. Endpoint ports are therefore
ephemeral and must always be printed or returned to the caller.

Endpoint discovery is an optional concrete-component capability. It does not expand the
lifecycle protocol and does not imply that Jenkins, Gitea, WireMock, Spinnaker, or Istio
share an operational API. Primary endpoints are exposed by default when explicitly
requested; ingress and the internal Orca, Clouddriver, Rosco, and Front50 APIs are
on-demand. All bindings remain host-loopback-only.

Preserving a failed environment must retain the test namespace and test-owned Jenkins
library configuration as well as Kind. Keeping only the cluster destroys the state that
usually matters most. Pytest-owned port-forward processes cannot survive pytest exiting,
so preservation prints a standalone `cicd-harness expose` command that attaches a new
foreground manager to the retained kube context. Use preservation with `--maxfail=1`:
WireMock mappings and Jenkins global configuration are session-scoped and later tests can
legitimately change them.

The rootful Podman acceptance exposed the default Gitea and WireMock endpoints to the
macOS host, called both APIs, captured their catalog in diagnostics, and tore the reduced
graph down in 66.40 seconds. A separate intentional-failure acceptance retained its
unique namespace and ConfigMap, reattached WireMock using the exact printed command, and
deleted the unique pytest cluster using the printed `--cluster-name` cleanup command.
