from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from cicd_harness.command import CommandRunner
from cicd_harness.config import HarnessProfile, load_profile
from cicd_harness.kind import KindCluster
from cicd_harness.kubectl import Kubectl

pytest_plugins = ["pytester"]

PocCluster = tuple[Path, HarnessProfile, CommandRunner, KindCluster, Kubectl]


@pytest.fixture
def poc_cluster() -> Iterator[PocCluster]:
    """Give direct PoCs a unique Kind cluster with unconditional teardown."""

    workspace = Path(__file__).parents[1]
    profile_name = os.getenv("CICD_PROFILE", "modern")
    profile = load_profile(
        workspace / f"profiles/{profile_name}.yaml",
        workspace=workspace,
    )
    suffix = f"direct-{uuid4().hex[:8]}"
    base = profile.kind.cluster_name[: 63 - len(suffix) - 1].rstrip("-")
    profile = profile.model_copy(
        update={
            "kind": profile.kind.model_copy(
                update={"cluster_name": f"{base}-{suffix}"}
            )
        }
    )
    runner = CommandRunner(cwd=workspace)
    cluster = KindCluster(profile, runner)
    try:
        cluster.create()
        yield workspace, profile, runner, cluster, Kubectl(cluster.context, runner)
    finally:
        cluster.delete()
