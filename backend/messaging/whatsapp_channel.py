# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""
WhatsApp messaging channel via neonize (Python bindings to Go's whatsmeow).

Uses QR-scan linked-device pairing — the same flow as WhatsApp Web. neonize
ships a ~30MB native library; no Node sidecar, no Meta Cloud API business
verification. Session credentials auto-persist in a per-user SQLite file at
`<DATA_DIR>/storage/messaging/whatsapp_{user_id}.db` (0600).

Lifecycle:
  - start():     resume every active MessagingConfig row (re-connects sessions paired previously)
  - configure(): wipe the session file and start a fresh pairing — QR pushed via WS `whatsapp_qr`
  - disconnect(): logout, delete session file, clear the DB row
  - send():      deliver a NotificationPayload to the paired self-JID

Inbound messages route to the PlannerAgent through the same callback Telegram
uses: async (text, user_id) -> str.

Ownership: a linked device sees EVERY chat on the phone.
The old handler ran any contact's or group's message through the agent as the
owner, answered them, and re-pointed notifications at whoever wrote last. Now
the only input is what the owner writes in their own "Message yourself" chat:
IsFromMe, in the chat addressed to the paired account's own phone JID or LID
(from client.get_me()). Everything else is ignored, and inbound messages
never move the notification target.

Ban risk: linked-device pairing on a personal WhatsApp number is grey-area
under ToS. The UI surfaces this clearly before the user scans.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid as _uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from sqlalchemy import select

from backend.config import settings
from backend.core.ws_manager import ws_manager
from backend.database import AsyncSessionLocal
from backend.messaging._shared import (
    deactivate_config,
    send_with_retry,
    split_text,
    strip_action_blocks,
    touch_last_message,
)
from backend.messaging.base import (
    MessagingChannel,
    NotificationEvent,
    NotificationPayload,
)
from backend.models.messaging_config import MessagingConfig

logger = logging.getLogger(__name__)

PlannerCallback = Callable[[str, str], Awaitable[str]]

# WhatsApp accepts much longer messages than Telegram; keep headroom.
WA_MSG_LIMIT = 4000

def _session_dir() -> Path:
    """Where WhatsApp session files live — inside DATA_DIR.

    This used to be `Path("storage") / "messaging"`, relative to the working
    directory. In a dev run that happens to be DATA_DIR; in the packaged app
    the launcher starts the backend inside the app bundle, so the session was
    written into the bundle and every update deleted it."""
    return settings.STORAGE_ROOT / "messaging"


# ── Lazy neonize import ──────────────────────────────────────────────────────

_neonize_mod: Optional[dict] = None
_import_error: Optional[str] = None


def _import_neonize() -> Optional[dict]:
    """Import neonize on demand. Returns a dict of the pieces we need, or None
    if the package is missing / native library fails to load."""
    global _neonize_mod, _import_error
    if _neonize_mod is not None:
        return _neonize_mod
    if _import_error is not None:
        return None
    try:
        from neonize.aioze.client import ClientFactory, NewAClient  # type: ignore
        from neonize.aioze.events import (  # type: ignore
            ConnectedEv,
            DisconnectedEv,
            LoggedOutEv,
            MessageEv,
            PairStatusEv,
        )
        from neonize.utils import build_jid  # type: ignore

        _neonize_mod = {
            "ClientFactory": ClientFactory,
            "NewAClient": NewAClient,
            "ConnectedEv": ConnectedEv,
            "DisconnectedEv": DisconnectedEv,
            "LoggedOutEv": LoggedOutEv,
            "MessageEv": MessageEv,
            "PairStatusEv": PairStatusEv,
            "build_jid": build_jid,
        }
        return _neonize_mod
    except Exception as e:
        _import_error = f"{type(e).__name__}: {e}"
        logger.warning("neonize unavailable — WhatsApp channel disabled: %s", _import_error)
        return None


def _session_path(user_id: str) -> Path:
    d = _session_dir()
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id or "local")
    path = d / f"whatsapp_{safe}.db"
    # One-time move from the old working-directory location.
    legacy = Path.cwd() / "storage" / "messaging" / path.name
    if not path.exists() and legacy.exists() and legacy.resolve() != path.resolve():
        try:
            for suffix in ("", "-wal", "-shm"):
                src = legacy.with_name(legacy.name + suffix)
                if src.exists():
                    src.replace(path.with_name(path.name + suffix))
            logger.info("Moved WhatsApp session %s → %s", legacy, path)
        except OSError as e:
            logger.warning("Could not move WhatsApp session from %s: %s", legacy, e)
    return path


def _remove_session(path: Path) -> None:
    """Delete a session database and its SQLite side files."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        f = path.with_name(path.name + suffix)
        try:
            if f.exists():
                f.unlink()
        except OSError as e:
            logger.warning("Could not delete WhatsApp session file %s: %s", f, e)


def _restrict(path: Path) -> None:
    """The session holds the linked device's keys in plaintext — owner-only."""
    try:
        if path.exists():
            path.chmod(0o600)
    except OSError:
        pass


# ── Per-user state ───────────────────────────────────────────────────────────


class _UserClient:
    """Per-user WhatsApp client state. Holds the neonize client and its
    long-running connect() task."""

    def __init__(self, user_id: str, client, factory, session_path: Path):
        self.user_id = user_id
        self.client = client
        self.factory = factory
        self.session_path = session_path
        self.chat_id: Optional[str] = None   # self-JID; destination for notifications
        # The paired account's own identities (phone number user part, LID user
        # part) from client.get_me(). Inbound is accepted only from the chat
        # addressed to one of these.
        self.self_ids: set[str] = set()
        # Connection state from ConnectedEv / DisconnectedEv / LoggedOutEv.
        # neonize's `is_connected` / `is_logged_in` return an un-awaited
        # coroutine — always truthy — so they could never say "offline".
        self.online: bool = False
        self.paired: bool = False
        self.qr_seen: bool = False           # True after on_qr fires; used by pair-timeout watchdog
        self.phone_user: Optional[str] = None  # own phone number (user part), from get_me()
        self.connect_task: Optional[asyncio.Task] = None
        self.pair_watchdog: Optional[asyncio.Task] = None
        # Ring buffer of message IDs we sent, to suppress our own echoes on the
        # linked-device feed. Self-chat and our own outbound replies both arrive
        # back with IsFromMe=True, so IsFromMe alone is not a reliable filter.
        self.sent_ids: deque[str] = deque(maxlen=200)

    def note_sent(self, msg_id: Optional[str]) -> None:
        if msg_id:
            self.sent_ids.append(str(msg_id))

    def is_own_echo(self, msg_id: Optional[str]) -> bool:
        return bool(msg_id) and str(msg_id) in self.sent_ids

    async def stop(self) -> None:
        """Stop the client and release its worker thread.

        neonize's connect runs the blocking Go `Neonize()` call on a worker
        thread that only returns when Go is told to STOP. `disconnect()` alone
        never did that, so every session parked a thread until exit (and
        could hold interpreter shutdown open). `client.stop()` is the call that
        ends it; cancelling the connect task sends the same signal as a
        fallback. Every step is bounded — this runs during app shutdown.
        """
        self.online = False
        try:
            if self.pair_watchdog and not self.pair_watchdog.done():
                self.pair_watchdog.cancel()
            if self.client is not None:
                for step in (self.client.disconnect, self.client.stop):
                    try:
                        await asyncio.wait_for(step(), timeout=5)
                    except Exception:
                        pass
            if self.connect_task and not self.connect_task.done():
                self.connect_task.cancel()
                try:
                    await asyncio.wait_for(self.connect_task, timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
        except Exception as e:
            logger.warning("WhatsApp stop error for user=%s: %s", self.user_id, e)

    def is_owner_message(self, is_from_me: bool, chat_user: Optional[str],
                         reply_user: Optional[str]) -> bool:
        """The owner writing in their own "Message yourself" chat — and only that."""
        if not is_from_me or not self.self_ids:
            return False
        return bool((chat_user and chat_user in self.self_ids)
                    or (reply_user and reply_user in self.self_ids))


# ── Channel ──────────────────────────────────────────────────────────────────


class WhatsAppChannel(MessagingChannel):
    channel_name = "whatsapp"

    def __init__(self) -> None:
        self._clients: dict[str, _UserClient] = {}
        self._planner_callback: Optional[PlannerCallback] = None
        # One pair / unpair / resume at a time. Two overlapping pairings each
        # deleted the session file under the other's live client, and the
        # loser kept running unreachable, with its own inbound handler.
        self._lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()

    # ── MessagingManager wiring ──────────────────────────────────────────────

    def set_planner_callback(self, callback: PlannerCallback) -> None:
        self._planner_callback = callback

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Resume every active WhatsApp session from DB on backend startup.
        Missing neonize silently no-ops — the frontend still renders the card."""
        if _import_neonize() is None:
            return

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(MessagingConfig).where(
                    MessagingConfig.channel == "whatsapp",
                    MessagingConfig.is_active == True,  # noqa: E712
                )
            )
            configs = result.scalars().all()

        for cfg in configs:
            path = _session_path(cfg.user_id)
            if not path.exists():
                # The session is gone (deleted, or lost with an old app bundle):
                # there is nothing to resume, so stop claiming it is active.
                logger.info("WhatsApp session file missing for user=%s; marking inactive", cfg.user_id)
                await deactivate_config(cfg.user_id, "whatsapp", clear_tokens=False)
                continue
            try:
                async with self._lock:
                    await self._spin_up(cfg.user_id, chat_id=cfg.chat_id)
            except Exception as e:
                logger.warning("Failed to resume WhatsApp for user=%s: %s", cfg.user_id, e)

    async def stop(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        await asyncio.gather(*(uc.stop() for uc in clients), return_exceptions=True)

    # ── MessagingChannel API ─────────────────────────────────────────────────

    async def is_configured(self, user_id: str) -> bool:
        uc = self._clients.get(user_id)
        return bool(uc and uc.paired and uc.online and uc.chat_id)

    async def send(self, user_id: str, payload: NotificationPayload) -> bool:
        uc = self._clients.get(user_id)
        if not uc or not uc.chat_id:
            return False

        text = _format_body(payload)
        neo = _import_neonize()
        if neo is None:
            return False

        target_user, target_server = _parse_stored_chat_id(uc.chat_id)
        try:
            jid = neo["build_jid"](target_user, target_server)
        except Exception as e:
            logger.warning(
                "WhatsApp build_jid failed for %s@%s: %s",
                target_user, target_server, e,
            )
            return False

        chunks = split_text(text, WA_MSG_LIMIT)

        async def _sender(chunk: str) -> None:
            resp = await uc.client.send_message(jid, chunk)
            uc.note_sent(getattr(resp, "ID", None))

        ok = await send_with_retry("whatsapp", user_id, chunks, _sender)
        if ok:
            await touch_last_message(user_id, "whatsapp")
        return ok

    # ── Public API used by backend/api/messaging.py ──────────────────────────

    async def configure(self, user_id: str) -> dict:
        """Wipe any existing session for this user and start a fresh pairing.
        Returns {awaiting_qr: True}. The QR bytes are pushed via WS `whatsapp_qr`.
        """
        neo = _import_neonize()
        if neo is None:
            raise ValueError(
                f"WhatsApp is unavailable on this machine: {_import_error or 'neonize not installed'}. "
                "Reinstall backend dependencies (pip install -r requirements.txt)."
            )

        async with self._lock:
            existing = self._clients.pop(user_id, None)
            if existing:
                # Unlink the old device from the phone too, or it stays listed
                # under Linked Devices forever.
                if existing.paired:
                    try:
                        await asyncio.wait_for(existing.client.logout(), timeout=5)
                    except Exception as e:
                        logger.debug("WhatsApp logout before re-pair failed: %s", e)
                await existing.stop()

            # Wipe the session so we get a fresh QR instead of resuming.
            _remove_session(_session_path(user_id))
            await self._spin_up(user_id, chat_id=None, fresh_pair=True)
        return {"awaiting_qr": True}

    async def disconnect(self, user_id: str) -> bool:
        async with self._lock:
            uc = self._clients.pop(user_id, None)
            if uc:
                if uc.paired:
                    try:
                        await asyncio.wait_for(uc.client.logout(), timeout=5)
                    except Exception as e:
                        logger.debug("WhatsApp logout failed for user=%s: %s", user_id, e)
                await uc.stop()
                # Remove the session so a reconnect needs a fresh pair.
                _remove_session(uc.session_path)

            await deactivate_config(user_id, "whatsapp", clear_tokens=False)
        await ws_manager.send({"type": "whatsapp_disconnected", "user_id": user_id}, user_id)
        return True

    async def send_test(self, user_id: str) -> bool:
        return await self.send(
            user_id,
            NotificationPayload(
                event=NotificationEvent.JOB_FAILED,
                title="ViralMint test",
                body="If you can read this, your WhatsApp link is live. 🎉",
            ),
        )

    def status(self, user_id: str) -> dict:
        """Synchronous status snapshot used by /api/messaging/status."""
        installed = _import_neonize() is not None
        uc = self._clients.get(user_id)

        if not installed:
            return {
                "connected": False,
                "installed": False,
                "pairing": False,
                "chat_id": None,
                "error": _import_error or "neonize not installed",
            }

        if not uc:
            return {
                "connected": False,
                "installed": True,
                "pairing": False,
                "chat_id": None,
            }

        return {
            "connected": bool(uc.online and uc.paired and uc.chat_id),
            "installed": True,
            "pairing": not uc.paired,
            # Paired but not online (boot before ConnectedEv, sleep, a network
            # drop): neonize reconnects on its own. The UI must NOT offer the
            # QR button here — re-pairing logs out a working session.
            "paired": bool(uc.paired),
            "chat_id": uc.chat_id,
        }

    # ── Internals ────────────────────────────────────────────────────────────

    async def _spin_up(self, user_id: str, chat_id: Optional[str], fresh_pair: bool = False) -> None:
        """Build a neonize client for the user, register handlers, kick off connect()."""
        neo = _import_neonize()
        if neo is None:
            raise RuntimeError("neonize is not available")

        path = _session_path(user_id)
        factory = neo["ClientFactory"](str(path))
        # neonize requires a stable uuid so the client can be uniquely
        # identified across restarts (it's stored alongside session state in
        # SQLite). Derive a deterministic uuid from the user_id.
        client_uuid = str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"viralmint:whatsapp:{user_id}"))
        client = factory.new_client(uuid=client_uuid)

        uc = _UserClient(user_id=user_id, client=client, factory=factory, session_path=path)
        uc.chat_id = chat_id
        # If a chat_id is already on file, the session is paired (we're just resuming)
        uc.paired = bool(chat_id) and not fresh_pair
        self._clients[user_id] = uc

        self._register_handlers(uc, neo)

        # client.connect() runs the event loop for this session. Launch as a task
        # so it doesn't block the API call — handlers fire as events arrive.
        uc.connect_task = asyncio.create_task(
            self._run_connect(uc), name=f"whatsapp-{user_id}"
        )
        logger.info("WhatsApp client spun up for user=%s (session=%s, resume=%s)",
                    user_id, path, not fresh_pair)

        # Fresh pairs: if QR never shows within 20s, the handshake was rejected
        # (usually "Client outdated 405" from WhatsApp rotating their minimum
        # version). Surface a specific warning instead of letting the dialog
        # hang at "Waiting for QR code…" indefinitely.
        if fresh_pair:
            uc.pair_watchdog = asyncio.create_task(
                self._watch_pair_start(uc), name=f"whatsapp-pair-watchdog-{user_id}"
            )

    async def _watch_pair_start(self, uc: "_UserClient") -> None:
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            return
        if uc.qr_seen or uc.paired:
            return
        logger.warning(
            "WhatsApp pair timeout for user=%s — no QR within 20s; likely 'Client outdated' from whatsmeow",
            uc.user_id,
        )
        await ws_manager.send_constraint_warning(
            constraint="whatsapp_handshake_timeout",
            message=(
                "WhatsApp didn't respond — the client library may be outdated. "
                "This is usually fixed in the next ViralMint update. "
                "If the dialog keeps hanging, close it and try again later."
            ),
            severity="warning",
            user_id=uc.user_id,
        )

    async def _run_connect(self, uc: _UserClient) -> None:
        try:
            # connect() only STARTS neonize's own task and returns it; awaiting
            # that task is what surfaces a connection error (it used to be
            # dropped unread), and cancelling us now cancels it — which makes
            # neonize tell Go to stop.
            inner = await uc.client.connect()
            if isinstance(inner, asyncio.Future):
                await inner
        except asyncio.CancelledError:
            raise
        except Exception as e:
            uc.online = False
            logger.warning("WhatsApp connect loop ended for user=%s: %s", uc.user_id, e)

    def _register_handlers(self, uc: _UserClient, neo: dict) -> None:
        user_id = uc.user_id
        client = uc.client

        # ── QR handler — neonize's default renders to terminal; override it to
        #    push the raw QR string to the frontend over WS.
        async def on_qr(_client, data_qr: bytes) -> None:
            uc.qr_seen = True
            try:
                qr_text = data_qr.decode("utf-8", errors="replace")
            except Exception:
                qr_text = ""
            await ws_manager.send(
                {"type": "whatsapp_qr", "user_id": user_id, "qr": qr_text},
                user_id,
            )
            logger.info("WhatsApp QR emitted for user=%s (%d chars)", user_id, len(qr_text))

        try:
            client.event.qr(on_qr)
        except Exception as e:
            logger.warning("Could not register WhatsApp QR handler: %s", e)

        # ── ConnectedEv — pairing succeeded or session resumed.
        async def on_connected(_client, _ev) -> None:
            uc.paired = True
            uc.online = True
            if uc.pair_watchdog and not uc.pair_watchdog.done():
                uc.pair_watchdog.cancel()
            _restrict(uc.session_path)
            await self._learn_self(uc)
            self_jid = _extract_self_jid(_client) or uc.phone_user
            if self_jid:
                uc.chat_id = self_jid
                await self._persist_connection(user_id, self_jid)

            await ws_manager.send(
                {
                    "type": "whatsapp_connected",
                    "user_id": user_id,
                    "chat_id": uc.chat_id,
                },
                user_id,
            )

            # Welcome message — best-effort; swallow failures silently
            if uc.chat_id:
                try:
                    target_user, target_server = _parse_stored_chat_id(uc.chat_id)
                    jid = neo["build_jid"](target_user, target_server)
                    resp = await _client.send_message(
                        jid,
                        "*ViralMint connected!* 🎬\n\n"
                        "You'll get a message here when scouts, downloads and videos finish.\n"
                        "To talk to me, write in this chat (Message yourself): "
                        "_download https://..._ or _scout cooking videos_. "
                        "Messages from anyone else are never read.",
                    )
                    uc.note_sent(getattr(resp, "ID", None))
                except Exception as e:
                    logger.debug("WhatsApp welcome message failed: %s", e)

        # ── DisconnectedEv — transient; neonize reconnects itself.
        async def on_disconnected(_client, _ev) -> None:
            uc.online = False
            await ws_manager.send(
                {"type": "whatsapp_disconnected", "user_id": user_id, "reason": "disconnected"},
                user_id,
            )

        # ── LoggedOutEv — user unlinked from the phone. Session is dead.
        async def on_logged_out(_client, _ev) -> None:
            logger.info("WhatsApp logged out for user=%s", user_id)
            uc.paired = False
            uc.online = False
            uc.chat_id = None
            # A retired client (configure()/disconnect() already replaced or
            # dropped it) must not touch shared state: the session file path
            # and the DB row now belong to whatever runs next — deleting them
            # here could wipe a fresh pairing mid-flight.
            if self._clients.get(user_id) is not uc:
                return
            # The session is dead: drop the client so status() stops reporting
            # "pairing" with no QR coming, stop it, and delete its keys.
            self._clients.pop(user_id, None)
            # Held until done: a bare create_task can be garbage-collected
            # mid-flight (asyncio keeps only a weak reference).
            task = asyncio.create_task(self._retire_logged_out(uc))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            async with AsyncSessionLocal() as db:
                row = await db.execute(
                    select(MessagingConfig).where(
                        MessagingConfig.user_id == user_id,
                        MessagingConfig.channel == "whatsapp",
                    )
                )
                cfg = row.scalar_one_or_none()
                if cfg:
                    cfg.is_active = False
                    cfg.chat_id = None
                    await db.commit()
            await ws_manager.send(
                {"type": "whatsapp_disconnected", "user_id": user_id, "reason": "logged_out"},
                user_id,
            )

        # ── PairStatusEv — belt-and-suspenders chat_id capture when pairing
        # succeeds, in case _extract_self_jid in on_connected didn't yield one.
        async def on_pair_status(_client, ev) -> None:
            logger.info("WhatsApp pair status user=%s: %s", user_id, ev)
            try:
                status = str(getattr(ev, "Status", "") or "")
                ident = getattr(ev, "ID", None)
                user_part = getattr(ident, "User", None) if ident else None
                if user_part and (not status or status.upper().endswith("SUCCESS")):
                    uc.paired = True
                    uc.self_ids.add(str(user_part))
                    if not uc.chat_id:
                        uc.chat_id = str(user_part)
                        await self._persist_connection(user_id, uc.chat_id)
            except Exception as e:
                logger.debug("WhatsApp pair status persist skipped: %s", e)

        # ── MessageEv — inbound chat → planner.
        async def on_message(_client, message) -> None:
            text, reply_user, reply_server, is_from_me, msg_id, chat_user = _extract_message(message)

            # On a linked device we see our own outbound messages come back with
            # IsFromMe=True. The only reliable way to tell those apart from the
            # user's own self-chat typing is to match the message ID against
            # IDs we just sent.
            if uc.is_own_echo(msg_id):
                return
            if not text:
                return
            # The owner's own "Message yourself" chat is the only input. A
            # linked device sees every chat on the phone: contacts, groups, and
            # the owner's own messages TO other people.
            # Only the owner's own messages can pass; skip the get_me() round
            # trip for everyone else's chatter on the phone.
            if not is_from_me:
                return
            if not uc.self_ids:
                await self._learn_self(uc)
            if not uc.is_owner_message(is_from_me, chat_user, reply_user):
                return
            if not self._planner_callback:
                return

            logger.info("WhatsApp inbound user=%s id=%s: %s", user_id, msg_id, text[:120])

            # Keep the phone-JID form of the owner's own chat as the
            # notification target (earlier builds stored the LID, which the
            # phone-namespace server can't route). Only ever the owner's own
            # identity — never whoever happened to write.
            if reply_user and reply_server and reply_user in uc.self_ids:
                combined = f"{reply_user}@{reply_server}"
                if uc.chat_id != combined:
                    uc.chat_id = combined
                    await self._persist_connection(user_id, combined)

            try:
                reply = await self._planner_callback(text, user_id)
            except Exception as e:
                logger.exception("WhatsApp planner callback failed for user=%s: %s", user_id, e)
                reply = "Something went wrong. Check ViralMint for details."

            clean = strip_action_blocks(reply) or "Done. ✅"

            # Route reply on the correct server (phone vs LID). Fall back to
            # the stored chat_id for older sessions that pre-date the split.
            if reply_user and reply_server:
                target_user, target_server = reply_user, reply_server
            elif uc.chat_id:
                target_user, target_server = _parse_stored_chat_id(uc.chat_id)
            else:
                return

            try:
                jid = neo["build_jid"](target_user, target_server)
            except Exception as e:
                logger.warning(
                    "WhatsApp build_jid failed for %s@%s: %s",
                    target_user, target_server, e,
                )
                return

            logger.info(
                "WhatsApp outbound JID | target=%s@%s built=%s",
                target_user, target_server, str(jid),
            )

            chunks = split_text(clean, WA_MSG_LIMIT)

            async def _reply_sender(chunk: str) -> None:
                resp = await _client.send_message(jid, chunk)
                uc.note_sent(getattr(resp, "ID", None))

            await send_with_retry("whatsapp", user_id, chunks, _reply_sender)
            await touch_last_message(user_id, "whatsapp")

        # Register each event handler
        try:
            client.event(neo["ConnectedEv"])(on_connected)
            client.event(neo["DisconnectedEv"])(on_disconnected)
            client.event(neo["LoggedOutEv"])(on_logged_out)
            client.event(neo["PairStatusEv"])(on_pair_status)
            client.event(neo["MessageEv"])(on_message)
        except Exception as e:
            logger.warning("Could not register WhatsApp event handlers: %s", e)

    async def _learn_self(self, uc: "_UserClient") -> None:
        """Record the paired account's own phone and LID user parts."""
        try:
            me = await asyncio.wait_for(uc.client.get_me(), timeout=10)
        except Exception as e:
            logger.warning("WhatsApp get_me failed for user=%s: %s", uc.user_id, e)
            return
        for jid in (getattr(me, "JID", None), getattr(me, "LID", None)):
            user_part = getattr(jid, "User", None) if jid is not None else None
            if user_part:
                uc.self_ids.add(str(user_part))
        phone = getattr(getattr(me, "JID", None), "User", None)
        if phone:
            uc.phone_user = str(phone)

    async def _retire_logged_out(self, uc: "_UserClient") -> None:
        await uc.stop()
        _remove_session(uc.session_path)

    async def _persist_connection(self, user_id: str, chat_id: str) -> None:
        async with AsyncSessionLocal() as db:
            row = await db.execute(
                select(MessagingConfig).where(
                    MessagingConfig.user_id == user_id,
                    MessagingConfig.channel == "whatsapp",
                )
            )
            cfg = row.scalar_one_or_none()
            if cfg:
                cfg.chat_id = chat_id
                cfg.is_active = True
                cfg.connected_at = cfg.connected_at or datetime.utcnow()
                cfg.last_message_at = datetime.utcnow()
            else:
                cfg = MessagingConfig(
                    user_id=user_id,
                    channel="whatsapp",
                    chat_id=chat_id,
                    is_active=True,
                    connected_at=datetime.utcnow(),
                )
                db.add(cfg)
            await db.commit()

# ── Helpers ──────────────────────────────────────────────────────────────────


def _extract_self_jid(client) -> Optional[str]:
    """Best-effort: neonize exposes the paired device's JID through different
    attributes depending on version. Try each."""
    for attr_path in ("me.JID", "me.jid", "store.ID", "store.id"):
        try:
            obj = client
            for part in attr_path.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if obj:
                # JID objects often stringify as "<number>@s.whatsapp.net"
                s = str(obj)
                # Strip device suffix (e.g. "12345:17@s.whatsapp.net" → "12345@s.whatsapp.net")
                s = re.sub(r":(\d+)@", "@", s)
                if "@" in s:
                    return s.split("@", 1)[0]
                if s.isdigit():
                    return s
        except Exception:
            continue
    return None


def _extract_message(
    message,
) -> tuple[str, Optional[str], Optional[str], bool, Optional[str], Optional[str]]:
    """Pull (text, reply_user, reply_server, is_from_me, msg_id, chat_user) out
    of a neonize MessageEv. `chat_user` is the conversation's own address — in
    the owner's self-chat, the owner's phone number or LID.

    Why reply_user + reply_server instead of a single JID string: WhatsApp's
    multi-device protocol uses two parallel address spaces — phone JIDs
    (`@s.whatsapp.net`) and Linked Identities (`@lid`). Messages can arrive
    addressed on either one. If a client replies to an LID via the phone
    server (or vice versa), the message routes into the void: the server
    ACKs the stanza but no device picks it up. Computing the right
    `(user, server)` pair at extract time is the whole bug.
    """
    text = ""
    reply_user: Optional[str] = None
    reply_server: Optional[str] = None
    is_from_me = False
    msg_id: Optional[str] = None
    chat_user_out: Optional[str] = None

    try:
        info = getattr(message, "Info", None)
        if info is not None:
            raw_id = getattr(info, "ID", None)
            if raw_id:
                msg_id = str(raw_id)
            src = getattr(info, "MessageSource", None)
            if src is not None:
                is_from_me = bool(getattr(src, "IsFromMe", False))
                chat = getattr(src, "Chat", None)
                sender_alt = getattr(src, "SenderAlt", None)
                recip_alt = getattr(src, "RecipientAlt", None)

                chat_user = getattr(chat, "User", None) if chat else None
                chat_server = getattr(chat, "Server", None) if chat else None
                if chat_user:
                    chat_user_out = str(chat_user)

                # Default: reply to whoever sent it, on their native server.
                if chat_user:
                    reply_user = str(chat_user)
                    reply_server = str(chat_server) if chat_server else "s.whatsapp.net"

                # LID addressing — prefer the phone-JID alternate so the
                # reply actually reaches a device. Self-chat uses RecipientAlt
                # (we're writing to ourselves, phone=recipient); inbound from
                # someone else uses SenderAlt.
                if chat_server == "lid":
                    alt = recip_alt if is_from_me else sender_alt
                    alt_user = getattr(alt, "User", None) if alt else None
                    alt_server = getattr(alt, "Server", None) if alt else None
                    if alt_user:
                        reply_user = str(alt_user)
                        reply_server = str(alt_server) if alt_server else "s.whatsapp.net"

                # Diagnostic: keep while the fix beds in.
                try:
                    def _jid_repr(j):
                        if j is None:
                            return None
                        return (
                            f"user={getattr(j, 'User', None)!r} "
                            f"server={getattr(j, 'Server', None)!r} "
                            f"agent={getattr(j, 'RawAgent', None)!r} "
                            f"device={getattr(j, 'Device', None)!r}"
                        )

                    logger.debug(
                        "WhatsApp routing | chat=[%s] sender=[%s] "
                        "sender_alt=[%s] recip_alt=[%s] addr_mode=%r "
                        "| chose reply_user=%r reply_server=%r",
                        _jid_repr(chat),
                        _jid_repr(getattr(src, "Sender", None)),
                        _jid_repr(sender_alt),
                        _jid_repr(recip_alt),
                        getattr(src, "AddressingMode", None),
                        reply_user, reply_server,
                    )
                except Exception as _diag_e:
                    logger.debug("WhatsApp routing diag skipped: %s", _diag_e)
    except Exception:
        pass

    try:
        msg = getattr(message, "Message", None)
        if msg is not None:
            # Plain text message
            conv = getattr(msg, "conversation", None)
            if conv:
                text = str(conv)
            else:
                # Quoted / reply text
                ext = getattr(msg, "extendedTextMessage", None)
                if ext is not None:
                    ext_text = getattr(ext, "text", None)
                    if ext_text:
                        text = str(ext_text)
    except Exception:
        pass

    return text.strip(), reply_user, reply_server, is_from_me, msg_id, chat_user_out


def _parse_stored_chat_id(raw: str) -> tuple[str, str]:
    """Accept 'user' (legacy) or 'user@server'. Default server = s.whatsapp.net."""
    if "@" in raw:
        user, server = raw.split("@", 1)
        return user, server or "s.whatsapp.net"
    return raw, "s.whatsapp.net"


def _format_body(payload: NotificationPayload) -> str:
    """Join title + body for WhatsApp. WhatsApp renders *bold* / _italic_ /
    ~strike~ natively, so our markdown passes through cleanly."""
    if payload.title and payload.body:
        return f"*{payload.title}*\n{payload.body}"
    return payload.title or payload.body or ""


# Singleton
whatsapp_channel = WhatsAppChannel()
