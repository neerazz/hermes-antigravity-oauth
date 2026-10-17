"""Antigravity Subscription DirectSDK client for Hermes Agent.

Drives the official Antigravity CLI (`agy`) as an external process in
stream-json mode to provide request-scoped completions using the user's
existing Antigravity / Gemini subscription quota.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from .models import (
        _FALLBACK_MODELS,
        _MODEL_ALIASES,
        _normalize_effort,
        resolve_model_and_effort,
    )
    from .process import (
        AGY_MARKER_BASE_URL,
        _kill_process_tree,
        _own_process_group,
        build_child_env,
        is_authenticated,
        resolve_agy_command,
        resolve_real_token_path,
        setup_isolated_home,
        terminate_process,
    )
    from .prompt import (
        _PROMPT_PREAMBLE,
        _ROLE_LABELS,
        _format_delta_prompt,
        _format_messages_as_prompt,
        _longest_tool_call_prefix_match,
        _messages_match_prefix,
        _parse_tool_block,
        _render_message_content,
    )
    from .stream import AntigravityStream, collect_stream_completion
    from . import soul as _soul
except ImportError:
    from models import (
        _FALLBACK_MODELS,
        _MODEL_ALIASES,
        _normalize_effort,
        resolve_model_and_effort,
    )
    from process import (
        AGY_MARKER_BASE_URL,
        _kill_process_tree,
        _own_process_group,
        build_child_env,
        is_authenticated,
        resolve_agy_command,
        resolve_real_token_path,
        setup_isolated_home,
        terminate_process,
    )
    from prompt import (
        _PROMPT_PREAMBLE,
        _ROLE_LABELS,
        _format_delta_prompt,
        _format_messages_as_prompt,
        _longest_tool_call_prefix_match,
        _messages_match_prefix,
        _parse_tool_block,
        _render_message_content,
    )
    from stream import AntigravityStream, collect_stream_completion
    import soul as _soul

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 300.0

# Issue #4 (Windows, WinError 32): terminating agy's process tree
# (taskkill /F /T) is asynchronous, so a child -- or a grandchild -- can
# still hold conversations/*.db for a short moment after termination
# returns, and TemporaryDirectory.cleanup() then raises PermissionError.
# Three quick attempts (0.1s, then 0.25s apart) cover that handle-release
# window: the worst case adds ~0.35s to a close() that was failing anyway,
# while the normal case (POSIX, or Windows where the handle is already
# gone) succeeds on the first attempt and pays none of it. After the last
# attempt a forced removal drops whatever is still unlocked, so close()
# never raises and never leaves the workspace behind needlessly.
#
# The tuple is the single source of truth for the retry budget: the
# attempts count is DERIVED from it (one try plus one backoff per retry),
# so extending the budget can never desynchronize the two -- a longer
# budget than tuple would index past its end and the IndexError would be
# swallowed by _remove_temp_dir's outer guard, turning a retry budget
# into a silent workspace leak.
_TEMP_DIR_CLEANUP_BACKOFF_SECONDS = (0.1, 0.25)
_TEMP_DIR_CLEANUP_ATTEMPTS = len(_TEMP_DIR_CLEANUP_BACKOFF_SECONDS) + 1

__all__ = [
    "AGY_MARKER_BASE_URL",
    "AntigravityClient",
    "AntigravityStream",
    "_FALLBACK_MODELS",
    "_MODEL_ALIASES",
    "_PROMPT_PREAMBLE",
    "_ROLE_LABELS",
    "_TEMP_DIR_CLEANUP_ATTEMPTS",
    "_TEMP_DIR_CLEANUP_BACKOFF_SECONDS",
    "_force_rmtree",
    "_format_delta_prompt",
    "_format_messages_as_prompt",
    "_kill_process_tree",
    "_longest_tool_call_prefix_match",
    "_messages_match_prefix",
    "_normalize_effort",
    "_own_process_group",
    "_parse_tool_block",
    "_render_message_content",
    "is_authenticated",
    "resolve_agy_command",
    "resolve_model_and_effort",
]


def _force_rmtree(path: str) -> None:
    """Best-effort forced removal of a workspace; never raises (issue #4).

    ``shutil.rmtree(path, ignore_errors=True)`` alone is WEAKER than the
    ``tempfile._rmtree`` that ``TemporaryDirectory.cleanup()`` uses:
    the latter chmod-resets read-only subtrees and retries them, the
    former silently skips them, which is exactly the residual leak this
    issue is about (a read-only ``conversations`` tree surviving the
    forced pass on the Windows shape of the bug). So: forced removal
    first; if anything survives, grant the owner write (and, for
    directories, search) permission -- the POSIX analogue of "the handle
    is gone now, try again" -- and sweep once more.

    The permission sweep deliberately SKIPS links using ``os.lstat`` so
    nothing is ever followed:

    * ``setup_isolated_home`` links the user's REAL OAuth token into the
      isolated HOME with ``os.symlink``, ``os.link``, or a ``copy2``
      fallback (process.py). A surviving token link is precisely the
      residual-leak shape of #4, and following it would chmod the user's
      real 0400 token OUTSIDE the workspace to 0600 -- out-of-scope
      permission widening on user data, on the exact path this issue is
      about. The residual link is a documented lesser evil. The
      hardlink case (the ``os.link`` fallback, ``st_nlink > 1``) is
      skipped for the same reason -- that inode is the user's token.
      On Windows the skip is deliberate for an additional reason:
      DeleteFile fails with ACCESS_DENIED on a file carrying
      FILE_ATTRIBUTE_READONLY, unlike POSIX where unlinking needs only
      parent-directory write, so a readonly hardlinked file -- and its
      now-unneeded parent chain -- survives removal THERE. Clearing
      that attribute would clear it on the shared file record, i.e. on
      the user's real token: the same out-of-scope widening the skip
      exists to prevent. That Windows remnant is accepted on purpose;
      the field shape (agy writes tokens writable, and #4's actual
      conversations/*.db is nlink=1) is swept and removed normally.
      On macOS, ``setup_isolated_home`` also links the user's real
      ``~/Library/Keychains`` directory into the isolated HOME
      (``_link_macos_keychains``, process.py), so the symlink skip
      protects that keychain data as well.
    * Skipping is also sufficient: unlinking an entry needs write
      permission on its PARENT directory -- which the sweep grants via
      the walk's own directory chmods -- never on the entry's target.
    * ``os.walk`` never follows symlinked directories either
      (``followlinks=False``), so recursion cannot escape the workspace
      through a linked directory.

    Both rmtree passes ignore errors and every chmod is individually
    suppressed, so this function cannot raise.
    """
    shutil.rmtree(path, ignore_errors=True)
    if not os.path.exists(path):
        return
    for dirpath, dirnames, filenames in os.walk(path):
        for name in (*dirnames, *filenames):
            entry = os.path.join(dirpath, name)
            with contextlib.suppress(OSError):
                entry_stat = os.lstat(entry)
                is_link = stat.S_ISLNK(entry_stat.st_mode)
                is_dir = stat.S_ISDIR(entry_stat.st_mode)
                # The hardlink check is scoped to non-directories: an
                # ordinary directory already has st_nlink >= 2 (itself
                # plus one link per subdirectory), so skipping every
                # nlink > 1 entry would skip the whole directory tree --
                # the sweep would stop working for the read-only-tree
                # case it exists for. Only a FILE's inode can be shared
                # with the outside world (the token os.link fallback).
                if is_link or (not is_dir and entry_stat.st_nlink > 1):
                    continue
                os.chmod(
                    entry,
                    entry_stat.st_mode
                    | stat.S_IWUSR
                    | (stat.S_IXUSR if is_dir else 0),
                )
    shutil.rmtree(path, ignore_errors=True)


def _close_at_exit(client_ref: weakref.ReferenceType) -> None:
    """Interpreter-exit hook: close a client its host never closed.

    Exit-path gap reported in GitHub issue neerazz/hermes-antigravity-oauth#4:
    ``hermes chat --oneshot`` exits without closing the main provider
    client, so the only remaining cleanup was TemporaryDirectory's own
    weakref finalizer. That finalizer runs while the persistent agy worker
    is still alive and holding ``conversations/*.db``, fails with WinError
    32 on Windows, prints a traceback and leaves the workspace -- including
    the session's agy conversation record -- behind. Routing exit through
    ``close()`` reuses its load-bearing order: terminate every process
    first, then remove the workspace with retries.

    Ordering: atexit runs callbacks last-in-first-out, and weakref.finalize
    registers its own exit callback no later than the first finalizer
    (here: the client's TemporaryDirectory). The hook is registered after
    that, so it always runs before the TemporaryDirectory finalizer.

    The hook holds only a weak reference, so it never keeps a client
    alive, and ``close()`` unregisters it, so a closed client leaves no
    hook behind in a long-running host. A host that leaves via ``os._exit``
    skips atexit entirely; nothing in-process can clean up on that path.
    """
    client = client_ref()
    if client is None:
        return
    try:
        client.close()
    except Exception:
        # An exit hook must not print a traceback -- that is the symptom.
        logger.debug("Antigravity client close at interpreter exit failed", exc_info=True)


class AntigravityClient:
    """OpenAI-compatible client facade driving Antigravity CLI.

    Async compatibility boundary (issue #8)
    --------------------------------------
    Hermes drives this client from its async auxiliary path: with
    ``HERMES_SKIP_ASYNC_WRAP`` the client is used as-is ("already async-safe"),
    and the caller then does ``await client.chat.completions.create(...)``.
    Everything the facade returns is therefore awaitable-yielding-itself —
    ``stream._AwaitableCompletion`` for the non-streaming plan (which simply
    wraps the already-assembled response) and ``stream.AntigravityStream`` for
    the streaming plan (awaitable plus ``async for`` via ``__anext__``). Sync
    callers are untouched: the objects keep the same attributes and the same
    blocking behavior, so no existing caller needs to change.

    Known limitation (documented on purpose, not fixed here): the non-streaming
    ``create()`` executes the whole subprocess round-trip synchronously before
    it returns, so awaiting it still blocks the event loop for the request's
    duration. This is identical to the pre-existing sync behavior and to
    Hermes' own CopilotACPClient shim, and there is no plugin-side fix without
    a Hermes hook that would let ``create()`` hand back a coroutine instead of
    a finished value.
    """

    # Instruct Hermes not to wrap this client in wire transports or async adapters
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "antigravity-directsdk"
        self.base_url = base_url or AGY_MARKER_BASE_URL
        self._command = command or resolve_agy_command()
        self._args = list(args or ["--output-format", "stream-json", "--disable-slash-commands"])
        self._temp_dir = None
        if cwd:
            self._cwd = cwd
        else:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="hermes_agy_")
            self._cwd = self._temp_dir.name

        # Isolate agy state and session index from the user's real ~/.gemini/antigravity-cli
        self._isolated_home, self._isolated_gemini_dir = setup_isolated_home(self._cwd)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat_completion))
        self.is_closed = False
        self._active_processes: set[subprocess.Popen] = set()
        # RLock (NOT a plain Lock) deliberately -- issue #10.
        #
        # A suspended AntigravityStream._stream_generator forms a
        # reference cycle with the stream (stream._generator <-> generator
        # frame <-> stream), so an ABANDONED stream (consumer stopped
        # iterating without close()) is reclaimable only by the cyclic
        # GC. That pass fires at an arbitrary allocation -- possibly on
        # the SAME thread while it already holds this lock (client.close(),
        # _get_or_spawn_worker, _create_chat_completion, ...). The
        # GC-finalized generator's finally block then runs stream.close()
        # -> client._terminate_worker()/_update_worker_history() ->
        # `with self._lock` on a thread that already holds it: a plain
        # Lock self-deadlocks here (deterministic hangs were observed
        # while developing #9). RLock makes that same-thread re-entrance
        # succeed; cross-thread mutual exclusion is unchanged.
        #
        # Trade-off, documented on purpose: re-entrance can MASK a future
        # lock-ordering bug that would otherwise deadlock loudly -- a
        # code path that re-acquires _lock without expecting to already
        # hold it is now silently allowed instead of hanging. Mutating
        # shared state under _lock must therefore never call back into
        # the client (a nested _lock acquisition, a state-dependent
        # branch, another lock); the region is for flat
        # acquire -> mutate -> release only. Known, accepted exceptions
        # inside the regions below are the bounded-but-blocking process
        # calls (Popen spawn, proc.wait(timeout=2), Windows taskkill
        # without timeout): they touch no client state, and moving them
        # out of the locked region would trade the deadlock for races.
        self._lock = threading.RLock()
        self._worker_proc: subprocess.Popen | None = None
        self._worker_model: str | None = None
        self._worker_effort: str | None = None
        self._worker_history: list[dict[str, Any]] = []
        # Cumulative usage snapshot of the current worker session. agy 1.2.10+
        # persistent workers report cumulative session usage, so per-turn
        # deltas need this baseline. The reference is replaced on every spawn
        # and termination; the dict contents are advanced in place by the
        # owning session's stream. Each stream captures the dict of the
        # session it was created for, so a stream left over from a terminated
        # session cannot corrupt a later session's baseline.
        self._worker_usage_baseline: dict[str, int] = {}
        self._worker_lock = threading.Lock()
        # Registered last, once every attribute close() reads exists; see
        # _close_at_exit for why this cannot be left to the host.
        self._exit_hook = functools.partial(_close_at_exit, weakref.ref(self))
        atexit.register(self._exit_hook)

    @staticmethod
    def _resolve_real_token_path() -> Path | None:
        return resolve_real_token_path()

    def _resolve_model_and_effort(
        self,
        model: str | None,
        reasoning_effort: str | None = None,
    ) -> tuple[str, str | None]:
        return resolve_model_and_effort(model, reasoning_effort)

    def _child_env(self) -> dict[str, str]:
        return build_child_env(self._isolated_home)

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        terminate_process(proc)

    def __enter__(self) -> "AntigravityClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        atexit.unregister(self._exit_hook)
        # Phase 1 -- terminate every child process BEFORE the workspace is
        # touched (issue #4): on Windows a still-running agy keeps
        # conversations/*.db open, and taskkill /F /T is asynchronous, so
        # removing the workspace first would race the OS and fail with
        # WinError 32. Ordering is therefore load-bearing, not cosmetic.
        with self._lock:
            self.is_closed = True
            self._terminate_worker_locked()
            procs = tuple(self._active_processes)
            self._active_processes.clear()
        for proc in procs:
            self._terminate_process(proc)
        # Phase 2 -- remove the private workspace, retrying transient
        # failures. close() must never raise from cleanup and must never
        # leave the client half-closed (issue #4).
        self._remove_temp_dir()

    def _remove_temp_dir(self) -> None:
        """Remove the private workspace; cannot raise, cannot half-finish.

        Issue #4 (reported on Windows 11, agy 1.2.3): cleanup raised
        PermissionError [WinError 32] on conversations/*.db because the
        agy child still held the file after taskkill /F /T returned (that
        kill is asynchronous). The old code swallowed the error and gave
        up, leaking the whole workspace. Now: bounded retries with short
        backoff (see _TEMP_DIR_CLEANUP_ATTEMPTS/_BACKOFF constants above
        for the sizing), then a forced removal of whatever is unlocked.

        Live Windows verification by the reporter is still pending and
        will be requested on the issue after this merges; the retry budget
        is deliberately small so the worst case stays under ~0.5s and the
        common clean path pays nothing.

        The client is already fully closed when this runs (is_closed set,
        every process terminated in close() phase 1), and _temp_dir is
        cleared unconditionally, so a second close() is a no-op and a
        lingering workspace never blocks a fresh client on the same path.
        """
        temp_dir = self._temp_dir
        if temp_dir is None:
            return
        self._temp_dir = None
        try:
            self._cleanup_temp_dir_with_retries(temp_dir)
        except Exception as exc:
            # Defense in depth: a cleanup path must never break close().
            # Only the workspace path is logged; the isolated HOME inside
            # it holds a token link but no secret content of its own, and
            # the path itself is this client's own private directory.
            logger.debug(
                "Antigravity workspace %s cleanup raised unexpectedly: %s",
                temp_dir.name,
                exc,
            )

    def _cleanup_temp_dir_with_retries(self, temp_dir: tempfile.TemporaryDirectory) -> None:
        """Try cleanup up to _TEMP_DIR_CLEANUP_ATTEMPTS times, then force.

        The loop catches EVERY exception -- not just OSError -- because the
        goal is that nothing can escape into close(): OSError
        (PermissionError/WinError 32, EBUSY, ENOTEMPTY -- the "handle
        still held" symptoms) is retried with backoff, while any other
        exception breaks out immediately and lands in the forced removal
        below. Retrying a failed TemporaryDirectory.cleanup() is safe: its
        finalizer is detached on the first call, so each attempt is a
        fresh rmtree pass.
        """
        for attempt in range(1, _TEMP_DIR_CLEANUP_ATTEMPTS + 1):
            try:
                temp_dir.cleanup()
                return
            except Exception as exc:
                retryable = isinstance(exc, OSError) and attempt < _TEMP_DIR_CLEANUP_ATTEMPTS
                if retryable:
                    logger.debug(
                        "Antigravity workspace %s cleanup attempt %d/%d failed (%s); retrying.",
                        temp_dir.name,
                        attempt,
                        _TEMP_DIR_CLEANUP_ATTEMPTS,
                        exc,
                    )
                    time.sleep(_TEMP_DIR_CLEANUP_BACKOFF_SECONDS[attempt - 1])
                    continue
                logger.debug(
                    "Antigravity workspace %s cleanup failed on attempt %d/%d (%s: %s); "
                    "forcing removal of whatever is unlocked.",
                    temp_dir.name,
                    attempt,
                    _TEMP_DIR_CLEANUP_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                )
                break
        if temp_dir.name:
            _force_rmtree(temp_dir.name)

    def _terminate_worker_locked(self) -> None:
        if self._worker_proc is not None:
            proc = self._worker_proc
            self._worker_proc = None
            self._worker_model = None
            self._worker_effort = None
            self._worker_history = []
            self._active_processes.discard(proc)
            self._terminate_process(proc)
        # The session is gone: any later usage from it belongs to a dead
        # session, and the next spawn must start from a clean baseline.
        self._worker_usage_baseline = {}

    def _terminate_worker(self) -> None:
        with self._lock:
            self._terminate_worker_locked()

    def _update_worker_history(self, messages: list[dict[str, Any]] | None) -> None:
        with self._lock:
            self._worker_history = list(messages or [])

    def _sync_workspace_rules(self, messages: list[dict[str, Any]]) -> bool:
        """Write SOUL.md + Hermes system prompt to the workspace GEMINI.md agy loads as rules.

        Returns True when rules are in the workspace (prompt then omits them). A changed digest
        retires the running worker, because agy reads rules at session start.
        """
        if not _soul.rules_enabled():
            return False
        rules = _soul.build_rules(_soul.system_parts_of(messages, _render_message_content), _PROMPT_PREAMBLE)
        try:
            digest = _soul.write_rules(self._cwd, rules)
        except OSError as exc:
            logger.warning("Antigravity: could not write workspace rules (%s); inlining them", exc)
            return False
        with self._lock:
            changed = getattr(self, "_rules_digest", None) not in (None, digest)
            self._rules_digest = digest
        if changed:
            self._terminate_worker()
        return True

    def _get_or_spawn_worker(self, model: str, effort: str | None) -> subprocess.Popen:
        with self._lock:
            if (
                self._worker_proc is not None
                and self._worker_proc.poll() is None
                and self._worker_model == model
                and self._worker_effort == effort
            ):
                return self._worker_proc

            self._terminate_worker_locked()

            cmd_args = [self._command, "--input-format", "stream-json", *self._args]
            if model:
                cmd_args.extend(["--model", model])
            if effort:
                cmd_args.extend(["--effort", effort])

            proc = subprocess.Popen(
                cmd_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._cwd,
                env=self._child_env(),
                **_own_process_group(),
            )
            self._worker_proc = proc
            self._worker_model = model
            self._worker_effort = effort
            self._worker_history = []
            self._worker_usage_baseline = {}
            self._active_processes.add(proc)
            return proc

    def _run_oneshot_completion(
        self,
        *,
        model: str,
        effort: str | None,
        messages: list[dict[str, Any]],
        timeout: float,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        stream: bool,
    ) -> Any:
        rules = self._sync_workspace_rules(messages)
        prompt_text = _format_messages_as_prompt(
            messages, model=model, tools=tools, tool_choice=tool_choice, rules_in_workspace=rules
        )
        cmd_args = [self._command, *self._args]
        if model:
            cmd_args.extend(["--model", model])
        if effort:
            cmd_args.extend(["--effort", effort])

        proc = subprocess.Popen(
            cmd_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=self._cwd,
            env=self._child_env(),
            **_own_process_group(),
        )

        try:
            if proc.stdin:
                proc.stdin.write(prompt_text + "\n")
                proc.stdin.flush()
                proc.stdin.close()
        except OSError:
            pass

        with self._lock:
            self._active_processes.add(proc)

        stream_iter = AntigravityStream(
            proc=proc,
            client=self,
            model=model,
            timeout=timeout,
            tools=tools,
            is_worker=False,
        )
        if stream:
            return stream_iter
        return collect_stream_completion(stream_iter)

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        reasoning_effort: str | None = None,
        **extra_kwargs: Any,
    ) -> Any:
        if self.is_closed:
            raise RuntimeError("AntigravityClient is closed.")

        if not is_authenticated():
            raise RuntimeError(
                "Antigravity CLI is not authenticated. Please run 'agy' in your terminal "
                "to log in with your Google account."
            )

        effort_param = reasoning_effort or extra_kwargs.get("reasoning_effort")
        resolved_model, effort = self._resolve_model_and_effort(model, effort_param)
        messages_list = list(messages or [])
        effective_timeout = (
            float(timeout)
            if isinstance(timeout, (int, float)) and timeout > 0
            else _DEFAULT_TIMEOUT_SECONDS
        )

        worker_acquired = self._worker_lock.acquire(blocking=False)
        if worker_acquired:
            try:
                rules = self._sync_workspace_rules(messages_list)
                proc = self._get_or_spawn_worker(resolved_model, effort)
                with self._lock:
                    is_continuation = _messages_match_prefix(self._worker_history, messages_list)

                if is_continuation:
                    delta_msgs = messages_list[len(self._worker_history):]
                    prompt_payload = _format_delta_prompt(delta_msgs)
                else:
                    if self._worker_history:
                        self._terminate_worker()
                        proc = self._get_or_spawn_worker(resolved_model, effort)
                    prompt_payload = _format_messages_as_prompt(
                        messages_list, model=resolved_model, tools=tools, tool_choice=tool_choice,
                        rules_in_workspace=rules,
                    )

                event_msg = {"event": "user", "message": {"content": prompt_payload}}
                try:
                    proc.stdin.write(json.dumps(event_msg) + "\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._terminate_worker()
                    proc = self._get_or_spawn_worker(resolved_model, effort)
                    prompt_payload = _format_messages_as_prompt(
                        messages_list, model=resolved_model, tools=tools, tool_choice=tool_choice,
                        rules_in_workspace=rules,
                    )
                    event_msg = {"event": "user", "message": {"content": prompt_payload}}
                    proc.stdin.write(json.dumps(event_msg) + "\n")
                    proc.stdin.flush()

                with self._lock:
                    worker_usage_baseline = self._worker_usage_baseline
                stream_iter = AntigravityStream(
                    proc=proc,
                    client=self,
                    model=resolved_model,
                    timeout=effective_timeout,
                    tools=tools,
                    is_worker=True,
                    worker_lock=self._worker_lock,
                    worker_lock_held=worker_acquired,
                    messages=messages_list,
                    usage_baseline=worker_usage_baseline,
                )
                if stream:
                    return stream_iter
                try:
                    return collect_stream_completion(stream_iter)
                finally:
                    if self._worker_lock.locked():
                        self._worker_lock.release()
            except Exception:
                self._terminate_worker()
                if self._worker_lock.locked():
                    self._worker_lock.release()
                raise
        else:
            return self._run_oneshot_completion(
                model=resolved_model,
                effort=effort,
                messages=messages_list,
                timeout=effective_timeout,
                tools=tools,
                tool_choice=tool_choice,
                stream=stream,
            )
