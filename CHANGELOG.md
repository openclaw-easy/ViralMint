# Changelog

All notable changes to ViralMint will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Clip Studio — extraction hardening ported from the hosted variant (7 bugfixes).**
  Fixes a `time_offset` double-count that silently dropped almost every clip
  past the first chunk on long videos; a clip-count estimator that assumed 40s
  clips (collapsing "3×15s from a 63s video" to 1); Whisper failures that
  silently downgraded to random duration-based clips instead of failing loudly;
  single-bound (min-only / max-only) duration overrides being ignored; the
  retry cascade widening past user-pinned bounds; and two caption/exception
  leaks into the output path. Adds `backend/core/concurrency.py` to cap
  parallel ffmpeg work.
- **Analyzer — chunked AI transcript correction.** The old single-call
  correction on `raw_text[:6000]` silently discarded everything past 6000 chars
  on long videos; now sentence-aligned chunking corrects the whole transcript
  with a per-chunk sanity guard (never loses content). Plus a `has_audio_stream`
  ffprobe preflight so a video-only/silent file raises a clear error instead of
  faster-whisper's opaque "tuple index out of range".
- **Captions — placement, flashing, and non-Latin fixes.** `alignment=5`
  (frame-center, ignores margins) → `alignment=2` (bottom-anchored) with
  per-aspect margins; phrase-aware line grouping with continuous-hold events so
  captions no longer blank out during Whisper's inter-word gaps; script-aware
  font fallback so CJK/Arabic/Thai captions stop burning as tofu boxes; libass
  preflight; concurrency-safe temp file; new `brainrot`/`urban`/`warm`/`mono`
  styles.
- **Music mix — voiceover level.** `amix` defaulted `normalize=1`, halving the
  voiceover to −6 dB; add `normalize=0` + an `alimiter` peak guard so the voice
  stays full-level with music as a true −20 dB bed.
- **Messaging — concurrent channel start.** `start_all()` now launches every
  channel in parallel with per-channel failure isolation, so the slowest
  channel no longer gates the rest.
- **Download hardening — pinned yt-dlp floor + TLS impersonation.**
  `requirements.txt` now pins `yt-dlp>=2026.7.4`: an unbounded `yt-dlp` on an
  old Python (macOS's system `python3` is 3.9) silently resolves to an ancient
  2025.10 release that fails on modern YouTube — the floor turns that into a
  loud install error instead of a broken downloader. Added
  `curl-cffi>=0.10,<0.15` and wired Chrome TLS impersonation into every
  yt-dlp call (`ytdlp_service`), so TLS-fingerprinting bot defenses
  (Cloudflare/Akamai) can't block downloads by handshake; degrades cleanly to
  urllib's fingerprint if curl-cffi is missing or incompatible.
- **Download reliability port from the hosted variant** (`ytdlp_service`):
  original-audio `format_sort` with `lang` leading (multi-language YouTube
  videos no longer download a dubbed audio track), exponential
  `retry_sleep_functions` per retry-pool, a 100 KB/s `throttledratelimit`
  guard that re-extracts stale signed URLs, and per-extractor args —
  PO-token-aware YouTube `player_client` ordering (token-free clients lead),
  `youtubetab` authcheck skip for public channel extraction, TikTok
  genuine-device-id flow, Twitter syndication API, Instagram/Reddit retry
  bumps. The pip self-update is now version-bounded (`yt-dlp>=2026.7.4`) so
  an outdated Python can't silently downgrade the downloader.

### Added
- **Tools page** — 18 single-purpose utilities (captions, reframe, audio-enhance, watermark, remove-silence, merge-clips, GIF, speed, trim, subtitles, auto-zoom, transform, music-visualizer, voice-over via Edge TTS, plus AI helpers: translate, metadata, hook-analysis, auto-chapters). The 13 ffmpeg/Whisper tools run fully locally with no API key; the AI helpers and the ✨ Enhance-prompt button use the user's own key (BYOK). Each tool has an inline result preview. New `/api/tools/*` router + `backend/core/tool_runners.py`. (AI media generators — image/music/video — are intentionally not in the OSS build.)
- **Proactive assistant** — the planner now reads live pipeline state (downloaded-not-clipped, generated-not-uploaded, scouted-not-downloaded) and surfaces the single highest-value next step. Backed by behavior-event instrumentation so the personalization engine learns from every completed job.

### Fixed
- Library self-heals — generated-video rows whose rendered file has been deleted are now pruned on list, so dead/broken tiles no longer linger.

### Security
- Bump `aiohttp` to `>=3.14.0,<4` — closes CVE-2026-34993 and CVE-2026-47265 (pip-audit). The frontend's `vite`/`esbuild` dev-server advisories are intentionally left for a future `vite` major bump: they affect only `npm run dev`, not the bundled app users ship, and the fix is a breaking change.
- Bump `cryptography` to `>=46.0.6,<47` — closes PYSEC-2026-35, GHSA-h4gh-qq45-vh27, CVE-2024-12797, CVE-2026-26007 (4 CVEs in the 43.x line).
- Bump `Pillow` to `>=12.2.0,<13.0` — closes CVE-2026-25990, 40192, 42308, 42309, 42310, 42311 (6 OOB / hang / memory-corruption issues affecting the thumbnail and ffmpeg image-processing paths). The `Image.ANTIALIAS` / `BICUBIC` monkeypatch in `backend/main.py` continues to work against Pillow 12.x.

### Changed
- Bump `openai` floor from `1.55` to `1.109.1` (still `<2.0`).
- Bump `playwright` floor from `1.58` to `1.59`.
- Bump 12 grouped Python minor/patch deps (dependabot `python-minor-patch` group).
- Bump `@mui/icons-material` 7.3.9→7.3.11, `axios`, `lucide-react` (dependabot `js-minor-patch` group).
- Bump CI actions — `actions/checkout` v4→v6 plus `setup-python`, `setup-node`, `codeql-action` (dependabot `ci-actions` group).

### Docs
- README — added an above-the-fold "Two ways to use ViralMint" callout clarifying the OSS variant (BYOK, Uploader agent, AGPL-3.0) vs the hosted SaaS at viralmint.net (prepaid credits, no auto-upload, closed-source). Helps new visitors pick the right variant without scrolling.

## [1.1.0] — 2026-05-07

### Added
- **OpenRouter as a third BYOK provider** — alongside Anthropic and OpenAI direct, a single OpenRouter API key now opens access to 300+ models (Claude, GPT, Gemini, Mixtral, Llama, etc.) through one credential. Configurable via `.env` or per-user in Settings → API Keys. See `backend/core/ai_provider.py`.

### Changed
- **Dependabot config** — minor/patch dependency updates are now batched into three groups (`python-minor-patch`, `js-minor-patch`, `ci-actions`) instead of arriving one PR at a time. Major framework versions (FastAPI / React / Pillow majors etc.) stay outside the groups so they always get explicit review.

## [1.0.0] — 2026-05-07

Initial open-source release.

### Added
- **Scout** — multi-platform trend discovery across YouTube, TikTok, Douyin, and Google Trends, with virality scoring and 3×–20× channel-baseline outlier detection.
- **Analyze** — local Whisper transcription plus AI insight extraction (hook, structure, tone, retention risks) per downloaded video.
- **Generate** — full pipeline: AI script → TTS voice → Pexels stock footage → word-by-word ASS captions → background music → finished mp4.
- **Clip Studio** — extract publishable 30–60s shorts from a long-form source; AI picks the best moments and burns captions.
- **Publish** — direct upload to YouTube (OAuth) and TikTok (OAuth or session cookie) with platform-optimized titles, descriptions, tags, and thumbnails.
- **Chat** — streaming WebSocket chat with the planner agent; action blocks dispatch background jobs (scout / download / analyze / generate / upload).
- **Messaging** — two-way chat over Telegram, WhatsApp, Discord, and Slack — same agent, different transport.
- **BYOK** — Anthropic / OpenAI / YouTube / Pexels / TikHub keys settable per-user in the UI or via `.env`. Per-user keys are AES-256 encrypted at rest.
- **Edge TTS** — 400+ free voices in 70+ languages; the default voiceover provider.
- **Universal downloader** — yt-dlp under the hood (1000+ sites supported).
- 92-test pytest suite covering crypto, scout scoring, captions, exception handling, HTTP utilities, and the async task runner.
- AGPL-3.0 license, SPDX headers on every Python source file.

### Security
- API binds to `127.0.0.1` (loopback) by default. Users who want LAN access can set `HOST=0.0.0.0` in `.env` knowingly.
- All third-party credentials encrypted with Fernet (AES-256) before being written to SQLite.
- No telemetry. No analytics. No cloud backend in the middle — keys go directly from your machine to the provider.

[Unreleased]: https://github.com/openclaw-easy/ViralMint/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/openclaw-easy/ViralMint/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/openclaw-easy/ViralMint/releases/tag/v1.0.0
