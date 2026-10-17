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
        """Forward only supported separate effort flags for manual Gemini requests."""
        if not supports_effort_flag(model):
            return {}, {}
        effort = None
        if isinstance(reasoning_config, dict):
            if reasoning_config.get("enabled") is False:
                effort = "low"
            else:
                effort = _normalize_effort(reasoning_config.get("effort"))
        return {}, {"reasoning_effort": effort} if effort else {}

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
            )
            if res.returncode != 0:
                logger.debug("Antigravity model discovery exited %s", res.returncode)
                return None
            return _parse_model_table(res.stdout)
        except Exception as exc:
            logger.debug("Antigravity fetch_models failed: %s", exc)

        return None

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

    Three deterministic/terminal categories are recognized here so Hermes'
    turn_api_error retry loop stops burning its 3-attempt budget on a verdict
    that cannot change on an unmodified retry:

    * Native-tool mismatch (stream.py's ``Antigravity tool unsupported: ...``):
      deterministic per profile — the model will keep calling the same
      unsupported native tool on an identical retry, since nothing about the
      request changed. Confirmed live against a Hermes deployment: a turn
      hitting this retried the exact same message 3/3 times before giving up,
      each attempt ~15-18s apart for no chance of success. Classified like
      Hermes' own ``_V_MALFORMED_TOOL_ARGS`` precedent in
      agent/error_classifier.py (format_error, non-retryable, no fallback):
      the broken turn is aborted instead of replayed unchanged.
    * Explicit quota wall (process.py's ``_check_early_quota_error``, which
      already recognizes agy's own ``RESOURCE_EXHAUSTED`` / "Individual quota
      reached" / 429 log lines and is documented there as surfacing only
      AFTER agy's internal ~140s/8-attempt retry budget is already
      exhausted): an additional Hermes-level retry on the same window cannot
      succeed, so this is marked non-retryable while still allowed to fall
      over to a different model/provider.
    * Explicit auth rejection (client.py's own "Antigravity CLI is not
      authenticated..." pre-flight raise, and agy 1.3.x's own "Authentication
      expired. Please log back in." wording — see agy's `changelog`
      subcommand, 1.3.4 notes): re-running the identical request cannot
      restore a session that requires an interactive `agy` login; marked
      non-retryable so the user sees one clear message instead of 3 retries.

    The pre-existing ``context_overflow`` patterns below are unchanged.
    """
    err_str = f"{error} {message}".lower()

    if "antigravity tool unsupported" in err_str:
        return {
            "reason": "format_error",
            "retryable": False,
            "should_fallback": False,
        }

    if any(
        pattern in err_str
        for pattern in (
            "resource_exhausted",
            "individual quota reached",
            "quota exceeded",
            "code 429",
        )
    ):
        return {
            "reason": "rate_limit",
            "retryable": False,
            "should_fallback": True,
        }

    if any(
        pattern in err_str
        for pattern in (
            "antigravity cli is not authenticated",
            "authentication expired",
        )
    ):
        return {
            "reason": "auth_permanent",
            "retryable": False,
            "should_fallback": True,
        }

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
