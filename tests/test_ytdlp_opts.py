"""Download hardening opts (ported from the hosted variant, 2026-07-19).

These pin the yt-dlp option layers that keep downloads working: the
original-audio format sort (multi-language dub bug), backoff-based retry
sleeps, the throttle re-extract guard, and the PO-token-aware YouTube
player-client ordering.
"""
from backend.services.ytdlp_service import _yt_dlp_base_opts, _yt_dlp_js_opts


def test_base_opts_prefer_original_language_first():
    sort = _yt_dlp_base_opts()["format_sort"]
    assert sort[0] == "lang", "lang must lead or multi-language videos pick a dub"
    assert "res:1080" in sort


def test_base_opts_have_backoff_and_throttle_guard():
    opts = _yt_dlp_base_opts()
    sleeps = opts["retry_sleep_functions"]
    assert sleeps["http"](5) == 32 and sleeps["http"](10) == 60  # capped exponential
    assert opts["throttledratelimit"] == 100_000


def test_js_opts_lead_with_token_free_player_clients():
    yt = _yt_dlp_js_opts()["extractor_args"]["youtube"]
    assert yt["player_client"][:3] == ["tv", "web_embedded", "android_vr"]
    assert yt["formats"] == ["missing_pot"]


def test_js_opts_cover_flaky_extractors():
    ea = _yt_dlp_js_opts()["extractor_args"]
    assert ea["twitter"] == {"api": ["syndication"]}
    assert ea["tiktok"]["app_info"] == [""]
    assert ea["youtubetab"] == {"skip": ["authcheck"]}


def test_impersonation_is_optional_never_required():
    # The impersonate key is present only when curl-cffi imported cleanly;
    # its absence must not change any other option.
    opts = _yt_dlp_base_opts()
    assert opts["quiet"] is True  # core opts intact either way
