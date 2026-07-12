# Image recipes

Image recipes are runtime assets and are included in both the source distribution and
wheel under `cicd_harness/_assets/images`.

| Recipe | Purpose | Build path |
|---|---|---|
| `istio-pilot-arm64/` | Exact-source Istio 1.10.6 pilot for the legacy ARM64 lane | `cicd-harness image build legacy istio-pilot-arm64` |
| `jenkins/` | Jenkins 2.426.1 plus the pinned Pipeline/shared-library plugin closure | Built automatically by the Jenkins component |

The ARM64 pilot image is a real control-plane binary but is not a complete Istio
distribution. Read [`docs/arm64-compatibility.md`](../docs/arm64-compatibility.md) before
using or publishing it.
