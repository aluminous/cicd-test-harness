from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from cicd_harness.command import CommandRunner


@dataclass(frozen=True)
class GiteaRepository:
    owner: str
    name: str
    internal_base_url: str = "http://gitea.harness-system.svc.cluster.local:3000"

    @property
    def clone_url(self) -> str:
        return f"{self.internal_base_url}/{self.owner}/{self.name}.git"

    def raw_commit_url(self, commit: str, path: str) -> str:
        return f"{self.internal_base_url}/{self.owner}/{self.name}/raw/commit/{commit}/{path}"


class GiteaClient:
    def __init__(
        self,
        base_url: str,
        *,
        username: str = "harness",
        password: str = "harness-password",
    ) -> None:
        self.username = username
        self.password = password
        self._client = httpx.Client(
            base_url=base_url,
            auth=(username, password),
            timeout=30,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GiteaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_repository(self, name: str, *, private: bool = False) -> GiteaRepository:
        response = self._client.post(
            "/api/v1/user/repos",
            json={
                "name": name,
                "default_branch": "main",
                "private": private,
                "auto_init": False,
            },
        )
        if response.status_code not in {201, 409, 422}:
            response.raise_for_status()
        return GiteaRepository(owner=self.username, name=name)

    def host_clone_url(self, repository: GiteaRepository) -> str:
        host = str(self._client.base_url).rstrip("/")
        return (
            f"{host.replace('://', f'://{self.username}:{self.password}@', 1)}"
            f"/{repository.owner}/{repository.name}.git"
        )


class GitWorkspace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.runner = CommandRunner(cwd=path)

    def initialize(self) -> None:
        self.runner.run(["git", "init", "--initial-branch=main"])
        self.runner.run(["git", "config", "user.name", "Harness"])
        self.runner.run(["git", "config", "user.email", "harness@example.invalid"])

    def commit(self, message: str) -> str:
        self.runner.run(["git", "add", "."])
        self.runner.run(["git", "commit", "-m", message])
        return self.runner.run(["git", "rev-parse", "HEAD"]).stdout.strip()

    def add_remote(self, url: str) -> None:
        self.runner.run(["git", "remote", "add", "origin", url])

    def push(self, *, branch: str = "main") -> None:
        self.runner.run(["git", "push", "--set-upstream", "origin", branch])
