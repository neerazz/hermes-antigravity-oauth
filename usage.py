"""Quota windows for `/usage`, read from agy's own `agy -p /usage` command.

agy answers it from its signed-in session without a model turn, so this keeps the plugin's
contract: no HTTP requests of its own, and the OAuth token is never read. Hermes types stay out of
this module so it imports without Hermes' runtime dependencies; the profile hook adapts the result.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple

try:
    from .process import _own_process_group, is_authenticated, resolve_agy_command
except ImportError:  # flat source tree under test
    from process import _own_process_group, is_authenticated, resolve_agy_command

# Print-mode slash commands shipped in agy 1.1.11. Older builds send "/usage" to the model as a
# prompt, which spends quota on every refresh.
MIN_AGY_VERSION = (1, 1, 11)
# Hermes discards the hook's result after 10 s (PLUGIN_USAGE_HOOK_DEADLINE_S) and abandons the
# thread without killing our children, so the agy runs share one budget that, plus the post-kill
# cleanup, ends before that. Our own cleanup is a taskkill and a drain of DRAIN_TIMEOUT_S each. On
# the Windows Job Object path the cleanup is Hermes': it closes the job before its taskkill, which
# then finds a dead tree; only a hung taskkill could overrun, and that costs one /usage result.
# is_authenticated()'s keyring probes carry their own timeouts and are not covered: once they eat
# the budget, agy is never spawned.
USAGE_BUDGET_S = 7.5
VERSION_TIMEOUT_S = 3.0
DRAIN_TIMEOUT_S = 1.0
USAGE_ARGS = ("-p", "/usage", "--output-format", "json", "--print-timeout", "6s")

_WINDOW_LABELS = {"weekly": "7d"}
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class QuotaWindow(NamedTuple):
    label: str
    used_percent: float
    reset_at: datetime | None
    detail: str | None


class UsageReport(NamedTuple):
    windows: tuple[QuotaWindow, ...]
    raw: dict[str, Any]


def fetch_usage_report() -> UsageReport | None:
    """Quota windows from `agy -p /usage`, or None when agy cannot answer without side effects."""
    deadline = time.monotonic() + USAGE_BUDGET_S
    # Signed out, agy starts its browser OAuth flow and blocks; never spawn it without a session.
    if not is_authenticated():
        return None
    agy = resolve_agy_command()
    if not agy_supports_print_usage(agy, deadline):
        return None
    stdout = run_agy(agy, USAGE_ARGS, deadline)
    if stdout is None:
        return None
    body = parse_usage_response(stdout)
    if body is None:
        return None
    windows = usage_windows(body["command"]["data"])
    if not windows:
        return None
    return UsageReport(windows=tuple(windows), raw=body)


def agy_supports_print_usage(agy: str, deadline: float) -> bool:
    stdout = run_agy(agy, ("--version",), min(deadline, time.monotonic() + VERSION_TIMEOUT_S))
    version = parse_agy_version(stdout or "")
    return version is not None and version >= MIN_AGY_VERSION


def parse_agy_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def run_agy(agy: str, args: tuple[str, ...], deadline: float) -> str | None:
    """stdout of a zero-exit agy run that finished before `deadline` (monotonic), else None."""
    timeout_s = deadline - time.monotonic()
    if timeout_s <= 0:
        return None
    argv = [agy, *args]
    # A private cwd keeps agy from adopting the caller's repository as its workspace. Windows
    # refuses to delete a directory a lingering agy child still sits in; that must not discard a
    # result that was already read.
    with tempfile.TemporaryDirectory(prefix="hermes-agy-usage-", ignore_cleanup_errors=True) as cwd:
        job_runner = _windows_job_runner()
        if job_runner is not None:
            result = job_runner(argv, timeout=timeout_s, env=_child_env(), cwd=cwd)
            return result.stdout if result is not None and result.returncode == 0 else None
        return _run_in_own_group(argv, cwd, timeout_s)


def _windows_job_runner() -> Callable[..., subprocess.CompletedProcess[str] | None] | None:
    """Hermes' Job Object probe runner on Windows; None on POSIX or where it is unusable."""
    # taskkill /T finds children through their live parent, so a child left behind by an agy that
    # already exited escapes it. Hermes' runner starts agy inside a Job Object and closes the job on
    # timeout, which takes the whole tree down regardless of ancestry. It also closes the job after
    # a clean exit, so nothing agy leaves behind outlives the call.
    if os.name != "nt":
        return None
    # A private Hermes API, so every way it can be unusable falls back to our own runner rather
    # than failing /usage: missing (Hermes < 0.21.4), a backend that won't import (the runner
    # imports it lazily and would report every run as a spawn failure; it needs psutil), or a
    # signature that no longer takes the arguments we pass.
    try:
        compat = importlib.import_module("hermes_cli._subprocess_compat")
        importlib.import_module("hermes_cli.local_runtime.processes")
        runner = compat.bounded_probe_run
        inspect.signature(runner).bind(["agy"], timeout=1.0, env={}, cwd=".")
    except Exception:  # fail-open capability probe, see above
        return None
    return runner


def _run_in_own_group(argv: list[str], cwd: str, timeout_s: float) -> str | None:
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            **_own_process_group(),
        )
    except OSError:
        return None
    try:
        stdout, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_agy_tree(proc)
        _drain(proc)
        return None
    return stdout if proc.returncode == 0 else None


def _kill_agy_tree(proc: subprocess.Popen) -> None:
    # The leader may already have exited while a child still holds stdout open, which is exactly
    # when a timeout fires, so neither branch checks whether the leader is alive (process.py's
    # _kill_process_tree does, and would skip the child).
    if os.name == "nt":
        # Only reached when Hermes' Job Object runner is unusable. taskkill can't reach children of
        # an already-exited leader there; _drain bounds the wait on those instead.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DRAIN_TIMEOUT_S,
                check=False,
            )
        return
    # The group id stays reserved while any member is alive, even after the leader exits.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — the nt branch above returns


def _drain(proc: subprocess.Popen) -> None:
    # A survivor can keep the pipe open; never wait on it unbounded.
    try:
        proc.communicate(timeout=DRAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # On Windows communicate() reads in a daemon thread that holds the pipe's lock, so close()
        # would block until the survivor exits; that thread is left to finish on its own.
        if os.name != "nt" and proc.stdout is not None:
            proc.stdout.close()
        # Reap the killed leader now rather than leaving it to Popen.__del__.
        proc.poll()


def parse_usage_response(stdout: str) -> dict[str, Any] | None:
    """The decoded body of a genuine `/usage` command response, else None."""
    try:
        body = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("status") != "SUCCESS":
        return None
    # A model turn means agy treated "/usage" as a prompt rather than running the command.
    if body.get("num_turns") != 0:
        return None
    command = body.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        return None
    return body if isinstance(command.get("data"), dict) else None


def usage_windows(data: dict[str, Any]) -> list[QuotaWindow]:
    """One window per quota bucket; buckets without a usable fraction are skipped, never zero-filled."""
    windows: list[QuotaWindow] = []
    seen_labels: set[str] = set()
    for group in _dicts(data.get("groups")):
        group_label = _group_label(group.get("name"))
        for bucket in _dicts(group.get("buckets")):
            window = _bucket_window(group_label, bucket)
            if window is None:
                continue
            # Consumers key rows and alert history by label, so it must be unique per provider.
            label = _unique_label(window.label, bucket.get("id"), seen_labels)
            seen_labels.add(label)
            windows.append(window._replace(label=label))
    return windows


def _bucket_window(group_label: str, bucket: dict[str, Any]) -> QuotaWindow | None:
    remaining = bucket.get("remaining_fraction")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        return None
    remaining = min(1.0, max(0.0, float(remaining)))
    window = str(bucket.get("window") or "").strip()
    label = " ".join(part for part in (group_label, _WINDOW_LABELS.get(window, window)) if part)
    description = bucket.get("description")
    return QuotaWindow(
        label=label or str(bucket.get("id") or "Quota"),
        used_percent=(1.0 - remaining) * 100.0,
        reset_at=_parse_reset(bucket.get("reset_time")),
        detail=description if isinstance(description, str) and description else None,
    )


def _unique_label(label: str, bucket_id: Any, seen: set[str]) -> str:
    if label not in seen:
        return label
    if bucket_id and f"{label} ({bucket_id})" not in seen:
        return f"{label} ({bucket_id})"
    n = 2
    while f"{label} ({n})" in seen:
        n += 1
    return f"{label} ({n})"


def _group_label(name: Any) -> str:
    """Short group name for a usage row: 'Claude and GPT models' -> 'Claude/GPT'."""
    words = str(name or "").split()
    if words and words[-1].lower() == "models":
        words = words[:-1]
    return " ".join(words).replace(" and ", "/")


def _parse_reset(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _child_env() -> dict[str, str]:
    # Every agy start may spawn its background updater (a console flash on Windows, issue #2).
    # agy reads this switch case-sensitively: only "true" disables it.
    return {**os.environ, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
