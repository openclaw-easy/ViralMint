# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Small shared helpers for the download package."""
from __future__ import annotations

from urllib.parse import urlparse


def host_of(url: str | None) -> str:
    """The URL's host, lowercased, without a port; "unknown" when there is none."""
    if not url:
        return "unknown"
    try:
        host = (urlparse(url).netloc or "").lower().split(":", 1)[0]
    except ValueError:
        return "unknown"
    return host or "unknown"
