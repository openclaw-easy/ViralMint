# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The cookie jar FILE: cleaning it, counting it, keeping it private.

yt-dlp's Netscape writer sometimes emits Chrome's native expiry (microseconds
since 1601) where the format wants Unix seconds; an on-disk audit found 97.8%
of lines that way, and yt-dlp's own reader then rejects the WHOLE file
("invalid Netscape format cookies file"). The lines are real cookies in the
wrong unit, so they are converted, not dropped.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_PLAUSIBLE_UNIX_MAX = 7_258_118_400                          # year 2200, Unix seconds
_CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000         # 1601 → 1970, microseconds
_PLAUSIBLE_CHROME_MAX = _CHROME_EPOCH_OFFSET_US + _PLAUSIBLE_UNIX_MAX * 1_000_000


def harden_permissions(path: Path) -> None:
    """0600: the jar holds live sessions for every site the person is signed in to."""
    try:
        path.chmod(0o600)
    except OSError as e:
        logger.debug("Couldn't chmod cookies file to 0600: %s", e)


def atomic_write(path: Path, content: str) -> None:
    """Temp file + rename, private before it is visible under its real name."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content)
        harden_permissions(tmp)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _is_data_line(stripped: str) -> bool:
    # `#HttpOnly_<domain>` is a cookie line: the prefix is part of the domain.
    return bool(stripped) and (not stripped.startswith("#") or stripped.startswith("#HttpOnly_"))


def count_cookies(path: Path) -> int:
    """Well-formed cookie lines in the jar; 0 when it can't be read."""
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines()
               if _is_data_line(line.strip()) and line.count("\t") == 6)


def normalize_expiry(raw: str) -> str | None:
    """A valid Unix-seconds expiry for `raw`, or None when it can't be recovered."""
    try:
        v = int(raw)
    except ValueError:
        return None
    if v == 0:
        return "0"                                           # session cookie
    if 0 < v <= _PLAUSIBLE_UNIX_MAX:
        return str(v)
    if _CHROME_EPOCH_OFFSET_US <= v <= _PLAUSIBLE_CHROME_MAX:
        return str((v - _CHROME_EPOCH_OFFSET_US) // 1_000_000)
    return None


def sanitize(path: Path) -> int:
    """Fix every expiry in place and drop lines that can't be fixed. Returns
    the number dropped. Keeps the file's modification time: the jar's age is
    when it was EXTRACTED, and cleaning it must not make it look fresh."""
    try:
        stat = path.stat()
        original = path.read_text(errors="ignore")
    except OSError:
        return 0

    dropped = converted = 0
    out: list[str] = []
    for raw in original.splitlines():
        if not _is_data_line(raw.strip()):
            out.append(raw)
            continue
        parts = raw.split("\t")
        expiry = normalize_expiry(parts[4]) if len(parts) == 7 else None
        if expiry is None:
            dropped += 1
            continue
        if expiry != parts[4]:
            parts[4] = expiry
            converted += 1
            out.append("\t".join(parts))
        else:
            out.append(raw)

    if dropped or converted:
        atomic_write(path, "\n".join(out) + "\n")
        try:
            os.utime(path, (stat.st_atime, stat.st_mtime))
        except OSError:
            pass
        logger.info("Sanitized %s: converted %d chrome-epoch expirations, dropped %d line(s)",
                    path.name, converted, dropped)
    else:
        harden_permissions(path)
    return dropped
