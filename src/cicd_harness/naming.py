from __future__ import annotations

import re
from uuid import uuid4


def dns_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        normalized = f"resource-{uuid4().hex[:8]}"
    return normalized[:63].rstrip("-")


def dns_name_with_suffix(value: str, suffix: str) -> str:
    normalized = dns_name(value)
    suffix = dns_name(suffix)
    base = normalized[: 63 - len(suffix) - 1].rstrip("-")
    return f"{base}-{suffix}"
