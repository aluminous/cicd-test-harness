# OSS preview release checklist

## Repository owner decisions

- Verify the repository links in `[project.urls]` before publishing package metadata.
- Confirm that `cicd-test-harness` is the intended public package name and reserve it on
  the chosen package index.
- Confirm the copyright holder shown in `LICENSE` and review the third-party notice list.
- Enable private security advisories, branch protection, and dependency update automation
  on the hosting platform.
- Decide whether the project needs a community code of conduct before accepting broad
  external contributions.

## Release validation

```bash
uv lock --check
uv sync --frozen --extra dev
uv run ruff check src tests
uv run pytest -q
uv run cicd-harness --log-level WARNING airgap check airgap-modern.example
uv run cicd-harness --log-level WARNING airgap check airgap-legacy.example
uv build
```

Install the wheel into an empty environment and verify that package-owned profiles and
assets are used rather than files from the checkout:

```bash
uv venv /tmp/cicd-harness-preview
uv pip install --python /tmp/cicd-harness-preview/bin/python dist/*.whl
cd /tmp
/tmp/cicd-harness-preview/bin/cicd-harness profile show modern
/tmp/cicd-harness-preview/bin/cicd-harness endpoints modern --components wiremock,gitea
```

Before tagging, run at least the reduced Gitea/WireMock/Jenkins live lane and one real
Spinnaker deployment on the intended release platform. The fast GitHub workflow does not
create privileged DinD infrastructure.

For changes to air-gap handling, also run one stack from the built wheel in a CI
container/VM with public egress denied. Use the organization's real OCI/Nexus endpoints,
build and push the fingerprinted Jenkins image first, and retain the JSON dependency plan
plus `--log-file` output as release evidence. Static preflight tests cannot prove that a
test-authored Jenkinsfile or Spinnaker pipeline avoids arbitrary public URLs.

For destination-inventory changes, also retain a packet capture from a cold-cache run.
Compare DNS query names and connection destinations with `airgap check --json`; DNS alone
does not cover cached names or literal IP addresses. For private-CA changes, exercise a
real TLS registry/Nexus endpoint plus an HTTPS WireMock origin and Spinnaker artifact.

For a release that changes ARM64 compatibility, also build the legacy pilot recipe with
both supported runtime command paths where available, inspect its embedded license and
`BUILD-METADATA.json`, and manually publish the GHCR image only after reviewing the
fidelity statement in `docs/arm64-compatibility.md`.

## Publish

- Update `CHANGELOG.md` and the project version.
- Build from a clean tree and inspect wheel metadata, licenses, and bundled assets.
- Create a signed `v0.1.0` tag and a hosting-platform release containing the wheel and
  source distribution.
- Publish through a trusted publisher/OIDC integration rather than a long-lived package
  index token.
- Install the published artifact in an empty project and repeat the profile/endpoint
  smoke test.
