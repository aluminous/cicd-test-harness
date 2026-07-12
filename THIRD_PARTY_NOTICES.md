# Third-party notices

This repository vendors deployment metadata so test environments remain reproducible and
do not depend on mutable installation URLs at runtime.

The following vendored material is licensed under the Apache License 2.0. A copy is
included at [`vendor/LICENSES/Apache-2.0.txt`](vendor/LICENSES/Apache-2.0.txt).

| Material | Version | Upstream |
|---|---:|---|
| Argo Rollouts installation manifests | 1.4.1, 1.8.3 | <https://github.com/argoproj/argo-rollouts> |
| Istio Helm charts and generated manifests | 1.10.6, 1.25.5 | <https://github.com/istio/istio> |
| Spinnaker BOM metadata | 1.25.4 | <https://github.com/spinnaker/spinnaker> |

Container images referenced by profiles are not redistributed by this Python package.
They are fetched from their named registries when a user starts an environment and remain
subject to their respective upstream licenses.
