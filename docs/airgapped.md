# Air-gapped operation

Air-gap support is an enforced profile mode, not only a list of mirror suggestions. A
profile declares the only hosts the harness may contact, rewrites every controlled image
to an internal registry, and routes bootstrap/build downloads through internal artifact
services. `stack-up` runs the preflight before creating Kind, and every application
manifest subsequently rendered through `harness.resources` is checked as well.

Start from `profiles/airgap-modern.example.yaml` or
`profiles/airgap-legacy.example.yaml`. Replace the `.example` hostnames, credential
environment variable names, and repository paths with local values.

```bash
export AIRGAP_REGISTRY_USERNAME=ci-reader
export AIRGAP_REGISTRY_PASSWORD='...'

uv run cicd-harness airgap check profiles/my-airgap.yaml
uv run cicd-harness doctor profiles/my-airgap.yaml --prepare
uv run cicd-harness stack-up profiles/my-airgap.yaml
```

`airgap check` prints the original/effective image and URL inventory and exits non-zero
when an effective host is outside `airgap.allowed_hosts`. Exact hosts, hosts with ports,
and `*.internal.example` suffix rules are accepted. Loopback and Kubernetes `.svc`
destinations are intrinsically local. The JSON form is intended for CI evidence:

```bash
uv run cicd-harness airgap check profiles/my-airgap.yaml --json > airgap-plan.json
```

## What must be mirrored

| Input | Disconnected consumer | Recommended internal source |
|---|---|---|
| Kind binary | host harness bootstrap | Nexus raw proxy/hosted repository |
| Kind node and all component images | Podman/Docker and Kind containerd | internal OCI registry |
| Jenkins core/base image | connected image builder only | internal OCI registry |
| pinned Jenkins plugin closure | connected image builder only | Nexus update-site/raw and Maven proxies |
| legacy Istio source archive | legacy ARM image builder | Nexus raw proxy/hosted repository |
| legacy Istio Go modules | legacy ARM image builder | Nexus Go proxy; set `GOSUMDB=off` |
| harness wheel and Python dependencies | CI environment bootstrap | Nexus PyPI proxy or an offline wheelhouse |
| `kubectl`, Helm, Git, Podman/Docker | CI base image/host | preinstall in the CI image or internal package repositories |

The vendored Argo Rollouts manifests and Istio Helm charts do not contact a chart or
manifest repository. Their referenced container images still need to exist in the
internal OCI registry. The preflight discovers Rollouts images from the vendored YAML and
enumerates the remaining component images from the profile.

Image rewrite destinations are prefixes. For example:

```yaml
airgap:
  enabled: true
  allowed_hosts:
    - registry.corp.example
    - nexus.corp.example

registry:
  rewrites:
    docker.io: registry.corp.example/dockerhub
    docker.gitea.com: registry.corp.example/gitea
    quay.io: registry.corp.example/quay
    registry.k8s.io: registry.corp.example/kubernetes
    us-docker.pkg.dev: registry.corp.example/spinnaker
  credentials:
    - server: registry.corp.example
      username_env: AIRGAP_REGISTRY_USERNAME
      password_env: AIRGAP_REGISTRY_PASSWORD
```

Adapt the prefixes to the routing exposed by the internal registry or its reverse proxy.
Both the original tag/digest suffix and repository path after the matched prefix are
preserved. The registry account therefore needs read access to each resulting path.
Populate every effective reference before disconnecting. For release-grade isolation,
promote cached proxy content into immutable hosted repositories (or otherwise prevent
cache eviction/upstream revalidation), disable public egress, and rerun the dependency
plan plus a live stack acceptance test.

## Jenkins plugins through Nexus

Jenkins plugins are installed while the derived harness image is built; a disconnected
`stack-up` should never install plugins at runtime. The plugin set in
`images/jenkins/plugins.txt` is transitively resolved and version-pinned, and its contents
plus all mirror settings are included in the derived-image fingerprint.

The image recipe supports the Jenkins plugin CLI's five mirror variables:

```yaml
jenkins:
  image: registry.corp.example/harness/jenkins:2.426.1-pipeline
  base_image: docker.io/jenkins/jenkins:2.426.1-lts-jdk17
  image_policy: pull-only
  plugin_mirrors:
    update_center: https://nexus.corp.example/repository/jenkins-updates
    experimental_update_center: https://nexus.corp.example/repository/jenkins-experimental
    download: https://nexus.corp.example/repository/jenkins-download
    plugin_info: https://nexus.corp.example/repository/jenkins-updates/current/plugin-versions.json
    incrementals: https://nexus.corp.example/repository/jenkins-incrementals
```

On a connected staging runner that can reach Nexus and the internal registry:

```bash
uv run cicd-harness image build profiles/my-airgap.yaml jenkins --push
```

Run this while Nexus can fill any missing cache entries. The disconnected environment
then needs only the derived OCI image; keeping plugin proxies available internally is
still useful for reproducible restaging and Jenkins update metadata, but `stack-up` does
not invoke the plugin installer.

The running Jenkins controller also disables both the ordinary update-center check and
the separate `Downloadable` metadata refresh. This matters because the latter otherwise
contacts Jenkins mirrors even when plugins were fully installed in the image.

Configure the update/download Nexus raw proxies so that upstream paths remain unchanged;
the metadata references plugin paths below those roots. The incrementals destination can
be a Nexus Maven proxy/group. Setting `image_policy: pull-only` makes `stack-up` pull the
prebuilt image and verify its harness fingerprint. It fails instead of falling back to a
networked plugin build. `pull-or-build` is useful in connected CI, and
`build-if-missing` retains the local-development behavior.

The variable names follow the official
[Jenkins Docker image/plugin CLI configuration](https://github.com/jenkinsci/docker#plugin-installation).
For Nexus repository behavior, see Sonatype's
[raw repository](https://help.sonatype.com/en/raw-repositories.html) and
[Docker registry](https://help.sonatype.com/en/docker-registry.html) documentation.

If Nexus uses a private CA, set `trust.ca_certificate`; the derived Jenkins image receives
the root before `jenkins-plugin-cli` runs, and the certificate bytes participate in the
image fingerprint. Avoid credentials in repository URLs or Docker build arguments because
they are retained in logs or image metadata. The simplest secure deployment is read-only
anonymous access from the isolated builder network, with writes restricted to the OCI
staging account.

## Spinnaker and “S3”

The harness does not contact AWS S3. Front50 is configured with the S3-compatible driver,
but its endpoint is `spin-minio.spinnaker.svc.cluster.local`; MinIO runs in the same Kind
cluster and its image is covered by registry rewriting. This preserves the Front50 object
storage behavior without an AWS dependency or Nexus S3 proxy.

Gate, Orca, Clouddriver, Front50, Rosco, Redis, and MinIO run from pinned images. Rosco
receives raw manifests or Kustomize input from the test-owned in-cluster Gitea service.
No Spinnaker BOM, Debian repository, Maven repository, or public Git server is consulted
during `stack-up` or the validated pipeline flows. New test-authored Spinnaker stages can
introduce their own URLs; the static preflight cannot inspect pipeline definitions that do
not exist yet, so those artifact accounts/stages must also target internal services.

The MinIO instance is API-only: update checks, SUBNET call-home, and its unused browser
console are disabled. Spinnaker 1.25.4's Kork 7.99.1 plugin cache attempts to refresh its
default repository before its disable property is reliably bound, so the five Java pods
resolve `raw.githubusercontent.com` to loopback. This narrow compatibility containment is
appropriate while plugins are outside the harness's test scope. If a test later needs
plugins, configure an internal repository and update this boundary rather than adding the
public hostname to the allowlist.

## Legacy Istio ARM image

The legacy profile can pull a prebuilt internal compatibility image first. Its reproducible
fallback build is also air-gap capable:

```yaml
istio:
  arm64_pilot:
    image: registry.corp.example/harness/istio-pilot:1.10.6-arm64
    pull_before_build: true
    source_url: https://nexus.corp.example/repository/github-raw/istio/istio/archive/refs/tags/1.10.6.tar.gz
    go_proxy: https://nexus.corp.example/repository/go-proxy
    go_sumdb: "off"
```

The source checksum and builder image digests remain authoritative. `GOPROXY` and
`GOSUMDB` are passed into the pinned builder container, and `airgap check` rejects a
`direct` Go fallback. See `docs/arm64-compatibility.md` for the image's fidelity boundary.
Current Nexus releases support the Go module proxy protocol; verify the repository format
against your installed Nexus version, or point `go_proxy` at another internal Go proxy.
See Sonatype's [Go repository guide](https://help.sonatype.com/en/go-repositories.html).

## Bootstrap and authentication

The Kind binary URL is profile-controlled and checksum-verified:

```yaml
kind:
  download_url_template: https://nexus.corp.example/repository/kind/v{version}/kind-{platform}
```

The disconnected CI image still needs the harness and its Python dependencies. Point the
installer at the Nexus PyPI group (for example with `UV_INDEX_URL`) or install a previously
exported wheelhouse. The built harness wheel contains its profiles, manifests, Helm charts,
image recipes, and fixtures.

Registry credentials are resolved from environment variables into temporary private
Docker/Podman auth files and Kubernetes pull secrets. Nexus downloads currently assume
read-only access from the CI/build network without per-request credentials; prefer network
policy or anonymous read on dedicated proxy repositories over embedding secrets in URLs.

## Private CA certificates

Use one PEM file when the internal registry/Nexus uses a private PKI or CI traffic passes
through a corporate TLS interception proxy:

```yaml
trust:
  ca_certificate: certificates/corporate-ca.pem
```

The path is relative to the profile workspace unless absolute. The certificate is
validated as PEM before network use, files containing private-key material are rejected,
and the root is always added to—not substituted for—the platform public roots. The
setting has these effects:

- harness HTTP downloads plus Git, curl, Python/Requests, pip, AWS, gRPC, Go, and Node
  subprocesses use a combined private/public bundle;
- on macOS with Podman, the root is installed idempotently into the selected rootful
  machine's Fedora trust store before the first image operation;
- the Kind node OS trusts the root, and containerd is restarted only when the root changes;
- each managed namespace receives a `harness-trust-bundle` ConfigMap. Harness-rendered
  workloads mount it read-only and receive the common PEM-compatible TLS variables;
- the Jenkins image build includes the CA in its OS/Java roots, and the CA bytes are part
  of the image fingerprint; and
- WireMock plus Gate, Orca, Clouddriver, Front50, and Rosco build an additive Java trust
  store in an init container, covering HTTPS proxy origins and artifact sources.

The Podman machine anchor persists after harness teardown because it is machine-level
configuration; a later profile run atomically replaces the harness-owned anchor. The
namespace ConfigMaps contain only public certificate material, not private keys.

An opt-in propagation PoC checks the live rootful Podman anchor, Kind anchor, namespace
bundle, and WireMock Java alias with any valid test CA certificate:

```bash
CICD_TEST_CA_CERTIFICATE=/path/to/test-root.pem \
  uv run pytest -q tests/test_poc_trust.py
```

This proves distribution of the configured root. Also test a real HTTPS registry/Nexus
request in the target corporate environment to validate that server's complete chain and
proxy behavior.

There are two intentional limits. A Docker/DinD daemon may need the CA to pull the Kind
node image before any container exists, so install it in the daemon's documented
`/etc/docker/certs.d/<registry>/ca.crt` location or bake it into the DinD image. Also,
arbitrary test-owned Java images do not understand PEM environment variables; bake the
root into those images or configure their Java trust store.

## Emergency TLS verification fallback

When the registered CA still cannot be used, a disposable isolated environment can opt
into one profile-wide fallback:

```yaml
trust:
  # Optional but recommended: clients without a native bypass may still use it.
  ca_certificate: certificates/corporate-ca.pem
  insecure_skip_tls_verify: true
```

The CA and fallback can coexist. The fallback takes precedence in harness-owned HTTP
clients. If the CA file itself is invalid or cannot be installed, omit `ca_certificate`;
certificate parsing and trust-store construction still fail closed. The fallback is
propagated as follows:

| Boundary | Behavior when enabled |
|---|---|
| Harness Python clients and downloads | an SSL context disables certificate and hostname checks |
| Host Git, curl, and wget subprocesses | native environment/config switches are installed for the harness process tree and restored at teardown |
| Managed Kubernetes workloads | Git/curl/wget/Node and best-effort Python/Go settings are injected, plus `CICD_HARNESS_INSECURE_SKIP_TLS_VERIFY=1` for application-native handling |
| Podman image operations | pull, build, and push use `--tls-verify=false` |
| Kind image pulls | current containerd receives host-scoped `certs.d/<host>/hosts.toml`; the 1.21 profile receives equivalent legacy host TLS entries |
| WireMock | reverse-proxy targets are already trusted by WireMock; the harness marker is also supplied |
| Gitea | HTTPS webhook verification is disabled with Gitea's native setting |
| Istio processes | the harness marker is supplied; Kubernetes and Istio control-plane identity TLS remain verified |
| Jenkins and Spinnaker workloads | common process settings and the harness marker are supplied; Git/curl-based steps honor them |

This is not a universal JVM or application-library override. Java has no safe
process-wide trust-all property; arbitrary Spinnaker artifact plugins and Jenkins plugin
installation should use the CA or target an in-cluster HTTP WireMock reverse proxy.
Python Requests does not honor `PYTHONHTTPSVERIFY`, and Go's `GOINSECURE` is specific to
module fetching rather than all HTTP clients, so custom applications must consume the
harness marker and set their own library option. Existing test-authored environment
variables are never overwritten.

Docker/DinD is also a daemon boundary: Docker has no per-pull bypass equivalent, so its
daemon must be started with the internal registry marked insecure (or with a working CA)
before the harness can pull the Kind node. Reusing a Kind cluster created without the
fallback is discouraged because its containerd registry configuration was fixed at
creation time. The harness never disables the Kubernetes API server, kubelet, client
certificate, or service-account TLS checks.

The built-in profiles pin `kind.containerd_registry_mode` to `hosts` (modern) or
`legacy` (Kubernetes 1.21). If a custom profile deliberately pairs a newer Kind binary
with an older node image, set this field to match the node image's containerd registry
configuration rather than relying on `auto` release inference.

This mode permits credential theft and undetected endpoint impersonation. Use it only on
a short-lived network containing no production credentials. `doctor` displays an
`unsafe TLS fallback` row, and lifecycle setup emits a warning so the reduced security is
visible in local and CI logs.

## DNS egress audit

The host allowlist is a static control over dependencies the harness owns; it cannot
predict a URL embedded in a Jenkinsfile, Git submodule, custom backend, or test-authored
Spinnaker artifact. Pair it with an observed network audit and an actual egress-denied CI
lane. For rootful Podman on macOS, capturing `port 53` on every Podman VM interface sees
the VM, runtime bridges, Kind nodes, and Kubernetes pods. It does not see DNS performed by
the macOS host itself. In Docker/DinD CI, capture on the outer CI container/daemon network
so the complete harness process tree is in scope.

Record both DNS queries and connection destinations. DNS alone misses cached answers,
literal IP addresses, DNS-over-HTTPS, and processes that began before capture. Start with
empty runtime/Kind state where practical, start capture before `pytest`, retain the pcap as
a CI artifact, and compare normalized query names against `airgap check --json`. A passive
capture is evidence; an egress-deny firewall remains the enforcement boundary.

The rootful Podman audit used focused controller, Jenkins, Spinnaker, MinIO-restart, and
private-CA lanes. It found and removed Jenkins runtime metadata, MinIO update/SUBNET,
Istio cloud-metadata, and Spinnaker default-plugin lookups. Expected cold-cache image
registry/CDN names remain in a connected profile; an air-gap profile must rewrite their
original registry references to the internal OCI host. See `docs/engineering-notes.md`
for the timestamp-correlation and historical-version gotchas.

## Command and lifecycle logs

CLI commands log lifecycle transitions, each subprocess command, exit status, and elapsed
time to stderr at `INFO`. Component start/readiness/teardown boundaries make a stalled
`stack-up` visible. Use `DEBUG` to include captured subprocess stdout/stderr, or retain a
file alongside CI diagnostics:

```bash
uv run cicd-harness --log-level DEBUG --log-file artifacts/stack-up.log \
  stack-up profiles/my-airgap.yaml
```

`CICD_HARNESS_LOG_LEVEL` and `CICD_HARNESS_LOG_FILE` provide equivalent defaults. Known
registry passwords, sensitive flag values, and HTTP URL user information are redacted.
Debug command output can still contain application-defined secrets unknown to the harness,
so protect it as CI diagnostic data.

## Deliberate boundaries

- Air-gap mode prevents unapproved image registries and controlled download URLs; it is
  not an operating-system firewall. Enforce egress denial around the CI container/VM as
  the authoritative backstop.
- A manifest bypassing `harness.resources` and invoking `kubectl` directly also bypasses
  dynamic image validation. Prefer the high-level resource API.
- Test-authored Jenkinsfiles, Git submodules, shell steps, and Spinnaker pipeline artifacts
  can name arbitrary destinations. Route them to WireMock, Gitea, Nexus, or another allowed
  internal service and verify the isolated lane under real egress denial.
