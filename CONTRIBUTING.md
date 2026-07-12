# Contributing

Thanks for helping improve the CI/CD test harness. This is an alpha preview, so focused
bug reports, compatibility results, and small composable changes are especially useful.

## Development setup

Prerequisites are Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The fast
test suite does not require Kubernetes or a container runtime.

```bash
uv sync --extra dev
uv run ruff check src tests
uv run pytest
uv build
```

Infrastructure tests are opt-in because they create Kind clusters and may download large
container images:

```bash
CICD_RUN_POC=1 uv run pytest -m poc -s --cicd-components wiremock,gitea
```

Use an exact component subset where possible. Full Spinnaker runs are slow and should be
reserved for changes that affect its lifecycle or pipeline behavior.

## Change guidelines

- Keep lifecycle composition generic, but keep Jenkins, Git, Spinnaker, Rollouts, and
  WireMock operations concrete.
- Preserve user-owned files and unrelated state. Test infrastructure must remain
  disposable and explicitly scoped.
- Add unit coverage for behavior and proportionate live coverage for infrastructure
  boundaries.
- Record expensive compatibility discoveries in `docs/engineering-notes.md`.
- Do not commit credentials, diagnostics, downloaded tools, virtual environments, or
  built distributions.
- Update `CHANGELOG.md` for user-visible behavior.

By submitting a contribution, you agree that it may be distributed under the repository's
MIT License.
