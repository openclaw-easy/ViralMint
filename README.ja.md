<div align="center">

<img src="frontend/public/icon-192.png" alt="ViralMint" width="96" height="96" />

# ViralMint

### クリエイターのためのオープンソース・ローカルファーストな動画パイプライン

**トレンド発掘 → 長尺動画をクリップ → キャプション付きショートを生成 → モーショングラフィックスを設計 → 22のローカルツールで仕上げ。**
すべて自分のマシン上で。自分の API キーを使用。間に SaaS なし。テレメトリなし。

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
[![Platform](https://img.shields.io/badge/macOS%20%7C%20Windows%20%7C%20Linux-lightgrey?style=for-the-badge)](#-クイックスタート)

[クイックスタート](#-クイックスタート) • [機能](#-機能) • [無料で使えるもの](#-api-キーなしで動くもの) • [BYOK](#-自分のキーを使う-byok) • [コントリビュート](CONTRIBUTING.md)

[English](README.md) · [简体中文](README.zh-CN.md) · **日本語**

<br/>

<img src="docs/screenshots/clipper-bench.webp" alt="ViralMint Clip Studio — a cutting bench with a filmstrip timeline, speech lane and pending cuts" width="900" />

<sub><i>長尺動画を本物のタイムラインでショートに — 元動画のフレームをドラッグし、文の境界にスナップし、あるいは AI に提案させる。トレンド発掘・文字起こし・AI 動画・モーショングラフィックス・22の編集ツールまで、すべて同じアプリの中で動きます。</i></sub>

</div>

---

> **手動のクリエイターが十数個のタブとアプリを横断してやっていることを、ViralMint は1つのローカルワークフローとして実行します。**
> YouTube・TikTok・Douyin を横断してトレンド動画を見つけ、ローカルの Whisper で文字起こし・分析し、長尺動画を公開できるショートに切り分け、好みの AI でオリジナル台本を書き、キャプション付きストック映像動画をレンダリングする。最後に、AI が下書きしたタイトル・説明文・タグ付きの完成 MP4 が手元に残り、そのまま投稿できます。ブラウザから操作するのも、Telegram・WhatsApp・Discord・Slack でチャットするのも自由自在です。

## ✨ ViralMint を選ぶ理由

|   |   |
|---|---|
| 🔒 **100% ローカル** | SQLite、ローカル Whisper、ローカル FFmpeg。台本・文字起こし・ダウンロード・生成動画はマシンの外に出ません。 |
| 🔑 **BYOK、仲介者なし** | 自分の Anthropic / OpenAI / OpenRouter / YouTube / Pexels キーを使用。Fernet（AES-128-CBC + HMAC-SHA256）で暗号化して保存し、プロバイダーへ直接送信 — 間に ViralMint のサーバーはありません。 |
| 🤖 **チャットのラッパーではなくエージェント** | 目的特化型のエージェント群 — Planner、Scout、Download、Analyzer、Generator — を、実際に処理を実行するストリーミング AI チャットがオーケストレーションします。 |
| ✂️ **切る・生成する・設計する** | 完成動画への3つの道: 本物のタイムラインで長尺動画からショートを切り出す、台本からキャプション付きストック映像動画を生成する、あるいは映像を一切使わずにモーショングラフィックスを作る。 |
| 📱 **スマホから操作** | Telegram・WhatsApp・Discord・Slack でプランナーと双方向チャット — 同じスレッドでジョブ通知も届きます。 |
| 🆓 **すぐに無料で使える** | ローカル Whisper、Edge TTS（400以上の音声）、ロイヤリティフリー音楽、Pexels ストック、22の内蔵ツール（ほとんどは FFmpeg + Whisper だけで動作） — 重い処理はすべて $0。プラグインする AI のぶんだけ課金されます。 |

<sub>実戦仕込み: 毎コミットで **2,800テストの pytest スイート** と、実アプリをエンドツーエンドで操作するブラウザハーネスが走ります。AGPL-3.0 — フォークして、改変して、その上でビジネスを構築できます。</sub>

---

## 🎯 機能

<table>
<tr>
<td width="50%" valign="top">

### 🔍 Scout
**YouTube、TikTok、Douyin** を横断するマルチプラットフォームのトレンド発掘（さらに動的検索で yt-dlp 対応の任意のサイトにも対応）。AI によるバイラリティスコアリング、Google トレンドの需要シグナル、再生速度分析、外れ値検出（チャンネル基準の3×〜20×）を備えます。

</td>
<td width="50%" valign="top">

### 🧠 Analyze
ローカル Whisper による文字起こし（長尺もクリーンに処理）に加え、AI によるインサイト抽出 — フック、構成、トーン、離脱リスク、推奨タイトル、そのまま実行できる再現プロンプト — をセグメント単位で採点し、具体的な改善提案を添えます。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🎬 Generate
フルパイプライン: AI 台本 → TTS 音声 → キーワードに合わせた Pexels ストック映像 → フレーズ単位のアニメーションキャプション（CJK / アラビア語 / タイ語対応）→ バランス調整された BGM → AI サムネイル → 完成した MP4。

</td>
<td width="50%" valign="top">

### ✂️ Clip Studio
1本の長尺動画 → 公開できる多数のショートを、**本物のタイムライン** の上で切り出します。元動画そのもののフィルムストリップをドラッグすると、ハンドルを動かすあいだカットの最初と最後のフレームがそのまま見え、切れ目は文の境界にスナップするので単語の途中から始まることがありません。正確なタイムコードを直接入力しても、番組ノートの時刻を貼り付けても構いませんし、**AI に提案させる** こともできます — AI のピックは調整も削除もできるブロックとして並び、レンダリングは何も起きません（フック・流れ・価値・トレンド適合・シェアされやすさで採点済み）。急ぐときは **Auto-cut**: 一度押すだけで全部やり、しかも「何本になるか」を先に伝えます。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🎞️ Motion Graphics
実写素材をまったく含まない第三の出力: キネティックタイポグラフィ、アニメーションする数値カード、ロワーサード、プロダクト紹介。作りたいものを説明すれば AI が本物のコンポジションを書き、埋め込みのアニメーションスタジオ（タイムライン・レイヤー・インスペクター付き）で仕上げて、**すべて自分のマシン上でレンダリング** します。オンデマンドインストール — ダウンロードには同梱されていません。

</td>
<td width="50%" valign="top">

### 🗂️ Library
所有するものすべてを一つのファセット表示に: レンダリング成果物、ダウンロード、あらゆるツールの出力、音楽フォルダ。2つの問いに2つのコントロールが対応します — タブはそのファイルが**何であるか**（動画 / 画像 / 音声 / ファイル）、チップは**どこから来たか**（作成 / 編集 / ソース）を示すので、ダウンロードした mp3 がどちらか一方を選ぶ必要はありません。**ソース別** にまとめれば、ダウンロード1本とそこから作ったすべてが並びます。**Activity** はどのページからでも開けて、進行中の処理を確認できます。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 💬 Chat
すべてのエージェントをオーケストレーションするストリーミング WebSocket チャット。*「料理動画を発掘して」* や *「この URL をダウンロードして」* と言えばそのまま実行。タップできるクイックリプライのチップ、コンポーザーをロックしない追加質問、リロードをまたいで残るリッチな結果カード。 チャットはあなたのライブなパイプラインを読み取り — *ダウンロード済みだが未クリップ*、*生成済みだが未投稿*、*発掘済みだが未ダウンロード* — 頼まれるのを待たずに、最も価値の高い次の一手を1つ提案します。

</td>
<td width="50%" valign="top">

### 📲 Messaging
**Telegram・WhatsApp・Discord・Slack** でスマホから双方向チャット。どこからでもプランナーに指示し、ジョブが完了したら通知を受け取る — 同じエージェント、異なるトランスポート。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### ⬇️ ユニバーサルダウンローダー
内部は yt-dlp — YouTube・TikTok・Bilibili・Instagram・Twitter/X・SoundCloud・Vimeo、その他 **1,800以上のサイト** に対応。ウォーターマークなし、広告なし、上限なし。

</td>
<td width="50%" valign="top">

### 🧰 22の内蔵ツール
単機能ユーティリティ — キャプション、リフレーム、GIF、速度、トリム、字幕、ウォーターマーク、結合、オートズーム、ミュージックビジュアライザー、ボイスオーバー、さらに AI ヘルパー（翻訳、メタデータ、フック分析、自動チャプター）。**ほとんどが FFmpeg + Whisper で100% ローカルに動作 — API キー不要。** それぞれにインラインの結果プレビューが付いています。

</td>
</tr>
</table>

---

## 🆓 API キーなしで動くもの

高価な部分は無料でローカルに動きます。プラグインする AI のぶんだけ課金されます。

| 機能 | 使用技術 | コスト |
|:--------|:-----------|:-----|
| 1,800以上のサイトからの動画ダウンロード | yt-dlp | $0 |
| 音声の文字起こし | ローカル faster-whisper | $0 |
| ボイスオーバー（400以上の音声、70以上の言語） | Edge TTS | $0 |
| 単語ごとのアニメーションキャプション | FFmpeg + ASS 字幕 | $0 |
| BGM ライブラリ | ロイヤリティフリーのローカルライブラリ | $0 |
| 効果音の自動配置 | FFmpeg 合成 | $0 |
| Tools: リフレーム、GIF、速度、トリム、ウォーターマーク、結合、オートズーム、ミュージックビジュアライザー、字幕… | FFmpeg + Whisper | $0 |

YouTube / TikTok / Pexels には引き続き無料の API キーが必要です — リンクは [BYOK セクション](#-自分のキーを使う-byok) に。

---

## 🚀 クイックスタート

### 前提条件

| ツール | macOS | Linux | Windows |
|:-----|:-----|:------|:--------|
| **Python 3.11+** | `brew install python` | `apt install python3.11` | [python.org](https://www.python.org/downloads/) |
| **Node.js 18+** | `brew install node` | `apt install nodejs npm` | [nodejs.org](https://nodejs.org/) |
| **FFmpeg** | `brew install ffmpeg` | `apt install ffmpeg` | [ffmpeg.org](https://ffmpeg.org/download.html) |

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

> 💡 **まだ API キーがない？** 起動後に **Settings → AI Provider** を開き、Anthropic・OpenAI・OpenRouter のキーを貼り付けてください。OpenRouter は300以上のモデルへの統一ゲートウェイで、1つのキーで Claude・GPT・Gemini・Llama・Mistral を利用できます。Edge TTS・Whisper・FFmpeg・yt-dlp はすべて設定不要でオフライン動作します。

### ソースからデスクトップアプリをビルド（任意）

ターミナルコマンドよりクリックできるアプリが欲しい場合、自己完結型の PyInstaller パイプラインがこのソースから macOS `.dmg`・Linux `.tar.gz`・Windows `.zip` をビルドします — UI は引き続きブラウザです。

```bash
PYTHON_BIN=./venv/bin/python VIRALMINT_VERSION=0.1.0-dev \
  bash desktop/scripts/build-app.sh
```

出力は `desktop/release/` に置かれます。初回ビルドは約10〜15分（PyInstaller のバンドルが最も時間のかかる部分です）。スキップ用フラグ、署名・公証の環境変数、スモークテストの手順は **[`desktop/README.md`](desktop/README.md)** にあります。

---

## 🔑 自分のキーを使う (BYOK)

各キーは `.env` *または* アプリ内の **Settings** でユーザーごとに設定できます — 設定されているほうが優先されます。ユーザーごとのキーは保存前に **Fernet で暗号化**（AES-128-CBC + HMAC-SHA256）されます。キーはプロバイダーへ直接送られ、間に入る ViralMint のバックエンドサーバーはありません。

| 用途 | プロバイダー | 場所 | コスト |
|:----|:---------|:------|:-----|
| AI チャット、台本作成、分析 | **Anthropic** · **OpenAI** · **OpenRouter** | [console.anthropic.com](https://console.anthropic.com) · [platform.openai.com](https://platform.openai.com/api-keys) · [openrouter.ai/keys](https://openrouter.ai/keys) — Settings → AI Provider | 従量課金 |
| YouTube 発掘・コメント・My Channels | YouTube Data API v3 | [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials) — Settings → Service API Keys | 無料 1日1万ユニット |
| ストック映像 | Pexels | [pexels.com/api](https://www.pexels.com/api/) | 無料 |
| プレミアムボイスオーバー（任意） | OpenAI TTS | [platform.openai.com](https://platform.openai.com/api-keys) | 従量課金 |
| TikTok / Douyin 発掘 | **TikHub API**（推奨） | [tikhub.io](https://tikhub.io) | 無料枠あり |
| Telegram / Discord / Slack | Bot トークン | Settings → Messaging | 無料 |
| WhatsApp | QR スキャンでペアリング | Settings → Messaging | 無料 |

> ⚠️ **TikTok / Douyin のセッションクッキーによる発掘** も Settings に高度なフォールバックとして用意されていますが、これは各プラットフォームの利用規約に違反しており、そのクッキーのアカウントが、プラットフォームから見て実際に動作しているアカウントとみなされます。**そのリスクを明確に受け入れた場合を除き、TikHub API 経路を使用してください。** 詳細は [LEGAL.md](LEGAL.md#tiktok) を参照してください。

---

## 🏗️ アーキテクチャ

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

### 技術スタック

| レイヤー | スタック |
|:------|:------|
| **バックエンド** | Python 3.11+ · FastAPI · SQLAlchemy 2.0 (async) · SQLite · WebSockets |
| **フロントエンド** | React 18 · Vite · MUI 7 · Zustand · React Router 6 |
| **AI (BYOK)** | Anthropic Claude SDK · OpenAI SDK · OpenRouter (1つのキーで300以上のモデル) |
| **文字起こし** | faster-whisper（ローカル、多言語、CPU int8） |
| **TTS** | Edge TTS（無料）· OpenAI TTS |
| **動画** | Pexels ストック · FFmpeg · Ken Burns 画像フォールバック |
| **キャプション** | FFmpeg + ASS（単語ごとのハイライトアニメーション） |
| **ダウンロード** | yt-dlp (1,800+ sites) |
| **メッセージング** | python-telegram-bot · discord.py · slack-sdk · neonize (WhatsApp) |
| **セキュリティ** | 保存時の認証情報に Fernet (AES-128-CBC + HMAC-SHA256) |

---

## 📸 スクリーンショット

<table>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/clipper-bench.webp"><img src="docs/screenshots/clipper-bench.webp" alt="Clip Studio — カッティングベンチ" /></a>
  <sub><b>Clip Studio — カッティングベンチ</b><br/>元動画そのもののフレームをドラッグ。音声レーン、文境界へのスナップ、IN/OUT プレビュー、入力できるタイムコード。Cut を押すまで何もレンダリングされません。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/auto-cut.webp"><img src="docs/screenshots/auto-cut.webp" alt="Auto-cut — 特急レーン" /></a>
  <sub><b>Auto-cut — 特急レーン</b><br/>AI に任せてレビュー工程を省略。押す前に、*この*動画では何本になるのかを伝えます。</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/library.webp"><img src="docs/screenshots/library.webp" alt="Library — 所有するものすべて" /></a>
  <sub><b>Library — 所有するものすべて</b><br/>レンダリング成果物・ダウンロード・全ツール出力・音楽フォルダを1つのビューに。タブは「何であるか」、チップは「どこから来たか」。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/motion-graphics.webp"><img src="docs/screenshots/motion-graphics.webp" alt="Motion Graphics — デザインされた映像をローカルで" /></a>
  <sub><b>Motion Graphics — デザインされた映像をローカルで</b><br/>キネティックタイポグラフィ、数値カード、ロワーサード。AI がコンポジションを書き、タイムラインで仕上げ、自分のマシンでレンダリング。</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/smart-video.webp"><img src="docs/screenshots/smart-video.webp" alt="Smart Video スタジオ" /></a>
  <sub><b>Smart Video スタジオ</b><br/>台本 → ナレーション → ストック映像 → 単語単位キャプション → BGM をワンパスで。自分のクリップを混ぜ、ビジュアルスタイルも選べます。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/tools.webp"><img src="docs/screenshots/tools.webp" alt="Tools — 22のユーティリティ" /></a>
  <sub><b>Tools — 22のユーティリティ</b><br/>リフレーム、クロップ、圧縮、ウォーターマーク、GIF、速度、トリム、字幕、ボイスオーバー、フック分析 — ほとんどが API キー不要でローカル動作。</sub>
</td>
</tr>
<tr>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/chat.webp"><img src="docs/screenshots/chat.webp" alt="Chat — 実際に動かすエージェント" /></a>
  <sub><b>Chat — 実際に動かすエージェント</b><br/>URL を貼る、ニッチを言う、動画を頼む。適切なエージェントを起動し、同じスレッドで結果を返します。</sub>
</td>
<td width="50%" align="center" valign="top">
  <a href="docs/screenshots/messaging.webp"><img src="docs/screenshots/messaging.webp" alt="Messaging — スマホから操作" /></a>
  <sub><b>Messaging — スマホから操作</b><br/>Telegram / WhatsApp / Discord / Slack を接続して同じエージェントに指示し、ジョブ完了の通知を受け取れます。</sub>
</td>
</tr>
</table>

---

## 📁 プロジェクト構成

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
│   └── services/                   # TTS, video gen, captions, music, yt-dlp, Whisper, …
│
├── frontend/
│   └── src/
│       ├── pages/                  # Chat · Channels · Library · Stock Video · Clip Studio · …
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

## 🤝 コントリビュート

プルリクエスト歓迎 — バグ修正、新プラットフォーム、追加のメッセージングチャンネル、パフォーマンス改善、ドキュメント、何でも。ワークフローとハウススタイルは [CONTRIBUTING.md](CONTRIBUTING.md) を、最初の issue を立てる前に [行動規範](CODE_OF_CONDUCT.md) をご確認ください。

- 📋 **最近の変更:** [CHANGELOG.md](CHANGELOG.md)
- 🐛 **バグ報告:** [issue を作成](https://github.com/openclaw-easy/ViralMint/issues/new?template=bug_report.md)
- 💡 **機能リクエスト:** [issue を作成](https://github.com/openclaw-easy/ViralMint/issues/new?template=feature_request.md)
- 🔐 **セキュリティ脆弱性:** [SECURITY.md](SECURITY.md) — 公開 issue は **立てないでください**。

## 📜 ライセンスと利用条件

ViralMint は **GNU Affero General Public License v3.0**（[LICENSE](LICENSE)）の下でライセンスされています。

- ✅ 個人利用・商用利用・改変・再配布が無料
- ✅ その上に SaaS を運営してよい
- ⚠️ 配布する場合（または公開ネットワークサービスとして運用する場合）、改変したソースを同じ AGPL-3.0 の条件で共有する必要があります

**ViralMint は自分のマシンで動かすツールです。** メンテナーはあなたのコンテンツをホストせず、API 呼び出しをプロキシもしません — すべての操作は、あなた自身のプラットフォームとキーで、あなたが行うものです。発掘やダウンローダーの機能を使う前に [LEGAL.md](LEGAL.md) を読み、何が公認されており（YouTube Data API、Pexels）、何が自己責任で（TikTok / Douyin のセッションクッキー発掘）、各プラットフォームの利用規約の下で何に対して責任を負うのかを理解してください。

---

### 🙋 セルフホストしたくない？

**[viralmint.net](https://viralmint.net)** にホスト版のビルドもあります — 発掘・分析・生成のエンジンは同じで、署名・公証済み、設定する API キーもありません（BYOK の代わりにプリペイドクレジット）。クローズドソースで — 自分でキーやインストールを管理したくない人向けの、異なるトレードオフです。詳しい比較と FAQ: **[docs/hosted-vs-self-hosted.md](docs/hosted-vs-self-hosted.md)**。ホスト版でよく使われる入口: [YouTube AI クリップ抽出](https://viralmint.net/youtube-clip-extractor/)、[YouTube 動画ダウンローダー](https://viralmint.net/tools/video-downloader/youtube/)、[AI 字幕ジェネレーター](https://viralmint.net/tools/ai-caption-generator/)、[AI 動画ジェネレーター](https://viralmint.net/tools/ai-video-generator/)。それ以外は、必要なものはすべてここにあります — 読み進めて `python run.py` を実行してください。

---

<div align="center">

## ⭐ ViralMint が役に立ったら、スターを

スターはこのプロジェクトを助ける最大の要素です — コントリビューターを引き寄せ、awesome リストへの掲載資格を解き、他のクリエイターに一見の価値があると伝えます。ワンクリックで完了します。

**[⭐ openclaw-easy/ViralMint にスター](https://github.com/openclaw-easy/ViralMint)**

<!-- Star-history chart. The repo slug is LOWERCASE on purpose: the
     mixed-case URL answers 301, and GitHub's image proxy caches whatever the
     first fetch returned. Re-probe before touching this:
     curl -s -o /dev/null -w '%{http_code}' 'https://api.star-history.com/svg?repos=openclaw-easy/viralmint&type=Date'
     NEVER put a raw github_pat_ token in this URL (it would be public). -->
<a href="https://star-history.com/#openclaw-easy/ViralMint&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/viralmint&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=openclaw-easy/viralmint&type=Date" />
    <img alt="ViralMint スター履歴チャート" src="https://api.star-history.com/svg?repos=openclaw-easy/viralmint&type=Date" width="600" />
  </picture>
</a>

**[📈 ViralMint のスター履歴を見る →](https://star-history.com/#openclaw-easy/ViralMint&Date)**

<br/>

**FastAPI、React、Whisper、FFmpeg、そして大量の非同期 Python で構築。**

ウェブサイト: **[viralmint.net](https://viralmint.net)** · ソース: **[github.com/openclaw-easy/ViralMint](https://github.com/openclaw-easy/ViralMint)** · ライセンス: **[AGPL-3.0](LICENSE)**

</div>
