import json
from pathlib import Path
from typing import Any

from cicd_harness.command import CommandResult
from cicd_harness.diagnostics import DiagnosticCollector
from cicd_harness.kubectl import Kubectl


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: list[str], **_: Any) -> CommandResult:
        normalized = tuple(argv)
        self.calls.append(normalized)
        rendered = " ".join(normalized)
        if "get pods -A -o json" in rendered:
            stdout = json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"namespace": "payments", "name": "api-123"},
                            "spec": {"containers": [{"name": "api"}]},
                            "status": {
                                "containerStatuses": [{"name": "api", "restartCount": 1}]
                            },
                        }
                    ]
                }
            )
            return CommandResult(normalized, 0, stdout, "")
        if " logs " in f" {rendered} ":
            return CommandResult(
                normalized,
                0,
                "clone http://harness:harness-password@gitea/repository.git\n",
                "",
            )
        if "virtualservice.networking.istio.io" in rendered:
            return CommandResult(normalized, 1, "", "resource unavailable")
        return CommandResult(normalized, 0, f"evidence for {rendered}\n", "")


def test_collector_captures_cluster_logs_sources_and_redacts(tmp_path: Path) -> None:
    runner = FakeRunner()
    kubectl = Kubectl("kind-test", runner)  # type: ignore[arg-type]
    collector = DiagnosticCollector(kubectl, tmp_path)

    destination = collector.collect(
        "tests/test_app.py::test_deploy",
        metadata={"profile": "modern"},
        sources={
            "wiremock": lambda: {
                "url": "http://harness:harness-password@wiremock/request"
            }
        },
    )

    summary = json.loads((destination / "summary.json").read_text())
    assert summary["metadata"] == {"profile": "modern"}
    assert (destination / "rollouts.yaml").exists()
    assert (destination / "pod-logs/payments__api-123__api.log").exists()
    assert (destination / "pod-logs/payments__api-123__api__previous.log").exists()
    assert summary["errors"] == [{"artifact": "virtual-services.yaml", "exitCode": 1}]
    assert "harness-password" not in (destination / "wiremock.json").read_text()
    assert "http://***:***@" in (destination / "wiremock.json").read_text()
    assert "harness-password" not in (
        destination / "pod-logs/payments__api-123__api.log"
    ).read_text()


def test_collector_records_optional_source_failure_without_raising(tmp_path: Path) -> None:
    runner = FakeRunner()
    kubectl = Kubectl("kind-test", runner)  # type: ignore[arg-type]
    collector = DiagnosticCollector(kubectl, tmp_path)

    def broken_source() -> dict[str, Any]:
        raise RuntimeError("service already unavailable")

    destination = collector.collect("failure", sources={"jenkins": broken_source})
    summary = json.loads((destination / "summary.json").read_text())

    assert any(error["artifact"] == "jenkins.json" for error in summary["errors"])
    assert (destination / "jenkins.json.error.txt").exists()
