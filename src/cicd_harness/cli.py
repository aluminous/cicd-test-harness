from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from cicd_harness.command import CommandRunner
from cicd_harness.component import ComponentGraph
from cicd_harness.components import default_components, select_components
from cicd_harness.config import HarnessProfile, load_profile
from cicd_harness.endpoints import EndpointCatalog, HostEndpoint, HostEndpointManager
from cicd_harness.environment import HarnessEnvironment
from cicd_harness.kind import KindCluster
from cicd_harness.kubectl import Kubectl


def _workspace() -> Path:
    return Path.cwd()


def _profile_path(workspace: Path, value: str) -> Path:
    requested = Path(value)
    if requested.is_absolute():
        return requested
    named = workspace / "profiles" / f"{value}.yaml"
    return named if named.exists() else workspace / requested


def _component_names(value: str | None) -> set[str] | None:
    if value is None:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


def _component_graph(profile: HarnessProfile, names: set[str] | None) -> ComponentGraph:
    selected = (
        select_components(profile, names)
        if names is not None
        else default_components(profile)
    )
    return ComponentGraph(selected)


def _endpoint_rows(endpoints: tuple[HostEndpoint, ...]) -> str:
    headings = ("NAME", "KIND", "HOST URL", "TARGET", "AUTHENTICATION")
    rows = [
        (
            endpoint.name,
            endpoint.kind.value,
            endpoint.url,
            (
                f"{endpoint.spec.namespace}/service/{endpoint.spec.service}:"
                f"{endpoint.spec.port}"
            ),
            endpoint.spec.authentication,
        )
        for endpoint in endpoints
    ]
    widths = [
        max(len(headings[index]), *(len(row[index]) for row in rows))
        for index in range(len(headings))
    ]
    return "\n".join(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in (headings, *rows)
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="cicd-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile_parser = subparsers.add_parser("profile")
    profile_subparsers = profile_parser.add_subparsers(dest="profile_command", required=True)
    show_parser = profile_subparsers.add_parser("show")
    show_parser.add_argument("name")

    up_parser = subparsers.add_parser("cluster-up")
    up_parser.add_argument("profile")
    down_parser = subparsers.add_parser("cluster-down")
    down_parser.add_argument("profile")
    down_parser.add_argument(
        "--cluster-name",
        help="override the profile cluster name (for a preserved pytest environment)",
    )
    stack_up_parser = subparsers.add_parser("stack-up")
    stack_up_parser.add_argument("profile")
    stack_up_parser.add_argument(
        "--components",
        help="comma-separated component names (default: every configured component)",
    )
    stack_up_parser.add_argument("--without-spinnaker", action="store_true")
    stack_up_parser.add_argument("--without-jenkins", action="store_true")
    stack_down_parser = subparsers.add_parser("stack-down")
    stack_down_parser.add_argument("profile")
    stack_down_parser.add_argument(
        "--cluster-name",
        help="override the profile cluster name (for a preserved pytest environment)",
    )
    endpoints_parser = subparsers.add_parser("endpoints")
    endpoints_parser.add_argument("profile")
    endpoints_parser.add_argument(
        "--components",
        help="comma-separated component names (default: every configured component)",
    )
    endpoints_parser.add_argument("--json", action="store_true")
    expose_parser = subparsers.add_parser("expose")
    expose_parser.add_argument("profile")
    expose_parser.add_argument("endpoints", nargs="*")
    expose_parser.add_argument(
        "--components",
        help="comma-separated component names (default: every configured component)",
    )
    expose_parser.add_argument(
        "--context",
        help="existing kube context (default: profile's Kind context)",
    )
    expose_parser.add_argument(
        "--all",
        action="store_true",
        help="expose deep-debug endpoints as well as the default catalog",
    )

    args = parser.parse_args()
    workspace = _workspace()
    if args.command == "profile":
        profile = load_profile(_profile_path(workspace, args.name), workspace=workspace)
        print(json.dumps(profile.model_dump(mode="json"), indent=2))
        return

    profile_name = args.profile
    profile = load_profile(_profile_path(workspace, profile_name), workspace=workspace)
    cluster_name = getattr(args, "cluster_name", None)
    if cluster_name:
        profile = profile.model_copy(
            update={"kind": profile.kind.model_copy(update={"cluster_name": cluster_name})}
        )
    runner = CommandRunner(cwd=workspace)
    if args.command in {"endpoints", "expose"}:
        graph = _component_graph(profile, _component_names(args.components))
        catalog = EndpointCatalog(graph)
        if args.command == "endpoints":
            if args.json:
                print(json.dumps(catalog.snapshot(), indent=2))
            else:
                for endpoint in catalog.list():
                    default = "default" if endpoint.default else "on-demand"
                    print(
                        f"{endpoint.name:<26} {endpoint.kind.value:<8} {default:<9} "
                        f"{endpoint.namespace}/service/{endpoint.service}:{endpoint.port} "
                        f"- {endpoint.description}"
                    )
            return
        context = args.context or f"kind-{profile.kind.cluster_name}"
        kubectl = Kubectl(context, runner)
        runner.run(kubectl.command("get", "--raw=/readyz"), timeout=15)
        manager = HostEndpointManager(catalog, kubectl)
        selected = catalog.names if args.all else (tuple(args.endpoints) or None)
        try:
            exposed = manager.expose_many(selected)
            print(f"Kubernetes context: {context}")
            print(_endpoint_rows(exposed))
            print("Press Ctrl-C to close host endpoints; the cluster will remain running.")
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
        finally:
            manager.close()

    cluster = KindCluster(profile, runner)
    if args.command == "cluster-up":
        cluster.create()
        print(cluster.context)
    elif args.command == "cluster-down":
        cluster.delete()
    elif args.command == "stack-up":
        environment = HarnessEnvironment(
            profile,
            workspace=workspace,
            runner=runner,
            component_names=_component_names(args.components),
            include_spinnaker=not args.without_spinnaker,
            include_jenkins=not args.without_jenkins,
        )
        environment.up()
        print(environment.cluster.context)
        print(f"Host endpoint catalog: cicd-harness endpoints {profile_name}")
    elif args.command == "stack-down":
        HarnessEnvironment(profile, workspace=workspace, runner=runner).down()


if __name__ == "__main__":
    main()
