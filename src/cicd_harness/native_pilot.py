from __future__ import annotations

import hashlib
import json
import platform
import shutil
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
        if self.config.pull_before_build and not self._local_image_exists():
            self.runner.run(
                [self.runtime, "pull", "--platform", "linux/arm64", self.image],
                check=False,
                timeout=600,
            )
        self.build()
        self._load_into_kind()

    @property
    def runtime(self) -> str:
        return self.profile.runtime.provider

    def build(self, *, force: bool = False) -> str:
        """Build the pinned pilot-only linux/arm64 compatibility image."""

        if not force and self._local_image_exists():
            return self.image
        source = self._source_tree()
        self._build_binary(source)
        self._stage_metadata(source)
        self.runner.run(self.build_command(source / "out"), timeout=900)
        return self.image

    def push(self) -> str:
        """Push a previously built image using the configured runtime credentials."""

        if not self._local_image_exists():
            raise HarnessError(f"image is not available locally: {self.image}")
        self.runner.run([self.runtime, "push", self.image], timeout=900)
        return self.image

    def build_command(self, context: Path) -> list[str | Path]:
        return [
            self.runtime,
            "build",
            "--no-cache",
            "--platform",
            "linux/arm64",
            "-t",
            self.image,
            "-f",
            self.config.containerfile,
            context,
        ]

    def helm_values(self) -> tuple[str]:
        # The 1.10 chart accepts a full reference when pilot.image contains a
        # slash. Keep global hub/tag untouched so injection still refers to
        # the official proxy image rather than this pilot-only shim.
        return (f"pilot.image={self.image}",)

    def _local_image_exists(self) -> bool:
        result = self.runner.run(
            [self.runtime, "image", "inspect", self.image],
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
                self.runtime,
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
        self._validate_arm64_elf(source / "out/pilot-discovery")

    def _stage_metadata(self, source: Path) -> None:
        output = source / "out"
        license_path = source / "LICENSE"
        if not license_path.is_file():
            raise HarnessError("Istio source archive does not contain LICENSE")
        shutil.copyfile(license_path, output / "LICENSE")
        metadata = {
            "component": "istio/pilot-discovery",
            "fidelity": "pilot control plane only; no proxyv2 or ingress data plane",
            "git_revision": self.config.git_revision,
            "source_sha256": self.config.source_sha256,
            "source_url": self.config.source_url,
            "target": "linux/arm64",
            "version": self.profile.istio.version,
        }
        (output / "BUILD-METADATA.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )

    def _load_into_kind(self) -> None:
        node = f"{self.profile.kind.cluster_name}-control-plane"
        present = self.runner.run(
            [self.runtime, "exec", node, "crictl", "inspecti", self.image],
            check=False,
            timeout=30,
        )
        if present.returncode == 0:
            return
        image_archive = self.workspace / ".tools/cache/istio-pilot-arm64.tar"
        image_archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.runner.run(
                [self.runtime, "save", "-o", image_archive, self.image],
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
                env={"KIND_EXPERIMENTAL_PROVIDER": self.runtime},
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
    def _validate_arm64_elf(path: Path) -> None:
        header = path.read_bytes()[:20]
        if len(header) < 20 or header[:4] != b"\x7fELF":
            raise HarnessError(f"pilot build did not produce an ELF binary: {path}")
        byte_order = "little" if header[5] == 1 else "big" if header[5] == 2 else None
        if byte_order is None:
            raise HarnessError(
                f"pilot build produced an ELF binary with invalid byte order: {path}"
            )
        machine = int.from_bytes(header[18:20], byte_order)
        if machine != 183:
            raise HarnessError(
                f"pilot build produced ELF machine {machine}; expected AArch64 (183)"
            )

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
