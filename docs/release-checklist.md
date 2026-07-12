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

## Publish

- Update `CHANGELOG.md` and the project version.
- Build from a clean tree and inspect wheel metadata, licenses, and bundled assets.
- Create a signed `v0.1.0` tag and a hosting-platform release containing the wheel and
  source distribution.
- Publish through a trusted publisher/OIDC integration rather than a long-lived package
  index token.
- Install the published artifact in an empty project and repeat the profile/endpoint
  smoke test.
