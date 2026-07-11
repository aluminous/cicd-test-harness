# Environment components

The harness deliberately generalizes only environment lifecycle and composition. Git,
Jenkins, Spinnaker, Rollouts, and WireMock keep concrete APIs because pretending those
systems share a useful build or delivery interface would hide important behavior from
tests.

Kind is the reusable substrate. After the cluster exists, a `ComponentGraph` starts
`EnvironmentComponent` instances in dependency order and stops every attempted component
in reverse order. The standard profile currently produces these nodes:

```text
argo-rollouts   istio   wiremock   gitea   jenkins
                                  └──────→ spinnaker
```

Only declared dependencies constrain ordering. For example, Spinnaker depends on Gitea
because the supplied Spinnaker configuration has a Gitea artifact account. Jenkins has
no graph dependency on Gitea: a Jenkins-only test remains valid, while a particular test
can still configure a job to use a Gitea repository.

## The component contract

A component owns readiness for one coherent capability. It receives the shared cluster,
Kubectl client, command runner, registry support, profile, and workspace through
`EnvironmentContext`.

```python
from cicd_harness import EnvironmentContext
from cicd_harness.component import BaseEnvironmentComponent


class ExampleController(BaseEnvironmentComponent):
    name = "example-controller"
    dependencies = frozenset({"argo-rollouts"})

    def start(self, context: EnvironmentContext, *, timeout: int) -> None:
        context.kubectl.apply("...")
        context.kubectl.wait_available(
            "example-controller", "example-system", timeout=timeout
        )
```

`stop()` is optional when deleting Kind is the correct cleanup boundary. Components
which allocate state outside that cluster must override it. A component should not expose
a generic provider abstraction; its normal client and high-level test facade remain the
right place for system-specific operations.

Components may optionally advertise concrete host endpoints without changing the
`EnvironmentComponent` lifecycle contract:

```python
from cicd_harness import EndpointKind, HostEndpointSpec


class ExampleController(BaseEnvironmentComponent):
    name = "example-controller"
    host_endpoints = (
        HostEndpointSpec(
            name="example-api",
            component=name,
            kind=EndpointKind.API,
            namespace="example-system",
            service="example-controller",
            port=8080,
            description="Example controller API",
        ),
    )
```

This is an optional discovery capability, not a generic service-provider interface. The
catalog validates endpoint ownership and duplicate names before opening any forwards.

Custom component lists can be supplied when constructing `HarnessRuntime` or
`HarnessEnvironment`:

```python
runtime = HarnessRuntime(
    profile,
    workspace=workspace,
    artifact_root=artifact_root,
    components=[ExampleController()],
)
```

Passing `components` replaces the standard graph. Passing `component_names` selects
standard components from the profile. These arguments are mutually exclusive.

## Selecting standard components

Every component configured by the profile starts by default. For local or focused test
runs, select the exact set:

```bash
uv run pytest tests/http --cicd-components wiremock
uv run pytest tests/builds --cicd-components wiremock,gitea,jenkins
uv run cicd-harness stack-up modern --components argo-rollouts,istio
```

`CICD_COMPONENTS` is the environment-variable equivalent for pytest. The older
`--cicd-without-spinnaker` and `--cicd-without-jenkins` flags remain convenient shorthands.
Selection is exact rather than silently adding dependencies: requesting `spinnaker`
without `gitea` fails immediately with a missing-dependency error.

Profile sections are independently optional. In particular, Gitea and WireMock may be
configured separately under `infra`, so a team can use only HTTP mocking without paying
for a writable Git server:

```yaml
infra:
  wiremock:
    image: wiremock/wiremock:3.13.1
```

## Failure behavior and diagnostics

The graph validates duplicate names, unknown dependencies, and cycles before creating
the cluster. During startup, each node transitions through `pending`, `starting`, and
`ready`. A failure is retained as `failed`; later nodes remain `pending`. This state is
captured before teardown in `component-graph.json`, so an installer failure does not
collapse into an ambiguous session-startup error.

Teardown is attempted for the component that failed as well as every component that
started before it. Teardown failures are aggregated, and cluster deletion still runs.
This gives custom components a chance to clean partially created external state without
losing cleanup of other components.

The graph is intentionally sequential in v1. Startup cost is dominated by pulls and
Kubernetes readiness, while deterministic resource pressure is valuable inside the
8 GiB target. Parallel graph layers can be added later without changing the component
contract if measurements show a worthwhile improvement.
