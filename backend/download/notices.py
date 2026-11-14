# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The one thing a download tells the person outside its own job: a warning,
once per episode, when the browser's cookies can't be read and downloads are
going on without the person's sign-in.
"""
from __future__ import annotations

import logging

from backend.download import cookies

logger = logging.getLogger(__name__)


async def after_download() -> None:
    """Call when a download ends, however it ends."""
    notice = cookies.store().take_notice()
    if not notice:
        return
    try:
        from backend.core.ws_manager import ws_manager
        await ws_manager.send_constraint_warning("browser_cookies_unreadable", notice)
    except Exception as e:  # noqa: BLE001 — a notice must never fail a download
        logger.debug("Cookie access warning not sent (non-fatal): %s", e)
