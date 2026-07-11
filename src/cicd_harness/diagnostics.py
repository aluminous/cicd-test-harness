from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cicd_harness.kubectl import Kubectl

DiagnosticSource = Callable[[], Any]


class DiagnosticCollector:
    """Best-effort failure evidence that never masks the original test error."""

    _probes: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("cluster-info.txt", ("cluster-info",)),
        ("nodes.txt", ("get", "nodes", "-o", "wide")),
        ("pods.txt", ("get", "pods", "-A", "-o", "wide")),
        (
            "workloads.txt",
            ("get", "deployments,statefulsets,daemonsets", "-A", "-o", "wide"),
        ),
        ("events.txt", ("get", "events", "-A", "--sort-by=.lastTimestamp")),
        ("pod-descriptions.txt", ("describe", "pods", "-A")),
        ("rollouts.yaml", ("get", "rollout.argoproj.io", "-A", "-o", "yaml")),
        ("replica-sets.yaml", ("get", "replicasets", "-A", "-o", "yaml")),
        (
            "virtual-services.yaml",
            ("get", "virtualservice.networking.istio.io", "-A", "-o", "yaml"),
        ),
    )

    def __init__(
        self,
        kubectl: Kubectl,
        root: Path,
        *,
        max_file_bytes: int = 2 * 1024 * 1024,
        secrets: tuple[str, ...] = ("harness-password",),
    ) -> None:
        self.kubectl = kubectl
        self.root = root
        self.max_file_bytes = max_file_bytes
        self.secrets = tuple(secret for secret in secrets if secret)

    def collect(
        self,
        label: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        sources: Mapping[str, DiagnosticSource] | None = None,
    ) -> Path:
        destination = self._destination(label)
        destination.mkdir(parents=True, exist_ok=False)
        summary: dict[str, Any] = {
            "collectedAt": datetime.now(UTC).isoformat(),
            "context": self.kubectl.context,
            "metadata": dict(metadata or {}),
            "files": [],
            "errors": [],
        }

        for filename, args in self._probes:
            try:
                result = self.kubectl.runner.run(
                    self.kubectl.command(*args),
                    check=False,
                    timeout=30,
                )
                output = result.stdout
                if result.stderr:
                    output += f"\n--- stderr ---\n{result.stderr}"
                if result.returncode:
                    output += f"\n--- exit code: {result.returncode} ---\n"
                    summary["errors"].append(
                        {"artifact": filename, "exitCode": result.returncode}
                    )
                self._write(destination / filename, output)
                summary["files"].append(filename)
            except Exception as exc:  # diagnostics must preserve the primary failure
                self._record_error(destination, summary, filename, exc)

        self._collect_pod_logs(destination, summary)
        for name, source in (sources or {}).items():
            filename = f"{_safe_name(name)}.json"
            try:
                payload = source()
                self._write(destination / filename, json.dumps(payload, indent=2, default=str))
                summary["files"].append(filename)
            except Exception as exc:  # diagnostics must preserve the primary failure
                self._record_error(destination, summary, filename, exc)

        self._write(destination / "summary.json", json.dumps(summary, indent=2, default=str))
        return destination

    def _collect_pod_logs(self, destination: Path, summary: dict[str, Any]) -> None:
        try:
            result = self.kubectl.runner.run(
                self.kubectl.command("get", "pods", "-A", "-o", "json"),
                check=False,
                timeout=30,
            )
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout or "could not list pods")
            pods = json.loads(result.stdout).get("items", [])
        except Exception as exc:  # diagnostics must preserve the primary failure
            self._record_error(destination, summary, "pod-logs", exc)
            return

        logs = destination / "pod-logs"
        logs.mkdir(exist_ok=True)
        for pod in pods:
            metadata = pod.get("metadata", {})
            namespace = str(metadata.get("namespace", "default"))
            pod_name = str(metadata.get("name", "unknown"))
            spec = pod.get("spec", {})
            statuses = {
                status.get("name"): status
                for status in (
                    pod.get("status", {}).get("initContainerStatuses", [])
                    + pod.get("status", {}).get("containerStatuses", [])
                )
            }
            containers = spec.get("initContainers", []) + spec.get("containers", [])
            for container in containers:
                container_name = str(container.get("name", "unknown"))
                stem = _safe_name(f"{namespace}__{pod_name}__{container_name}")
                self._collect_one_log(
                    logs / f"{stem}.log",
                    namespace,
                    pod_name,
                    container_name,
                    summary,
                )
                if int(statuses.get(container_name, {}).get("restartCount", 0)) > 0:
                    self._collect_one_log(
                        logs / f"{stem}__previous.log",
                        namespace,
                        pod_name,
                        container_name,
                        summary,
                        previous=True,
                    )

    def _collect_one_log(
        self,
        path: Path,
        namespace: str,
        pod: str,
        container: str,
        summary: dict[str, Any],
        *,
        previous: bool = False,
    ) -> None:
        args = [
            "logs",
            pod,
            "-n",
            namespace,
            "-c",
            container,
            "--tail=2000",
            "--timestamps=true",
        ]
        if previous:
            args.append("--previous")
        relative = str(path.relative_to(path.parents[1]))
        try:
            result = self.kubectl.runner.run(
                self.kubectl.command(*args),
                check=False,
                timeout=30,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n--- stderr ---\n{result.stderr}"
            if result.returncode:
                summary["errors"].append(
                    {"artifact": relative, "exitCode": result.returncode}
                )
            self._write(path, output)
            summary["files"].append(relative)
        except Exception as exc:  # diagnostics must preserve the primary failure
            self._record_error(path.parent, summary, path.name, exc)

    def _record_error(
        self,
        destination: Path,
        summary: dict[str, Any],
        artifact: str,
        exc: Exception,
    ) -> None:
        error_name = f"{_safe_name(artifact)}.error.txt"
        self._write(destination / error_name, f"{type(exc).__name__}: {exc}\n")
        summary["errors"].append({"artifact": artifact, "error": str(exc)})
        summary["files"].append(error_name)

    def _destination(self, label: str) -> Path:
        candidate = self.root / _safe_name(label)
        suffix = 1
        while candidate.exists():
            suffix += 1
            candidate = self.root / f"{_safe_name(label)}-{suffix}"
        return candidate

    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self._redact(content)
        encoded = content.encode(errors="replace")
        if len(encoded) > self.max_file_bytes:
            half = self.max_file_bytes // 2
            encoded = (
                encoded[:half]
                + b"\n\n--- diagnostic truncated ---\n\n"
                + encoded[-half:]
            )
        path.write_bytes(encoded)

    def _redact(self, content: str) -> str:
        redacted = re.sub(
            r"(https?://)([^/@\s:]+):([^/@\s]+)@",
            r"\1***:***@",
            content,
        )
        for secret in self.secrets:
            redacted = redacted.replace(secret, "***")
        return redacted


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-._")
    return normalized[:160] or "diagnostics"
