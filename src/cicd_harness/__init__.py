"""Ephemeral CI/CD integration-test harness."""

from importlib.metadata import PackageNotFoundError, version

from cicd_harness.component import ComponentGraph, EnvironmentComponent, EnvironmentContext
from cicd_harness.config import HarnessProfile, load_profile
from cicd_harness.endpoints import (
    EndpointCatalog,
    EndpointKind,
    HostEndpoint,
    HostEndpointManager,
    HostEndpointSpec,
)
from cicd_harness.testing import HarnessRuntime, TestHarness

try:
    __version__ = version("cicd-test-harness")
except PackageNotFoundError:  # source tree imported without installing the project
    __version__ = "0+unknown"

__all__ = [
    "ComponentGraph",
    "EnvironmentComponent",
    "EnvironmentContext",
    "EndpointCatalog",
    "EndpointKind",
    "HarnessProfile",
    "HarnessRuntime",
    "HostEndpoint",
    "HostEndpointManager",
    "HostEndpointSpec",
    "TestHarness",
    "load_profile",
    "__version__",
]
