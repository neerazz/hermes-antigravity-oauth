"""Model catalog mapping and reasoning effort resolution for Antigravity."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.1-pro",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
]

_MODEL_ALIASES = {
    "default": "gemini-3.8-flash",
    "flash": "gemini-3.8-flash",
    "gemini-flash": "gemini-3.8-flash",
    "gemini-3.8": "gemini-3.8-flash",
    "pro": "gemini-3.1-pro",
    "gemini-pro": "gemini-3.1-pro",
    "gemini-3.1": "gemini-3.1-pro",
    "sonnet": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6-thinking",
    "claude-opus": "claude-opus-4-6-thinking",
}


def _normalize_effort(effort: str | None) -> str | None:
    if not effort:
        return None
    e = str(effort).strip().lower()
    if e in ("none", "off", "minimal"):
        return "low"
    if e in ("xhigh", "max", "ultra"):
        return "high"
    if e in ("low", "medium", "high"):
        return e
    return "medium"


def resolve_model_and_effort(
    model: str | None,
    reasoning_effort: str | None = None,
) -> tuple[str, str | None]:
    """Map user/hermes model request to concrete CLI model ID and effort level."""
    m = str(model or "gemini-3.8-flash").strip()
    m = _MODEL_ALIASES.get(m.lower(), m)

    # Check if the model already contains an explicit effort suffix
    base_model = m
    suffix_effort = None
    for suffix, eff in (("-high", "high"), ("-medium", "medium"), ("-low", "low")):
        if m.endswith(suffix):
            suffix_effort = eff
            base_model = m[:-len(suffix)]
            break

    # Resolve effort: explicit argument > model suffix > config setting > default
    effort = _normalize_effort(reasoning_effort)
    if not effort and suffix_effort:
        effort = suffix_effort
    if not effort:
        try:
            from hermes_cli.config import load_config_readonly
            cfg_effort = load_config_readonly().get("agent", {}).get("reasoning_effort")
            effort = _normalize_effort(cfg_effort)
        except Exception:
            pass
    if not effort:
        effort = "medium"

    # Map base_model + effort to concrete agy model ID
    if base_model == "gemini-3.1-pro":
        # gemini-3.1-pro only has -low and -high in agy
        concrete_effort = "low" if effort == "low" else "high"
        return f"{base_model}-{concrete_effort}", concrete_effort
    elif base_model.startswith("gemini-") and "flash" in base_model:
        concrete_effort = effort if effort in ("low", "medium", "high") else "medium"
        return f"{base_model}-{concrete_effort}", concrete_effort
    else:
        # Local patch: agy rejects --effort for non-Gemini models (claude-*, gpt-oss-*):
        # "--effort is not supported for model ...". Their effort, if any, is baked into the id.
        return m, None


# --- Model catalog cache -------------------------------------------------
# `agy models` hits the network on every invocation ("Fetching available
# models...", ~2s), and Hermes calls fetch_models() on every provider/model
# switch. A file cache with a TTL keeps switches instant while staying
# fresh; any live failure falls back to the stale cache, then to
# _FALLBACK_MODELS. All helpers are best-effort and never raise.

_MODELS_CACHE_TTL_SECONDS = 3600.0
_MODELS_CACHE_DIRNAME = "hermes-antigravity-oauth"
_MODELS_CACHE_FILENAME = "models.json"


def models_cache_ttl() -> float:
    """Cache TTL in seconds; ANTIGRAVITY_MODELS_CACHE_TTL overrides, <= 0 disables."""
    try:
        return float(os.getenv("ANTIGRAVITY_MODELS_CACHE_TTL", str(_MODELS_CACHE_TTL_SECONDS)))
    except ValueError:
        return _MODELS_CACHE_TTL_SECONDS


def models_cache_path() -> Path:
    """Cache file location; ANTIGRAVITY_MODELS_CACHE_FILE overrides the full path."""
    override = os.getenv("ANTIGRAVITY_MODELS_CACHE_FILE", "").strip()
    if override:
        return Path(override)
    base = os.getenv("XDG_CACHE_HOME", "").strip()
    if base:
        return Path(base) / _MODELS_CACHE_DIRNAME / _MODELS_CACHE_FILENAME
    return Path.home() / ".cache" / _MODELS_CACHE_DIRNAME / _MODELS_CACHE_FILENAME


def load_models_cache(max_age_seconds: float | None) -> list[str] | None:
    """Return cached base model ids, or None on any miss/problem.

    max_age_seconds=None accepts a cache of any age (stale fallback);
    otherwise only a cache written within the age is returned.
    """
    try:
        path = models_cache_path()
        raw = json.loads(path.read_text(encoding="utf-8"))
        models = raw.get("models") if isinstance(raw, dict) else None
        fetched_at = raw.get("fetched_at") if isinstance(raw, dict) else None
        if (not isinstance(models, list) or not models
                or not all(isinstance(m, str) and m.strip() for m in models)
                or not isinstance(fetched_at, (int, float))):
            return None
        if max_age_seconds is not None and (time.time() - float(fetched_at)) > max_age_seconds:
            return None
        return list(models)
    except (OSError, ValueError):
        return None


def save_models_cache(models: list[str]) -> None:
    """Persist base model ids; silently ignored on any filesystem problem."""
    if not models:
        return
    try:
        path = models_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "models": list(models)}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def clean_base_models(raw_ids: list[str]) -> list[str]:
    """Normalize raw `agy models` ids to deduplicated base names.

    `gemini-3.8-flash-{low,medium,high}` becomes `gemini-3.8-flash` so
    Hermes' reasoning-effort picker handles depth; non-Gemini ids keep
    their baked-in effort suffix.
    """
    clean_models: list[str] = []
    seen: set[str] = set()
    for m in raw_ids:
        base = m
        for suffix in ("-high", "-medium", "-low"):
            if m.endswith(suffix) and (m.startswith("gemini-") or "flash" in m or "pro" in m):
                base = m[: -len(suffix)]
                break
        if base not in seen:
            seen.add(base)
            clean_models.append(base)
    return clean_models
