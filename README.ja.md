<div align="center">

<img src="frontend/public/icon-192.png" alt="ViralMint" width="96" height="96" />

# ViralMint

### オープンソースのバイラルコンテンツ制作パイプライン

**トレンド発掘 → 競合分析 → 動画生成 → 自動投稿 → スマホからどこでも操作。**
100% ローカル。自分の API キーを使用。SaaS ロックインなし。テレメトリなし。

[🌐 ウェブサイト](https://viralmint.net) • [クイックスタート](#-クイックスタート) • [機能](#-機能) • [アーキテクチャ](#-アーキテクチャ) • [API キー](#-自分のキーを使う-byok) • [コントリビュート](CONTRIBUTING.md)

[English](README.md) · [简体中文](README.zh-CN.md) · **日本語**

</div>

## 🚦 ViralMint の2つの使い方

発掘・分析・生成のエンジンは同じ。運用上のトレードオフだけが異なります。あなたの働き方に合うほうを選んでください。

<table>
<tr>
<td width="50%" valign="top">

### 🛠 セルフホスト（このリポジトリ）

**自分のマシン · BYOK · AGPL-3.0**

- ✅ YouTube + TikTok へ直接投稿する **Uploader エージェント** を含むフルパイプライン
- ✅ Telegram / WhatsApp / Discord / Slack でスマホから操作
- ✅ 100% ローカル — キーもスクリプトも動画もマシンの外に出ません
- ✅ 改変・フォーク・再配布が可能（AGPL-3.0）
- ⚠️ API キー・インストール・アップデートは自分で管理

```bash
git clone https://github.com/openclaw-easy/ViralMint
cd ViralMint && python run.py
```

**[👉 クイックスタートガイドへ ↓](#-クイックスタート)**

</td>
<td width="50%" valign="top">

### ☁️ [viralmint.net](https://viralmint.net) でホスト版を利用

**インストール不要 · プリペイドクレジット · 毎日のスターター無料枠**

- ✅ **セットアップ不要** — サインインすればすぐ使える
- ✅ API キーの設定不要 — 請求もダッシュボードも1つに集約
- ✅ 署名・公証済みのデスクトップインストーラー（Mac / Win / Linux）
- ✅ OSS 版には無い追加機能: **AI Music Studio**、**Visual Style プリセット**、**翻訳＆吹き替え**、洗練された **Tools** ページ
- ⚠️ 自動アップロードなし（mp4 をダウンロードして手動で投稿）
- ⚠️ クローズドソースの SaaS

**[🚀 viralmint.net を無料で試す →](https://viralmint.net)**

</td>
</tr>
</table>

以下の README は **セルフホスト** 版について説明しています。このやり方を選ぶなら読み進めてください。ホスト版の体験を求めるなら **[viralmint.net](https://viralmint.net)** へどうぞ。

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
[![Platform](https://img.shields.io/badge/macOS%20%7C%20Windows%20%7C%20Linux-lightgrey?style=for-the-badge)](#-クイックスタート)

</div>

---

> **手動のクリエイターが毎日やっていることを、ViralMint は1つのワークフローで自動化します。**
> 5つのプラットフォームを横断してトレンド動画を見つけ、ローカルの Whisper で文字起こし・分析し、AI でオリジナル台本を書き、単語ごとのキャプション付きストック映像動画をレンダリングし、YouTube と TikTok へ直接投稿する — すべてを1つのコマンドから。ブラウザで話しかけるのも、Telegram・WhatsApp・Discord・Slack でチャットするのも自由自在です。

<p align="center">
  <img src="docs/screenshots/chat.webp" alt="ViralMint Chat — streaming AI agent that scouts trending videos, analyzes channels, and orchestrates the full pipeline" width="900" />
  <br/>
  <sub><i>AI エージェントとチャット: URL を貼る、発掘を頼む、ワークフローを起動する — 適切なパイプラインをバックグラウンドで実行します。</i></sub>
</p>

## ✨ ViralMint を選ぶ理由

|   |   |
|---|---|
| 🔒 **100% ローカル** | SQLite、ローカル Whisper、ローカル FFmpeg。台本・文字起こし・ダウンロード・生成動画はマシンの外に出ません。 |
| 🔑 **BYOK** | 自分の Anthropic / OpenAI / OpenRouter / YouTube / Pexels キーを使用。AES-256 で暗号化して保存。間に入る ViralMint のバックエンドはありません。 |
| 📱 **必要なときはスマホ優先** | Telegram・WhatsApp・Discord・Slack でプランナーエージェントと双方向チャット。同じスレッドでジョブ通知も受け取れます。 |
| 🤖 **チャットのラッパーではなくエージェントベース** | Scout・Download・Analyzer・Generator・Uploader・Planner — 目的特化型の6つのエージェントを、ストリーミング AI チャットがオーケストレーションします。 |
| 🆓 **すぐに無料で使える** | Edge TTS（400以上の音声）、ローカル Whisper、ロイヤリティフリー音楽、Pexels ストック — 重い処理はすべて無料。アップグレードを選んだぶんだけ課金されます。 |
| 🪪 **AGPL-3.0** | 個人利用も、ビジネス構築も、フォークも、改変も可能。唯一のお願いは、配布するなら改変部分を同じライセンスで共有することだけです。 |

---

## 🚀 クイックスタート

### 前提条件

| ツール | macOS | Linux | Windows |
|:-----|:-----|:------|:--------|
| **Python 3.11+** | `brew install python` | `apt install python3.11` | [python.org](https://www.python.org/downloads/) |
| **Node.js 18+** | `brew install node` | `apt install nodejs npm` | [nodejs.org](https://nodejs.org/) |
| **FFmpeg** | `brew install ffmpeg` | `apt install ffmpeg` | [ffmpeg.org](https://ffmpeg.org/download.html) |
| **ImageMagick** | `brew install imagemagick` | `apt install imagemagick` | [imagemagick.org](https://imagemagick.org/) |

### インストールと実行

```bash
git clone https://github.com/openclaw-easy/ViralMint.git
cd ViralMint

python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                                  # optional — keys can also be set in the UI
python run.py
```

初回起動時に、フロントエンドの依存関係をインストールし、SPA をビルドし、API を起動して、ブラウザで **http://localhost:16888** を開きます。

> 💡 **まだ API キーがない？** 起動後に Settings → AI Provider を開き、Anthropic・OpenAI・OpenRouter のキーを直接 UI に貼り付けてください。OpenRouter は統一ゲートウェイで、1つのキーで Claude・GPT・Gemini・Llama・Mistral を利用できます。Edge TTS・Whisper・FFmpeg・yt-dlp は設定不要でオフライン動作します。

### ソースからデスクトップ `.app` をビルド（任意）

ターミナルコマンドよりクリックできる `.app` が欲しい場合、このリポジトリには自己完結型の PyInstaller パイプラインが含まれており、この OSS ソースから macOS `.dmg`・Linux `.tar.gz`・Windows `.zip` を生成できます — 同じコードで、実行時にターミナルは不要です。

```bash
PYTHON_BIN=./venv/bin/python VIRALMINT_VERSION=0.1.0-dev \
  bash desktop/scripts/build-app.sh
```

出力は `desktop/release/` に置かれます。初回ビルドは約10〜15分（PyInstaller のバンドルが最も時間のかかる部分です）。スキップ用フラグ、署名・公証の環境変数、スモークテストの手順は **[`desktop/README.md`](desktop/README.md)** にあります。

補足: このビルドは **OSS アプリのバニラなバンドル** です — UI はブラウザで、トレイランチャーも自動アップデートも無く、Developer ID を用意しない限り署名済みバイナリにはなりません。追加の洗練された機能（AI Music Studio、Visual Style プリセット、AI 画像・動画ジェネレーター、署名・公証済みインストーラー）はプリビルドの [viralmint.net](https://viralmint.net) インストーラーで提供されます。

---

<div align="center">

### 🚀 セットアップ抜きで洗練された体験がほしい？

**[viralmint.net](https://viralmint.net)** を試してください — 発掘・分析・生成のエンジンは同じで、ホスト版、API キーの設定不要。30秒でサインアップ、評価用の毎日の無料枠付き。署名・公証済みデスクトップインストーラー + AI Music Studio + Visual Style プリセット + 翻訳＆吹き替えを同梱。

**[viralmint.net を無料で試す →](https://viralmint.net)**

</div>

---

## 🎯 機能

<table>
<tr>
<td width="50%" valign="top">

### 🔍 Scout
**YouTube、TikTok、Douyin、Google トレンド** を横断するマルチプラットフォームのトレンド発掘。AI によるバイラリティスコアリング、再生速度分析、外れ値検出（チャンネル基準の3×〜20×）を備えます。

</td>
<td width="50%" valign="top">

### 🧠 Analyze
ローカル Whisper による文字起こし（長尺もクリーンに処理）、AI によるインサイト抽出（フック、構成、トーン、離脱リスク、推奨タイトルとフック、そのまま実行できる再現プロンプト）、セグメント単位のスコアリング、動画ごとの改善提案。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🎬 Generate
フルパイプライン: AI 台本 → TTS 音声 → キーワードに合わせた Pexels ストック映像 → フレーズ単位のアニメーションキャプション（CJK / アラビア語 / タイ語対応）→ バランス調整された BGM → AI サムネイル。

</td>
<td width="50%" valign="top">

### ✂️ Clip Studio
1本の長尺動画 → 公開できる多数のショート。AI がベストな瞬間を見つけ、**フック・流れ・価値・トレンド適合・シェアされやすさ** でそれぞれを採点し、カットを文の切れ目にスナップし、繰り返された話は除外します。**プラットフォームやジャンル** でピックを偏らせたり、欲しいものを具体的に記述したり（*「ウケたジョークすべて」*）、**手動で時間範囲** を選んだりできます — オプションで無音トリミング、絵文字キャプション、焼き込みのフックオーバーレイも。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📤 Publish
**YouTube**（OAuth）と **TikTok**（OAuth またはセッションクッキー）へ、プラットフォーム最適化されたタイトル・説明文・タグ・サムネイル付きで直接アップロード。

</td>
<td width="50%" valign="top">

### 💬 Chat
すべてのエージェントをオーケストレーションするストリーミング WebSocket チャット。*「料理動画を発掘して」* や *「この URL をダウンロードして」* と言えばそのまま実行。タップできるクイックリプライのチップ、コンポーザーをロックしない追加質問、リロードをまたいで残るリッチな結果カード。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📲 Messaging
**Telegram・WhatsApp・Discord・Slack** でスマホから双方向チャット。ジョブ通知を受け取り、どこからでもプランナーに指示 — 同じエージェント、異なるトランスポート。

</td>
<td width="50%" valign="top">

### ⬇️ Universal Downloader
内部は yt-dlp — YouTube・TikTok・Bilibili・Instagram・Twitter・SoundCloud・Vimeo、その他1000以上のサイトに対応。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🧰 Tools
18の単機能ユーティリティ — キャプション、リフレーム、GIF、速度、トリム、字幕、ウォーターマーク、結合、オートズーム、ミュージックビジュアライザー、ボイスオーバー、さらに AI ヘルパー（翻訳、メタデータ、フック分析、自動チャプター）。**ほとんどが ffmpeg + Whisper で100% ローカルに動作 — API キー不要。** それぞれにインラインの結果プレビューと、自分のキーでプロンプトを磨く ✨ Enhance ボタンが付いています。

</td>
<td width="50%" valign="top">

### ✨ プロアクティブアシスタント
チャットはあなたのライブなパイプラインを読み取り — *ダウンロード済みだが未クリップ*、*生成済みだが未アップロード*、*発掘済みだが未ダウンロード* — 頼まれるのを待たずに、最も価値の高い次の一手を1つ提案します。

</td>
</tr>
</table>

---

## 📸 スクリーンショット

<table>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/library.webp"><img src="docs/screenshots/library.webp" alt="Library — Scout results with virality scores" /></a>
  <sub><b>Library — 発掘結果</b><br/>154本の動画を発見。AI バイラリティスコア順にソートされ、ワンクリックでダウンロード可能。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/clip-studio.webp"><img src="docs/screenshots/clip-studio.webp" alt="Clip Studio — extract viral shorts from a long-form video" /></a>
  <sub><b>Clip Studio — バイラルクリップ抽出</b><br/>AI が長尺動画からベストな30〜60秒を選び、採点し、自動でキャプションを焼き込みます。</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/messaging.webp"><img src="docs/screenshots/messaging.webp" alt="Messaging — Telegram, WhatsApp, Discord, Slack" /></a>
  <sub><b>Messaging — スマホからチャット</b><br/>Telegram・WhatsApp・Discord・Slack を接続してプランナーエージェントを操作し、ジョブ通知を受け取れます。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/channel-analysis.webp"><img src="docs/screenshots/channel-analysis.webp" alt="My Channels — channel analytics" /></a>
  <sub><b>My Channels — チャンネル分析</b><br/>任意の YouTube / TikTok チャンネルを URL で接続。再生数・エンゲージメント・中央値再生数・外れ値検出を表示。</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/smart-video.webp"><img src="docs/screenshots/smart-video.webp" alt="Stock Video / Smart Video studio" /></a>
  <sub><b>Smart Video スタジオ</b> <i>（Visual Style プリセットや AI Music タブなどの追加機能は <a href="https://viralmint.net">デスクトップインストーラー</a> 限定です）</i><br/>自分のクリップとストック映像をミックス。単語ごとのキャプション、BGM、コスト見積もり。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/tools.webp"><img src="docs/screenshots/tools.webp" alt="Tools — single-purpose utilities" /></a>
  <sub><b>Tools</b> <i>（<a href="https://viralmint.net">デスクトップインストーラー</a> に同梱）</i><br/>Quick Chain、AI Image、AI Music、ボイスオーバー、リフレーム、ウォーターマーク、翻訳＆吹き替え、フック検出 — 動画を仕上げるための単機能ユーティリティ。</sub>
</td>
</tr>
</table>

---

## 🆓 API キーなしで動くもの

| 機能 | 使用技術 | コスト |
|:--------|:-----------|:-----|
| 1000以上のサイトからの動画ダウンロード | yt-dlp | $0 |
| 音声の文字起こし | ローカル faster-whisper | $0 |
| ボイスオーバー（400以上の音声、70以上の言語） | Edge TTS | $0 |
| 単語ごとのアニメーションキャプション | FFmpeg + ASS 字幕 | $0 |
| BGM ライブラリ | ロイヤリティフリーのローカルライブラリ | $0 |
| 効果音の自動配置 | FFmpeg 合成 | $0 |
| Tools: リフレーム、GIF、速度、トリム、ウォーターマーク、結合、オートズーム、ミュージックビジュアライザー、字幕… | FFmpeg + Whisper | $0 |

YouTube / TikTok / Pexels には引き続き無料の API キーが必要です — リンクは次のセクションに。

---

## 🔑 自分のキーを使う (BYOK)

各キーは `.env` *または* アプリ内の Settings でユーザーごとに設定できます — 設定されているほうが優先されます。ユーザーごとのキーは保存前に **AES-256 で暗号化** されます。キーはプロバイダーへ直接送られ、間に入る ViralMint のバックエンドサーバーはありません。

| 用途 | プロバイダー | 場所 | コスト |
|:----|:---------|:------|:-----|
| AI チャット、台本作成、分析 | **Anthropic** · **OpenAI** · **OpenRouter** | [console.anthropic.com](https://console.anthropic.com) · [platform.openai.com](https://platform.openai.com/api-keys) · [openrouter.ai/keys](https://openrouter.ai/keys) — Settings → AI Provider | 従量課金 |
| YouTube 発掘・コメント・My Channels | YouTube Data API v3 | [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials) — Settings → Service API Keys | 無料 1日1万ユニット |
| ストック映像 | Pexels | [pexels.com/api](https://www.pexels.com/api/) | 無料 |
| プレミアムボイスオーバー（任意） | OpenAI TTS | [platform.openai.com](https://platform.openai.com/api-keys) | 従量課金 |
| TikTok / Douyin 発掘 | **TikHub API**（推奨） | [tikhub.io](https://tikhub.io) | 無料枠あり |
| YouTube / TikTok アップロード | OAuth | Settings でワンクリック | 無料 |
| Telegram / Discord / Slack | Bot トークン | Settings → Messaging | 無料 |
| WhatsApp | QR スキャンでペアリング | Settings → Messaging | 無料 |

> ⚠️ **TikTok / Douyin のセッションクッキーによる発掘** も Settings に高度なフォールバックとして用意されていますが、これは各プラットフォームの利用規約に違反しており、クッキーの元となる TikTok / Douyin アカウントが、TikTok / Douyin から見て実際に動作しているアカウントとみなされます。**そのリスクを明確に受け入れた場合を除き、TikHub API 経路を使用してください。** 詳細は [LEGAL.md](LEGAL.md#tiktok) を参照してください。

---

## 🏗️ アーキテクチャ

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

### 技術スタック

| レイヤー | スタック |
|:------|:------|
| **バックエンド** | Python 3.11+ · FastAPI · SQLAlchemy 2.0 (async) · SQLite · WebSockets |
| **フロントエンド** | React 18 · Vite · MUI 7 · Zustand · React Router 6 |
| **AI (BYOK)** | Anthropic Claude SDK · OpenAI SDK · OpenRouter (1つのキーで300以上のモデル) |
| **文字起こし** | faster-whisper（ローカル、多言語、GPU 対応） |
| **TTS** | Edge TTS（無料）· OpenAI TTS |
| **動画** | Pexels ストック · FFmpeg · Ken Burns 画像フォールバック |
| **キャプション** | FFmpeg + ASS（単語ごとのハイライトアニメーション） |
| **ダウンロード** | yt-dlp |
| **メッセージング** | python-telegram-bot · discord.py · slack-sdk · neonize (WhatsApp) |
| **セキュリティ** | 保存時の認証情報に Fernet (AES-256) |

---

## 📁 プロジェクト構成

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

## 🤝 コントリビュート

プルリクエスト歓迎 — バグ修正、新プラットフォーム、追加のメッセージングチャンネル、パフォーマンス改善、ドキュメント、何でも。ワークフローとハウススタイルは [CONTRIBUTING.md](CONTRIBUTING.md) を、最初の issue を立てる前に [行動規範](CODE_OF_CONDUCT.md) をご確認ください。

- 📋 **最近の変更:** [CHANGELOG.md](CHANGELOG.md) を参照
- 🐛 **バグ報告:** [issue を作成](https://github.com/openclaw-easy/ViralMint/issues/new?template=bug_report.md)
- 💡 **機能リクエスト:** [issue を作成](https://github.com/openclaw-easy/ViralMint/issues/new?template=feature_request.md)
- 🔐 **セキュリティ脆弱性:** [SECURITY.md](SECURITY.md) を参照 — 公開 issue は **立てないでください**。

## 📜 ライセンスと利用条件

ViralMint は **GNU Affero General Public License v3.0**（[LICENSE](LICENSE)）の下でライセンスされています。

実際のところ、それは次を意味します:

- ✅ 個人利用・商用利用・改変・再配布が無料
- ✅ その上に SaaS を運営してよい
- ⚠️ 配布する場合（または公開ネットワークサービスとして運用する場合）、改変したソースを同じ AGPL-3.0 の条件で共有する必要があります

**ViralMint は自分のマシンで動かすツールです。** メンテナーはあなたのコンテンツをホストせず、API 呼び出しをプロキシもしません — すべての操作は、あなた自身のプラットフォームとキーで、あなたが行うものです。発掘やダウンローダーの機能を使う前に [LEGAL.md](LEGAL.md) を読み、何が公認されており（YouTube Data API、OAuth アップロード、Pexels）、何が自己責任で（TikTok / Douyin のセッションクッキー発掘）、各プラットフォームの利用規約の下で何に対して責任を負うのかを理解してください。

---

<div align="center">

## ⭐ スター履歴

<a href="https://www.star-history.com/#openclaw-easy/ViralMint&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/ViralMint&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/ViralMint&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=openclaw-easy/ViralMint&type=Date" width="640" />
  </picture>
</a>

---

**FastAPI、React、Whisper、FFmpeg、そして大量の非同期 Python で構築。**

</div>

<table align="center">
<tr>
<td width="50%" align="center" valign="top">

### ☁️ ホスト版のほうがいい？

インストール不要、API キーの設定不要、無料のスターター枠付き。
**署名・公証済みのデスクトップインストーラー。**
AI Music Studio · Visual Style プリセット · Tools ページを同梱。

**[→ viralmint.net を無料で試す](https://viralmint.net)**

</td>
<td width="50%" align="center" valign="top">

### ⭐ このプロジェクトを応援したい？

いちばん早い応援方法: 上部のスターボタンを押すこと。
スターは awesome リストへの掲載資格を解き、コントリビューターを引き寄せ、
このプロジェクトに価値があると世界に示します。

**[→ openclaw-easy/ViralMint にスター](https://github.com/openclaw-easy/ViralMint)**

</td>
</tr>
</table>

<div align="center">

プロジェクトサイト: **[viralmint.net](https://viralmint.net)** · ソース: **[github.com/openclaw-easy/ViralMint](https://github.com/openclaw-easy/ViralMint)** · ライセンス: **[AGPL-3.0](LICENSE)**

</div>
