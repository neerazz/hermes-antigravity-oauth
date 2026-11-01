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
import math
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple
from uuid import uuid4

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
    fetched_at: datetime | None = None


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
    if type(body.get("num_turns")) is not int or body["num_turns"] != 0:
        return None
    token_usage = body.get("usage")
    if not isinstance(token_usage, dict) or any(
        type(token_usage.get(key)) not in (int, float) or token_usage[key] != 0
        for key in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
    ) or any("tokens" in str(key).lower() and value != 0 for key, value in token_usage.items()):
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
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
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


# Cached reads never authenticate, resolve agy or spawn a process. Only the explicit
# refresh entry point below invokes the original guarded PR3 runner.
MAX_CACHE_BYTES = 256 * 1024
FRESHNESS_SECONDS = 30 * 60
_POOLS = {"Gemini Models": ("gemini-5h", "gemini-weekly"),
          "Claude and GPT models": ("3p-5h", "3p-weekly")}
_ARGON_GROUP = "Gemini 4 Argon"
_ARGON_BUCKETS = {"5h": "gemini-argon-5h", "weekly": "gemini-argon-wtu-weekly"}
_ARGON_GROUP_RE = re.compile(r"\bargon\b", re.IGNORECASE)
_ARGON_ID_RE = re.compile(r"(?:[a-z0-9]+[-_])*argon(?:[-_][a-z0-9]+)*\Z", re.IGNORECASE)


def cache_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    else:
        home = get_hermes_home()
    # plugin_data_dir() creates directories; reads must remain strictly read-only.
    return home / "plugin-data" / "antigravity-oauth" / "quota" / "latest.json"


def _check_directory(path: Path) -> None:
    # Profile ancestry is trusted, but do not follow existing symlink directories.
    for directory in (path, *path.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("unsafe_state")


def _open_state(path: Path, flags: int) -> int:
    before = path.lstat() if path.exists() or path.is_symlink() else None
    if before and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
        raise ValueError("unsafe_state")
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        opened, after = os.fstat(fd), path.lstat()
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not stat.S_ISREG(after.st_mode)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                or before and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError("unsafe_state")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _argon_target(is_argon_group: bool, ident: Any, window: Any, disabled: bool) -> tuple[str, Any] | None:
    if not isinstance(ident, str) or not ident.strip():
        return None
    raw = ident.strip()
    if any(raw == known for ids in _POOLS.values() for known in ids):
        return None
    if not is_argon_group and not _ARGON_ID_RE.fullmatch(raw):
        return None
    lower = raw.lower()
    inferred = "5h" if lower.endswith(("-5h", "_5h")) else "weekly" if lower.endswith(("-weekly", "_weekly")) else None
    win = inferred or (window if window in _ARGON_BUCKETS else None)
    if win in _ARGON_BUCKETS:
        return _ARGON_BUCKETS[win], (win if disabled and window is None else window)
    return None


def _validate_meters(meters: Any) -> list[dict]:
    if not isinstance(meters, list) or len(meters) > 6:
        raise ValueError("invalid_cache")
    clean, seen = [], set()
    for meter in meters:
        if not isinstance(meter, dict):
            raise ValueError("invalid_cache")
        group, ident = meter.get("group"), meter.get("id")
        allowed = _POOLS.get(group) or (tuple(_ARGON_BUCKETS.values()) if group == _ARGON_GROUP else ())
        if not allowed or ident not in allowed or ident in seen:
            raise ValueError("invalid_cache")
        seen.add(ident)
        window = "5h" if ident.endswith("5h") else "weekly"
        value, reset, disabled = meter.get("remaining_fraction"), meter.get("reset_time"), meter.get("disabled", False)
        if type(disabled) is not bool or meter.get("window") != window:
            raise ValueError("invalid_cache")
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError("invalid_cache")
        if reset is not None and _parse_reset(reset) is None:
            raise ValueError("invalid_cache")
        clean.append(dict(id=ident, group=group, window=window, remaining_fraction=value,
                          reset_time=reset, disabled=disabled))
    return clean


def _disabled(value: dict) -> bool:
    if any(key in value and type(value[key]) is not bool for key in ("enabled", "disabled")):
        raise ValueError("invalid_disabled_flag")
    return value.get("disabled") is True or value.get("enabled") is False


def _read_cache(path: Path) -> dict | None:
    try:
        _check_directory(path.parent)
        fd = _open_state(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_CACHE_BYTES:
            raise ValueError("invalid_cache")
        text = stream.read(MAX_CACHE_BYTES + 1)
    if len(text) > MAX_CACHE_BYTES:
        raise ValueError("invalid_cache")
    record = json.loads(text)
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError("invalid_cache")
    observed = record.get("observed_at")
    if observed is not None and (_parse_reset(observed) is None or not re.fullmatch(r"[a-f0-9]{32}", str(record.get("observation_id")))):
        raise ValueError("invalid_cache")
    health = record.get("health")
    if not isinstance(health, dict) or health.get("status") not in ("ok", "error"):
        raise ValueError("invalid_cache")
    meters = _validate_meters(record.get("meters"))
    if meters and observed is None:
        raise ValueError("invalid_cache")
    return dict(schema_version=1, observed_at=observed, observation_id=record.get("observation_id"),
                meters=meters, health={"status": health["status"]})


def _atomic_cache(path: Path, record: dict) -> None:
    if path.exists() or path.is_symlink():
        fd = _open_state(path, os.O_RDONLY)
        os.close(fd)
    fd, temporary = tempfile.mkstemp(prefix=".quota-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class _RefreshBusy(Exception):
    pass


@contextlib.contextmanager
def _refresh_lock(path: Path):
    # Persistent OS lock: process death releases ownership; no stale-lock deletion race.
    missing, existing = [], path.parent
    while not existing.exists() and not existing.is_symlink():
        missing.append(existing)
        existing = existing.parent
    _check_directory(existing)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        _check_directory(directory)
    _check_directory(path.parent)
    fd = _open_state(path.parent / "refresh.lock", os.O_RDWR | os.O_CREAT)
    try:
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            import errno
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise _RefreshBusy from exc
        yield
    finally:
        os.close(fd)


def refresh_usage(*, now: datetime | None = None) -> dict:
    """Explicit guarded refresh only. No login, inference, credits or automatic jobs."""
    try:
        path = cache_path()
        with _refresh_lock(path):
            previous = _read_cache(path)
            try:
                report = fetch_usage_report()
                observed = now or datetime.now(timezone.utc)
                previous_at = _parse_reset(previous["observed_at"]) if previous else None
                if previous_at and observed < previous_at:
                    raise ValueError("clock_regression")
                if report is None:
                    raise ValueError("refresh_unavailable")
                meters = []
                for group in _dicts(report.raw.get("command", {}).get("data", {}).get("groups")):
                    raw_name = group.get("name")
                    is_argon_group = isinstance(raw_name, str) and raw_name not in _POOLS and bool(_ARGON_GROUP_RE.search(raw_name))
                    name = _ARGON_GROUP if is_argon_group else raw_name
                    group_disabled = _disabled(group) if (name in _POOLS or is_argon_group) else False
                    for bucket in _dicts(group.get("buckets")):
                        raw_id, raw_window = bucket.get("id"), bucket.get("window")
                        disabled = (_disabled(bucket) if (name in _POOLS or is_argon_group or (isinstance(raw_id, str) and _ARGON_ID_RE.fullmatch(raw_id.strip()))) else False) or group_disabled
                        argon = _argon_target(is_argon_group, raw_id, raw_window, disabled)
                        if argon is not None:
                            target_group, (ident, window) = _ARGON_GROUP, argon
                        elif isinstance(name, str) and raw_id in _POOLS.get(name, ()):
                            target_group, ident, window = name, raw_id, raw_window
                        else:
                            continue
                        value = bucket.get("remaining_fraction")
                        reset = _parse_reset(bucket.get("reset_time"))
                        meters.append(dict(id=ident, group=target_group, window=window,
                            remaining_fraction=value, reset_time=reset.isoformat() if reset else None,
                            disabled=disabled))
                    if is_argon_group and group_disabled and not any(m["group"] == _ARGON_GROUP for m in meters):
                        meters.append(dict(id=_ARGON_BUCKETS["weekly"], group=_ARGON_GROUP, window="weekly",
                            remaining_fraction=None, reset_time=None, disabled=True))
                record = dict(schema_version=1, observed_at=observed.isoformat(), observation_id=uuid4().hex,
                              meters=_validate_meters(meters), health={"status": "ok"})
                _atomic_cache(path, record)
            except (OSError, ValueError, TypeError):
                failed: dict = previous or dict(schema_version=1, observed_at=None, observation_id=None, meters=[])
                failed["health"] = {"status": "error"}
                _atomic_cache(path, failed)
                return {"ok": False, "error": {"code": "refresh_unavailable"}}
        return read_usage_status(now=observed)
    except _RefreshBusy:
        return {"ok": False, "error": {"code": "refresh_busy"}}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "error": {"code": "state_unavailable"}}


def read_usage_status(*, now: datetime | None = None) -> dict:
    """Read cached metadata only; elapsed resets never fabricate a refill."""
    observed_now = now or datetime.now(timezone.utc)
    try:
        record = _read_cache(cache_path())
    except (OSError, ValueError, TypeError):
        record = dict(observed_at=None, observation_id=None, meters=[], health={"status": "error"})
    observed = _parse_reset(record["observed_at"]) if record else None
    age = (observed_now - observed).total_seconds() if observed else None
    health = record["health"] if record else None
    cache_stale = age is None or age < 0 or age >= FRESHNESS_SECONDS or health != {"status": "ok"}
    meters = []
    for item in record["meters"] if record else []:
        reset = _parse_reset(item["reset_time"])
        seconds = max(0, (reset - observed_now).total_seconds()) if reset else None
        meters.append(dict(item, seconds_to_reset=seconds, reset_due=seconds == 0 if seconds is not None else None,
                           available=not cache_stale and not item["disabled"] and item["remaining_fraction"] is not None
                           and item["remaining_fraction"] > 0 and seconds is not None and seconds > 0))
    stale = cache_stale or any(m["reset_due"] for m in meters)
    pools = [dict(pool=group, available=not stale and all(any(m["id"] == ident and m["available"] for m in meters) for ident in ids))
             for group, ids in _POOLS.items()]
    argon_meters = [m for m in meters if m["group"] == _ARGON_GROUP]
    if argon_meters:
        pools.append(dict(pool=_ARGON_GROUP, available=not stale and all(m["available"] for m in argon_meters)))
    freshness = "error" if health == {"status": "error"} else "missing" if observed is None else "stale" if stale else "fresh"
    return {"ok": True, "snapshot": dict(observed_at=record["observed_at"] if record else None,
        observation_id=record["observation_id"] if record else None, age_seconds=age,
        freshness_limit_seconds=FRESHNESS_SECONDS, freshness=freshness, stale=stale,
        health=health, meters=meters, pools=pools)}


def read_cached_usage_report(*, now: datetime | None = None) -> UsageReport | None:
    view = read_usage_status(now=now)["snapshot"]
    if not view["observed_at"] or not view["meters"]:
        return None
    groups = [{"name": group, "buckets": [m for m in view["meters"] if m["group"] == group and not m["disabled"]]}
              for group in (*_POOLS, _ARGON_GROUP)]
    windows = usage_windows({"groups": groups})
    windows = [w._replace(reset_at=None, detail=f"Cached {view['freshness']} observation; reset {w.reset_at.isoformat()} due/unconfirmed")
               if w.reset_at and w.reset_at <= (now or datetime.now(timezone.utc))
               else w._replace(detail=f"Cached {view['freshness']} observation") for w in windows]
    return UsageReport(tuple(windows), view, _parse_reset(view["observed_at"]))


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Cache-only quota status or explicit guarded refresh")
    parser.add_argument("action", choices=("status", "refresh"), nargs="?", default="status")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = refresh_usage() if args.action == "refresh" else read_usage_status()
    if args.json:
        print(json.dumps(result, allow_nan=False))
    elif result["ok"]:
        view = result["snapshot"]
        print(f"Quota: {view['freshness']} | observed {view['observed_at'] or 'Unknown'} | age {view['age_seconds']}s")
        for meter in view["meters"]:
            value = meter["remaining_fraction"]
            remaining = "Unknown" if value is None else f"{value * 100:.1f}%"
            reset = "due/unconfirmed" if meter["reset_due"] else meter["reset_time"] or "Unknown"
            print(f"{meter['id']}: {remaining} remaining | reset {reset}")
    else:
        print(f"Quota unavailable ({result['error']['code']}); no confirmed observation.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
