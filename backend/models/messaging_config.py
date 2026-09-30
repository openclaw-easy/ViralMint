# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
from sqlalchemy import Column, String, Boolean, DateTime
from datetime import datetime
from uuid import uuid4
from backend.database import Base


class MessagingConfig(Base):
    __tablename__ = "messaging_configs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String(36), default="local", index=True)
    channel = Column(String(20), nullable=False)   # telegram | whatsapp | discord | slack

    # Channel-specific credentials (all encrypted)
    bot_token_encrypted = Column(String, nullable=True)   # Telegram bot token
    chat_id = Column(String, nullable=True)               # Telegram chat_id (set when the owner pairs)
    api_key_encrypted = Column(String, nullable=True)     # WhatsApp/Slack API key
    webhook_url_encrypted = Column(String, nullable=True) # Slack/Feishu webhook URL

    # State
    is_active = Column(Boolean, default=True)
    connected_at = Column(DateTime, nullable=True)
    last_message_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
