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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from providers import register_provider
from providers.base import ProviderProfile

if TYPE_CHECKING:
    from agent.account_usage import AccountUsageSnapshot

logger = logging.getLogger(__name__)

try:
    from .models import _CLI_EFFORTS, _FALLBACK_MODELS, _normalize_effort, supports_effort_flag
except ImportError:  # flat source tree under test
    from models import _CLI_EFFORTS, _FALLBACK_MODELS, _normalize_effort, supports_effort_flag

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")
_TABLE_HEADERS = {"id", "model", "model id", "model_id"}
_DIAGNOSTIC_IDS = {"error", "warning", "failed", "failure"}


def _parse_model_table(output: str) -> list[str] | None:
    """Validate agy's ID/name table; preserve IDs, order and case exactly.

    None means invalid/incomplete discovery; [] is a successful empty output.
    Progress or headers alone cannot establish a successful empty table.
    """
    models: list[str] = []
    seen: set[str] = set()
    metadata = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().rstrip(".") == "fetching available models":
            metadata = True
            continue
        fields = raw_line.split("\t")
        model_id = fields[0]
        if (
            len(fields) == 2
            and model_id.lower() in _TABLE_HEADERS
            and fields[1].strip().lower() in {"name", "model name", "display name"}
        ):
            metadata = True
            continue
        if (
            len(fields) != 2
            or not fields[1].strip()
            or not _MODEL_ID.fullmatch(model_id)
            or model_id.lower().rstrip(":") in _DIAGNOSTIC_IDS | _TABLE_HEADERS
        ):
            return None
        if model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return None if metadata and not models else models


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
        """Native variants own their effort; only suffixless Gemini accepts a flag."""
        return _CLI_EFFORTS if supports_effort_flag(model) else ()

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Project the session for live reasoning; forward effort only when agy accepts it.

        The separate CLI effort flag is valid only for manual suffixless Gemini
        requests. The session projection is independent of the model.
        """
        top_level: dict[str, Any] = {}
        session_id = context.get("session_id")
        if isinstance(session_id, str) and session_id:
            from .live_reasoning import ensure_registered
            ensure_registered()
            top_level["_agy_session_id"] = session_id
        if not supports_effort_flag(model):
            return {}, top_level
        effort = None
        if isinstance(reasoning_config, dict):
            if reasoning_config.get("enabled") is False:
                effort = "low"
            else:
                effort = _normalize_effort(reasoning_config.get("effort"))
        if effort:
            top_level["reasoning_effort"] = effort
        return {}, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ) -> list[str] | None:
        """Return only exact IDs from a successful live `agy models` table.

        None means failed discovery; [] means successful empty discovery.
        No static catalog or plugin-owned cache is used.
        """
        try:
            from .client import resolve_agy_command
        except ImportError:
            # Loaded outside a package (e.g. a flat source tree under test):
            # the absolute name is the same module.
            from client import resolve_agy_command

        try:
            cmd = resolve_agy_command()
            res = subprocess.run(
                [cmd, "models"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0,
            )
            if res.returncode != 0:
                logger.debug("Antigravity model discovery exited %s", res.returncode)
                return None
            return _parse_model_table(res.stdout)
        except Exception as exc:
            logger.debug("Antigravity fetch_models failed: %s", exc)

        return None

    def fetch_account_usage(
        self, *, base_url: str | None = None, api_key: str | None = None
    ) -> AccountUsageSnapshot | None:
        """`/usage` hook: weekly quota windows reported by `agy -p /usage`, or None."""
        try:
            from .usage import fetch_usage_report
        except ImportError:  # flat source tree under test
            from usage import fetch_usage_report

        report = fetch_usage_report()
        if report is None:
            return None
        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

        return AccountUsageSnapshot(
            provider=self.name,
            source="agy_usage",
            fetched_at=datetime.now(timezone.utc),
            windows=tuple(
                AccountUsageWindow(
                    label=window.label,
                    used_percent=window.used_percent,
                    reset_at=window.reset_at,
                    detail=window.detail,
                )
                for window in report.windows
            ),
            raw=report.raw,
        )

    def get_model_context_length(self, model: str) -> int | None:
        """Defer to Hermes-native metadata unless a positive override is set.

        This public hook declares no account-serving capacity of its own.
        Unrecognized exact IDs may receive Hermes' generic unknown fallback.
        """
        env_val = os.environ.get("ANTIGRAVITY_CONTEXT_LENGTH")
        if env_val:
            try:
                val = int(env_val.strip())
                if val > 0:
                    return val
            except ValueError:
                pass
        return None

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
    default_aux_model="",
    fallback_models=_FALLBACK_MODELS,
    supports_vision=True,
    classify_api_error=_classify_antigravity_error,
    auth_handler=antigravity_auth_handler,
)

register_provider(antigravity_profile)


def register(ctx):
    """Optional general-plugin lifecycle entry point (provider discovery skips it)."""
    from .live_reasoning import register as register_reasoning
    return register_reasoning(ctx)
