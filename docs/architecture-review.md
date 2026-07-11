# Architecture review after proxy support

## Scope and conclusion

The review covered lifecycle ownership, configuration, public test APIs, component
clients, diagnostics, isolation, secret handling, and the new reverse-proxy path. No
blocking redesign is required before application teams begin using the harness. The
cleanup that was necessary before adding more surface area has been implemented.

The current dependency direction is:

```text
profile configuration
        ↓
HarnessEnvironment → ComponentGraph → concrete components → Kind/Kubernetes
        ↓
HarnessRuntime → per-test TestHarness → domain APIs
        ↓                              ↘ low-level clients
diagnostics ← tracked domain state ← application assertions
```

WireMock is infrastructure, not an observability dependency on Istio. Test-owned DNS
aliases route selected HTTP dependencies through WireMock, while harness management
clients can address the origin services directly.

The graph deliberately abstracts lifecycle only. Git/build/delivery providers retain
concrete APIs; a shared provider interface would erase the Jenkins job model, Spinnaker
stage model, and Git-specific revision semantics that tests need to observe.

## Required cleanup completed

### Make lifecycle component-based

The monolithic startup sequence is now a validated DAG of `EnvironmentComponent`
instances. Standard components can be selected exactly, profiles may omit entire
sections, WireMock and Gitea install independently, and custom components receive a
small shared `EnvironmentContext`. Lifecycle states are preserved in startup diagnostics;
teardown visits all attempted components in reverse order before deleting Kind.

### Make component endpoints host-discoverable

Concrete components now advertise optional UI, API, and traffic endpoints through a
validated catalog. A session-owned manager opens loopback-only Kubernetes port-forwards;
pytest can expose named endpoints, and the standalone CLI can attach to a retained
cluster. This remains independent of Istio and does not generalize component operations.

### Isolate HTTP testing concerns

`testing.py` had reached 1,827 lines and was accumulating Kubernetes alias creation,
mocking, proxy routing, normalized request records, and every other domain API. The mock
and proxy facade now lives in `testing_http.py`; DNS normalization is shared through
`naming.py`; low-level WireMock behavior remains in `wiremock.py`. Existing
`TestHarness.mocks` behavior is preserved and `TestHarness.proxies` is additive.

### Make aliases test-owned

The old mock facade created selector Services in the session-owned `harness-system`
namespace. Names accumulated across tests and could collide. Logical mock and proxy names
are now `ExternalName` Services in the isolated test namespace, so Kubernetes teardown
owns their cleanup and each test gets an independent DNS name.

### Bound proxy memory

Proxy request journals and response bodies can grow much faster than ordinary stubs.
WireMock now has profile-configurable journal entry, response-body, and upstream timeout
limits. Defaults are 1,000 events, 64 KiB per logged response body, and 30 seconds.

### Define temporal expectation semantics

An intercept registered midway through a scenario must not count matching requests that
already passed through. Expectations now record a matching baseline at registration and
verify only later calls. This behavior is live-tested on the same Gitea health route.

### Protect diagnostic credentials

Proxy journals may contain production-shaped authorization and session headers.
Diagnostic snapshots now redact Authorization, Proxy-Authorization, Cookie, Set-Cookie,
and X-API-Key values before the generic secret-value redactor runs. Raw records remain
available to the running test because they may be the subject of an assertion.

### Normalize WireMock version differences

WireMock 3.13 request-search responses do not carry the full proxy serve event. The
public `RequestRecord` is built from the complete journal, providing stable method, URL,
headers, body, status, duration, mapping, match, and proxy fields without exposing that
schema difference to tests.

## Boundaries that remain sound

- `HarnessEnvironment` owns the disposable cluster and delegates dependency order to the
  component graph; failed startup still tears down at the coarsest safe boundary.
- Installers render version/profile differences, while HTTP clients contain component
  API mechanics. Test authors consume neither directly unless using `advanced`.
- Session-scoped expensive infrastructure plus function-scoped namespaces remains the
  correct tradeoff for the 8 GiB budget.
- Istio and Rollouts stay systems under test. Harness request observation does not depend
  on sidecars, Telemetry resources, or a particular Istio API version.
- Failure evidence is collected before namespaces, clients, port-forwards, and the
  cluster are removed.

## Recommended next refactors

These are not blockers, but should precede another large feature set:

1. Split the remaining `testing.py` domain facades into Git, Jenkins, Spinnaker,
   Rollouts, application resources, and service connections, re-exporting current public
   names for compatibility.
2. Introduce a small typed `HarnessServices` interface instead of having facade modules
   reach through the private `_services` registry. This will make facade unit tests and
   alternate clients simpler.
3. Build the proposed standard logging API from source adapters: Kubernetes containers,
   Jenkins consoles, Spinnaker stages, and WireMock `RequestRecord`s. Diagnostics should
   consume those adapters instead of maintaining a second collection path.
4. Add a profile schema version before published profiles need migrations.
5. If same-cluster parallel pytest execution becomes important, replace global WireMock
   reset and Jenkins global-library mutation with test-token-scoped mappings and state.
   Separate pytest processes already remain isolated by cluster name.
6. Add opt-in live lanes for authenticated private registries and external HTTPS proxy
   targets. Unit coverage currently verifies their security-sensitive plumbing, while
   the live proxy acceptance uses in-cluster HTTP.

The next architectural pressure point is therefore code organization and a shared
observability abstraction—not lifecycle ownership, provider generalization, or the
Kubernetes/Spinnaker topology.
