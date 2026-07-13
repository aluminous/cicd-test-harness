# Changelog

All notable user-visible changes will be documented here. The project follows semantic
versioning after the `0.1` preview line; APIs may still change between preview releases.

## Unreleased

- enforceable air-gap profiles with dependency inventory, host allowlisting, internal
  registry rewrites, Nexus-routable Kind and legacy Istio inputs, and dynamic manifest
  image validation;
- Nexus-configurable Jenkins plugin image builds plus a fingerprint-verified `pull-only`
  policy for disconnected execution;
- additive profile-configured private CA propagation through host tools, rootful Podman,
  Kind/containerd, managed workloads, Jenkins, WireMock, and Spinnaker Java trust; and
- an explicit emergency `trust.insecure_skip_tls_verify` profile fallback propagated to
  harness clients, subprocesses, workloads, Podman, Kind registries, WireMock, and Gitea,
  with documented daemon and application-library boundaries;
- timed, redacted command/component lifecycle logs with configurable verbosity and file
  output;
- runtime egress cleanup for Jenkins metadata refresh, MinIO update/SUBNET calls, Istio
  cloud detection, and Spinnaker 1.25.4 default plugin repositories, validated with
  rootful-Podman DNS packet captures; and
- ownership and unconditional teardown for direct PoC Kind clusters, preventing a leaked
  second cluster from exhausting the 8 GiB test budget.

## 0.1.0 - 2026-07-12

Initial public preview:

- disposable Kind environments for Kubernetes 1.31 and 1.21;
- Argo Rollouts 1.8.3 and 1.4.1 with Istio traffic-weight assertions;
- writable Gitea, WireMock mocks/reverse proxies, Jenkins Pipeline/shared libraries, and
  a memory-reduced Spinnaker 1.25.4 service slice with raw and Kustomize pipelines;
- dependency-ordered `EnvironmentComponent` lifecycle graph and exact component subsets;
- high-level pytest APIs, automatic failure diagnostics, private-registry rewriting, host
  endpoint exposure, and failed-environment preservation;
- self-contained wheel assets and checksum-pinned automatic Kind installation;
- reproducible Docker/Podman build and manual GHCR publication for the exact-source
  Istio 1.10.6 ARM64 pilot compatibility image; and
- contributor, security, third-party licensing, environment-doctor, and fast CI
  documentation suitable for an initial OSS preview;
- complete English and Korean project readmes with matching operational and fidelity
  guidance.
