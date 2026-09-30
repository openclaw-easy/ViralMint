# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""backend/messaging/whatsapp_channel.py — session files, lifecycle, pairing,
connection events, sends and the inbound handler, against a fake neonize.

neonize is never really started: `_neonize_mod` is replaced by a dict of
fakes, so no Go runtime, no QR handshake and no network.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.messaging import whatsapp_channel as w
from backend.messaging.base import NotificationEvent, NotificationPayload
from tests._messaging_fakes import add_row, capture_ws, get_rows, no_retry_sleep, reset_channel_rows  # noqa: F401


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeEvent:
    def __init__(self, *, fail_qr=False, fail_events=False):
        self.by_type = {}
        self.fail_qr = fail_qr
        self.fail_events = fail_events

    def qr(self, fn):
        if self.fail_qr:
            raise RuntimeError("no qr hook in this neonize")
        self.by_type["qr"] = fn

    def __call__(self, ev_type):
        if self.fail_events:
            raise RuntimeError("no events in this neonize")

        def deco(fn):
            self.by_type[ev_type] = fn
            return fn
        return deco


class FakeWA:
    """The parts of neonize's NewAClient the channel calls."""

    def __init__(self, *, me=None, connect_result=None):
        self.event = FakeEvent()
        self.send_message = AsyncMock(return_value=SimpleNamespace(ID="out-1"))
        self.disconnect = AsyncMock()
        self.stop = AsyncMock()
        self.logout = AsyncMock()
        self.get_me = AsyncMock(return_value=me or SimpleNamespace(
            JID=SimpleNamespace(User="15550001111"), LID=SimpleNamespace(User="987654321")))
        self._connect_result = connect_result
        self.connect = AsyncMock(side_effect=self._connect)

    async def _connect(self):
        return self._connect_result


class FakeFactory:
    made: list = []

    def __init__(self, path):
        self.path = path

    def new_client(self, uuid):
        c = FakeWA()
        c.uuid = uuid
        FakeFactory.made.append(c)
        return c


@pytest.fixture
def neo(monkeypatch, tmp_path):
    FakeFactory.made = []
    mod = {k: k for k in ("ConnectedEv", "DisconnectedEv", "LoggedOutEv", "PairStatusEv", "MessageEv")}
    mod["ClientFactory"] = FakeFactory
    mod["NewAClient"] = object
    mod["build_jid"] = lambda u, s: f"{u}@{s}"
    monkeypatch.setattr(w, "_neonize_mod", mod)
    monkeypatch.setattr(w, "_import_error", None)
    monkeypatch.setattr(w, "_session_dir", lambda: tmp_path / "storage" / "messaging")
    return mod


@pytest.fixture
def no_neonize(monkeypatch):
    monkeypatch.setattr(w, "_neonize_mod", None)
    monkeypatch.setattr(w, "_import_error", "ImportError: no neonize")


@pytest.fixture
def quiet_touch(monkeypatch):
    touched = []

    async def touch(user_id, channel):
        touched.append((user_id, channel))
    monkeypatch.setattr(w, "touch_last_message", touch)
    return touched


@pytest.fixture
def wired(neo, quiet_touch, monkeypatch, tmp_path):
    """A paired, online owner session with its handlers registered."""
    ws = capture_ws(monkeypatch)
    ch = w.WhatsAppChannel()
    planner = AsyncMock(return_value="reply <action>{}</action>")
    ch.set_planner_callback(planner)
    client = FakeWA()
    uc = w._UserClient("local", client, None, tmp_path / "s.db")
    uc.paired, uc.online, uc.chat_id = True, True, "15550001111@s.whatsapp.net"
    uc.self_ids = {"15550001111", "987654321"}
    ch._clients["local"] = uc
    ch._register_handlers(uc, neo)
    persisted = []

    async def persist(user_id, chat_id):
        persisted.append(chat_id)
    monkeypatch.setattr(ch, "_persist_connection", persist)
    h = client.event.by_type
    return SimpleNamespace(ch=ch, uc=uc, client=client, planner=planner, ws=ws, persisted=persisted,
                           touched=quiet_touch, h=h)


def _msg(text, *, from_me=True, chat_user="15550001111", chat_server="s.whatsapp.net",
         msg_id="m1", recip_alt=None, sender_alt=None, ext_text=None):
    chat = SimpleNamespace(User=chat_user, Server=chat_server) if chat_user is not None else None
    src = SimpleNamespace(IsFromMe=from_me, Chat=chat, SenderAlt=sender_alt, RecipientAlt=recip_alt,
                          Sender=None, AddressingMode=None)
    ext = SimpleNamespace(text=ext_text) if ext_text is not None else None
    return SimpleNamespace(Info=SimpleNamespace(ID=msg_id, MessageSource=src),
                           Message=SimpleNamespace(conversation=text, extendedTextMessage=ext))


@pytest_asyncio.fixture
async def wa_rows():
    await reset_channel_rows("whatsapp")
    yield
    await reset_channel_rows("whatsapp")


# ── import / session files ───────────────────────────────────────────────────

class TestImport:
    def test_a_known_failure_is_not_retried(self, no_neonize):
        assert w._import_neonize() is None

    def test_a_failed_import_is_remembered(self, monkeypatch):
        monkeypatch.setattr(w, "_neonize_mod", None)
        monkeypatch.setattr(w, "_import_error", None)
        monkeypatch.setitem(sys.modules, "neonize.aioze.client", None)   # import → ImportError
        assert w._import_neonize() is None
        assert w._import_error and "neonize.aioze.client" in w._import_error
        assert w._import_neonize() is None          # remembered, not retried


class TestSessionFiles:
    def test_legacy_session_is_moved_into_the_data_dir(self, neo, monkeypatch, tmp_path):
        legacy_root = tmp_path / "cwd"
        legacy = legacy_root / "storage" / "messaging"
        legacy.mkdir(parents=True)
        (legacy / "whatsapp_a_b.db").write_text("keys")
        (legacy / "whatsapp_a_b.db-wal").write_text("wal")
        monkeypatch.setattr(w.Path, "cwd", classmethod(lambda cls: legacy_root))
        p = w._session_path("a/b")                 # unsafe chars are replaced
        assert p.name == "whatsapp_a_b.db" and p.parent == tmp_path / "storage" / "messaging"
        assert p.read_text() == "keys" and Path(str(p) + "-wal").read_text() == "wal"
        assert not (legacy / "whatsapp_a_b.db").exists()

    def test_a_failed_move_is_only_logged(self, neo, monkeypatch, tmp_path):
        legacy_root = tmp_path / "cwd"
        legacy = legacy_root / "storage" / "messaging"
        legacy.mkdir(parents=True)
        (legacy / "whatsapp_local.db").write_text("keys")
        monkeypatch.setattr(w.Path, "cwd", classmethod(lambda cls: legacy_root))

        def refuse(self, target):
            raise OSError("cross-device")
        monkeypatch.setattr(w.Path, "replace", refuse)
        p = w._session_path("")                    # empty id → "local"
        assert p.name == "whatsapp_local.db" and not p.exists()

    def test_remove_session_deletes_side_files(self, tmp_path, monkeypatch):
        p = tmp_path / "s.db"
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(p) + suffix).write_text("x")
        w._remove_session(p)
        assert list(tmp_path.iterdir()) == []
        p.write_text("x")

        def refuse(self, *a, **k):
            raise OSError("busy")
        monkeypatch.setattr(w.Path, "unlink", refuse)
        w._remove_session(p)                       # logged, not raised
        assert p.exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_restrict(self, tmp_path, monkeypatch):
        p = tmp_path / "s.db"
        w._restrict(p)                             # missing: no-op
        p.write_text("x")
        p.chmod(0o644)
        w._restrict(p)
        assert (p.stat().st_mode & 0o777) == 0o600

        def refuse(self, mode):
            raise OSError("ro fs")
        monkeypatch.setattr(w.Path, "chmod", refuse)
        w._restrict(p)


# ── _UserClient ──────────────────────────────────────────────────────────────

class TestUserClient:
    @pytest.mark.asyncio
    async def test_stop_releases_everything(self, tmp_path):
        c = FakeWA()
        uc = w._UserClient("u", c, None, tmp_path / "s.db")
        uc.online = True
        uc.pair_watchdog = asyncio.create_task(asyncio.sleep(60))
        uc.connect_task = asyncio.create_task(asyncio.sleep(60))
        await uc.stop()
        assert uc.online is False
        c.disconnect.assert_awaited_once()
        c.stop.assert_awaited_once()
        assert uc.connect_task.cancelled()
        await asyncio.sleep(0)
        assert uc.pair_watchdog.cancelled()

    @pytest.mark.asyncio
    async def test_stop_tolerates_failures_and_no_client(self, tmp_path):
        c = FakeWA()
        c.disconnect.side_effect = RuntimeError("x")
        c.stop.side_effect = RuntimeError("y")
        uc = w._UserClient("u", c, None, tmp_path / "s.db")
        await uc.stop()
        c.stop.assert_awaited_once()               # a failed disconnect doesn't skip stop
        await w._UserClient("u", None, None, tmp_path / "s.db").stop()

    @pytest.mark.asyncio
    async def test_stop_logs_an_unexpected_error(self, tmp_path):
        uc = w._UserClient("u", FakeWA(), None, tmp_path / "s.db")
        uc.pair_watchdog = SimpleNamespace(done=lambda: (_ for _ in ()).throw(RuntimeError("odd")))
        await uc.stop()                            # swallowed

    def test_echo_and_owner_checks(self, tmp_path):
        uc = w._UserClient("u", None, None, tmp_path / "s.db")
        uc.note_sent(None)
        uc.note_sent("abc")
        assert uc.is_own_echo("abc") and not uc.is_own_echo(None) and not uc.is_own_echo("x")
        assert not uc.is_owner_message(True, "1", None), "no identity known yet"
        uc.self_ids = {"1"}
        assert uc.is_owner_message(True, None, "1")
        assert not uc.is_owner_message(False, "1", "1")
        assert not uc.is_owner_message(True, "2", None)


# ── lifecycle ────────────────────────────────────────────────────────────────

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_without_neonize_is_a_no_op(self, no_neonize):
        await w.WhatsAppChannel().start()

    @pytest.mark.asyncio
    async def test_boot_resumes_sessions_and_retires_missing_ones(self, neo, wa_rows, monkeypatch):
        await add_row("whatsapp", user_id="has-session", is_active=True, chat_id="1@s.whatsapp.net")
        await add_row("whatsapp", user_id="lost-session", is_active=True, chat_id="2")
        await add_row("whatsapp", user_id="broken", is_active=True, chat_id="3")
        w._session_path("has-session").write_text("keys")
        w._session_path("broken").write_text("keys")
        ch = w.WhatsAppChannel()
        spun = []

        async def spin(user_id, chat_id, fresh_pair=False):
            spun.append((user_id, chat_id))
            if user_id == "broken":
                raise RuntimeError("corrupt session")
        monkeypatch.setattr(ch, "_spin_up", spin)
        await ch.start()
        assert sorted(spun) == [("broken", "3"), ("has-session", "1@s.whatsapp.net")]
        (lost,) = await get_rows("whatsapp", "lost-session")
        assert not lost.is_active

    @pytest.mark.asyncio
    async def test_stop_stops_every_client(self, tmp_path):
        ch = w.WhatsAppChannel()
        a, b = FakeWA(), FakeWA()
        ch._clients = {"a": w._UserClient("a", a, None, tmp_path / "a"),
                       "b": w._UserClient("b", b, None, tmp_path / "b")}
        await ch.stop()
        assert not ch._clients
        a.stop.assert_awaited_once()
        b.stop.assert_awaited_once()

    def test_status_shapes(self, neo, tmp_path):
        ch = w.WhatsAppChannel()
        assert ch.status("u") == {"connected": False, "installed": True, "pairing": False, "chat_id": None}
        uc = w._UserClient("u", FakeWA(), None, tmp_path / "s")
        ch._clients["u"] = uc
        assert ch.status("u")["pairing"] is True
        uc.paired, uc.chat_id = True, "1"
        st = ch.status("u")
        assert st == {"connected": False, "installed": True, "pairing": False, "paired": True, "chat_id": "1"}

    def test_status_when_not_installed(self, no_neonize):
        st = w.WhatsAppChannel().status("u")
        assert st["installed"] is False and st["error"] == "ImportError: no neonize"


# ── send ─────────────────────────────────────────────────────────────────────

class TestSend:
    @pytest.mark.asyncio
    async def test_not_sendable(self, wired, monkeypatch):
        p = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="t", body="b")
        assert await wired.ch.send("nobody", p) is False
        assert await wired.ch.is_configured("local") is True
        wired.uc.online = False
        assert await wired.ch.is_configured("local") is False
        # neonize vanished (e.g. native lib failed after boot)
        monkeypatch.setattr(w, "_neonize_mod", None)
        monkeypatch.setattr(w, "_import_error", "gone")
        assert await wired.ch.send("local", p) is False

    @pytest.mark.asyncio
    async def test_a_jid_that_cannot_be_built(self, wired, neo, monkeypatch):
        def bad(u, s):
            raise ValueError("bad jid")
        monkeypatch.setitem(neo, "build_jid", bad)
        p = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="t", body="b")
        assert await wired.ch.send("local", p) is False
        wired.client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_records_its_own_ids(self, wired):
        p = NotificationPayload(event=NotificationEvent.JOB_FAILED, title="Done", body="your video")
        assert await wired.ch.send("local", p) is True
        wired.client.send_message.assert_awaited_once_with(
            "15550001111@s.whatsapp.net", "*Done*\nyour video")
        assert wired.uc.is_own_echo("out-1")
        assert wired.touched == [("local", "whatsapp")]

    @pytest.mark.asyncio
    async def test_send_test_and_a_failed_send(self, wired, no_retry_sleep):
        assert await wired.ch.send_test("local") is True
        assert "WhatsApp link is live" in wired.client.send_message.await_args.args[1]
        wired.client.send_message.side_effect = RuntimeError("offline")
        wired.touched.clear()
        assert await wired.ch.send_test("local") is False
        assert wired.touched == []


# ── configure / disconnect / spin-up ─────────────────────────────────────────

class TestConfigure:
    @pytest.mark.asyncio
    async def test_without_neonize(self, no_neonize):
        with pytest.raises(ValueError, match="no neonize"):
            await w.WhatsAppChannel().configure("u")

    @pytest.mark.asyncio
    async def test_re_pair_unlinks_the_old_device_and_wipes_the_session(self, neo, monkeypatch, tmp_path):
        ws = capture_ws(monkeypatch)
        ch = w.WhatsAppChannel()
        old = FakeWA()
        old.logout.side_effect = RuntimeError("phone offline")      # best-effort
        old_uc = w._UserClient("cov-w", old, None, tmp_path / "old.db")
        old_uc.paired = True
        ch._clients["cov-w"] = old_uc
        session = w._session_path("cov-w")
        session.write_text("old keys")

        assert await ch.configure("cov-w") == {"awaiting_qr": True}
        old.logout.assert_awaited_once()
        old.stop.assert_awaited_once()
        assert not session.exists()
        new_uc = ch._clients["cov-w"]
        assert new_uc is not old_uc and new_uc.paired is False and new_uc.chat_id is None
        assert new_uc.pair_watchdog is not None and new_uc.connect_task is not None
        assert FakeFactory.made[-1].uuid                           # deterministic uuid passed
        await asyncio.sleep(0)
        FakeFactory.made[-1].connect.assert_awaited_once()
        await ch.stop()
        assert new_uc.pair_watchdog.cancelled() or new_uc.pair_watchdog.done()
        assert ws.warnings == []

    @pytest.mark.asyncio
    async def test_an_unpaired_old_client_is_not_logged_out(self, neo, tmp_path):
        ch = w.WhatsAppChannel()
        old = FakeWA()
        ch._clients["u"] = w._UserClient("u", old, None, tmp_path / "o.db")
        await ch.configure("u")
        old.logout.assert_not_called()
        old.stop.assert_awaited_once()
        await ch.stop()

    @pytest.mark.asyncio
    async def test_resume_spin_up_counts_as_paired(self, neo):
        ch = w.WhatsAppChannel()
        await ch._spin_up("u", chat_id="1@s.whatsapp.net")
        uc = ch._clients["u"]
        assert uc.paired and uc.pair_watchdog is None
        a = FakeFactory.made[-1].uuid
        await ch._spin_up("u", chat_id="1")
        assert FakeFactory.made[-1].uuid == a, "uuid must be stable per user"
        await ch.stop()

    @pytest.mark.asyncio
    async def test_spin_up_without_neonize(self, no_neonize):
        with pytest.raises(RuntimeError):
            await w.WhatsAppChannel()._spin_up("u", None)

    @pytest.mark.asyncio
    async def test_disconnect_logs_out_and_forgets(self, neo, wa_rows, monkeypatch, tmp_path):
        ws = capture_ws(monkeypatch)
        await add_row("whatsapp", user_id="cov-w", is_active=True, chat_id="1")
        ch = w.WhatsAppChannel()
        c = FakeWA()
        c.logout.side_effect = RuntimeError("already gone")
        path = tmp_path / "s.db"
        path.write_text("keys")
        uc = w._UserClient("cov-w", c, None, path)
        uc.paired = True
        ch._clients["cov-w"] = uc
        assert await ch.disconnect("cov-w") is True
        c.logout.assert_awaited_once()
        c.stop.assert_awaited_once()
        assert not path.exists() and "cov-w" not in ch._clients
        (row,) = await get_rows("whatsapp", "cov-w")
        assert not row.is_active and row.chat_id is None
        assert ws.types() == ["whatsapp_disconnected"]

        await ch.disconnect("cov-w")                     # nothing running: still clean
        assert ws.types() == ["whatsapp_disconnected"] * 2


class TestWatchdogAndConnect:
    @pytest.mark.asyncio
    async def test_no_qr_in_time_warns(self, monkeypatch, tmp_path):
        ws = capture_ws(monkeypatch)
        monkeypatch.setattr(w.asyncio, "sleep", AsyncMock())
        ch = w.WhatsAppChannel()
        uc = w._UserClient("u", None, None, tmp_path / "s")
        await ch._watch_pair_start(uc)
        assert ws.warnings[0]["constraint"] == "whatsapp_handshake_timeout"

    @pytest.mark.asyncio
    async def test_a_qr_or_a_pair_silences_the_watchdog(self, monkeypatch, tmp_path):
        ws = capture_ws(monkeypatch)
        monkeypatch.setattr(w.asyncio, "sleep", AsyncMock())
        ch = w.WhatsAppChannel()
        uc = w._UserClient("u", None, None, tmp_path / "s")
        uc.qr_seen = True
        await ch._watch_pair_start(uc)
        uc.qr_seen, uc.paired = False, True
        await ch._watch_pair_start(uc)
        assert ws.warnings == []

    @pytest.mark.asyncio
    async def test_a_cancelled_watchdog_returns_quietly(self, monkeypatch, tmp_path):
        monkeypatch.setattr(w.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
        await w.WhatsAppChannel()._watch_pair_start(w._UserClient("u", None, None, tmp_path / "s"))

    @pytest.mark.asyncio
    async def test_connect_error_marks_offline(self, tmp_path):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fut.set_exception(ConnectionError("405 client outdated"))
        c = FakeWA(connect_result=fut)
        uc = w._UserClient("u", c, None, tmp_path / "s")
        uc.online = True
        await w.WhatsAppChannel()._run_connect(uc)
        assert uc.online is False

    @pytest.mark.asyncio
    async def test_connect_that_returns_nothing(self, tmp_path):
        uc = w._UserClient("u", FakeWA(connect_result=None), None, tmp_path / "s")
        uc.online = True
        await w.WhatsAppChannel()._run_connect(uc)
        assert uc.online is True

    @pytest.mark.asyncio
    async def test_cancel_propagates(self, tmp_path):
        c = FakeWA()
        c.connect = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await w.WhatsAppChannel()._run_connect(w._UserClient("u", c, None, tmp_path / "s"))


# ── event handlers ───────────────────────────────────────────────────────────

class TestEvents:
    @pytest.mark.asyncio
    async def test_qr_is_pushed_to_the_ui(self, wired):
        await wired.h["qr"](wired.client, b"2@abc,def")
        assert wired.uc.qr_seen
        (msg, _), = wired.ws.sent
        assert msg == {"type": "whatsapp_qr", "user_id": "local", "qr": "2@abc,def"}

    @pytest.mark.asyncio
    async def test_an_undecodable_qr_is_sent_empty(self, wired):
        await wired.h["qr"](wired.client, "not-bytes")      # str has no .decode
        assert wired.ws.sent[0][0]["qr"] == ""

    def test_handler_registration_failures_are_logged(self, neo, tmp_path):
        c = FakeWA()
        c.event = FakeEvent(fail_qr=True, fail_events=True)
        w.WhatsAppChannel()._register_handlers(w._UserClient("u", c, None, tmp_path / "s"), neo)
        assert c.event.by_type == {}

    @pytest.mark.asyncio
    async def test_connected_learns_self_persists_and_welcomes(self, wired, tmp_path):
        wired.uc.paired = wired.uc.online = False
        wired.uc.self_ids = set()
        wired.uc.pair_watchdog = asyncio.create_task(asyncio.sleep(60))
        wired.uc.session_path.write_text("keys")
        await wired.h["ConnectedEv"](wired.client, object())
        await asyncio.sleep(0)
        uc = wired.uc
        assert uc.paired and uc.online and uc.pair_watchdog.cancelled()
        assert uc.self_ids == {"15550001111", "987654321"} and uc.phone_user == "15550001111"
        assert uc.chat_id == "15550001111" and wired.persisted == ["15550001111"]
        assert wired.ws.types() == ["whatsapp_connected"]
        wired.client.send_message.assert_awaited_once()
        assert wired.client.send_message.await_args.args[0] == "15550001111@s.whatsapp.net"
        assert uc.is_own_echo("out-1"), "the welcome must not echo back as a command"

    @pytest.mark.asyncio
    async def test_connected_without_any_identity(self, wired):
        wired.uc.chat_id = None
        wired.client.get_me.side_effect = RuntimeError("not logged in")
        wired.client.send_message.side_effect = RuntimeError("never")
        await wired.h["ConnectedEv"](wired.client, object())
        assert wired.uc.chat_id is None and wired.persisted == []
        wired.client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_connected_welcome_failure_is_swallowed(self, wired):
        wired.client.send_message.side_effect = RuntimeError("offline")
        await wired.h["ConnectedEv"](wired.client, object())
        assert wired.uc.online

    @pytest.mark.asyncio
    async def test_disconnected_is_transient(self, wired):
        await wired.h["DisconnectedEv"](wired.client, object())
        assert wired.uc.online is False and wired.uc.paired is True
        assert wired.ws.sent[0][0]["reason"] == "disconnected"

    @pytest.mark.asyncio
    async def test_logged_out_retires_the_live_session(self, wired, wa_rows):
        await add_row("whatsapp", user_id="local", is_active=True, chat_id="1")
        wired.uc.session_path.write_text("keys")
        await wired.h["LoggedOutEv"](wired.client, object())
        assert "local" not in wired.ch._clients
        assert wired.uc.paired is False and wired.uc.chat_id is None
        await asyncio.gather(*list(wired.ch._background))
        wired.client.stop.assert_awaited()
        assert not wired.uc.session_path.exists()
        (row,) = await get_rows("whatsapp", "local")
        assert not row.is_active and row.chat_id is None
        assert wired.ws.sent[-1][0]["reason"] == "logged_out"

    @pytest.mark.asyncio
    async def test_logged_out_without_a_row(self, wired, wa_rows):
        await wired.h["LoggedOutEv"](wired.client, object())
        await asyncio.gather(*list(wired.ch._background))
        assert await get_rows("whatsapp", "local") == []

    @pytest.mark.asyncio
    async def test_a_retired_client_logging_out_touches_nothing(self, wired, tmp_path):
        replacement = w._UserClient("local", FakeWA(), None, tmp_path / "new.db")
        wired.ch._clients["local"] = replacement
        wired.uc.session_path.write_text("keys")
        await wired.h["LoggedOutEv"](wired.client, object())
        assert wired.ch._clients["local"] is replacement
        assert wired.uc.session_path.exists() and wired.ws.sent == []

    @pytest.mark.asyncio
    async def test_pair_status_success_records_identity(self, wired):
        wired.uc.chat_id, wired.uc.paired, wired.uc.self_ids = None, False, set()
        await wired.h["PairStatusEv"](wired.client, SimpleNamespace(
            Status="PAIR_SUCCESS", ID=SimpleNamespace(User="1555")))
        assert wired.uc.paired and wired.uc.chat_id == "1555" and "1555" in wired.uc.self_ids
        assert wired.persisted == ["1555"]
        # A later success keeps the existing target.
        await wired.h["PairStatusEv"](wired.client, SimpleNamespace(Status="", ID=SimpleNamespace(User="1666")))
        assert wired.uc.chat_id == "1555" and wired.persisted == ["1555"]

    @pytest.mark.asyncio
    async def test_pair_status_failures_change_nothing(self, wired):
        wired.uc.chat_id, wired.uc.paired = None, False
        await wired.h["PairStatusEv"](wired.client, SimpleNamespace(Status="PAIR_ERROR", ID=SimpleNamespace(User="1")))
        await wired.h["PairStatusEv"](wired.client, SimpleNamespace(Status="PAIR_SUCCESS", ID=None))

        class Exploding:
            @property
            def Status(self):
                raise RuntimeError("proto")
        await wired.h["PairStatusEv"](wired.client, Exploding())
        assert not wired.uc.paired and wired.uc.chat_id is None and wired.persisted == []


class TestInbound:
    @pytest.mark.asyncio
    async def test_our_own_echo_is_ignored(self, wired):
        wired.uc.note_sent("m1")
        await wired.h["MessageEv"](wired.client, _msg("scout", msg_id="m1"))
        wired.planner.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_text_is_ignored(self, wired):
        await wired.h["MessageEv"](wired.client, _msg(""))
        wired.planner.assert_not_called()

    @pytest.mark.asyncio
    async def test_identity_is_learned_on_demand(self, wired):
        wired.uc.self_ids = set()
        await wired.h["MessageEv"](wired.client, _msg("scout"))
        wired.client.get_me.assert_awaited_once()
        wired.planner.assert_awaited_once_with("scout", "local")

    @pytest.mark.asyncio
    async def test_no_planner_means_no_reply(self, wired):
        wired.ch._planner_callback = None
        await wired.h["MessageEv"](wired.client, _msg("scout"))
        wired.client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_lid_self_chat_replies_on_the_phone_jid(self, wired):
        wired.uc.chat_id = "987654321@lid"                 # older builds stored the LID
        m = _msg("scout", chat_user="987654321", chat_server="lid",
                 recip_alt=SimpleNamespace(User="15550001111", Server=None))
        await wired.h["MessageEv"](wired.client, m)
        assert wired.uc.chat_id == "15550001111@s.whatsapp.net"
        assert wired.persisted == ["15550001111@s.whatsapp.net"]
        wired.client.send_message.assert_awaited_once_with("15550001111@s.whatsapp.net", "reply")
        assert wired.touched == [("local", "whatsapp")]

    @pytest.mark.asyncio
    async def test_a_planner_crash_still_answers(self, wired):
        wired.planner.side_effect = RuntimeError("boom")
        await wired.h["MessageEv"](wired.client, _msg("scout"))
        assert "Something went wrong" in wired.client.send_message.await_args.args[1]

    @pytest.mark.asyncio
    async def test_an_action_only_reply_says_done(self, wired):
        wired.planner.return_value = "<action>x</action>"
        await wired.h["MessageEv"](wired.client, _msg("scout", ext_text=None))
        assert wired.client.send_message.await_args.args[1] == "Done. ✅"

    @pytest.mark.asyncio
    async def test_a_jid_failure_drops_the_reply(self, wired, neo, monkeypatch):
        def bad(u, s):
            raise ValueError("bad")
        monkeypatch.setitem(neo, "build_jid", bad)
        await wired.h["MessageEv"](wired.client, _msg("scout"))
        wired.planner.assert_awaited_once()
        wired.client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_to_the_stored_target_and_nowhere_to_reply(self, wired, monkeypatch):
        # A message whose chat address is unknown, but the owner check passes
        # (patched): the reply goes to the stored chat_id…
        monkeypatch.setattr(wired.uc, "is_owner_message", lambda *a: True)
        await wired.h["MessageEv"](wired.client, _msg("scout", chat_user=None))
        wired.client.send_message.assert_awaited_once_with("15550001111@s.whatsapp.net", "reply")
        # …and with no stored target either, nothing is sent.
        wired.uc.chat_id = None
        wired.client.send_message.reset_mock()
        await wired.h["MessageEv"](wired.client, _msg("scout", chat_user=None))
        wired.client.send_message.assert_not_called()


class TestLearnSelf:
    @pytest.mark.asyncio
    async def test_partial_identity(self, tmp_path):
        c = FakeWA(me=SimpleNamespace(JID=None, LID=SimpleNamespace(User="999")))
        uc = w._UserClient("u", c, None, tmp_path / "s")
        await w.WhatsAppChannel()._learn_self(uc)
        assert uc.self_ids == {"999"} and uc.phone_user is None


class TestPersistConnection:
    @pytest.mark.asyncio
    async def test_creates_then_updates_the_row(self, wa_rows):
        ch = w.WhatsAppChannel()
        await ch._persist_connection("cov-w", "1@s.whatsapp.net")
        (row,) = await get_rows("whatsapp", "cov-w")
        assert row.chat_id == "1@s.whatsapp.net" and row.is_active and row.connected_at
        first = row.connected_at
        await ch._persist_connection("cov-w", "2@s.whatsapp.net")
        (row,) = await get_rows("whatsapp", "cov-w")
        assert row.chat_id == "2@s.whatsapp.net" and row.connected_at == first and row.last_message_at


# ── pure helpers ─────────────────────────────────────────────────────────────

class TestHelpers:
    def test_extract_self_jid(self):
        assert w._extract_self_jid(SimpleNamespace(me=SimpleNamespace(JID="1555:17@s.whatsapp.net"))) == "1555"
        assert w._extract_self_jid(SimpleNamespace(me=None, store=SimpleNamespace(ID="1666"))) == "1666"
        assert w._extract_self_jid(SimpleNamespace(me=SimpleNamespace(JID="weird"))) is None
        assert w._extract_self_jid(SimpleNamespace()) is None

        class Boom:
            def __str__(self):
                raise RuntimeError("proto")
        assert w._extract_self_jid(SimpleNamespace(me=SimpleNamespace(JID=Boom()))) is None

    def test_extract_message_from_a_contact_on_lid_uses_sender_alt(self):
        m = _msg("hi", from_me=False, chat_user="555", chat_server="lid",
                 sender_alt=SimpleNamespace(User="1777", Server="s.whatsapp.net"))
        text, ru, rs, me, mid, cu = w._extract_message(m)
        assert (text, ru, rs, me, mid, cu) == ("hi", "1777", "s.whatsapp.net", False, "m1", "555")

    def test_extract_message_quoted_text_and_default_server(self):
        m = _msg(None, chat_server=None, ext_text="  quoted  ")
        text, ru, rs, *_ = w._extract_message(m)
        assert text == "quoted" and rs == "s.whatsapp.net"

    def test_extract_message_tolerates_garbage(self):
        class Bad:
            @property
            def Info(self):
                raise RuntimeError("x")

            @property
            def Message(self):
                raise RuntimeError("y")
        assert w._extract_message(Bad()) == ("", None, None, False, None, None)
        assert w._extract_message(SimpleNamespace(Info=None, Message=None))[0] == ""

    def test_parse_and_format(self):
        assert w._parse_stored_chat_id("1") == ("1", "s.whatsapp.net")
        assert w._parse_stored_chat_id("1@lid") == ("1", "lid")
        assert w._parse_stored_chat_id("1@") == ("1", "s.whatsapp.net")
        ev = NotificationEvent.JOB_FAILED
        assert w._format_body(NotificationPayload(event=ev, title="T", body="B")) == "*T*\nB"
        assert w._format_body(NotificationPayload(event=ev, title="T", body="")) == "T"
        assert w._format_body(NotificationPayload(event=ev, title="", body="")) == ""
