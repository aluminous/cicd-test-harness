from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from cicd_harness.command import CommandRunner
from cicd_harness.config import load_profile
from cicd_harness.infra import InfraStack
from cicd_harness.kind import KindCluster
from cicd_harness.kubectl import Kubectl
from cicd_harness.readiness import wait_for_http
from cicd_harness.wiremock import ResponseSpec, WireMockClient

pytestmark = [
    pytest.mark.poc,
    pytest.mark.skipif(not os.getenv("CICD_RUN_POC"), reason="PoC disabled"),
]


def test_kind_gitea_and_wiremock_round_trip(tmp_path: Path) -> None:
    workspace = Path(__file__).parents[1]
    profile_name = os.getenv("CICD_PROFILE", "modern")
    profile = load_profile(workspace / f"profiles/{profile_name}.yaml", workspace=workspace)
    runner = CommandRunner(cwd=workspace)
    cluster = KindCluster(profile, runner)
    cluster.create()
    kubectl = Kubectl(cluster.context, runner)
    infra = InfraStack(profile, kubectl, workspace)
    infra.install()
    infra.bootstrap_gitea()
    repository_name = f"manifests-{uuid4().hex[:8]}"

    with (
        kubectl.port_forward("harness-system", "service/wiremock", 8080) as wiremock_forward,
        kubectl.port_forward("harness-system", "service/gitea", 3000) as gitea_forward,
    ):
        mocks = WireMockClient(wiremock_forward.url)
        try:
            mocks.reset()
            scanner = mocks.service(
                "scanner",
                host="scanner-mock.harness-system.svc.cluster.local",
            )
            scanner.expect(
                method="POST",
                path="/v1/scans",
                response=ResponseSpec(status=202, json={"scanId": "scan-123"}),
                json_paths={"$.repository": repository_name},
                times=1,
            )
            response = httpx.post(
                f"{wiremock_forward.url}/v1/scans",
                headers={"Host": "scanner-mock.harness-system.svc.cluster.local"},
                json={"repository": repository_name},
            )
            assert response.status_code == 202

            gitea_proxy = mocks.proxy(
                "gitea-proxy",
                host="gitea-proxy.harness-system.svc.cluster.local",
                target="http://gitea.harness-system.svc.cluster.local:3000",
            )
            passed_through = httpx.get(
                f"{wiremock_forward.url}/api/healthz",
                headers={"Host": gitea_proxy.host},
            )
            assert passed_through.status_code == 200
            assert passed_through.json()["status"] == "pass"

            gitea_proxy.intercept(
                method="GET",
                path="/api/healthz",
                response={"status": 503, "json": {"status": "injected-failure"}},
                times=1,
            )
            intercepted = httpx.get(
                f"{wiremock_forward.url}/api/healthz",
                headers={"Host": gitea_proxy.host},
            )
            assert intercepted.status_code == 503
            assert intercepted.json()["status"] == "injected-failure"

            proxy_records = gitea_proxy.records()
            assert len(proxy_records) == 2
            assert [record.proxied for record in proxy_records].count(True) == 1
            assert {record.response_status for record in proxy_records} == {200, 503}
            mocks.verify()
        finally:
            mocks.close()

        create_repo = httpx.post(
            f"{gitea_forward.url}/api/v1/user/repos",
            auth=("harness", "harness-password"),
            json={
                "name": repository_name,
                "default_branch": "main",
                "private": False,
                "auto_init": False,
            },
        )
        assert create_repo.status_code in {201, 409, 422}, create_repo.text

        repository = tmp_path / "repository"
        repository.mkdir()
        git = CommandRunner(cwd=repository)
        git.run(["git", "init", "--initial-branch=main"])
        git.run(["git", "config", "user.name", "Harness"])
        git.run(["git", "config", "user.email", "harness@example.invalid"])
        (repository / "rollout.yaml").write_text(
            "apiVersion: argoproj.io/v1alpha1\nkind: Rollout\n"
        )
        git.run(["git", "add", "rollout.yaml"])
        git.run(["git", "commit", "-m", "Seed rollout"])
        git.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                f"http://harness:harness-password@127.0.0.1:{gitea_forward.local_port}"
                    f"/harness/{repository_name}.git",
            ]
        )
        git.run(["git", "push", "--set-upstream", "origin", "main"])

        raw = wait_for_http(
            f"{gitea_forward.url}/harness/{repository_name}/raw/branch/main/rollout.yaml",
            timeout=10,
        )
        assert "kind: Rollout" in raw.text
