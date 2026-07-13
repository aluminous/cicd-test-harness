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

The same builder is available from an installed package as
`cicd-harness image build legacy istio-pilot-arm64`, supports Docker and Podman, asserts
the output ELF architecture, and embeds the upstream license and build metadata. The
complete compatibility and GHCR publication contract is in `docs/arm64-compatibility.md`.

Do not assume a digest copied from a multi-architecture tag identifies the tag's manifest
list. The original Go 1.16.15 pin was the ARM64 platform manifest; Docker on an amd64
GitHub runner pulled it correctly and then failed with `exec format error`. The profile
now pins separate amd64 and arm64 builder manifests. The builder selects (or accepts an
explicit override for) the platform used to run Go, while `GOARCH` and the output image
remain ARM64.

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
resolved passwords are passed to diagnostic redaction.

Private CA support cannot be implemented as only `SSL_CERT_FILE`. The runtime pulls the
Kind node before Kubernetes exists; remote Podman executes inside a VM; Kind has a second
containerd trust boundary; Java ignores PEM client variables; and replacing a bundle can
accidentally discard public roots. The profile therefore accepts one additive PEM root.
It is combined with platform roots for host/common workload clients, installed into the
rootful macOS Podman machine and Kind node, baked into Jenkins, and converted to an
additive Java trust store by WireMock/Spinnaker init containers. Docker daemon bootstrap
and arbitrary application Java trust stores remain explicit platform/application duties.

The Podman VM is rpm-ostree based. Installing a diagnostic package with
`rpm-ostree install --apply-live` can download successfully but fail because the live
overlay changes directories, leaving an interrupted live-commit marker or pending
deployment. Do not mutate the VM merely to audit a test. For the DNS audit, extracting a
signature-verified Fedora `tcpdump` RPM beneath `/var/tmp` provided a disposable binary
without changing the booted deployment; clean any failed pending deployment before the
test. Capture on `any` inside the VM covers container/Kind traffic but not host macOS DNS.

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

## Air-gap dependency control

Treat disconnected support as two separate phases: connected artifact staging and
disconnected environment execution. Jenkins plugin resolution and the legacy Istio ARM
compile belong to staging whenever possible; the resulting images are immutable runtime
inputs. A pull-only policy must fail on a missing or stale fingerprint instead of quietly
switching back to an online build.

Front50's `s3` configuration was initially suspicious, but the effective endpoint is the
in-cluster MinIO service. Audit effective configuration rather than feature names: the
actual external gaps were the Kind binary, public OCI registries, Jenkins update sites,
the legacy Istio source archive, and Go module/checksum services. The vendored charts and
manifests do not fetch their original release repositories at install time.

Static profile validation is necessary but insufficient. Application manifests are
created after startup, so image allowlisting also belongs in the shared manifest rendering
path. Conversely, test-authored Jenkinsfiles and Spinnaker stages contain arbitrary code
and URLs; an OS/network egress deny remains the authoritative guardrail for those paths.

Prefer distributing the CA over disabling verification. Trust must exist in the host or
CI container, a Docker daemon or remote Podman VM, Kind/containerd, and connected image
builder bases. Remote Podman is a particular trap because a macOS path is not necessarily
a VM path. The emergency `insecure_skip_tls_verify` option therefore cannot be one magic
environment variable: it maps to harness HTTP SSL contexts, Git/curl/wget/Node settings,
Podman's CLI switch, host-scoped Kind containerd `certs.d` files, WireMock, Gitea, and a
harness marker for application-native handling. Docker/DinD bootstrap and arbitrary JVM,
Go, or Python library clients remain explicit daemon/application concerns. Never weaken
Kubernetes control-plane TLS just to make an internal registry usable.

Containerd registry namespaces are not always literal endpoints. `docker.io` is a logical
namespace whose registry server is `registry-1.docker.io`; writing the logical hostname
as `hosts.toml`'s server makes containerd request image layers from the Docker Hub website
and fail with an unexpected `text/html` media type. Keep the configuration directory named
`docker.io`, but use the registry endpoint as its `server` and host entry. Private registry
names and the other controlled public registry names are literal in the current profiles.

WireMock 3.13.1 exposes `--trust-all-proxy-targets`, but that setting belongs to browser
proxying and this pinned image fails at startup if it is supplied without
`--enable-browser-proxying`. Harness proxy mappings use reverse-proxy mode, which already
trusts target certificates by default. Do not change WireMock's operating mode merely to
make the global fallback look more explicit; preserve reverse-proxy semantics and pass the
harness marker for any future application-specific integration.

Containerd 1.x and 2.x registry configuration cannot be patched with both plugin tables
at once. The containerd 2 table caused the Kubernetes 1.21 node's kubelet/containerd
bootstrap to fail. The pinned 1.21 node also contains a legacy `k8s.gcr.io` mirror, and
containerd 1.6 rejects combining that `registry.mirrors` table with `config_path`. It
therefore receives host-scoped legacy `registry.configs.<host>.tls.insecure_skip_verify`
entries. Kind node images produced by Kind 0.27 and newer already enable
`/etc/containerd/certs.d` and use `hosts.toml`. This keeps the version distinction out of
individual tests without deleting the old node's required mirror.

## Air-gap validation findings

Allowed-host validation has two layers. The static preflight inventories the effective
Kind download URL, rewritten Kind/component/controller images, Jenkins image-build
mirrors, legacy Istio source/Go endpoints, and URLs embedded in the Spinnaker manifest.
It compares normalized exact hosts, exact host/port pairs, or wildcard subdomains with
`airgap.allowed_hosts`. The shared manifest renderer repeats image validation for
test-authored Kubernetes resources. Unit tests prove rejection of unmirrored registries,
external Spinnaker storage, Go's `direct` fallback, and public application images.

That model deliberately does not claim to be a firewall. A cold-cache rootful Podman run
was therefore captured with `tcpdump` on the VM's `any` interface, starting before each
focused PoC. This observes Podman bridges, the Kind node, and pod traffic after it enters
the VM; macOS-host traffic is outside that boundary. Capturing `any` repeats the same DNS
packet as it crosses several interfaces, so packet counts are not request counts. Podman's
resolver also emits randomized HINFO probes and search-suffix attempts; normalize query
names and correlate timestamps/source addresses rather than treating every line as a new
application request.

The audit found four nonessential runtime lookups that the original static inventory
could not see:

- Jenkins core still downloaded update-center tool metadata even when the ordinary update
  center was disabled. `hudson.model.UpdateCenter.never=true` plus a practically infinite
  `hudson.model.DownloadService$Downloadable.defaultInterval` disables both paths. Plugin
  resolution remains a connected image-build operation and uses the configured Nexus
  endpoints in an air-gap profile.
- MinIO performed an update lookup and its bundled browser console contacted SUBNET.
  `MINIO_UPDATE=off`, `MINIO_CALLHOME_ENABLE=off`, and `MINIO_BROWSER=off` make this API-only
  Front50 store quiet. A captured replacement-pod startup contained only the internal
  `spin-redis` service name and no `dl.min.io` or `subnet.min.io` query.
- Istio's platform detection queried `metadata.google.internal` even on Kind. Setting
  `GCE_METADATA_HOST=127.0.0.1:9` for istiod and the one ingress gateway keeps the harmless
  detection attempt on closed loopback without making the harness depend on Istio for
  egress control.
- Kork 7.99.1 enables Spinnaker's default remote plugin repositories and immediately
  refreshes `raw.githubusercontent.com`. In this historical release, both the documented
  YAML property and the equivalent JVM system property were present but consumed too late
  to affect plugin-cache initialization. All five Java pods therefore use a narrow
  `hostAliases` mapping of that hostname to loopback. A capture spanning several scheduled
  refresh intervals had no matching DNS query after the replacement pods' creation time.
  This is version-specific containment, not a general outbound policy; remove or replace
  it with an internal repository configuration if plugin loading becomes a test goal.

Useful upstream anchors for repeating this audit are Jenkins 2.426.1's
[`UpdateCenter`](https://github.com/jenkinsci/jenkins/blob/jenkins-2.426.1/core/src/main/java/hudson/model/UpdateCenter.java)
and [`DownloadService`](https://github.com/jenkinsci/jenkins/blob/jenkins-2.426.1/core/src/main/java/hudson/model/DownloadService.java),
MinIO's pinned
[`MINIO_*` constants](https://github.com/minio/minio/blob/RELEASE.2024-10-29T16-01-48Z/internal/config/constants.go),
and Kork 7.99.1's
[`PluginsConfigurationProperties`](https://github.com/spinnaker/kork/blob/v7.99.1/kork-plugins/src/main/java/com/netflix/spinnaker/config/PluginsConfigurationProperties.java).
Read the source matching the image tag: current documentation can describe behavior that
did not yet bind correctly in these 2021 Spinnaker services.

Expected cold-cache names were image registries and their signed CDN/object-store
redirects. The Jenkins-focused trace also contained public Jenkins mirrors, but every one
was timestamped to the derived-image build network before the Jenkins pod started; the
air-gap profile redirects those build-time inputs to Nexus. Separate live controller,
Jenkins, Spinnaker, and private-CA propagation tests passed and cleaned their clusters.
The CA test verified the rootful VM anchor, Kind-node anchor, namespace bundle, and live
WireMock Java trust-store alias; a release environment should additionally exercise its
real TLS registry/Nexus certificate chain.

One full combined run initially exhausted the 8 GiB budget because the direct PoC tests
created a first Kind cluster without owning teardown, then the fixture created a second
one. Direct PoCs now use a unique `poc_cluster` fixture with unconditional cleanup. This
was a lifecycle leak, not Spinnaker's steady-state requirement, and is another reason every
test-only substrate needs an explicit owner even when the process is expected to exit.

Lifecycle logs should identify both semantic boundaries (component starting/ready) and
subprocess boundaries (redacted command, exit code, elapsed time). Captured stdout/stderr
is most useful at DEBUG, but it may contain application-defined secrets the harness does
not know; CI systems must protect diagnostic artifacts even after known credentials and
URL user information are redacted.
