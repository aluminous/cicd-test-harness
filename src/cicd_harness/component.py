from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from cicd_harness.command import CommandRunner
from cicd_harness.config import HarnessProfile
from cicd_harness.errors import HarnessError
from cicd_harness.kind import KindCluster
from cicd_harness.kubectl import Kubectl
from cicd_harness.registry import RegistrySupport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnvironmentContext:
    profile: HarnessProfile
    workspace: Path
    runner: CommandRunner
    cluster: KindCluster
    kubectl: Kubectl
    registry: RegistrySupport


@runtime_checkable
class EnvironmentComponent(Protocol):
    name: str
    dependencies: frozenset[str]

    def start(self, context: EnvironmentContext, *, timeout: int) -> None: ...

    def stop(self, context: EnvironmentContext) -> None: ...


class BaseEnvironmentComponent:
    dependencies: frozenset[str] = frozenset()

    def stop(self, context: EnvironmentContext) -> None:
        """Kubernetes resources are normally removed with the disposable cluster."""


class ComponentState(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"
    STOP_FAILED = "stop-failed"


T = TypeVar("T", bound=EnvironmentComponent)


class ComponentGraph:
    """Validate and run concrete environment components in dependency order."""

    def __init__(self, components: list[EnvironmentComponent]) -> None:
        self._components: dict[str, EnvironmentComponent] = {}
        for component in components:
            if component.name in self._components:
                raise HarnessError(f"duplicate environment component: {component.name}")
            self._components[component.name] = component
        self._order = self._topological_order()
        self._states = {
            name: ComponentState.PENDING
            for name in self._components
        }
        self._attempted: list[str] = []

    @property
    def order(self) -> tuple[str, ...]:
        return self._order

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._components)

    def has(self, name: str) -> bool:
        return name in self._components

    def configured(self, name: str) -> EnvironmentComponent:
        component = self._components.get(name)
        if component is None:
            visible = ", ".join(self._components) or "none"
            raise HarnessError(
                f"environment component {name!r} is not configured; available: {visible}"
            )
        return component

    def state(self, name: str) -> ComponentState:
        if name not in self._states:
            raise HarnessError(f"environment component is not configured: {name}")
        return self._states[name]

    def snapshot(self) -> dict[str, str]:
        return {name: state.value for name, state in self._states.items()}

    def require(self, name: str, expected_type: type[T] | None = None) -> T:
        component = self._components.get(name)
        if component is None:
            visible = ", ".join(self._components) or "none"
            raise HarnessError(
                f"environment component {name!r} is not configured; available: {visible}"
            )
        if self._states[name] != ComponentState.READY:
            raise HarnessError(
                f"environment component {name!r} is {self._states[name].value}, not ready"
            )
        if expected_type is not None and not isinstance(component, expected_type):
            raise HarnessError(
                f"environment component {name!r} is {type(component).__name__}, "
                f"expected {expected_type.__name__}"
            )
        return component  # type: ignore[return-value]

    def start(self, context: EnvironmentContext, *, timeout: int) -> None:
        if self._attempted:
            raise HarnessError("environment component graph has already been started")
        for name in self._order:
            component = self._components[name]
            self._attempted.append(name)
            self._states[name] = ComponentState.STARTING
            started = time.monotonic()
            logger.info("component %s: starting", name)
            try:
                component.start(context, timeout=timeout)
            except Exception as exc:
                self._states[name] = ComponentState.FAILED
                logger.exception(
                    "component %s: failed after %.1fs",
                    name,
                    time.monotonic() - started,
                )
                raise HarnessError(
                    f"environment component {name!r} failed to start: {exc}"
                ) from exc
            self._states[name] = ComponentState.READY
            logger.info("component %s: ready after %.1fs", name, time.monotonic() - started)

    def stop(self, context: EnvironmentContext) -> None:
        errors: list[str] = []
        for name in reversed(self._attempted):
            if self._states[name] == ComponentState.STOPPED:
                continue
            component = self._components[name]
            self._states[name] = ComponentState.STOPPING
            started = time.monotonic()
            logger.info("component %s: stopping", name)
            try:
                component.stop(context)
            except Exception as exc:
                self._states[name] = ComponentState.STOP_FAILED
                logger.exception("component %s: teardown failed", name)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
            else:
                self._states[name] = ComponentState.STOPPED
                logger.info("component %s: stopped after %.1fs", name, time.monotonic() - started)
        if errors:
            raise HarnessError("environment component teardown failed: " + "; ".join(errors))

    def _topological_order(self) -> tuple[str, ...]:
        names = set(self._components)
        for component in self._components.values():
            missing = component.dependencies - names
            if missing:
                rendered = ", ".join(sorted(missing))
                raise HarnessError(
                    f"environment component {component.name!r} has missing dependencies: "
                    f"{rendered}"
                )
        remaining = {
            name: set(component.dependencies)
            for name, component in self._components.items()
        }
        order: list[str] = []
        while remaining:
            ready = [name for name in self._components if name in remaining and not remaining[name]]
            if not ready:
                cycle = ", ".join(name for name in self._components if name in remaining)
                raise HarnessError(f"environment component dependency cycle: {cycle}")
            for name in ready:
                order.append(name)
                remaining.pop(name)
                for dependencies in remaining.values():
                    dependencies.discard(name)
        return tuple(order)
