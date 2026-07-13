# Test-developer experience coverage

This audit models a custom frontend and release backend that creates Jenkins jobs,
triggers Jenkins and Spinnaker, commits deployment state to Git, and observes Argo
Rollouts. It is deliberately application-first: a covered case should not require the
author to manage Kind, port-forwards, component credentials, or raw polling loops.

| # | Representative test | Author-facing path | v1 status |
|---:|---|---|---|
| 1 | Submit a release through the backend and poll its public state | `harness.service()` + `wait_until()` | High-level |
| 2 | Verify the backend asynchronously created the correct multibranch job | `wait_for_job()` + `assert_job()` | High-level |
| 3 | Discover `main` and `release`, execute the repo Jenkinsfile, and load `@Library('example')` from a second Git repo | repository templates + `create_library()` + `run_branch()` | Live-validated |
| 4 | Ensure an API request triggered a new Jenkins build rather than reusing an old result | `latest_build_number()` + application call + `wait_for_build(after=...)` | High-level |
| 5 | Mock several Jenkins/backend outbound APIs, including errors and latency | `mocks.service().expect()` with status/body/JSONPath/delay | High-level |
| 6 | Wait for a callback and assert its exact JSON body and call count | `expectation.wait()` + returned request journal; teardown verifies exactness | High-level |
| 7 | Assert Jenkins console output and archived build products | `JenkinsBuild.console`, `.artifacts()`, `.artifact()` | High-level |
| 8 | Verify a Jenkins/backend Git commit changed the expected manifest at the exact pushed SHA | `repository.refresh()` + `read()` + `raw_url()` | High-level |
| 9 | Ensure the backend triggered a new raw-manifest Spinnaker execution | `execution_ids()` baseline + `wait_for_execution()` | High-level |
| 10 | Exercise the real Rosco Kustomize bake/deploy path and assert both stages | `deploy_kustomize()` + `execution.assert_stage()` | Live-validated |
| 11 | Observe the canary pause with one stable and one canary ReplicaSet and 50/50 Istio weights | `rollout.wait_for_canary()` + ReplicaSet/traffic assertions | Live-validated |
| 12 | Ensure completion does not hang and leaves exactly one scale-down-pending old ReplicaSet | `wait_healthy()` + `assert_scale_down_pending()` | Live-validated |
| 13 | Diagnose a missing callback, failed Jenkins build, or failed Spinnaker stage | mock verification, expected result/status, stage assertion, automatic evidence | High-level |
| 14 | Run two releases concurrently and associate every callback/build/execution correctly | unique names and journals help, but multi-execution correlation remains test-specific | Partial |
| 15 | Test GitHub-specific PR, commit-status, check-run, webhook, or branch-protection behavior | raw Gitea API through `advanced.gitea` where compatible | Gap |

## Remaining v1 blind spots

- Browser/UI automation is not bundled. The frontend can be reached with
  `harness.service()`, but a project wanting DOM-level tests should layer Playwright on
  the returned URL.
- WireMock reverse proxies provide pass-through observation and selective interception
  without depending on Istio. Code and Jenkinsfiles must accept the proxy URL, or the
  deployment must reserve a logical Service name for the proxy and a separate origin
  name. Arbitrary network-level redirection and hard-coded public TLS hosts remain out of
  scope.
- Gitea provides real writable Git and a useful subset of HTTP APIs, but it is not a
  complete GitHub API emulator. GitHub Apps, checks, statuses, PR semantics, webhook
  signatures, and branch protection need a later compatibility slice or a dedicated
  emulator.
- Jenkins credential creation, folders, agents, build cancellation, and fine-grained
  plugin configuration remain on the raw client/escape hatch. Job and global-library
  inspection cover the current production-shaped cases.
- The high-level Spinnaker API observes executions and stages but does not yet list or
  deeply compare saved pipeline configuration. Tests for a system that authors pipeline
  definitions—not merely triggers them—will need the raw Gate client initially.
- Multiple releases may run concurrently inside one test, but the current observer
  chooses a new Jenkins build by job/number and a Spinnaker execution by application/name.
  A project-specific correlation ID should be asserted in callbacks, parameters, or
  execution payloads. Separate pytest tests must not run concurrently in one session
  because WireMock mappings/journals and Jenkins global libraries are session-global.
- Private registries with basic credentials and one additive PEM corporate CA are
  modeled end to end for the host harness, rootful macOS Podman, Kind/containerd,
  Jenkins, WireMock, Spinnaker, and common workload TLS clients. Docker daemon bootstrap,
  arbitrary Java application trust stores, insecure HTTP registries, cloud credential
  helpers, and short-lived token refresh remain external concerns.
- Application logs are always captured on failure, but there is no high-level streaming
  log assertion. `harness.advanced.kubectl` is the current escape hatch.

These boundaries are intentional for v1. The largest application-flow hole—observing
Jenkins and Spinnaker work initiated by the backend—has a high-level API; the remaining
items are either provider-specific or uncommon enough to add after real test suites
show which fidelity is valuable.
