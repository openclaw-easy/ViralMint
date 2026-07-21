# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
WebSocket connection manager.
Supports multiple concurrent connections (for future multi-user support).
"""
import json
import logging
from typing import Dict, Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        # user_id → set of active WebSocket connections
        self._connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: str = "local"):
        await websocket.accept()
        if user_id not in self._connections:
            self._connections[user_id] = set()
        self._connections[user_id].add(websocket)
        logger.info(f"WS connected: {user_id} (total: {len(self._connections[user_id])})")

    def disconnect(self, websocket: WebSocket, user_id: str = "local"):
        if user_id in self._connections:
            self._connections[user_id].discard(websocket)
            if not self._connections[user_id]:
                del self._connections[user_id]
        logger.info(f"WS disconnected: {user_id}")

    async def send(self, message: dict, user_id: str = "local"):
        """Send a JSON message to all connections for a user."""
        msg_type = message.get("type", "unknown")
        logger.debug("WS send → %s | type=%s", user_id, msg_type)

        # Backend is the single writer of rich cards + job rows into chat
        # history (attributed via the CHAT_SESSION ContextVar). on_ws_event
        # decides internally which types persist and must never raise, but we
        # still guard here so a history-write bug can never break WS delivery.
        try:
            from backend.core.chat_history import on_ws_event
            await on_ws_event(message, user_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("chat-history hook failed (non-fatal): %s", e)

        data = json.dumps(message)
        if user_id not in self._connections:
            logger.debug("WS send skipped — no connections for %s", user_id)
            return
        dead = set()
        for ws in self._connections[user_id]:
            try:
                await ws.send_text(data)
            except Exception as e:
                logger.debug("WS send failed for %s (removing dead connection): %s", user_id, e)
                dead.add(ws)
        for ws in dead:
            self._connections[user_id].discard(ws)

    async def broadcast(self, message: dict):
        """Send to ALL connected users."""
        for user_id in list(self._connections.keys()):
            await self.send(message, user_id)

    async def send_progress(self, job_id: str, pct: float, step: str, user_id: str = "local"):
        await self.send({
            "type": "job_progress",
            "job_id": job_id,
            "percent": round(pct, 1),
            "step": step,
        }, user_id)

    async def send_constraint_warning(
        self,
        constraint: str,
        message: str,
        severity: str = "warning",  # warning | error
        wizard_id: str = None,
        user_id: str = "local",
    ):
        await self.send({
            "type": "constraint_warning",
            "constraint": constraint,
            "severity": severity,
            "message": message,
            "wizard_id": wizard_id,
        }, user_id)


# Singleton — import this everywhere
ws_manager = ConnectionManager()
