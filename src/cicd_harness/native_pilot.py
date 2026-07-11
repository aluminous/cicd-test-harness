from __future__ import annotations

import hashlib
import platform
import tarfile
from pathlib import Path

import httpx

from cicd_harness.command import CommandRunner
from cicd_harness.config import HarnessProfile, NativePilotConfig
from cicd_harness.errors import HarnessError
from cicd_harness.registry import RegistrySupport


class NativePilotBuilder:
    """Build the exact legacy pilot source on arm64 development hosts."""

    def __init__(
        self,
        profile: HarnessProfile,
        config: NativePilotConfig,
        runner: CommandRunner,
        registry: RegistrySupport | None = None,
    ) -> None:
        self.profile = profile
        self.config = config
        self.runner = runner
        self.registry = registry or RegistrySupport(profile, runner)
        self.workspace = runner.cwd

    @property
    def image(self) -> str:
        return self.registry.image(self.config.image)

    @staticmethod
    def host_needs_shim() -> bool:
        return platform.machine().lower() in {"arm64", "aarch64"}

    def prepare(self) -> None:
        if not self._local_image_exists():
            source = self._source_tree()
            self._build_binary(source)
            self.runner.run(
                [
                    "podman",
                    "build",
                    "--no-cache",
                    "-t",
                    self.image,
                    "-f",
                    self.config.containerfile,
                    source / "out",
                ],
                timeout=900,
            )
        self._load_into_kind()

    def helm_values(self) -> tuple[str]:
        # The 1.10 chart accepts a full reference when pilot.image contains a
        # slash. Keep global hub/tag untouched so injection still refers to
        # the official proxy image rather than this pilot-only shim.
        return (f"pilot.image={self.image}",)

    def _local_image_exists(self) -> bool:
        result = self.runner.run(
            ["podman", "image", "exists", self.image],
            check=False,
            timeout=30,
        )
        return result.returncode == 0

    def _source_tree(self) -> Path:
        tools = self.workspace / ".tools"
        cache = tools / "cache"
        build = tools / "build"
        cache.mkdir(parents=True, exist_ok=True)
        build.mkdir(parents=True, exist_ok=True)
        archive_path = cache / f"istio-{self.profile.istio.version}.tar.gz"
        if not archive_path.exists() or self._sha256(archive_path) != self.config.source_sha256:
            with httpx.stream(
                "GET",
                self.config.source_url,
                follow_redirects=True,
                timeout=120,
            ) as r:
                r.raise_for_status()
                with archive_path.open("wb") as output:
                    for chunk in r.iter_bytes():
                        output.write(chunk)
        actual = self._sha256(archive_path)
        if actual != self.config.source_sha256:
            raise HarnessError(
                "Istio source checksum mismatch: "
                f"expected {self.config.source_sha256}, got {actual}"
            )

        source = build / f"istio-{self.profile.istio.version}"
        if not (source / "go.mod").exists():
            with tarfile.open(archive_path, "r:gz") as archive:
                self._validate_archive(archive, build)
                archive.extractall(
                    build,
                    filter="fully_trusted",  # paths and links validated above
                )
        return source

    def _build_binary(self, source: Path) -> None:
        (source / "out").mkdir(exist_ok=True)
        module_cache = self.workspace / ".tools/cache/istio-go-mod"
        build_cache = self.workspace / ".tools/cache/istio-go-build"
        module_cache.mkdir(parents=True, exist_ok=True)
        build_cache.mkdir(parents=True, exist_ok=True)
        version = self.profile.istio.version
        ldflags = (
            f"-s -w -X istio.io/pkg/version.buildVersion={version} "
            f"-X istio.io/pkg/version.buildGitRevision={self.config.git_revision} "
            "-X istio.io/pkg/version.buildStatus=Clean "
            f"-X istio.io/pkg/version.buildTag={version} "
            "-X istio.io/pkg/version.buildHub=docker.io/istio"
        )
        command = (
            f'go build -trimpath -ldflags "{ldflags}" '
            "-o /src/out/pilot-discovery ./pilot/cmd/pilot-discovery"
        )
        self.runner.run(
            [
                "podman",
                "run",
                "--rm",
                "--memory=5g",
                "-v",
                f"{source}:/src",
                "-v",
                f"{module_cache}:/go/pkg/mod",
                "-v",
                f"{build_cache}:/root/.cache/go-build",
                "-w",
                "/src",
                "-e",
                "CGO_ENABLED=0",
                "-e",
                "GOOS=linux",
                "-e",
                "GOARCH=arm64",
                self.registry.image(self.config.builder_image),
                "sh",
                "-c",
                command,
            ],
            timeout=1200,
        )

    def _load_into_kind(self) -> None:
        node = f"{self.profile.kind.cluster_name}-control-plane"
        present = self.runner.run(
            ["podman", "exec", node, "crictl", "inspecti", self.image],
            check=False,
            timeout=30,
        )
        if present.returncode == 0:
            return
        image_archive = self.workspace / ".tools/cache/istio-pilot-arm64.tar"
        image_archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.runner.run(
                ["podman", "save", "-o", image_archive, self.image],
                timeout=300,
            )
            self.runner.run(
                [
                    self.profile.kind.binary,
                    "load",
                    "image-archive",
                    image_archive,
                    "--name",
                    self.profile.kind.cluster_name,
                ],
                env={"KIND_EXPERIMENTAL_PROVIDER": "podman"},
                timeout=300,
            )
        finally:
            image_archive.unlink(missing_ok=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validate_archive(archive: tarfile.TarFile, destination: Path) -> None:
        root = destination.resolve()
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise HarnessError(f"unsafe path in Istio source archive: {member.name}")
            if member.isdev():
                raise HarnessError(f"unsupported member in Istio source archive: {member.name}")
            if member.issym():
                link_target = (target.parent / member.linkname).resolve()
                if link_target != root and root not in link_target.parents:
                    raise HarnessError(f"unsafe link in Istio source archive: {member.name}")
            if member.islnk():
                link_target = (destination / member.linkname).resolve()
                if link_target != root and root not in link_target.parents:
                    raise HarnessError(f"unsafe link in Istio source archive: {member.name}")
