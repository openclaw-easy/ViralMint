# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Tests for backend.core.setup_wizard — the data-driven WIZARDS engine.

Pure data module: WIZARDS maps wizard_id → {title, steps[...]} and
SETUP_NEEDS maps a planner capability → wizard_id. These tests pin the
structural contract the wizard runner / planner depend on (step typing,
required fields per action, SETUP_NEEDS integrity).
"""
from backend.core.setup_wizard import SETUP_NEEDS, WIZARDS

_VALID_ACTIONS = {
    "wait_confirm", "open_url", "open_url_dynamic", "text_input",
    "select", "oauth_button", "wait_oauth", "wait_ws_event", "success",
}


def test_expected_wizards_present():
    for wid in ("douyin_cookie", "tiktok_cookie", "youtube_auth",
                "tiktok_upload_auth", "telegram"):
        assert wid in WIZARDS


def test_every_wizard_has_title_and_nonempty_steps():
    for wid, wiz in WIZARDS.items():
        assert wiz.get("title"), f"{wid} missing title"
        steps = wiz.get("steps")
        assert isinstance(steps, list) and steps, f"{wid} has no steps"


def test_step_ids_are_sequential_from_one():
    for wid, wiz in WIZARDS.items():
        ids = [s["id"] for s in wiz["steps"]]
        assert ids == list(range(1, len(ids) + 1)), f"{wid} step ids not 1..n"


def test_every_step_action_is_known():
    for wid, wiz in WIZARDS.items():
        for step in wiz["steps"]:
            assert step.get("action") in _VALID_ACTIONS, \
                f"{wid} step {step.get('id')} has unknown action {step.get('action')!r}"


def test_action_specific_required_fields():
    for wid, wiz in WIZARDS.items():
        for step in wiz["steps"]:
            action = step["action"]
            if action == "open_url":
                assert step.get("url"), f"{wid}: open_url needs url"
            elif action == "open_url_dynamic":
                assert step.get("url_field"), f"{wid}: open_url_dynamic needs url_field"
            elif action == "text_input":
                assert step.get("field"), f"{wid}: text_input needs field"
            elif action == "oauth_button":
                assert step.get("endpoint"), f"{wid}: oauth_button needs endpoint"
            elif action == "wait_ws_event":
                assert step.get("event"), f"{wid}: wait_ws_event needs event"


def test_wait_confirm_steps_have_confirm_label():
    for wid, wiz in WIZARDS.items():
        for step in wiz["steps"]:
            if step["action"] == "wait_confirm":
                assert step.get("confirm_label"), f"{wid} step {step['id']} missing confirm_label"


def test_high_risk_wizards_flagged_and_lead_with_confirm():
    for wid in ("douyin_cookie", "tiktok_cookie"):
        wiz = WIZARDS[wid]
        assert wiz.get("risk_level") == "high"
        assert wiz["steps"][0]["action"] == "wait_confirm"


def test_telegram_wizard_ends_on_ws_event():
    steps = WIZARDS["telegram"]["steps"]
    assert steps[-1]["action"] == "wait_ws_event"
    assert steps[-1]["event"] == "telegram_connected"


# ── SETUP_NEEDS mapping ────────────────────────────────────────────────────

def test_setup_needs_values_reference_real_wizards():
    for need, wid in SETUP_NEEDS.items():
        assert wid in WIZARDS, f"SETUP_NEEDS[{need}]={wid} has no wizard"


def test_setup_needs_expected_capabilities():
    assert SETUP_NEEDS["telegram_notifications"] == "telegram"
    assert SETUP_NEEDS["douyin_scout"] == "douyin_cookie"
    assert SETUP_NEEDS["tiktok_scout"] == "tiktok_cookie"
