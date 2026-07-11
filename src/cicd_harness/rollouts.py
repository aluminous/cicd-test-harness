from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from cicd_harness.errors import ReadinessError
from cicd_harness.kubectl import Kubectl


@dataclass(frozen=True)
class ReplicaSetState:
    name: str
    pod_hash: str | None
    desired: int
    ready: int
    role: str
    scale_down_deadline: str | None


class RolloutProbe:
    def __init__(self, kubectl: Kubectl, namespace: str, name: str) -> None:
        self.kubectl = kubectl
        self.namespace = namespace
        self.name = name

    def get(self) -> dict[str, Any]:
        return self.kubectl.get_json(
            f"rollout.argoproj.io/{self.name}",
            "-n",
            self.namespace,
        )

    def wait_for(self, predicate: Any, *, description: str, timeout: float = 180) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.get()
            if predicate(last):
                return last
            time.sleep(0.5)
        raise ReadinessError(f"rollout did not reach {description}; last object: {last}")

    def wait_healthy(self, *, timeout: float = 180) -> dict[str, Any]:
        return self.wait_for(
            lambda rollout: rollout.get("status", {}).get("phase") == "Healthy",
            description="Healthy",
            timeout=timeout,
        )

    def wait_paused(self, *, timeout: float = 180) -> dict[str, Any]:
        return self.wait_for(
            lambda rollout: bool(rollout.get("status", {}).get("pauseConditions")),
            description="a canary pause",
            timeout=timeout,
        )

    def replica_sets(self) -> list[ReplicaSetState]:
        rollout = self.get()
        status = rollout.get("status", {})
        stable_hash = status.get("stableRS")
        current_hash = status.get("currentPodHash")
        payload = self.kubectl.get_json(
            "replicasets",
            "-n",
            self.namespace,
        )
        states: list[ReplicaSetState] = []
        for item in payload.get("items", []):
            metadata = item.get("metadata", {})
            owners = metadata.get("ownerReferences", [])
            if not any(
                owner.get("kind") == "Rollout" and owner.get("name") == self.name
                for owner in owners
            ):
                continue
            spec = item.get("spec", {})
            rs_status = item.get("status", {})
            pod_hash = metadata.get("labels", {}).get("rollouts-pod-template-hash")
            role = "old"
            if pod_hash == stable_hash:
                role = "stable"
            if current_hash != stable_hash and pod_hash == current_hash:
                role = "canary"
            states.append(
                # Argo Rollouts 1.8 uses the unqualified key while older
                # releases used a qualified annotation.
                # Keep both so the same probe works for both test profiles.
                ReplicaSetState(
                    name=metadata["name"],
                    pod_hash=pod_hash,
                    desired=int(spec.get("replicas", 0)),
                    ready=int(rs_status.get("readyReplicas", 0)),
                    role=role,
                    scale_down_deadline=(
                        metadata.get("annotations", {}).get("scale-down-deadline")
                        or metadata.get("annotations", {}).get(
                            "argo-rollouts.argoproj.io/scale-down-deadline"
                        )
                    ),
                )
            )
        return states
