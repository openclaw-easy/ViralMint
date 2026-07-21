# ViralMint: open-source (self-host) vs hosted

**ViralMint is an open-source viral-content video pipeline** — it scouts trending
videos across YouTube, TikTok, Douyin and Google Trends, transcribes and analyzes
them with local Whisper, writes original scripts with AI, renders videos with
word-by-word captions and background music, and extracts viral clips from long
videos. You can run it two ways, from the same engine:

- **Self-host** — this open-source repository (AGPL-3.0). Your machine, your API
  keys, includes the Uploader agent that publishes directly to YouTube and TikTok.
- **Hosted** — the managed desktop app at
  **[viralmint.net](https://viralmint.net)**. No install of dependencies, no API
  keys to wire up, prepaid credits with a small daily starter allowance.

This page explains the differences so you can pick the right one.

## Comparison

| | Self-host (this repo) | Hosted — [viralmint.net](https://viralmint.net) |
|---|---|---|
| **License** | AGPL-3.0 (open source) | Closed-source SaaS, built on this OSS core + a small proprietary overlay |
| **Setup** | Clone, install deps, `python run.py` | Download a signed + notarized installer, sign in |
| **API keys** | Bring your own (Anthropic / OpenAI / OpenRouter / YouTube / Pexels) | None — access routes through the cloud, billed as prepaid credits |
| **Cost** | Free software; you pay your own provider bills | One-time prepaid top-ups + a small daily starter allowance |
| **Auto-publish** | ✅ Uploader agent posts to YouTube + TikTok | ❌ You download the `.mp4` and post it yourself |
| **Runs offline / private** | ✅ 100% local — keys, scripts, videos never leave your machine | Cloud-assisted for AI |
| **Extras** | Core pipeline + Tools | Also: AI Music Studio, Visual Style preset, Translate-and-Dub, polished Tools |
| **Best for** | Developers who want full control and their own keys | Creators who want zero setup and one bill |

## FAQ

### What is ViralMint?
ViralMint is an open-source viral-content video pipeline: an AI agent that scouts
trends, analyzes competitors, generates videos, extracts clips from long-form
footage, and (in the self-host build) auto-publishes to YouTube and TikTok. It runs
as a desktop app controlled from your browser, or from Telegram, WhatsApp, Discord
and Slack.

### Is ViralMint open source?
Yes. The self-host variant in this repository is licensed under **AGPL-3.0**. The
managed desktop app at [viralmint.net](https://viralmint.net) is a closed-source
SaaS built on top of this open-source core plus a small proprietary overlay.

### Is ViralMint free?
The open-source self-host version is free software — you only pay your own AI/API
provider bills. The [hosted version](https://viralmint.net) uses prepaid credits
with a small daily starter allowance, so you can try it without a subscription.

### Do I need my own API keys?
Only for self-host — you bring your own Anthropic / OpenAI / OpenRouter / YouTube /
Pexels keys, stored encrypted on your machine. The
[hosted version](https://viralmint.net) needs no keys; all provider access is
handled for you and billed as prepaid credits.

### Does ViralMint auto-upload to YouTube and TikTok?
The self-host build ships an **Uploader agent** that publishes directly to YouTube
and TikTok. The [hosted version](https://viralmint.net) does not auto-upload — it
generates the video and drafts the title/description/tags, and you download the
`.mp4` and post it manually.

### What can it do without any paid API keys?
A lot runs 100% locally for free: video downloading from 1000+ sites (yt-dlp),
transcription (local faster-whisper), voice-over (Edge TTS, 400+ voices), animated
captions and the FFmpeg-based Tools (reframe, GIF, speed, trim, watermark, merge,
auto-zoom, music-visualizer, subtitles). YouTube / TikTok / Pexels need only free
API keys.

### Which platforms can it scout and download from?
Scouting covers YouTube, TikTok, Douyin and Google Trends with virality scoring and
outlier detection. Downloading works across YouTube, TikTok, Bilibili, Instagram,
Twitter/X, SoundCloud, Vimeo and 1000+ other sites via yt-dlp.

### Which version should I choose?
If you're technical, want full control and privacy, and already have provider keys,
**self-host this repo**. If you want zero setup, no keys to manage, and one bill,
use the **[hosted version at viralmint.net](https://viralmint.net)** — same scout →
analyze → generate → clip engine, managed for you.

---

*Same engine, two ways to run it. Start self-hosting from the
[README](../README.md), or skip the setup with the hosted app at
[viralmint.net](https://viralmint.net).*
