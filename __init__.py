"""Antigravity OAuth provider plugin for Hermes Agent.

Sign-in is delegated to the official Antigravity CLI (``agy``): ``hermes auth add antigravity-oauth``
runs agy's own Google OAuth flow, and inference streams through ``agy`` using that session. The plugin
never reads, copies, or transmits the OAuth token.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

_FALLBACK_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.1-pro",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)


class AntigravityOAuthProfile(ProviderProfile):
    """Google Antigravity provider profile (auth owned by the agy CLI)."""

    def create_client(self, **client_kwargs: Any) -> Any:
        """Create the Antigravity client facade."""
        from .client import AntigravityClient

        return AntigravityClient(**client_kwargs)

    def setup_status(self, **kwargs: Any) -> dict[str, Any] | None:
        """Gate for `hermes model`: agy present + signed in; offers agy's own login when not."""
        try:
            from .auth import probe
        except ImportError:
            from auth import probe
        return probe()

    def supported_reasoning_efforts(
        self, model: str | None
    ) -> tuple[str, ...] | None:
        """Declared reasoning-effort vocabulary for models on this provider.
        
        Enables Hermes /model picker and /reasoning commands to offer appropriate
        thinking effort options (low, medium, high) for reasoning-capable models.
        """
        m = (model or "").lower()
        if "gemini-3.1-pro" in m:
            return ("low", "high")
        if "gemini" in m or "flash" in m or "pro" in m:
            return ("low", "medium", "high")
        if "claude" in m:
            return ("low", "medium", "high")
        if "gpt" in m:
            return ()
        return ("low", "medium", "high")

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Extract reasoning effort and forward as top-level api_kwargs to AntigravityClient."""
        effort = None
        if isinstance(reasoning_config, dict):
            if reasoning_config.get("enabled") is False:
                effort = "low"
            else:
                effort = reasoning_config.get("effort")
        top_level: dict[str, Any] = {}
        if effort:
            top_level["reasoning_effort"] = str(effort).strip().lower()
        return {}, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
        use_cache: bool = True,
    ) -> list[str] | None:
        """Query `agy models` and normalize to clean, deduplicated base models.

        Separates model families from thinking efforts (e.g. `gemini-3.8-flash-{low,medium,high}`
        becomes `gemini-3.8-flash`), allowing Hermes' native reasoning effort picker to handle
        the thinking depth cleanly.

        `agy models` hits the network on every call (~2s), and Hermes calls
        this on every provider/model switch, so a fresh file cache (TTL 1h,
        see models.models_cache_ttl) is returned without spawning agy. A
        stale cache is still preferred over the hardcoded fallback when the
        live call fails.
        """
        try:
            from .models import clean_base_models, load_models_cache, models_cache_ttl, save_models_cache
        except ImportError:
            # Loaded outside a package (e.g. a flat source tree under test):
            # the absolute name is the same module.
            from models import clean_base_models, load_models_cache, models_cache_ttl, save_models_cache
        try:
            from .client import resolve_agy_command
        except ImportError:
            # Loaded outside a package (e.g. a flat source tree under test):
            # the absolute name is the same module.
            from client import resolve_agy_command

        ttl = models_cache_ttl()
        if use_cache and ttl > 0:
            cached = load_models_cache(ttl)
            if cached:
                return cached
        cmd = resolve_agy_command()
        try:
            res = subprocess.run(
                [cmd, "models"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            raw_models: list[str] = []
            for raw_line in res.stdout.strip().splitlines():
                line = raw_line.strip()
                if not line or "fetching" in line.lower():
                    continue
                parts = line.split()
                if parts:
                    model_id = parts[0]
                    if any(c in model_id.lower() for c in ("gemini", "claude", "gpt", "model")):
                        raw_models.append(model_id)

            if raw_models:
                clean_models = clean_base_models(raw_models)
                if use_cache and ttl > 0:
                    save_models_cache(clean_models)
                return clean_models
        except Exception as exc:
            logger.debug("Antigravity fetch_models failed: %s", exc)

        if use_cache and ttl > 0:
            stale = load_models_cache(None)
            if stale:
                logger.debug("Antigravity fetch_models: serving stale catalog cache")
                return stale
        return list(_FALLBACK_MODELS)

    def get_model_context_length(self, model: str) -> int | None:
        """Declared context window for Antigravity CLI.
        
        Defaults to 200,000 tokens (allowing conversations to comfortably pass
        140k-160k tokens before auto-compression). Configurable via the
        ANTIGRAVITY_CONTEXT_LENGTH environment variable.
        """
        env_val = os.environ.get("ANTIGRAVITY_CONTEXT_LENGTH")
        if env_val:
            try:
                val = int(env_val.strip())
                if val > 0:
                    return val
            except ValueError:
                pass
        return 200_000

    def classify_api_error(
        self,
        error: Exception,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        message: str = "",
        body: Any = None,
        model: str | None = None,
    ) -> dict[str, Any] | None:
        return _classify_antigravity_error(
            error,
            status_code=status_code,
            error_code=error_code,
            message=message,
            body=body,
            model=model,
        )


def _classify_antigravity_error(
    error: Exception,
    *,
    status_code: int | None = None,
    error_code: str | None = None,
    message: str = "",
    body: Any = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Classify agy CLI specific runtime errors so Hermes' smart failover /
    recovery pipeline triggers auto-compression and retry instead of failing.
    """
    err_str = f"{error} {message}".lower()
    if any(
        pattern in err_str
        for pattern in (
            "subscriber fell behind updates",
            "stalled for 5s",
            "empty result (status='success')",
            "empty result (status=\"success\")",
            "context canceled",
            "max_trajectory_tokens",
            "max trajectory tokens",
        )
    ):
        return {
            "reason": "context_overflow",
            "retryable": True,
            "should_compress": True,
        }
    return None


try:
    from .auth import antigravity_auth_handler
except ImportError:  # flat source tree under test
    from auth import antigravity_auth_handler

antigravity_profile = AntigravityOAuthProfile(
    name="antigravity-oauth",
    aliases=("agy-oauth", "google-antigravity"),
    display_name="Google Antigravity (OAuth)",
    description="Gemini, Claude and GPT-OSS on your Google Antigravity plan, signed in via the official agy CLI",
    signup_url="https://antigravity.google/cli",
    base_url="agy://local",
    api_mode="chat_completions",
    auth_type="external_process",
    process_command="agy",
    process_args=("--output-format", "stream-json", "--disable-slash-commands"),
    process_command_env_vars=("ANTIGRAVITY_COMMAND", "AGY_CLI_PATH", "ANTIGRAVITY_CLI_PATH"),
    process_args_env_var="ANTIGRAVITY_ARGS",
    default_aux_model="gemini-3.8-flash",
    fallback_models=_FALLBACK_MODELS,
    supports_vision=True,
    classify_api_error=_classify_antigravity_error,
    auth_handler=antigravity_auth_handler,
)

register_provider(antigravity_profile)
