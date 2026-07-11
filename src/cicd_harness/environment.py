from __future__ import annotations

from pathlib import Path

from cicd_harness.command import CommandRunner
from cicd_harness.component import ComponentGraph, EnvironmentComponent, EnvironmentContext
from cicd_harness.components import default_components, select_components
from cicd_harness.config import HarnessProfile
from cicd_harness.kind import KindCluster
from cicd_harness.kubectl import Kubectl
from cicd_harness.registry import RegistrySupport


class HarnessEnvironment:
    """Own the dependency-ordered lifecycle of one disposable profile."""

    def __init__(
        self,
        profile: HarnessProfile,
        *,
        workspace: Path,
        runner: CommandRunner | None = None,
        components: list[EnvironmentComponent] | None = None,
        component_names: set[str] | None = None,
        include_spinnaker: bool = True,
        include_jenkins: bool = True,
    ) -> None:
        self.profile = profile
        self.workspace = workspace
        self.runner = runner or CommandRunner(cwd=workspace)
        self.registry = RegistrySupport(profile, self.runner)
        self.cluster = KindCluster(profile, self.runner, self.registry)
        self.kubectl = Kubectl(self.cluster.context, self.runner)
        self.context = EnvironmentContext(
            profile=profile,
            workspace=workspace,
            runner=self.runner,
            cluster=self.cluster,
            kubectl=self.kubectl,
            registry=self.registry,
        )
        if components is not None and component_names is not None:
            raise ValueError("components and component_names are mutually exclusive")
        if components is not None:
            selected = components
        elif component_names is not None:
            selected = select_components(
                profile,
                component_names,
                include_spinnaker=include_spinnaker,
                include_jenkins=include_jenkins,
            )
        else:
            selected = default_components(
                profile,
                include_spinnaker=include_spinnaker,
                include_jenkins=include_jenkins,
            )
        self.components = ComponentGraph(selected)

    def up(
        self,
        *,
        timeout: int = 900,
    ) -> None:
        self.cluster.create()
        self.components.start(self.context, timeout=timeout)

    def down(self) -> None:
        component_error: Exception | None = None
        try:
            self.components.stop(self.context)
        except Exception as exc:
            component_error = exc
        finally:
            try:
                self.cluster.delete()
            finally:
                self.registry.close()
        if component_error is not None:
            raise component_error
