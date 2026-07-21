<div align="center">

<img src="frontend/public/icon-192.png" alt="ViralMint" width="96" height="96" />

# ViralMint

### 开源的爆款内容生产流水线

**发现趋势 → 分析对手 → 生成视频 → 自动发布 → 用手机随时随地掌控。**
100% 本地运行。自带 API 密钥。不绑定任何 SaaS。零遥测。

[🌐 官网](https://viralmint.net) • [快速开始](#-快速开始) • [功能特性](#-功能特性) • [架构](#-架构) • [API 密钥](#-自带密钥-byok) • [参与贡献](CONTRIBUTING.md)

[English](README.md) · **简体中文** · [日本語](README.ja.md)

</div>

## 🚦 使用 ViralMint 的两种方式

同一套「发现 + 分析 + 生成」引擎，只是运维取舍不同。挑一个最贴合你工作方式的即可。

<table>
<tr>
<td width="50%" valign="top">

### 🛠 自托管（本仓库）

**你自己的机器 · BYOK · AGPL-3.0**

- ✅ 完整流水线，含可直接发布到 YouTube + TikTok 的 **上传智能体（Uploader）**
- ✅ 通过 Telegram / WhatsApp / Discord / Slack 用手机操控
- ✅ 100% 本地——密钥、脚本、视频永不离开你的机器
- ✅ 可修改、可 fork、可再分发（AGPL-3.0）
- ⚠️ API 密钥、安装与更新都由你自己管理

```bash
git clone https://github.com/openclaw-easy/ViralMint
cd ViralMint && python run.py
```

**[👉 快速开始指南 ↓](#-快速开始)**

</td>
<td width="50%" valign="top">

### ☁️ 托管在 [viralmint.net](https://viralmint.net)

**免安装 · 预付额度 · 每日新手额度**

- ✅ **零配置**——登录即用
- ✅ 无需自己接入 API 密钥——一张账单、一个面板
- ✅ 已签名 + 已公证的桌面安装包（Mac / Win / Linux）
- ✅ OSS 版本不含的加料功能：**AI 音乐工作室**、**视觉风格预设**、**翻译并配音**、精致的 **工具（Tools）** 页面
- ⚠️ 不支持自动上传（需自行下载 mp4 后手动发布）
- ⚠️ 闭源 SaaS

**[🚀 免费试用 viralmint.net →](https://viralmint.net)**

</td>
</tr>
</table>

下方的 README 记录的是 **自托管** 版本——如果这正是你想走的路，请继续往下读；想要开箱即用的托管体验，直接前往 **[viralmint.net](https://viralmint.net)**。

<div align="center">

<!-- Activity badges (top row) — these auto-update from GitHub, so they
     reflect real maintenance signal at a glance for awesome-list reviewers
     and new visitors. -->
[![Stars](https://img.shields.io/github/stars/openclaw-easy/ViralMint?style=for-the-badge&logo=github&color=yellow)](https://github.com/openclaw-easy/ViralMint/stargazers)
[![Last commit](https://img.shields.io/github/last-commit/openclaw-easy/ViralMint?style=for-the-badge&color=brightgreen)](https://github.com/openclaw-easy/ViralMint/commits/main)
[![Release](https://img.shields.io/github/v/release/openclaw-easy/ViralMint?style=for-the-badge&color=blue&label=latest)](https://github.com/openclaw-easy/ViralMint/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/openclaw-easy/ViralMint/ci.yml?branch=main&style=for-the-badge&logo=githubactions&logoColor=white&label=CI)](https://github.com/openclaw-easy/ViralMint/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue?style=for-the-badge)](LICENSE)

<!-- Stack badges (second row) — what the project is built on. -->
[![Website](https://img.shields.io/badge/Website-viralmint.net-0d9f6e?style=for-the-badge)](https://viralmint.net)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![React 18](https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Platform](https://img.shields.io/badge/macOS%20%7C%20Windows%20%7C%20Linux-lightgrey?style=for-the-badge)](#-快速开始)

</div>

---

> **手动创作者每天要做的事，ViralMint 用一条工作流全部自动化。**
> 跨 5 大平台发现热门视频，用本地 Whisper 转写并分析，用 AI 撰写原创脚本，渲染带逐字字幕的素材视频，再直接发布到 YouTube 和 TikTok——全部只需一条命令。可以在浏览器里跟它对话，也可以在 Telegram、WhatsApp、Discord 或 Slack 上跟它聊。

<p align="center">
  <img src="docs/screenshots/chat.webp" alt="ViralMint Chat — streaming AI agent that scouts trending videos, analyzes channels, and orchestrates the full pipeline" width="900" />
  <br/>
  <sub><i>与 AI 智能体对话：粘贴一个链接、要它去发现趋势、或直接启动一条工作流——它会在后台跑起正确的流水线。</i></sub>
</p>

## ✨ 为什么选择 ViralMint

|   |   |
|---|---|
| 🔒 **100% 本地** | SQLite、本地 Whisper、本地 FFmpeg。你的脚本、转写、下载和生成的视频永不离开你的机器。 |
| 🔑 **BYOK（自带密钥）** | 使用你自己的 Anthropic / OpenAI / OpenRouter / YouTube / Pexels 密钥，落盘时以 AES-256 加密。中间没有任何 ViralMint 后端。 |
| 📱 **需要时随手用手机** | 通过 Telegram、WhatsApp、Discord 或 Slack 与规划智能体（Planner）双向对话，任务通知也发到同一个会话里。 |
| 🤖 **是智能体架构，不是套壳聊天** | Scout、Download、Analyzer、Generator、Uploader、Planner——六个各司其职的智能体，由流式 AI 对话统一编排。 |
| 🆓 **开箱即免费** | Edge TTS（400+ 语音）、本地 Whisper、免版税音乐、Pexels 素材——最重的活儿全都免费。只为你主动选择升级的部分付费。 |
| 🪪 **AGPL-3.0** | 个人使用、以此创业、fork、修改都可以。唯一的要求：若你对外分发，需以相同许可证公开你的修改。 |

---

## 🚀 快速开始

### 前置依赖

| 工具 | macOS | Linux | Windows |
|:-----|:-----|:------|:--------|
| **Python 3.11+** | `brew install python` | `apt install python3.11` | [python.org](https://www.python.org/downloads/) |
| **Node.js 18+** | `brew install node` | `apt install nodejs npm` | [nodejs.org](https://nodejs.org/) |
| **FFmpeg** | `brew install ffmpeg` | `apt install ffmpeg` | [ffmpeg.org](https://ffmpeg.org/download.html) |
| **ImageMagick** | `brew install imagemagick` | `apt install imagemagick` | [imagemagick.org](https://imagemagick.org/) |

### 安装与运行

```bash
git clone https://github.com/openclaw-easy/ViralMint.git
cd ViralMint

python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                                  # optional — keys can also be set in the UI
python run.py
```

首次运行会安装前端依赖、构建 SPA、启动 API，并在浏览器中打开 **http://localhost:16888**。

> 💡 **还没有 API 密钥？** 启动后打开「设置 → AI 提供方」，把你的 Anthropic、OpenAI 或 OpenRouter 密钥直接粘进界面即可。OpenRouter 是一个统一网关——一把密钥就能用上 Claude、GPT、Gemini、Llama 和 Mistral。Edge TTS、Whisper、FFmpeg 和 yt-dlp 无需任何配置即可离线工作。

### 从源码构建桌面 `.app`（可选）

如果你更想要一个可点击的 `.app`，而不是终端命令，本仓库自带一套自包含的 PyInstaller 流水线，可把这份 OSS 源码打包成 macOS 的 `.dmg`、Linux 的 `.tar.gz` 或 Windows 的 `.zip`——同一份代码，运行时无需终端。

```bash
PYTHON_BIN=./venv/bin/python VIRALMINT_VERSION=0.1.0-dev \
  bash desktop/scripts/build-app.sh
```

产物会输出到 `desktop/release/`。首次构建约需 10–15 分钟（PyInstaller 打包是耗时大头）。跳过标志、签名/公证的环境变量以及冒烟测试步骤都写在 **[`desktop/README.md`](desktop/README.md)** 里。

注意：这份构建是 **OSS 应用的纯净打包**——浏览器就是界面，没有托盘启动器、没有自动更新，除非你提供自己的 Developer ID，否则也没有签名二进制文件。额外的精致体验（AI 音乐工作室、视觉风格预设、AI 图像/视频生成器、已签名并公证的安装包）来自预构建的 [viralmint.net](https://viralmint.net) 安装包。

---

<div align="center">

### 🚀 想要精致体验又不想折腾配置？

试试 **[viralmint.net](https://viralmint.net)**——同一套「发现 + 分析 + 生成」引擎，托管好、无需接入 API 密钥。30 秒注册，含用于试用的每日免费额度，附带已签名 + 已公证的桌面安装包 + AI 音乐工作室 + 视觉风格预设 + 翻译并配音。

**[免费试用 viralmint.net →](https://viralmint.net)**

</div>

---

## 🎯 功能特性

<table>
<tr>
<td width="50%" valign="top">

### 🔍 Scout（发现）
跨 **YouTube、TikTok、抖音和 Google Trends** 的多平台趋势发现，配合 AI 爆款评分、播放增速分析，以及异常值检测（相对频道基线的 3×–20×）。

</td>
<td width="50%" valign="top">

### 🧠 Analyze（分析）
本地 Whisper 转写，长视频也处理得干净利落；AI 洞察提取（钩子、结构、语气、留存风险、建议标题与钩子，以及一条可直接复用的复刻提示词）；分段级评分，以及针对每个视频的改进建议。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🎬 Generate（生成）
完整流水线：AI 脚本 → TTS 配音 → 按关键词匹配的 Pexels 素材 → 感知短语的动态字幕（支持中日韩 / 阿拉伯语 / 泰语）→ 音量均衡的背景音乐 → AI 缩略图。

</td>
<td width="50%" valign="top">

### ✂️ Clip Studio（切片工作室）
一条长视频 → 众多可发布的短视频。AI 找出最精彩的片段，并从 **钩子、流畅度、价值、趋势契合度和传播力** 逐项打分，把切点对齐到句子边界，剔除重复讲述的内容。可按 **平台或题材** 偏好挑选，可精确描述你想要什么（*「每个真正好笑的梗」*），也可 **手动指定时间区间**——还能选配静音修剪、emoji 字幕，以及烧录进画面的钩子浮层。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📤 Publish（发布）
直接上传到 **YouTube**（OAuth）和 **TikTok**（OAuth 或会话 Cookie），并配上按平台优化的标题、描述、标签与缩略图。

</td>
<td width="50%" valign="top">

### 💬 Chat（对话）
流式 WebSocket 对话，统一编排每一个智能体。说一句 *「去发现做菜的视频」* 或 *「下载这个链接」*，它就直接跑起来。可点按的快捷回复标签、绝不锁住输入框的追问，以及刷新后依然保留的富结果卡片。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📲 Messaging（消息）
通过 **Telegram、WhatsApp、Discord、Slack** 从手机双向对话。随时随地接收任务提醒、指挥规划智能体——同一个智能体，不同的通道。

</td>
<td width="50%" valign="top">

### ⬇️ 通用下载器
底层由 yt-dlp 驱动——支持 YouTube、TikTok、Bilibili、Instagram、Twitter、SoundCloud、Vimeo，以及 1000+ 其他站点。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🧰 Tools（工具）
18 个单一用途的小工具——字幕、重构图、GIF、变速、裁剪、字幕文件、水印、合并、自动缩放、音乐可视化、配音，外加 AI 助手（翻译、元数据、钩子分析、自动章节）。**大多数 100% 本地运行于 ffmpeg + Whisper——无需 API 密钥。** 每个都带内嵌的结果预览，以及一个 ✨ 增强按钮，用你自己的密钥润色提示词。

</td>
<td width="50%" valign="top">

### ✨ 主动式助手
对话会读取你的实时流水线——*已下载但未切片*、*已生成但未上传*、*已发现但未下载*——并主动建议价值最高的下一步，而不是干等你开口。

</td>
</tr>
</table>

---

## 📸 界面截图

<table>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/library.webp"><img src="docs/screenshots/library.webp" alt="Library — Scout results with virality scores" /></a>
  <sub><b>资料库——Scout 结果</b><br/>发现 154 条视频，按 AI 爆款分排序，一键即可下载。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/clip-studio.webp"><img src="docs/screenshots/clip-studio.webp" alt="Clip Studio — extract viral shorts from a long-form video" /></a>
  <sub><b>Clip Studio——爆款切片提取</b><br/>AI 从长视频里挑出最佳的 30–60 秒片段，逐一打分，并自动烧录字幕。</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/messaging.webp"><img src="docs/screenshots/messaging.webp" alt="Messaging — Telegram, WhatsApp, Discord, Slack" /></a>
  <sub><b>Messaging——从手机对话</b><br/>连接 Telegram、WhatsApp、Discord 或 Slack，即可操控规划智能体并接收任务通知。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/channel-analysis.webp"><img src="docs/screenshots/channel-analysis.webp" alt="My Channels — channel analytics" /></a>
  <sub><b>My Channels——频道分析</b><br/>用链接即可接入任意 YouTube/TikTok 频道。播放量、互动、中位播放量与异常值检测一览无余。</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/smart-video.webp"><img src="docs/screenshots/smart-video.webp" alt="Stock Video / Smart Video studio" /></a>
  <sub><b>Smart Video 工作室</b> <i>（视觉风格预设、AI 音乐标签页等加料功能为 <a href="https://viralmint.net">桌面安装包</a> 独享）</i><br/>把你自己的片段与素材混剪；逐字字幕；背景音乐；成本估算器。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/tools.webp"><img src="docs/screenshots/tools.webp" alt="Tools — single-purpose utilities" /></a>
  <sub><b>Tools</b> <i>（内置于 <a href="https://viralmint.net">桌面安装包</a>）</i><br/>Quick Chain、AI 图像、AI 音乐、配音、重构图、水印、翻译 + 配音、钩子检测器——用于收尾视频的单一用途小工具。</sub>
</td>
</tr>
</table>

---

## 🆓 无需 API 密钥即可使用的功能

| 功能 | 由谁驱动 | 费用 |
|:--------|:-----------|:-----|
| 从 1000+ 站点下载视频 | yt-dlp | $0 |
| 音频转写 | 本地 faster-whisper | $0 |
| 配音（400+ 语音，70+ 语言） | Edge TTS | $0 |
| 逐字动态字幕 | FFmpeg + ASS 字幕 | $0 |
| 背景音乐库 | 本地免版税曲库 | $0 |
| 音效自动铺放 | FFmpeg 合成 | $0 |
| 工具：重构图、GIF、变速、裁剪、水印、合并、自动缩放、音乐可视化、字幕文件…… | FFmpeg + Whisper | $0 |

YouTube/TikTok/Pexels 仍需免费的 API 密钥——链接见下一节。

---

## 🔑 自带密钥 (BYOK)

每把密钥都可以在 `.env` *或* 应用内「设置」中按用户设置——谁被设置了就以谁优先。按用户设置的密钥在存储前会经 **AES-256 加密**。密钥直连服务提供方；ViralMint 中间没有任何后端服务器。

| 用于 | 提供方 | 在哪里设置 | 费用 |
|:----|:---------|:------|:-----|
| AI 对话、脚本撰写、分析 | **Anthropic** · **OpenAI** · **OpenRouter** | [console.anthropic.com](https://console.anthropic.com) · [platform.openai.com](https://platform.openai.com/api-keys) · [openrouter.ai/keys](https://openrouter.ai/keys) —— 设置 → AI 提供方 | 按用量付费 |
| YouTube 发现 · 评论 · My Channels | YouTube Data API v3 | [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials) —— 设置 → 服务 API 密钥 | 每天免费 10K 配额 |
| 素材视频 | Pexels | [pexels.com/api](https://www.pexels.com/api/) | 免费 |
| 高级配音（可选） | OpenAI TTS | [platform.openai.com](https://platform.openai.com/api-keys) | 按用量付费 |
| TikTok / 抖音发现 | **TikHub API**（推荐） | [tikhub.io](https://tikhub.io) | 有免费额度 |
| YouTube / TikTok 上传 | OAuth | 设置中一键完成 | 免费 |
| Telegram / Discord / Slack | Bot 令牌 | 设置 → Messaging | 免费 |
| WhatsApp | 扫码配对 | 设置 → Messaging | 免费 |

> ⚠️ **TikTok / 抖音会话 Cookie 发现** 也作为高级兜底方案在设置中提供，但它违反平台的服务条款，而且平台看到「在活动」的正是那把 Cookie 对应的 TikTok/抖音账号。**除非你已明确接受这一风险，否则请使用 TikHub API 路径。** 详见 [LEGAL.md](LEGAL.md#tiktok)。

---

## 🏗️ 架构

```
                     ┌────────────────────────────────────────────────┐
                     │            React 18 + MUI 7 SPA                │
                     │       (served by FastAPI in production)        │
                     │  Chat · Channels · Library · Stock Video       │
                     │  Clip Studio · Messaging · Settings            │
                     └─────────────────┬──────────────────────────────┘
                                       │  HTTP + WebSocket
                                       ▼
                     ┌────────────────────────────────────────────────┐
                     │           FastAPI · localhost:16888            │
                     ├────────────────────────────────────────────────┤
                     │  Planner Agent ─── streaming chat + actions    │
                     │  Scout Agent ───── YouTube · TikTok · Douyin · │
                     │                    Google Trends               │
                     │  Download Agent ── yt-dlp (1000+ sites)        │
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

### 技术栈

| 层 | 技术栈 |
|:------|:------|
| **后端** | Python 3.11+ · FastAPI · SQLAlchemy 2.0（异步）· SQLite · WebSockets |
| **前端** | React 18 · Vite · MUI 7 · Zustand · React Router 6 |
| **AI（BYOK）** | Anthropic Claude SDK · OpenAI SDK · OpenRouter（一把密钥用上 300+ 模型） |
| **转写** | faster-whisper（本地、多语言、可感知 GPU） |
| **TTS** | Edge TTS（免费）· OpenAI TTS |
| **视频** | Pexels 素材 · FFmpeg · Ken Burns 图片兜底 |
| **字幕** | FFmpeg + ASS（逐字高亮动画） |
| **下载** | yt-dlp |
| **消息** | python-telegram-bot · discord.py · slack-sdk · neonize（WhatsApp） |
| **安全** | Fernet（AES-256）加密落盘凭据 |

---

## 📁 项目结构

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
│       ├── pages/                  # Chat · Channels · Library · Stock Video · …
│       ├── components/             # Reusable UI (chat, settings, videos, …)
│       ├── hooks/                  # WebSocket, settings, jobs, source video
│       └── store/                  # Zustand global state
│
├── tests/                          # pytest test suite (92 tests)
├── storage/                        # Downloaded videos, audio, generated output (gitignored)
│
├── requirements.txt
├── .env.example
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE                         # AGPL-3.0
```

---

## 🤝 参与贡献

欢迎提交 Pull Request——修 bug、加新平台、加新消息通道、性能优化、文档，什么都行。请先读 [CONTRIBUTING.md](CONTRIBUTING.md) 了解流程与代码风格，并在提第一个 issue 前查阅 [行为准则](CODE_OF_CONDUCT.md)。

- 📋 **最近变更：** 见 [CHANGELOG.md](CHANGELOG.md)
- 🐛 **报告 bug：** [新建 issue](https://github.com/openclaw-easy/ViralMint/issues/new?template=bug_report.md)
- 💡 **提功能需求：** [新建 issue](https://github.com/openclaw-easy/ViralMint/issues/new?template=feature_request.md)
- 🔐 **安全漏洞：** 见 [SECURITY.md](SECURITY.md)——**请勿** 公开提 issue。

## 📜 许可证与使用条款

ViralMint 采用 **GNU Affero 通用公共许可证 v3.0（AGPL-3.0）** 授权（[LICENSE](LICENSE)）。

具体来说，这意味着：

- ✅ 个人使用、商业使用、修改、再分发均免费
- ✅ 可以在它之上运营 SaaS
- ⚠️ 若你对外分发它（或将其作为公开的网络服务运行），必须以相同的 AGPL-3.0 条款公开修改后的源码

**ViralMint 是一个你在自己机器上运行的工具。** 维护者既不托管你的内容，也不代理你的 API 调用——每一步动作都是你在用自己的平台和密钥亲自完成。使用发现与下载功能前，请先读 [LEGAL.md](LEGAL.md)，弄清楚哪些是被认可的（YouTube Data API、OAuth 上传、Pexels）、哪些是自担风险的（TikTok/抖音会话 Cookie 发现），以及在各平台服务条款下你需要为哪些内容负责。

---

<div align="center">

## ⭐ Star 历史

<a href="https://www.star-history.com/#openclaw-easy/ViralMint&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/ViralMint&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/ViralMint&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=openclaw-easy/ViralMint&type=Date" width="640" />
  </picture>
</a>

---

**用 FastAPI、React、Whisper、FFmpeg，以及大量异步 Python 打造。**

</div>

<table align="center">
<tr>
<td width="50%" align="center" valign="top">

### ☁️ 更想要托管版本？

免安装、无需配置 API 密钥、含免费新手额度。
**已签名 + 已公证的桌面安装包。**
含 AI 音乐工作室 · 视觉风格预设 · Tools 页面。

**[→ 免费试用 viralmint.net](https://viralmint.net)**

</td>
<td width="50%" align="center" valign="top">

### ⭐ 想帮这个项目一把？

最快的方式：点一下顶部的 star 按钮。
Star 能解锁 awesome-list 收录资格、吸引贡献者，
也向世界宣告：这件事很重要。

**[→ 给 openclaw-easy/ViralMint 点 Star](https://github.com/openclaw-easy/ViralMint)**

</td>
</tr>
</table>

<div align="center">

项目官网：**[viralmint.net](https://viralmint.net)** · 源码：**[github.com/openclaw-easy/ViralMint](https://github.com/openclaw-easy/ViralMint)** · 许可证：**[AGPL-3.0](LICENSE)**

</div>
