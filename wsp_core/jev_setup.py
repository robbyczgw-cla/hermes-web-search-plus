"""Optional Jev onboarding helpers. Secrets never appear in returned strings."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

JEV_DECISIONS = ("search_type", "extract_quality", "language_fill")
JEV_ENV = "TYPESAFE_API_KEY"
JEV_SIGNUP = "https://docs.typesafe.ai"
JEV_MODEL = "jev-1.13.0"
DEFAULT_MIN_CONFIDENCE = 0.85
SEARCH_TYPE_MIN_CONFIDENCE = 0.95


def parse_decisions(raw: Optional[str]) -> tuple[str, ...]:
    if raw is None or not str(raw).strip():
        return JEV_DECISIONS
    items = [part.strip() for part in str(raw).split(",") if part.strip()]
    unknown = [item for item in items if item not in JEV_DECISIONS]
    if unknown:
        raise ValueError(
            "Unknown Jev decision(s): "
            + ", ".join(unknown)
            + ". Choose from "
            + ", ".join(JEV_DECISIONS)
        )
    return tuple(dict.fromkeys(items))


def key_source(env: Optional[Mapping[str, str]] = None, config: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """Where the TypeSafe key comes from: only ``TYPESAFE_API_KEY`` as Hermes resolves it.

    Key files are no longer read (Hermes plugin rule 11); ``config`` is kept for callers.
    """
    env = env or {}
    return "env" if str(env.get(JEV_ENV) or "").strip() else None


def key_present(env: Optional[Mapping[str, str]] = None, config: Optional[Mapping[str, Any]] = None) -> bool:
    return key_source(env, config) is not None


def apply_jev_config(config: dict[str, Any], *, enabled: bool, decisions: Sequence[str]) -> dict[str, Any]:
    jev = dict(config.get("jev") or {})
    jev["enabled"] = bool(enabled)
    chosen = set(decisions)
    for name in JEV_DECISIONS:
        jev[name] = name in chosen if enabled else False
    jev.setdefault("min_confidence", DEFAULT_MIN_CONFIDENCE)
    jev.setdefault("search_type_min_confidence", SEARCH_TYPE_MIN_CONFIDENCE)
    jev.setdefault("timeout_s", 8.0)
    jev.pop("api_key", None)
    jev.pop("api_key_file", None)  # key files are no longer read; the key lives in Hermes
    config["jev"] = jev
    return config


def status_payload(env: Optional[Mapping[str, str]] = None, config: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    jev = (config or {}).get("jev") if isinstance(config, Mapping) else None
    if not isinstance(jev, Mapping):
        jev = {}
    source = key_source(env, config)
    decisions = {name: bool(jev.get(name, False)) for name in JEV_DECISIONS}
    return {
        "enabled": bool(jev.get("enabled", False)),
        "key_present": source is not None,
        "key_source": source,
        "decisions": decisions,
        "model": JEV_MODEL,
        "signup_url": JEV_SIGNUP,
    }


def plan_lines(*, want_enable: Optional[bool], decisions: Sequence[str], has_key: bool,
               currently_enabled: bool = False) -> list[str]:
    lines = [
        "Optional Jev — default off. Not a search provider.",
        f"  Signup: {JEV_SIGNUP}",
        "  Reads TYPESAFE_API_KEY through Hermes. Secrets are never printed.",
        "  Decisions: search_type overlay, extract_quality, language_fill.",
    ]
    if want_enable is False and currently_enabled:
        lines.append("  Plan: switch Jev off (it is on now); your stored key stays untouched.")
        return lines
    if want_enable is False:
        lines.append("  Plan: leave Jev disabled.")
        return lines
    if want_enable is None:
        lines.append("  Plan: ask whether to enable (default no).")
        return lines
    names = ", ".join(decisions) if decisions else "(none)"
    key_note = "key already present" if has_key else "store the key with `hermes config set TYPESAFE_API_KEY <key>`"
    lines.append(f"  Plan: enable Jev ({names}); {key_note}.")
    return lines
