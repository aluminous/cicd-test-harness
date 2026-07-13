from __future__ import annotations

from collections.abc import Mapping


def canonical_image(image: str) -> str:
    first, separator, _ = image.partition("/")
    if not separator:
        return f"docker.io/library/{image}"
    if "." not in first and ":" not in first and first != "localhost":
        return f"docker.io/{image}"
    return image


def canonical_prefix(prefix: str) -> str:
    value = prefix.strip("/")
    first, separator, _ = value.partition("/")
    if not separator and ("." in first or ":" in first or first == "localhost"):
        return value
    if "." not in first and ":" not in first and first != "localhost":
        return f"docker.io/{value}"
    return value


def rewrite_image(image: str, rewrites: Mapping[str, str]) -> str:
    if not rewrites or image == "auto":
        return image
    canonical = canonical_image(image)
    ordered = sorted(
        (
            (canonical_prefix(source), destination.strip("/"))
            for source, destination in rewrites.items()
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for source, destination in ordered:
        if canonical == source:
            return destination
        if canonical.startswith(source) and canonical[len(source) : len(source) + 1] in {
            "/",
            ":",
            "@",
        }:
            return f"{destination}{canonical[len(source):]}"
    return image


def image_registry_host(image: str) -> str:
    return canonical_image(image).partition("/")[0]
