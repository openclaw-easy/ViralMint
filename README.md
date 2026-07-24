<div align="center">

<img src="frontend/public/icon-192.png" alt="ViralMint" width="96" height="96" />

# ViralMint

### The open-source, local-first video pipeline for creators

**Scout trends → clip long videos → generate AI shorts → auto-publish to YouTube & TikTok.**
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

<img src="docs/screenshots/chat.webp" alt="ViralMint Chat — streaming AI agent that scouts trending videos, analyzes channels, and orchestrates the full pipeline" width="900" />

<sub><i>Chat with the AI agent — paste a URL, ask it to scout a niche, or kick off a workflow, and it runs the right pipeline in the background.</i></sub>

</div>

---

> **What manual creators do across a dozen tabs and apps, ViralMint runs as one local workflow.**
> Find trending videos across YouTube, TikTok and Douyin, transcribe and analyze them with local Whisper, cut long videos into publishable shorts, write original scripts with the AI of your choice, render captioned stock-footage videos — and post directly to YouTube and TikTok. Drive it from a browser, or chat with it on Telegram, WhatsApp, Discord, or Slack.

## ✨ Why ViralMint

|   |   |
|---|---|
| 🔒 **100% local** | SQLite, local Whisper, local FFmpeg. Your scripts, transcripts, downloads, and generated videos never leave your machine. |
| 🔑 **BYOK, no middleman** | Bring your own Anthropic / OpenAI / OpenRouter / YouTube / Pexels keys. Encrypted at rest with AES-256, sent straight to the provider — there is no ViralMint server in between. |
| 🤖 **Agents, not a chat wrapper** | Six purpose-built agents — Planner, Scout, Download, Analyzer, Generator, and **Uploader** — orchestrated by a streaming AI chat that actually runs the work. |
| 📤 **It publishes for you** | Direct upload to YouTube and TikTok with AI-drafted titles, descriptions, tags, and thumbnails. The full loop, not just generation. |
| 📱 **Runs from your phone** | Two-way chat with the planner over Telegram, WhatsApp, Discord, or Slack — and job alerts in the same thread. |
| 🆓 **Free out of the box** | Local Whisper, Edge TTS (400+ voices), royalty-free music, Pexels stock, and 18 FFmpeg tools — the heavy lifting costs $0. Pay only for the AI you choose to plug in. |

<sub>Battle-tested: a **~1,000-test pytest suite** (999 and counting) runs on every commit. AGPL-3.0 — fork it, modify it, build a business on it.</sub>

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
One long video → many publishable shorts. AI finds the best moments and scores each on **hook, flow, value, trend-fit and shareability**, snaps cuts to sentence boundaries, and drops re-told stories. Bias picks by **platform or genre**, describe what you want (*"every joke that landed"*), or hand-pick **manual time ranges** — with optional silence trimming, emoji captions, and a burned-in hook overlay.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📤 Publish
Direct upload to **YouTube** (OAuth) and **TikTok** (OAuth or session cookie) with platform-optimized titles, descriptions, tags, and thumbnails — so a finished video actually gets posted.

</td>
<td width="50%" valign="top">

### 💬 Chat
Streaming WebSocket chat that orchestrates every agent. Say *"scout cooking videos"* or *"download this URL"* and it just runs. Tappable quick-reply chips, follow-up questions that never lock the composer, and rich result cards that persist across reloads.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📲 Messaging
Two-way chat from your phone via **Telegram, WhatsApp, Discord, Slack**. Command the planner from anywhere and get job alerts as they finish — same agent, different transport.

</td>
<td width="50%" valign="top">

### ⬇️ Universal downloader
yt-dlp under the hood — YouTube, TikTok, Bilibili, Instagram, Twitter/X, SoundCloud, Vimeo, and **1,800+ other sites**. No watermarks, no ads, no cap.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🧰 18 built-in tools
Single-purpose utilities — captions, reframe, GIF, speed, trim, subtitles, watermark, merge, auto-zoom, music-visualizer, voice-over, plus AI helpers (translate, metadata, hook analysis, auto-chapters). **Most run 100% locally on FFmpeg + Whisper — no API key.** Each has an inline result preview.

</td>
<td width="50%" valign="top">

### ✨ Proactive assistant
The chat reads your live pipeline — *downloaded-but-not-clipped*, *generated-but-not-uploaded*, *scouted-but-not-downloaded* — and suggests the single highest-value next step instead of waiting to be asked.

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
| Tools: reframe, GIF, speed, trim, watermark, merge, auto-zoom, music-visualizer, subtitles… | FFmpeg + Whisper | $0 |

YouTube / TikTok / Pexels still need free API keys — links in the [BYOK section](#-bring-your-own-keys-byok).

---

## 🚀 Quick Start

### Prerequisites

| Tool | macOS | Linux | Windows |
|:-----|:-----|:------|:--------|
| **Python 3.11+** | `brew install python` | `apt install python3.11` | [python.org](https://www.python.org/downloads/) |
| **Node.js 18+** | `brew install node` | `apt install nodejs npm` | [nodejs.org](https://nodejs.org/) |
| **FFmpeg** | `brew install ffmpeg` | `apt install ffmpeg` | [ffmpeg.org](https://ffmpeg.org/download.html) |
| **ImageMagick** | `brew install imagemagick` | `apt install imagemagick` | [imagemagick.org](https://imagemagick.org/) |

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

Every key can be set in `.env` *or* per-user inside the app under **Settings** — whichever is set takes priority. Per-user keys are **AES-256 encrypted** before storage. Keys go straight to the provider; ViralMint has no backend server in the middle.

| For | Provider | Where | Cost |
|:----|:---------|:------|:-----|
| AI chat, scripting, analysis | **Anthropic** · **OpenAI** · **OpenRouter** | [console.anthropic.com](https://console.anthropic.com) · [platform.openai.com](https://platform.openai.com/api-keys) · [openrouter.ai/keys](https://openrouter.ai/keys) — Settings → AI Provider | Pay-per-use |
| YouTube scouting · comments · My Channels | YouTube Data API v3 | [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials) — Settings → Service API Keys | Free 10K units/day |
| Stock footage | Pexels | [pexels.com/api](https://www.pexels.com/api/) | Free |
| Premium voiceover (optional) | OpenAI TTS | [platform.openai.com](https://platform.openai.com/api-keys) | Pay-per-use |
| TikTok / Douyin scouting | **TikHub API** (recommended) | [tikhub.io](https://tikhub.io) | Free tier |
| YouTube / TikTok upload | OAuth | One-click in Settings | Free |
| Telegram / Discord / Slack | Bot tokens | Settings → Messaging | Free |
| WhatsApp | QR-scan pairing | Settings → Messaging | Free |

> ⚠️ **TikTok / Douyin session-cookie scouting** is available as an advanced fallback in Settings, but it violates the platforms' Terms of Service and the cookie's account is the one the platform sees acting. **Use the TikHub API path unless you have specifically accepted that risk.** See [LEGAL.md](LEGAL.md#tiktok) for details.

---

## 🏗️ Architecture

```
                     ┌────────────────────────────────────────────────┐
                     │            React 18 + MUI 7 SPA                │
                     │       (served by FastAPI in production)        │
                     │  Chat · Channels · Library · Stock Video       │
                     │  Clip Studio · Messaging · Tools · Settings    │
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
                     │  Uploader Agent ── YouTube + TikTok OAuth      │
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
| **Transcription** | faster-whisper (local, multilingual, GPU-aware) |
| **TTS** | Edge TTS (free) · OpenAI TTS |
| **Video** | Pexels stock · FFmpeg · Ken Burns image fallback |
| **Captions** | FFmpeg + ASS (word-by-word highlight animation) |
| **Download** | yt-dlp (1,800+ sites) |
| **Messaging** | python-telegram-bot · discord.py · slack-sdk · neonize (WhatsApp) |
| **Security** | Fernet (AES-256) for credentials at rest |

---

## 📸 Screenshots

<table>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/library.webp"><img src="docs/screenshots/library.webp" alt="Library — Scout results with virality scores" /></a>
  <sub><b>Library — Scout results</b><br/>Videos discovered across platforms, sorted by AI virality score, downloadable in one click.</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/clip-studio.webp"><img src="docs/screenshots/clip-studio.webp" alt="Clip Studio — extract viral shorts from a long-form video" /></a>
  <sub><b>Clip Studio — viral clip extraction</b><br/>AI picks the best 30–60s moments from a long video, scores them, and burns captions automatically.</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/messaging.webp"><img src="docs/screenshots/messaging.webp" alt="Messaging — Telegram, WhatsApp, Discord, Slack" /></a>
  <sub><b>Messaging — chat from your phone</b><br/>Connect Telegram, WhatsApp, Discord, or Slack to control the planner and receive job notifications.</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/channel-analysis.webp"><img src="docs/screenshots/channel-analysis.webp" alt="My Channels — channel analytics" /></a>
  <sub><b>My Channels — channel analytics</b><br/>Connect any YouTube/TikTok channel by URL. View counts, engagement, median views, and outlier detection.</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/smart-video.webp"><img src="docs/screenshots/smart-video.webp" alt="Smart Video studio" /></a>
  <sub><b>Smart Video studio</b><br/>Mix your own clips with stock footage; word-by-word captions; background music; live cost estimator.</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/tools.webp"><img src="docs/screenshots/tools.webp" alt="Tools — single-purpose utilities" /></a>
  <sub><b>Tools — 18 utilities</b><br/>Reframe, watermark, GIF, speed, trim, subtitles, voice-over, hook analysis — most run locally with no API key.</sub>
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
│   ├── agents/                     # Planner, Scout, Download, Analyzer, Generator, Uploader
│   ├── api/                        # REST + WebSocket endpoints
│   ├── core/                       # AI client, BYOK key resolver, crypto, WebSocket manager
│   ├── messaging/                  # Telegram / WhatsApp / Discord / Slack channels
│   ├── models/                     # SQLAlchemy models
│   └── services/                   # TTS, video gen, captions, music, yt-dlp, Whisper, …
│
├── frontend/
│   └── src/
│       ├── pages/                  # Chat · Channels · Library · Stock Video · Clip Studio · …
│       ├── components/             # Reusable UI (chat, settings, videos, …)
│       ├── hooks/                  # WebSocket, settings, jobs, source video
│       └── store/                  # Zustand global state
│
├── tests/                          # pytest suite (~1,000 tests)
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

**ViralMint is a tool you run on your own machine.** The maintainers don't host your content or proxy your API calls — every action is you, acting on your own platforms and keys. Read [LEGAL.md](LEGAL.md) before using the scouting and downloader features so you understand what's sanctioned (YouTube Data API, OAuth uploads, Pexels), what's at-your-own-risk (TikTok/Douyin session-cookie scouting), and what you're responsible for under each platform's Terms of Service.

---

### 🙋 Don't want to self-host?

There's also a hosted build at **[viralmint.net](https://viralmint.net)** — the same scout + analyze + generate engine, signed and notarized, with no API keys to wire up (prepaid credits instead of BYOK). It's closed-source and doesn't auto-upload — a different set of trade-offs for people who'd rather not run their own keys and installs. Full comparison + FAQ: **[docs/hosted-vs-self-hosted.md](docs/hosted-vs-self-hosted.md)**. Otherwise, everything you need is right here — read on and `python run.py`.

---

<div align="center">

## ⭐ If ViralMint is useful to you, star it

Stars are the single biggest thing that helps this project — they attract contributors, unlock awesome-list eligibility, and tell other creators it's worth a look. It takes one click.

**[⭐ Star openclaw-easy/ViralMint](https://github.com/openclaw-easy/ViralMint)**

<a href="https://www.star-history.com/#openclaw-easy/ViralMint&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/ViralMint&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/ViralMint&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=openclaw-easy/ViralMint&type=Date" width="640" />
  </picture>
</a>

<br/>

**Built with FastAPI, React, Whisper, FFmpeg, and a lot of async Python.**

Website: **[viralmint.net](https://viralmint.net)** · Source: **[github.com/openclaw-easy/ViralMint](https://github.com/openclaw-easy/ViralMint)** · License: **[AGPL-3.0](LICENSE)**

</div>
