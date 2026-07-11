from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from cicd_harness.command import CommandRunner
from cicd_harness.errors import ReadinessError


class PortForward:
    def __init__(self, process: subprocess.Popen[str], local_port: int) -> None:
        self.process = process
        self.local_port = local_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def __enter__(self) -> PortForward:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class Kubectl:
    def __init__(self, context: str, runner: CommandRunner, binary: str = "kubectl") -> None:
        self.context = context
        self.runner = runner
        self.binary = binary

    def command(self, *args: str) -> list[str]:
        return [self.binary, "--context", self.context, *args]

    def apply(self, manifest: str, *, namespace: str | None = None) -> None:
        args = ["apply", "-f", "-"]
        if namespace is not None:
            args.extend(("--namespace", namespace))
        self.runner.run(self.command(*args), input_text=manifest)

    def apply_file(self, path: Path, *, namespace: str | None = None) -> None:
        args = ["apply", "-f", str(path)]
        if namespace is not None:
            args.extend(("--namespace", namespace))
        self.runner.run(self.command(*args))

    def delete_file(self, path: Path) -> None:
        self.runner.run(self.command("delete", "-f", path, "--ignore-not-found"), check=False)

    def get_json(self, resource: str, *args: str) -> Any:
        result = self.runner.run(self.command("get", resource, *args, "-o", "json"))
        return json.loads(result.stdout)

    def wait_available(self, deployment: str, namespace: str, timeout: int = 180) -> None:
        self.runner.run(
            self.command(
                "-n",
                namespace,
                "wait",
                f"deployment/{deployment}",
                "--for=condition=Available",
                f"--timeout={timeout}s",
            ),
            timeout=timeout + 10,
        )

    def exec(self, namespace: str, pod: str, *args: str, check: bool = True) -> str:
        result = self.runner.run(
            self.command("-n", namespace, "exec", pod, "--", *args),
            check=check,
        )
        return result.stdout

    def first_pod(self, namespace: str, selector: str) -> str:
        payload = self.get_json("pods", "-n", namespace, "-l", selector)
        items = payload.get("items", [])
        if not items:
            raise ReadinessError(f"no pod found in {namespace} matching {selector}")
        return items[0]["metadata"]["name"]

    def port_forward(
        self,
        namespace: str,
        resource: str,
        remote_port: int,
        *,
        timeout: float = 15,
    ) -> PortForward:
        local_port = _free_port()
        process = self.runner.popen(
            self.command(
                "-n",
                namespace,
                "port-forward",
                resource,
                f"{local_port}:{remote_port}",
                "--address=127.0.0.1",
            )
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise ReadinessError(f"port-forward exited early: {output}")
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=0.2):
                    return PortForward(process, local_port)
            except OSError:
                time.sleep(0.1)
        process.terminate()
        raise ReadinessError(f"port-forward did not open localhost:{local_port}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
