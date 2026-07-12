# ARM64 compatibility and image builds

This document is the support boundary for Apple Silicon and Linux ARM64. It records which
components run natively, which run under emulation, and which compatibility artifacts the
harness builds. Keep it in sync with the profiles and image recipes; an ARM64 workaround
that exists only in a developer's local image cache is a release bug.

## Compatibility matrix

| Component | Modern profile | Legacy profile | ARM64 behavior |
|---|---|---|---|
| Kind / Kubernetes | 1.31.14 | 1.21.14 | Native ARM64 node images; legacy requires rootful Podman or Docker/DinD |
| Argo Rollouts | 1.8.3 | 1.4.1 | Native controller images; canary and ReplicaSet assertions are real |
| Istio control plane | 1.25.5 official image | 1.10.6 exact-source pilot build | Modern is native; legacy uses the recipe below |
| Istio ingress data plane | Official proxyv2 | Selector-compatible placeholder | Legacy ingress traffic is deliberately not claimed |
| Spinnaker 1.25.4 slice | Upstream images | Upstream images | amd64 images run under Podman binfmt/QEMU; amd64 CI is authoritative |
| Jenkins 2.426.1 | Derived multi-architecture image | Same | Native ARM64 base plus pinned Pipeline plugins |
| Gitea / WireMock | Profile images | Profile images | Exercised successfully in the ARM64 lanes |

The legacy Istio boundary is intentionally narrow. Gateway and VirtualService admission,
the Gateway selector, and Argo Rollouts VirtualService weight reconciliation are real.
The placeholder does not contain `proxyv2`, so it cannot validate ingress requests, Envoy
xDS behavior, or sidecar traffic. Those remain amd64 CI responsibilities.

## Reproducible Istio 1.10.6 pilot image

An installed wheel contains the profile and Containerfile required by the build. The
Python builder downloads the exact Istio `1.10.6` source archive, verifies its SHA-256,
cross-compiles `pilot-discovery` with the tag's pinned Go 1.16 builder image, validates
that the result is an AArch64 ELF binary, and builds a `linux/arm64` scratch image. Docker
and Podman are both supported:

```bash
cicd-harness image build legacy istio-pilot-arm64
cicd-harness image build legacy istio-pilot-arm64 --runtime docker --force
```

The first command uses the runtime and local tag from `profiles/legacy.yaml`. `stack-up`
calls the same builder automatically on ARM64 hosts. Source, module, and compiler caches
live below `.tools/`; the image itself embeds:

- the upstream Apache 2.0 license;
- the source URL, source checksum, upstream Git revision, target, and fidelity statement
  in `/usr/share/cicd-harness/BUILD-METADATA.json`; and
- OCI labels identifying the upstream revision and the pilot-only support boundary.

The pinned inputs are:

| Input | Value |
|---|---|
| Istio version | `1.10.6` |
| Git revision | `fd053c6165d21105d66dac6e3d0649db2dde5b86` |
| Source SHA-256 | `c737648a6dc6b4bb3a5ac1dfc202469ced73e54c83cc591db917120c5590aae4` |
| Go builder, amd64 | `golang@sha256:35fa3cfd4ec01a520f6986535d8f70a5eeef2d40fb8019ff626da24989bdd4f1` |
| Go builder, arm64 | `golang@sha256:79e277312aa1ba8dce542a30260fea7f797c4aaf264300a7d56683aa25e4fc16` |
| Target | `linux/arm64` |

The Go builder digests are platform manifests, not a multi-architecture index. The
harness selects the host builder architecture while always setting `GOARCH=arm64` and
building a `linux/arm64` output image. For a remote runtime whose architecture differs
from the Python host, pass `--builder-platform linux/amd64` or `linux/arm64` explicitly.

To use another registry, copy the legacy profile, change `istio.arm64_pilot.image`, and
either build and push directly or set `pull_before_build: true` to try the prebuilt image
before falling back to a local exact-source build:

```bash
cicd-harness image build my-legacy-profile.yaml istio-pilot-arm64 \
  --tag registry.example.test/platform/istio-pilot:1.10.6-arm64-poc \
  --push
```

Registry credentials use the same environment-variable-backed `registry.credentials`
configuration as the rest of the harness. The CLI installs the temporary Docker or
Podman auth file and removes it when the build finishes.

## GHCR publication

`.github/workflows/publish-arm64-image.yml` is a manual, least-privilege publisher for:

```text
ghcr.io/aluminous/cicd-harness-istio-pilot:1.10.6-arm64-poc
```

It uses the repository-scoped `GITHUB_TOKEN`, requests only `contents: read` and
`packages: write`, invokes the same packaged builder with Docker, and validates the local
image architecture before pushing. The amd64 runner cross-compiles the ARM64 binary
without QEMU. Run it with:

```bash
gh workflow run publish-arm64-image.yml \
  -R aluminous/cicd-test-harness \
  -f tag=1.10.6-arm64-poc
```

The workflow is manual because this is a compatibility artifact, not an official Istio
image. A package created from the private repository remains private unless an owner
explicitly changes its GHCR visibility. Pin a published digest in release profiles rather
than treating the human-readable tag as immutable.

## Why there is no legacy proxyv2 ARM64 image

The official Istio 1.10.6 `pilot-agent` crashes under nested x86 emulation on ARM64, while
its packaged Envoy binary starts. A hybrid image with a native `pilot-agent` and official
amd64 Envoy is technically possible, but it has not passed a real request-routing and xDS
test. Publishing it now would imply fidelity we have not established. The engineering
notes retain the experiment details so this can be resumed without repeating discovery.
