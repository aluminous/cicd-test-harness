# Changelog

All notable user-visible changes will be documented here. The project follows semantic
versioning after the `0.1` preview line; APIs may still change between preview releases.

## 0.1.0 - 2026-07-12

Initial public preview:

- disposable Kind environments for Kubernetes 1.31 and 1.21;
- Argo Rollouts 1.8.3 and 1.4.1 with Istio traffic-weight assertions;
- writable Gitea, WireMock mocks/reverse proxies, Jenkins Pipeline/shared libraries, and
  a memory-reduced Spinnaker 1.25.4 service slice with raw and Kustomize pipelines;
- dependency-ordered `EnvironmentComponent` lifecycle graph and exact component subsets;
- high-level pytest APIs, automatic failure diagnostics, private-registry rewriting, host
  endpoint exposure, and failed-environment preservation;
- self-contained wheel assets and checksum-pinned automatic Kind installation.
- contributor, security, third-party licensing, environment-doctor, and fast CI
  documentation suitable for an initial OSS preview.
