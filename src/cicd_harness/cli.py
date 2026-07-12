from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from cicd_harness import __version__
from cicd_harness.command import CommandRunner
from cicd_harness.component import ComponentGraph
from cicd_harness.components import default_components, select_components
from cicd_harness.config import HarnessProfile, load_profile_argument
from cicd_harness.endpoints import EndpointCatalog, HostEndpoint, HostEndpointManager
from cicd_harness.environment import HarnessEnvironment
from cicd_harness.errors import HarnessError
from cicd_harness.kind import KindCluster
from cicd_harness.kubectl import Kubectl
from cicd_harness.native_pilot import NativePilotBuilder
from cicd_harness.registry import RegistrySupport
from cicd_harness.tooling import ensure_kind_binary


def _workspace() -> Path:
    return Path.cwd()


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
    try:
        _main()
    except (HarnessError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _main() -> None:
    parser = argparse.ArgumentParser(prog="cicd-harness")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("profile")
    doctor_parser.add_argument(
        "--prepare",
        action="store_true",
        help="download and verify the profile-pinned Kind binary",
    )
    image_parser = subparsers.add_parser("image")
    image_subparsers = image_parser.add_subparsers(dest="image_command", required=True)
    image_build_parser = image_subparsers.add_parser("build")
    image_build_parser.add_argument("profile")
    image_build_parser.add_argument("recipe", choices=("istio-pilot-arm64",))
    image_build_parser.add_argument(
        "--runtime",
        choices=("docker", "podman"),
        help="override the profile container runtime",
    )
    image_build_parser.add_argument(
        "--builder-platform",
        choices=("linux/amd64", "linux/arm64"),
        help="override the platform used to run the pinned Go builder",
    )
    image_build_parser.add_argument("--tag", help="override the output image reference")
    image_build_parser.add_argument("--force", action="store_true")
    image_build_parser.add_argument("--push", action="store_true")

    args = parser.parse_args()
    workspace = _workspace()
    if args.command == "profile":
        profile = load_profile_argument(args.name, workspace=workspace)
        print(json.dumps(profile.model_dump(mode="json"), indent=2))
        return

    profile_name = args.profile
    profile = load_profile_argument(profile_name, workspace=workspace)
    cluster_name = getattr(args, "cluster_name", None)
    if cluster_name:
        profile = profile.model_copy(
            update={"kind": profile.kind.model_copy(update={"cluster_name": cluster_name})}
        )
    runtime_override = getattr(args, "runtime", None)
    if runtime_override:
        profile = profile.model_copy(
            update={"runtime": profile.runtime.model_copy(update={"provider": runtime_override})}
        )
    runner = CommandRunner(cwd=workspace)
    if args.command == "image":
        native_pilot = profile.istio.arm64_pilot if profile.istio is not None else None
        if native_pilot is None:
            raise HarnessError(f"profile {profile.name!r} has no ARM64 pilot image recipe")
        if args.tag:
            native_pilot = native_pilot.model_copy(update={"image": args.tag})
        registry = RegistrySupport(profile, runner)
        registry.install_runtime_auth(profile.runtime.provider)
        try:
            builder = NativePilotBuilder(
                profile,
                native_pilot,
                runner,
                registry,
                builder_platform=args.builder_platform,
            )
            image = builder.build(force=args.force)
            if args.push:
                builder.push()
            print(image)
        finally:
            registry.close()
        return
    if args.command == "doctor":
        checks: list[tuple[str, bool, str]] = []
        for executable in (profile.runtime.provider, "kubectl", "helm", "git"):
            path = shutil.which(executable)
            checks.append(
                (
                    executable,
                    path is not None,
                    path or "not found on PATH",
                )
            )
        runtime = runner.run(
            [profile.runtime.provider, "info"],
            check=False,
            timeout=20,
        ) if shutil.which(profile.runtime.provider) else None
        if runtime is not None:
            checks.append(
                (
                    f"{profile.runtime.provider} connection",
                    runtime.returncode == 0,
                    "available" if runtime.returncode == 0 else "runtime is not reachable",
                )
            )
        if args.prepare:
            binary = ensure_kind_binary(profile.kind)
            checks.append(("kind", True, f"verified at {binary}"))
        elif profile.kind.binary.is_file():
            checks.append(("kind", True, f"cached at {profile.kind.binary}"))
        else:
            checks.append(
                (
                    "kind",
                    True,
                    f"will download and verify v{profile.kind.version} on first use",
                )
            )
        width = max(len(name) for name, _, _ in checks)
        for name, passed, detail in checks:
            print(f"{'ok' if passed else 'missing':<7} {name:<{width}}  {detail}")
        failures = [name for name, passed, _ in checks if not passed]
        if failures:
            raise HarnessError("environment checks failed: " + ", ".join(failures))
        print(
            f"ok      {'memory budget':<{width}}  "
            f"{profile.runtime.memory_budget_mib} MiB"
        )
        return
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
