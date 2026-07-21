# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
import os
import secrets
import logging
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

logger = logging.getLogger(__name__)


def _resolve_data_dir() -> Path:
    """Honor VIRALMINT_DATA_DIR (packaged builds point it at a user-writable
    path); dev runs and fresh checkouts fall back to CWD so the DB, storage
    and .env all live relative to where you launched — matching the original
    OSS behavior."""
    override = os.getenv("VIRALMINT_DATA_DIR")
    if override:
        p = Path(override).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return Path.cwd()


DATA_DIR = _resolve_data_dir()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(DATA_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ───────────────────────────────────────────
    APP_NAME: str = "ViralMint"
    DEBUG: bool = True
    # Loopback by default. The README markets ViralMint as "100% local —
    # your scripts, transcripts, downloads, and generated videos never
    # leave your machine," and binding to 0.0.0.0 silently breaks that
    # promise: anyone on the same WiFi can reach your library, chat
    # sessions and (encrypted) credential store. Users who genuinely
    # want LAN access (e.g. driving the planner from a phone) can set
    # HOST=0.0.0.0 in their .env explicitly.
    HOST: str = "127.0.0.1"
    PORT: int = 16888
    SECRET_KEY: str = Field(default="")
    ENCRYPTION_KEY: str = Field(default="")  # Fernet key — auto-generated on first run if empty

    # ── Database ──────────────────────────────────────
    DATABASE_URL: str = f"sqlite+aiosqlite:///{DATA_DIR / 'viralmint.db'}"

    # ── AI Providers (BYOK) ───────────────────────────
    # Set at least one. OpenRouter unlocks Claude / GPT / Gemini / Llama
    # / Mistral through a single key — handy if you want to mix premium
    # models without managing multiple provider accounts.
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""

    # ── Service keys (BYOK) ───────────────────────────
    # All optional — features gracefully degrade when keys are missing.
    YOUTUBE_API_KEY: str = ""           # YouTube scout, channel reader, comments
    TIKHUB_API_KEY: str = ""            # TikTok / Douyin scout (alternative: cookies in Settings)
    PEXELS_API_KEY: str = ""            # Stock video footage

    # ── Upload OAuth ──────────────────────────────────
    YOUTUBE_CLIENT_ID: str = ""
    YOUTUBE_CLIENT_SECRET: str = ""
    YOUTUBE_REDIRECT_URI: str = "http://localhost:16888/api/settings/youtube-callback"

    TIKTOK_CLIENT_KEY: str = ""
    TIKTOK_CLIENT_SECRET: str = ""
    TIKTOK_REDIRECT_URI: str = "http://localhost:16888/api/settings/tiktok-upload-callback"

    # ── Frontend ──────────────────────────────────────
    FRONTEND_URL: str = "http://localhost:5173"

    # ── Storage paths ─────────────────────────────────
    @property
    def STORAGE_ROOT(self) -> Path:
        return DATA_DIR / "storage"

    @property
    def VIDEOS_DIR(self) -> Path:
        return self.STORAGE_ROOT / "videos"

    @property
    def AUDIO_DIR(self) -> Path:
        return self.STORAGE_ROOT / "audio"

    @property
    def GENERATED_DIR(self) -> Path:
        return self.STORAGE_ROOT / "generated"

    @property
    def THUMBNAILS_DIR(self) -> Path:
        return self.STORAGE_ROOT / "thumbnails"

    @property
    def TMP_DIR(self) -> Path:
        return self.STORAGE_ROOT / "tmp"


def _ensure_secrets(s: Settings) -> Settings:
    """Auto-generate SECRET_KEY and ENCRYPTION_KEY if missing OR invalid.

    Treat .env.example placeholder strings as "not set" so a fresh checkout
    (where someone `cp .env.example .env`'d) doesn't wedge with a literal
    "generate-with-..." value masquerading as a key. Also validate that
    ENCRYPTION_KEY is a real Fernet key (32 url-safe base64 bytes) — without
    this guard, every encrypt() / decrypt() blows up at first use with a
    cryptic "Fernet key must be 32 url-safe base64-encoded bytes" message.

    A VALID key already present (env var or a real .env value) is used as-is
    and never regenerated or rewritten — so a test harness that injects a
    valid Fernet key via env keeps a stable key across the process.
    """
    from cryptography.fernet import Fernet
    env_path = DATA_DIR / ".env"
    lines_to_append = []

    # Anything that starts with `change-me-` or matches the legacy
    # placeholder is a template literal copied from .env.example, not a key.
    secret_placeholders = {
        "change-me-in-production-use-secrets-token-hex-32",
        "generate-with-python-secrets-token-hex-32",
    }
    if not s.SECRET_KEY or s.SECRET_KEY in secret_placeholders:
        key = secrets.token_hex(32)
        s.__dict__["SECRET_KEY"] = key  # override in memory
        lines_to_append.append(f"SECRET_KEY={key}")
        logger.warning("SECRET_KEY was not set or held a placeholder — generated and saved to .env")

    encryption_placeholders = {"generate-with-fernet-generate-key"}

    def _is_valid_fernet(value: str) -> bool:
        try:
            Fernet(value.encode() if isinstance(value, str) else value)
            return True
        except Exception:
            return False

    if (
        not s.ENCRYPTION_KEY
        or s.ENCRYPTION_KEY in encryption_placeholders
        or not _is_valid_fernet(s.ENCRYPTION_KEY)
    ):
        key = Fernet.generate_key().decode()
        s.__dict__["ENCRYPTION_KEY"] = key
        lines_to_append.append(f"ENCRYPTION_KEY={key}")
        logger.warning("ENCRYPTION_KEY was missing/invalid — generated and saved to .env")

    if lines_to_append:
        # Persist to .env so keys survive restarts. We have to REWRITE existing
        # lines (not just append) — when the .env held a placeholder for a key
        # we just regenerated, an append-only path would leave the placeholder
        # in place and the regen would happen again on every restart, churning
        # a fresh key each time and breaking decryption of anything written in
        # the previous session.
        existing = env_path.read_text() if env_path.exists() else ""
        existing_lines = existing.splitlines() if existing else []
        new_kv = {line.split("=", 1)[0]: line for line in lines_to_append}
        replaced_keys: set[str] = set()
        out_lines: list[str] = []
        for line in existing_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                out_lines.append(line)
                continue
            key_name = stripped.split("=", 1)[0].strip()
            if key_name in new_kv:
                out_lines.append(new_kv[key_name])
                replaced_keys.add(key_name)
            else:
                out_lines.append(line)
        for key_name, line in new_kv.items():
            if key_name not in replaced_keys:
                out_lines.append(line)
        env_path.write_text("\n".join(out_lines) + "\n")

    return s


# Singleton — import this everywhere
settings = _ensure_secrets(Settings())
