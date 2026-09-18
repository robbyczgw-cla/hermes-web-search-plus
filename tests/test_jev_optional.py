from __future__ import annotations

from config import DEFAULT_CONFIG, _deepcopy_default_config, _validate_runtime_config
from jev_optional import (
    extract_item_action,
    filter_extract_results,
    keyword_vertical,
    maybe_fill_language,
    maybe_search_type,
    settings_from_config,
)


def test_default_config_jev_is_off():
    jev = DEFAULT_CONFIG["jev"]
    assert jev["enabled"] is False
    assert jev["search_type"] is False
    assert jev["extract_quality"] is False
    assert jev["language_fill"] is False
    assert settings_from_config(_deepcopy_default_config()).enabled is False


def test_validate_runtime_rejects_api_key_in_config():
    cfg = _deepcopy_default_config()
    cfg["jev"]["api_key"] = "nope"
    try:
        _validate_runtime_config(cfg)
    except ValueError as exc:
        assert "api_key" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_disabled_extract_keeps_cloudflare_page():
    content = "Just a moment... Checking your browser. Ray ID 123."
    action, meta = extract_item_action(content, config=_deepcopy_default_config())
    assert action == "keep"
    assert meta is None


def _on(**flags):
    cfg = _deepcopy_default_config()
    cfg["jev"]["enabled"] = True
    for key, value in flags.items():
        cfg["jev"][key] = value
    return cfg


def test_extract_regex_rejects_short_challenge_without_jev():
    content = "Just a moment... Checking your browser. Ray ID 123."
    action, meta = extract_item_action(content, config=_on(extract_quality=True))
    assert action == "reject"
    assert meta["backend"] == "regex"


def test_extract_jev_overrides_long_captcha_article():
    body = (
        "Researchers studied how CAPTCHA and hCaptcha affect checkout conversion. "
        * 20
    )

    def chooser(**kwargs):
        return "usable_content", 0.94

    action, meta = extract_item_action(
        body, query="captcha conversion study", config=_on(extract_quality=True), chooser=chooser
    )
    assert action == "keep"
    assert meta["backend"] == "jev"
    assert meta["overrode"] == "blocked_or_challenge"


def test_extract_low_confidence_does_not_override():
    body = (
        "Researchers studied how CAPTCHA and hCaptcha affect checkout conversion. "
        * 20
    )

    def chooser(**kwargs):
        return "usable_content", 0.4

    action, meta = extract_item_action(
        body, config=_on(extract_quality=True), chooser=chooser
    )
    assert action == "reject"
    assert meta["backend"] == "jev_fallback_regex"


def test_filter_extract_keeps_error_rows():
    rows = [{"url": "https://x.example", "error": "timeout"}]
    kept, meta = filter_extract_results(rows, config=_on(extract_quality=True))
    assert kept == rows
    assert meta is None


def test_language_fill_only_when_wsp_none():
    def chooser(**kwargs):
        return "de", 0.96

    filled, meta = maybe_fill_language(
        "Öffnungszeiten Apotheke Graz", "en", config=_on(language_fill=True), chooser=chooser
    )
    assert filled == "en"
    assert meta is None

    filled, meta = maybe_fill_language(
        "Öffnungszeiten Apotheke Graz", None, config=_on(language_fill=True), chooser=chooser
    )
    assert filled == "de"
    assert meta["applied"] is True


def test_keyword_vertical_news_and_search():
    assert keyword_vertical("breaking news Wien") == "news"
    assert keyword_vertical("python asyncio tutorial") == "search"
    assert keyword_vertical("near constant time comparison HMAC") == "search"


def test_search_type_skips_jev_when_keyword_is_search():
    calls = []

    def chooser(**kwargs):
        calls.append(1)
        return "news", 0.99

    label, meta = maybe_search_type(
        "python asyncio tutorial", "search", config=_on(search_type=True), chooser=chooser
    )
    assert label == "search"
    assert meta is None
    assert calls == []


def test_search_type_explicit_news_is_not_overridden():
    def chooser(**kwargs):
        return "search", 0.99

    label, meta = maybe_search_type(
        "python asyncio tutorial", "news", config=_on(search_type=True), chooser=chooser
    )
    assert label == "news"
    assert meta is None


def test_search_type_overlay_confirms_news_at_095():
    def chooser(**kwargs):
        return "news", 0.97

    label, meta = maybe_search_type(
        "breaking news Wien", "search", config=_on(search_type=True), chooser=chooser
    )
    assert label == "news"
    assert meta["applied"] == "news"


def test_search_type_overlay_falls_back_below_095():
    def chooser(**kwargs):
        return "news", 0.90

    label, meta = maybe_search_type(
        "press briefing Raft consensus", "search", config=_on(search_type=True), chooser=chooser
    )
    assert label == "search"
    assert meta["applied"] == "search"


def test_search_type_overlay_vetoes_keyword_news():
    def chooser(**kwargs):
        return "search", 0.99

    label, meta = maybe_search_type(
        "live python tutorial tonight", "search", config=_on(search_type=True), chooser=chooser
    )
    assert label == "search"
    assert meta["applied"] == "search"


def test_disabled_search_type_leaves_requested():
    def chooser(**kwargs):
        raise AssertionError("jev must not run")

    label, meta = maybe_search_type(
        "breaking news Wien", "search", config=_deepcopy_default_config(), chooser=chooser
    )
    assert label == "search"
    assert meta is None
