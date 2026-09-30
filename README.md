<div align="center">

<img src="frontend/public/icon-192.png" alt="ViralMint" width="96" height="96" />

# ViralMint

### The open-source, local-first video pipeline for creators

**Scout trends → clip long videos → generate captioned shorts → design motion graphics → polish with 22 local tools.**
All on your machine. Bring your own API keys. No SaaS in the middle. No telemetry.

<!-- Activity badges (top row) — auto-update from GitHub, so they reflect
     real maintenance signal at a glance for awesome-list reviewers and new
     visitors. -->
[![Stars](https://img.shields.io/github/stars/openclaw-easy/ViralMint?style=for-the-badge&logo=github&color=yellow)](https://github.com/openclaw-easy/ViralMint/stargazers)
[![Last commit](https://img.shields.io/github/last-commit/openclaw-easy/ViralMint?style=for-the-badge&color=brightgreen)](https://github.com/openclaw-easy/ViralMint/commits/main)
[![Release](https://img.shields.io/github/v/release/openclaw-easy/ViralMint?style=for-the-badge&color=blue&label=latest)](https://github.com/openclaw-easy/ViralMint/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/openclaw-easy/ViralMint/ci.yml?branch=main&style=for-the-badge&logo=githubactions&logoColor=white&label=CI)](https://github.com/openclaw-easy/ViralMint/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue?style=for-the-badge)](LICENSE)

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![React 18](https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Platform](https://img.shields.io/badge/macOS%20%7C%20Windows%20%7C%20Linux-lightgrey?style=for-the-badge)](#-quick-start)

[Quick Start](#-quick-start) • [Features](#-features) • [What's free](#-what-works-without-api-keys) • [BYOK](#-bring-your-own-keys-byok) • [Contributing](CONTRIBUTING.md)

**English** · [简体中文](README.zh-CN.md) · [日本語](README.ja.md)

<br/>

<img src="docs/screenshots/clipper-bench.webp" alt="ViralMint Clip Studio — a cutting bench with a filmstrip timeline, speech lane and pending cuts" width="900" />

<sub><i>Cut a long video into shorts on a real timeline — drag across its own frames, snap to sentence boundaries, or let the AI propose the moments. Scouting, transcription, AI video, motion graphics and 22 editing tools all run from the same app.</i></sub>

</div>

---

> **What manual creators do across a dozen tabs and apps, ViralMint runs as one local workflow.**
> Find trending videos across YouTube, TikTok and Douyin, transcribe and analyze them with local Whisper, cut long videos into publishable shorts on a real timeline, write original scripts with the AI of your choice, render captioned stock-footage videos, design animated motion graphics — and get back a finished MP4 with an AI-drafted title, description and tags, ready to post. Drive it from a browser, or chat with it on Telegram, WhatsApp, Discord, or Slack.

## ✨ Why ViralMint

|   |   |
|---|---|
| 🔒 **100% local** | SQLite, local Whisper, local FFmpeg. Your scripts, transcripts, downloads, and generated videos never leave your machine. |
| 🔑 **BYOK, no middleman** | Bring your own Anthropic / OpenAI / OpenRouter / YouTube / Pexels keys. Encrypted at rest with Fernet (AES-128-CBC, HMAC-signed), sent straight to the provider — there is no ViralMint server in between. |
| 🤖 **Agents, not a chat wrapper** | Purpose-built agents — Planner, Scout, Download, Analyzer and Generator — orchestrated by a streaming AI chat that actually runs the work. |
| ✂️ **Clip, generate, or design** | Three ways to a finished video: cut shorts from a long source on a real timeline, generate a captioned stock-footage video from a script, or author motion graphics with no footage at all. |
| 📱 **Runs from your phone** | Two-way chat with the planner over Telegram, WhatsApp, Discord, or Slack — and job alerts in the same thread. |
| 🆓 **Free out of the box** | Local Whisper, Edge TTS (400+ voices), royalty-free music, Pexels stock, and 22 built-in tools (most of them pure FFmpeg + Whisper) — the heavy lifting costs $0. Pay only for the AI you choose to plug in. |

<sub>Battle-tested: a **2,800-test pytest suite** runs on every commit, plus a browser harness that drives the real app end to end. AGPL-3.0 — fork it, modify it, build a business on it.</sub>

---

## 🎯 Features

<table>
<tr>
<td width="50%" valign="top">

### 🔍 Scout
Multi-platform trend discovery across **YouTube, TikTok, and Douyin** (plus any yt-dlp-supported site via dynamic search), with AI virality scoring, Google-Trends demand signals, view-velocity analysis, and outlier detection (3×–20× the channel baseline).

</td>
<td width="50%" valign="top">

### 🧠 Analyze
Local Whisper transcription with clean long-form handling, plus AI insight extraction — hook, structure, tone, retention risks, suggested titles, and a ready-to-run recreate prompt — scored per segment with concrete improvement suggestions.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🎬 Generate
Full pipeline: AI script → TTS voiceover → Pexels stock footage matched to keywords → phrase-aware animated captions (with CJK / Arabic / Thai support) → balanced background music → AI thumbnail → finished MP4.

</td>
<td width="50%" valign="top">

### ✂️ Clip Studio
One long video → many publishable shorts, cut on a **real timeline**. Drag across a filmstrip of the source's own frames, watch the first and last frame of your cut as you move the handles, and snap to sentence boundaries so a clip never opens mid-word. Type an exact timecode, paste times from show notes, or **ask the AI** — its picks land as blocks you can retime or delete before anything renders, scored on hook, flow, value, trend-fit and shareability. In a hurry, **Auto-cut** does the whole thing in one press and tells you how many clips that means first.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🎞️ Motion Graphics
A third kind of output, with no footage in it at all: kinetic typography, animated stat cards, lower thirds, product showcases. Describe what you want and the AI writes a real composition; refine it in an embedded animation studio with a timeline, layers and an inspector, then **render it entirely on your own machine**. Installs on demand — it isn't in the download.

</td>
<td width="50%" valign="top">

### 🗂️ Library
One faceted view over everything you own — renders, downloads, every tool output, your music folder. Two questions get two controls: tabs say what a file **is** (video / image / audio / files), chips say where it **came from** (created / edited / sources), so a downloaded mp3 doesn't have to pick a side. Group **by source** to see a download beside everything you made from it, and open **Activity** from any page to watch work in flight.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 💬 Chat
Streaming WebSocket chat that orchestrates every agent. Say *"scout cooking videos"* or *"download this URL"* and it just runs. Tappable quick-reply chips, follow-up questions that never lock the composer, and rich result cards that persist across reloads. The chat reads your live pipeline — *downloaded-but-not-clipped*, *generated-but-not-posted*, *scouted-but-not-downloaded* — and suggests the single highest-value next step instead of waiting to be asked.

</td>
<td width="50%" valign="top">

### 📲 Messaging
Two-way chat from your phone via **Telegram, WhatsApp, Discord, Slack**. Command the planner from anywhere and get job alerts as they finish — same agent, different transport.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### ⬇️ Universal downloader
yt-dlp under the hood — YouTube, TikTok, Bilibili, Instagram, Twitter/X, SoundCloud, Vimeo, and **1,800+ other sites**. No watermarks, no ads, no cap.

</td>
<td width="50%" valign="top">

### 🧰 22 built-in tools
Single-purpose utilities — captions, reframe, crop, compress, GIF, speed, trim, subtitles, watermark, merge, auto-zoom, music-visualizer, voice-over, silence removal, video download, plus AI helpers (translate, metadata, hook analysis, auto-chapters). **Most run 100% locally on FFmpeg + Whisper — no API key.** Each has an inline result preview and a history of what it produced.

</td>
</tr>
</table>

---

## 🆓 What works without API keys

The expensive parts are free and local. You only pay for the AI you choose to plug in.

| Feature | Powered by | Cost |
|:--------|:-----------|:-----|
| Video downloading from 1,800+ sites | yt-dlp | $0 |
| Audio transcription | Local faster-whisper | $0 |
| Voiceover (400+ voices, 70+ languages) | Edge TTS | $0 |
| Word-by-word animated captions | FFmpeg + ASS subtitles | $0 |
| Background music library | Royalty-free local library | $0 |
| Sound-effects auto-placement | FFmpeg-synthesized | $0 |
| Tools: reframe, crop, compress, GIF, speed, trim, watermark, merge, auto-zoom, music-visualizer, subtitles… | FFmpeg + Whisper | $0 |
| Motion-graphics rendering | Local HyperFrames engine | $0 |

YouTube / TikTok / Pexels still need free API keys — links in the [BYOK section](#-bring-your-own-keys-byok).

---

## 🚀 Quick Start

### Prerequisites

| Tool | macOS | Linux | Windows |
|:-----|:-----|:------|:--------|
| **Python 3.11+** | `brew install python` | `apt install python3.11` | [python.org](https://www.python.org/downloads/) |
| **Node.js 18+** | `brew install node` | `apt install nodejs npm` | [nodejs.org](https://nodejs.org/) |
| **FFmpeg** | `brew install ffmpeg` | `apt install ffmpeg` | [ffmpeg.org](https://ffmpeg.org/download.html) |

### Install & run

```bash
git clone https://github.com/openclaw-easy/ViralMint.git
cd ViralMint

python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                                  # optional — keys can also be set in the UI
python run.py
```

The first run installs frontend deps, builds the SPA, starts the API, and opens your browser at **http://localhost:16888**.

> 💡 **No API key yet?** Open **Settings → AI Provider** after launch and paste an Anthropic, OpenAI, or OpenRouter key. OpenRouter is a single gateway to 300+ models — one key gets you Claude, GPT, Gemini, Llama, and Mistral. Edge TTS, Whisper, FFmpeg, and yt-dlp all work offline with zero configuration.

### Build a desktop app from source (optional)

Prefer a clickable app to a terminal command? A self-contained PyInstaller pipeline builds a macOS `.dmg`, Linux `.tar.gz`, or Windows `.zip` from this source — your browser is still the UI.

```bash
PYTHON_BIN=./venv/bin/python VIRALMINT_VERSION=0.1.0-dev \
  bash desktop/scripts/build-app.sh
```

Output lands in `desktop/release/`. First build takes ~10–15 min (PyInstaller bundling is the long pole). Skip flags, signing/notarization env vars, and the smoke-test recipe are in **[`desktop/README.md`](desktop/README.md)**.

---

## 🔑 Bring your own keys (BYOK)

Every key can be set in `.env` *or* per-user inside the app under **Settings** — whichever is set takes priority. Per-user keys are **Fernet-encrypted** (AES-128-CBC + HMAC-SHA256) before storage. Keys go straight to the provider; ViralMint has no backend server in the middle.

| For | Provider | Where | Cost |
|:----|:---------|:------|:-----|
| AI chat, scripting, analysis | **Anthropic** · **OpenAI** · **OpenRouter** | [console.anthropic.com](https://console.anthropic.com) · [platform.openai.com](https://platform.openai.com/api-keys) · [openrouter.ai/keys](https://openrouter.ai/keys) — Settings → AI Provider | Pay-per-use |
| YouTube scouting · comments · My Channels | YouTube Data API v3 | [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials) — Settings → Service API Keys | Free 10K units/day |
| Stock footage | Pexels | [pexels.com/api](https://www.pexels.com/api/) | Free |
| Premium voiceover (optional) | OpenAI TTS | [platform.openai.com](https://platform.openai.com/api-keys) | Pay-per-use |
| TikTok / Douyin scouting | **TikHub API** (recommended) | [tikhub.io](https://tikhub.io) | Free tier |
| Telegram / Discord / Slack | Bot tokens | Settings → Messaging | Free |
| WhatsApp | QR-scan pairing | Settings → Messaging | Free |

> ⚠️ **TikTok / Douyin session-cookie scouting** is available as an advanced fallback in Settings, but it violates the platforms' Terms of Service and the cookie's account is the one the platform sees acting. **Use the TikHub API path unless you have specifically accepted that risk.** See [LEGAL.md](LEGAL.md#tiktok) for details.

---

## 🏗️ Architecture

```
                     ┌────────────────────────────────────────────────┐
                     │            React 18 + MUI 7 SPA                │
                     │       (served by FastAPI in production)        │
                     │  Chat · Scout · Channels · Library             │
                     │  Stock Video · Clip Studio · Motion Graphics   │
                     │  Tools · Messaging · Settings                  │
                     └─────────────────┬──────────────────────────────┘
                                       │  HTTP + WebSocket
                                       ▼
                     ┌────────────────────────────────────────────────┐
                     │           FastAPI · localhost:16888            │
                     ├────────────────────────────────────────────────┤
                     │  Planner Agent ─── streaming chat + actions    │
                     │  Scout Agent ───── YouTube · TikTok · Douyin   │
                     │                    (+ yt-dlp dynamic search)   │
                     │  Download Agent ── yt-dlp (1,800+ sites)       │
                     │  Analyzer Agent ── Whisper + AI insights       │
                     │  Generator Agent ─ Script → TTS → Stock →      │
                     │                    Captions → Music → MP4      │
                     │  Motion Renderer ─ local HyperFrames engine    │
                     │  Messaging ─────── Telegram · WhatsApp ·       │
                     │                    Discord · Slack             │
                     └─────────────────┬──────────────────────────────┘
                                       │
              ┌────────────────────────┼─────────────────────────┐
              ▼                        ▼                         ▼
    ┌─────────────────┐    ┌──────────────────────┐    ┌──────────────────┐
    │  SQLite local   │    │  storage/ on disk    │    │  External APIs   │
    │  (encrypted     │    │  videos · audio ·    │    │  (BYOK, direct)  │
    │   credentials)  │    │  thumbnails · sfx    │    │                  │
    └─────────────────┘    └──────────────────────┘    └──────────────────┘
```

### Tech stack

| Layer | Stack |
|:------|:------|
| **Backend** | Python 3.11+ · FastAPI · SQLAlchemy 2.0 (async) · SQLite · WebSockets |
| **Frontend** | React 18 · Vite · MUI 7 · Zustand · React Router 6 |
| **AI (BYOK)** | Anthropic Claude SDK · OpenAI SDK · OpenRouter (300+ models via one key) |
| **Transcription** | faster-whisper (local, multilingual, CPU int8) |
| **TTS** | Edge TTS (free) · OpenAI TTS |
| **Video** | Pexels stock · FFmpeg · Ken Burns image fallback |
| **Captions** | FFmpeg + ASS (word-by-word highlight animation) |
| **Download** | yt-dlp (1,800+ sites) |
| **Messaging** | python-telegram-bot · discord.py · slack-sdk · neonize (WhatsApp) |
| **Security** | Fernet (AES-128-CBC + HMAC-SHA256) for credentials at rest |

---

## 📸 Screenshots

<table>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/clipper-bench.webp"><img src="docs/screenshots/clipper-bench.webp" alt="Clip Studio — the cutting bench, with a filmstrip timeline and pending cuts" /></a>
  <sub><b>Clip Studio — the cutting bench</b><br/>Drag across the source's own frames. Speech lane, sentence snapping, IN/OUT previews, and typeable timecodes. Nothing renders until you press Cut.</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/auto-cut.webp"><img src="docs/screenshots/auto-cut.webp" alt="Auto-cut — the AI picks the moments and cuts them in one press" /></a>
  <sub><b>Auto-cut — the express lane</b><br/>Trust the AI and skip the review step. It says how many clips that means for <i>this</i> video before you press the button.</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/library.webp"><img src="docs/screenshots/library.webp" alt="Library — media tabs and origin chips over renders, downloads and tool outputs" /></a>
  <sub><b>Library — everything you own</b><br/>One view over renders, downloads, every tool output and your music folder. Tabs say what a file is; chips say where it came from.</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/motion-graphics.webp"><img src="docs/screenshots/motion-graphics.webp" alt="Motion Graphics — an embedded animation studio with a timeline and inspector" /></a>
  <sub><b>Motion Graphics — designed video, rendered locally</b><br/>Kinetic type, stat cards, lower thirds. The AI writes the composition; you refine it on a timeline and render on your own machine.</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/smart-video.webp"><img src="docs/screenshots/smart-video.webp" alt="Smart Video studio" /></a>
  <sub><b>Smart Video studio</b><br/>Script → voiceover → stock footage → word-by-word captions → music, in one pass. Mix in your own clips and pick a visual style.</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/tools.webp"><img src="docs/screenshots/tools.webp" alt="Tools — 22 single-purpose utilities" /></a>
  <sub><b>Tools — 22 utilities</b><br/>Reframe, crop, compress, watermark, GIF, speed, trim, subtitles, voice-over, hook analysis — most run locally with no API key.</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/chat.webp"><img src="docs/screenshots/chat.webp" alt="Chat — the agent that runs the pipeline" /></a>
  <sub><b>Chat — the agent that runs it</b><br/>Paste a URL, name a niche, or ask for a video. It dispatches the right agent and reports back in the same thread.</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/messaging.webp"><img src="docs/screenshots/messaging.webp" alt="Messaging — Telegram, WhatsApp, Discord, Slack" /></a>
  <sub><b>Messaging — drive it from your phone</b><br/>Connect Telegram, WhatsApp, Discord or Slack to command the same agent and get job alerts as they finish.</sub>
</td>
</tr>
</table>

---

## 📁 Project structure

```
ViralMint/
├── run.py                          # 🚀 Single entry point
├── launcher.py                     # System-tray launcher (optional)
│
├── backend/
│   ├── agents/                     # Planner, Scout, Download, Analyzer, Generator
│   ├── api/                        # REST + WebSocket endpoints
│   ├── core/                       # AI client, BYOK key resolver, crypto, WebSocket manager
│   ├── messaging/                  # Telegram / WhatsApp / Discord / Slack channels
│   ├── models/                     # SQLAlchemy models
│   └── services/                   # TTS, video gen, captions, music, yt-dlp, Whisper,
│                                   #   motion rendering, library index, …
│
├── frontend/
│   └── src/
│       ├── pages/                  # Chat · Scout · Channels · Library · Stock Video ·
│       │                           #   Clip Studio · Motion Graphics · Tools · Messaging
│       ├── components/             # Reusable UI (chat, settings, videos, …)
│       ├── hooks/                  # WebSocket, settings, jobs, source video
│       └── store/                  # Zustand global state
│
├── tests/                          # pytest suite (2,800+ tests)
├── storage/                        # Downloaded videos, audio, generated output (gitignored)
│
├── requirements.txt
├── .env.example
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE                         # AGPL-3.0
```

---

## 🤝 Contributing

Pull requests welcome — bug fixes, new platforms, additional messaging channels, performance work, docs, anything. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and house style, and the [Code of Conduct](CODE_OF_CONDUCT.md) before opening your first issue.

- 📋 **Recent changes:** [CHANGELOG.md](CHANGELOG.md)
- 🐛 **Report a bug:** [open an issue](https://github.com/openclaw-easy/ViralMint/issues/new?template=bug_report.md)
- 💡 **Request a feature:** [open an issue](https://github.com/openclaw-easy/ViralMint/issues/new?template=feature_request.md)
- 🔐 **Security vulnerability:** [SECURITY.md](SECURITY.md) — **do not** file a public issue.

## 📜 License & responsible use

ViralMint is licensed under the **GNU Affero General Public License v3.0** ([LICENSE](LICENSE)).

- ✅ Free for personal use, commercial use, modification, and redistribution
- ✅ Run a SaaS on top of it
- ⚠️ If you distribute it (or run it as a public network service), you must share your modified source under the same AGPL-3.0 terms

**ViralMint is a tool you run on your own machine.** The maintainers don't host your content or proxy your API calls — every action is you, acting on your own platforms and keys. Read [LEGAL.md](LEGAL.md) before using the scouting and downloader features so you understand what's sanctioned (YouTube Data API, Pexels), what's at-your-own-risk (TikTok/Douyin session-cookie scouting), and what you're responsible for under each platform's Terms of Service.

---

### 🙋 Don't want to self-host?

There's also a hosted build at **[viralmint.net](https://viralmint.net)** — the same scout + analyze + generate engine, signed and notarized, with no API keys to wire up (prepaid credits instead of BYOK). It's closed-source — a different set of trade-offs for people who'd rather not run their own keys and installs. Full comparison + FAQ: **[docs/hosted-vs-self-hosted.md](docs/hosted-vs-self-hosted.md)**. The hosted tools most people arrive through: the [AI video clipper for YouTube](https://viralmint.net/youtube-clip-extractor/), the [YouTube video downloader](https://viralmint.net/tools/video-downloader/youtube/), the [AI caption generator](https://viralmint.net/tools/ai-caption-generator/) and the [AI video generator](https://viralmint.net/tools/ai-video-generator/). Otherwise, everything you need is right here — read on and `python run.py`.

---

<div align="center">

## ⭐ If ViralMint is useful to you, star it

Stars are the single biggest thing that helps this project — they attract contributors, unlock awesome-list eligibility, and tell other creators it's worth a look. It takes one click.

**[⭐ Star openclaw-easy/ViralMint](https://github.com/openclaw-easy/ViralMint)**

<!-- Star-history chart. The repo slug is LOWERCASE on purpose: the
     mixed-case URL answers 301, and GitHub's image proxy caches whatever the
     first fetch returned. Re-probe before touching this:
     curl -s -o /dev/null -w '%{http_code}' 'https://api.star-history.com/svg?repos=openclaw-easy/viralmint&type=Date'
     NEVER put a raw github_pat_ token in this URL (it would be public). -->
<a href="https://star-history.com/#openclaw-easy/ViralMint&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/viralmint&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/viralmint&type=Date" />
    <img alt="ViralMint star history chart" src="https://api.star-history.com/svg?repos=openclaw-easy/viralmint&type=Date" width="600" />
  </picture>
</a>

**[📈 See ViralMint's star history →](https://star-history.com/#openclaw-easy/ViralMint&Date)**

<br/>

**Built with FastAPI, React, Whisper, FFmpeg, and a lot of async Python.**

Website: **[viralmint.net](https://viralmint.net)** · Source: **[github.com/openclaw-easy/ViralMint](https://github.com/openclaw-easy/ViralMint)** · License: **[AGPL-3.0](LICENSE)**

</div>
