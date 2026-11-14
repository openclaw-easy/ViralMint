# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""The download path.

Callers import two things:

    from backend.download.fetch import fetch_video
    from backend.download.info import get_video_info, list_channel_videos, ...

Inside, every failure is read once and answered by one table:

  classify.py    the only place that reads error text
  policy.py      the table from error class to one step, and the budget
  engine.py      the one attempt loop: the probe and the download
  fetch.py       cooldown → probe → engine → one link fix
  info.py        metadata, channel listings, subtitles only
  cookies.py     CookieStore: what an attempt gets, and whether we may ask the browser again
  cookie_jar.py  the jar file: cleaning, counting, permissions
  opts.py        yt-dlp options, always built the same way
  progress.py    the stall watchdog and the cancel checks
  outputs.py     finding, checking, repairing and cleaning what a download leaves on disk
  health.py      per-host 429 cooldown; the yt-dlp update streak
  messages.py    the sentences people read
  notices.py     the one "running without your cookies" warning

To change behaviour: a new error wording is a row in
tests/fixtures/download_error_corpus.json (plus a phrase in classify.py); a new
response is a row in policy.decide. Nothing else retries, waits or reads errors.
"""
