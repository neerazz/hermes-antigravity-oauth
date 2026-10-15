"""Streaming iterator and response assembly for Antigravity stream-json events."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Iterator, NamedTuple

try:
    from .process import _check_early_quota_error
    from .prompt import _longest_tool_call_prefix_match, _parse_tool_block
    from . import native_tools as _native_tools
except ImportError:
    from process import _check_early_quota_error
    from prompt import _longest_tool_call_prefix_match, _parse_tool_block
    import native_tools as _native_tools

logger = logging.getLogger(__name__)


class _UsageDeltas(NamedTuple):
    """Per-turn usage derived from cumulative worker-session counters."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int


def _counter_delta(usage_data: dict[str, Any], baseline: dict[str, int], field: str) -> int:
    """Per-turn delta of one cumulative usage counter reported by a worker.

    agy 1.2.10+ persistent workers report cumulative session usage, so each
    turn must subtract the snapshot captured after the previous turn. The
    baseline is updated in place with the latest snapshot. A missing field
    contributes 0 and leaves the baseline untouched, so an omitted optional
    field never resets unrelated counters. A value below the baseline means
    the CLI restarted its counters for this session: the current value is
    already this turn's usage and becomes the new baseline.

    Known limitations of the remaining handling (kept on purpose; no code
    change, no better signal exists in the delta alone):

    * A value below the baseline is a SUFFICIENT but not a definitive restart
      detector. A CLI restart whose counter already reports >= the old
      baseline reads as a small positive delta: the pre-restart usage is
      attributed to that (in fact empty) turn instead of restarting the
      accounting.
    * A counter first reported mid-session (no previous snapshot, e.g.
      cache_read_tokens appearing only once the prompt cache warms) is
      attributed in full to the current turn. How that session-to-date value
      splits across earlier turns is unknowable, so this turn over-reports.
    """
    if field not in usage_data:
        return 0
    value = int(usage_data[field] or 0)
    previous = baseline.get(field)
    if previous is None or value < previous:
        baseline[field] = value
        return value
    delta = value - previous
    baseline[field] = value
    return delta


def _delta_worker_usage(usage_data: dict[str, Any], baseline: dict[str, int]) -> _UsageDeltas:
    """Convert cumulative worker-session usage into per-turn deltas.

    The baseline dict belongs to the worker session that produced this usage
    and is updated in place. Callers must hand in the baseline captured when
    the worker stream was created, so a stream left over from a terminated
    session can never corrupt the baseline of the session that replaced it.

    total_tokens is expected to be stably present or absent for a session. If
    it flaps (present -> absent -> present), the absent turn falls back to
    input+output without advancing the total_tokens snapshot, so the next
    present turn's total delta spans two turns.
    """
    input_delta = _counter_delta(usage_data, baseline, "input_tokens")
    output_delta = _counter_delta(usage_data, baseline, "output_tokens")
    if "total_tokens" in usage_data:
        total_delta = _counter_delta(usage_data, baseline, "total_tokens")
    else:
        # total_tokens is optional: fall back to the per-turn input+output.
        total_delta = input_delta + output_delta
    return _UsageDeltas(
        input_tokens=input_delta,
        output_tokens=output_delta,
        total_tokens=total_delta,
        cache_read_tokens=_counter_delta(usage_data, baseline, "cache_read_tokens"),
    )


async def _ready(value: Any) -> Any:
    """Trivial coroutine that immediately returns *value*.

    The return channel of every ``__await__`` in this module: awaiting an
    already-completed result must produce that result without doing any work
    (no re-execution, no re-reading of the subprocess).
    """
    return value


class _AwaitableCompletion(SimpleNamespace):
    """A completed ChatCompletion that is also awaitable, yielding itself.

    Hermes' async auxiliary path awaits ``create()`` even for clients that
    declare ``HERMES_SKIP_ASYNC_WRAP`` ("already async-safe"): the plugin is
    expected to return something legal to ``await`` from its synchronous,
    already-finished round-trip. A plain ``SimpleNamespace`` is not awaitable,
    which surfaced as ``TypeError: object SimpleNamespace can't be used in
    'await' expression`` on vision/auxiliary calls (issue #8).

    The attribute shape is exactly ``SimpleNamespace``'s
    (``.id/.choices/.usage/.model``); only awaitability is added, so sync
    callers see byte-identical objects.
    """

    def __await__(self) -> Any:
        # Yield a coroutine that immediately returns self. Deliberately NOT
        # `iter([self])`: a Task receiving a non-Future yield crashes with
        # "Task got bad yield", so the yield must come from a real coroutine.
        return _ready(self).__await__()


class AntigravityStream(Iterator[Any]):
    """Streaming iterator yielding OpenAI ChatCompletionChunk objects from CLI stream-json."""

    response: Any = None  # Mock response attribute for Hermes Relay compatibility

    def __init__(
        self,
        *,
        proc: subprocess.Popen,
        client: Any,
        model: str,
        timeout: float,
        tools: list[dict[str, Any]] | None = None,
        is_worker: bool = False,
        worker_lock: threading.Lock | None = None,
        worker_lock_held: bool | None = None,
        messages: list[dict[str, Any]] | None = None,
        usage_baseline: dict[str, int] | None = None,
    ):
        # Loud pairing, not a defaulted bool (review nit): a future site
        # passing worker_lock WITHOUT worker_lock_held would silently never
        # release it -- a permanent _worker_lock leak that degrades every
        # later turn to the oneshot path with no error anywhere. An
        # explicit raise (not an assert: `python -O` strips asserts and
        # would restore the fail-silent default) keeps the pairing from
        # ever drifting apart again.
        if (worker_lock is None) != (worker_lock_held is None):
            raise ValueError(
                "worker_lock and worker_lock_held must be passed together: "
                "the stream of a turn that failed to acquire _worker_lock "
                "must not hold (or release) it, and the stream of a turn "
                "that acquired it must carry the ownership flag so its "
                "cleanup can release it."
            )
        self.proc = proc
        self.client = client
        self.model = model
        self.timeout = timeout
        self.has_tools = bool(tools)
        self.tool_names = _native_tools.tool_names(tools)
        self.is_worker = is_worker
        self.worker_lock = worker_lock
        self.messages = messages
        # Ownership record for the worker request lock. threading.Lock is
        # NOT owner-bound: locked() is true and release() succeeds from any
        # thread, so a lock()/locked() probe cannot tell "my lock" from
        # "someone else's". The client sets this True ONLY for the stream
        # built for the turn whose `_worker_lock.acquire(blocking=False)`
        # actually succeeded (worker streams of turns that fell back to
        # oneshot, and oneshot streams, never pass it). Every release goes
        # through _release_worker_lock_once, which consults this flag
        # instead of probing state it does not own.
        self._worker_lock_held: bool = bool(worker_lock_held and worker_lock is not None)
        # Cumulative usage snapshot of the worker session that owns this
        # stream; only worker streams receive one (see client._create_chat_completion).
        self.usage_baseline = usage_baseline
        self.conversation_id = ""
        self._closed = False
        self._interrupted = False
        self._finished = False
        self._early_error: str | None = None
        self._generator = self._stream_generator()
        # Private single-thread executor for the async path, created lazily on
        # the first __anext__ (sync consumers never create one, and a retired
        # reference is what keeps a stale submit loud). Retired -- shutdown
        # (wait=False, cancel_futures=False) -- by close() OR by exhaustion in
        # __next__, so no idle thread outlives either end of life. See
        # __anext__ for why this is not the shared default executor.
        self._async_executor: ThreadPoolExecutor | None = None

    def __iter__(self) -> "AntigravityStream":
        return self

    def __next__(self) -> Any:
        if self._closed:
            raise StopIteration
        try:
            return next(self._generator)
        except StopIteration:
            self._closed = True
            # Exhaustion is end of life for the stream even when the worker
            # lives on (the success path deliberately does not close()), so
            # the private async executor is retired here too; otherwise its
            # idle thread lingers until the stream object is collected. Covers
            # sync and async consumption alike, because __anext__ reaches this
            # point through _pull_chunk -> __next__.
            self._retire_executor()
            if not self.is_worker:
                self.close()
            raise
        except Exception:
            self.close()
            raise

    def _release_worker_lock_once(self) -> None:
        """Release the worker request lock exactly once, and only if ours.

        Ownership, not a locked() probe. ``threading.Lock`` is not
        owner-bound: ``locked()`` reports any holder's state and
        ``release()`` succeeds from any thread, so the old
        ``if self.worker_lock.locked(): release()`` probe could not tell
        "my lock" from "someone else's". A stream that finished normally
        releases here on its success path (clearing the flag), but if such
        a stream was abandoned WITHOUT close(), its generator is only
        finalized later by the cyclic GC -- at an arbitrary moment on an
        arbitrary thread, long after another turn may have acquired the
        lock. Probing then would release the LIVE turn's lock underneath
        its owner, letting a third request acquire it and write to the
        same worker concurrently: protocol corruption. The flag is set
        only for the stream of the turn that actually acquired the lock
        and is cleared on first release, so a finalizer racing its own
        owner's release (or running after it) is inert.
        """
        if self._worker_lock_held:
            self._worker_lock_held = False
            with contextlib.suppress(Exception):
                self.worker_lock.release()

    def _retire_executor(self) -> None:
        """Shut down the private async executor, if the async path created one.

        Called from close() and from the exhaustion path in __next__, so the
        discipline lives here once and both ends of life behave the same.

        wait=False is mandatory: retirement also runs ON the executor thread
        (the exhaustion path is reached from _pull_chunk, and the error path
        from close()), so joining would deadlock on the caller.
        cancel_futures=False: an in-flight _pull_chunk has already started and
        cannot be cancelled -- and it does not need to be, because the
        terminating process makes its readline() return EOF, so the worker
        thread finishes on its own. The reference is kept (not None'd) so a
        stale submit after retirement fails loudly instead of silently
        starting a new executor; __anext__ guards that with its _closed check
        first.
        """
        executor = self._async_executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=False)

    def __await__(self) -> Any:
        """Allow ``chunks = await create(stream=True)`` on the async wire.

        Same contract as :class:`_AwaitableCompletion`: the stream (and the
        subprocess round-trip behind it) is already fully constructed, so
        awaiting yields the stream itself without consuming any chunk;
        consumption goes through ``__aiter__``/``__anext__`` below.
        """
        return _ready(self).__await__()

    def __aiter__(self) -> "AntigravityStream":
        return self

    def _pull_chunk(self) -> tuple[bool, Any]:
        """Run one sync ``__next__``; ``(True, None)`` means the stream is done.

        End of stream must be reported as a *value*: StopIteration cannot be
        raised into a Future (asyncio and ``concurrent.futures`` reject it
        with "StopIteration interacts badly with generators"), so letting it
        escape the worker thread would wedge the awaiting coroutine forever
        instead of ending the ``async for``. Runs on this stream's private
        executor (see ``__anext__``), so a blocking readline occupies one
        private thread for at most the request timeout.
        """
        try:
            return False, self.__next__()
        except StopIteration:
            return True, None

    async def __anext__(self) -> Any:
        """One blocking subprocess read per await, off the event loop.

        Each ``next()`` runs on this stream's PRIVATE single-thread executor
        (created lazily on the first async pull; sync consumers never create
        one). The shared default executor is deliberately not used: one
        ``readline()`` can occupy its thread for the whole request timeout
        (300s by default), and starving every other ``run_in_executor(None,
        ...)`` user in the host loop for that long is not acceptable.

        The closed check comes FIRST, before the executor is touched: after
        ``close()`` or exhaustion the executor is retired, and submitting to a
        dead executor raises RuntimeError -- a closed stream is simply done,
        so ``StopAsyncIteration`` is the caller-visible outcome.

        The worker-side stop sentinel from ``_pull_chunk`` is mapped back to
        ``StopAsyncIteration`` so ``async for`` terminates exactly like the
        sync ``for`` loop; every other exception propagates unchanged, after
        the same ``close()`` the sync path performs.

        Known limitation (documented, deliberately not fixed): awaiting the
        stream object itself only yields the stream, so a consumer still
        drives the subprocess chunk by chunk from worker threads. Per-chunk,
        not per-request, non-blocking is the best a plugin can do without a
        Hermes hook that would hand out a coroutine from create().

        Cancelling an in-flight ``__anext__`` (e.g. a ``wait_for`` timeout)
        leaves the stream unclosed: the worker lock stays held, the worker
        process stays alive, and the private executor's worker stays blocked
        in ``readline()`` until something calls ``close()``. Hermes' own
        ``_aggregate_chat_stream_async`` supplies that ``close()`` on
        realistic paths -- its ``finally`` runs ``_close_chunk_stream(chunks,
        allow_aclose=True)``, which finds this class's sync ``close()`` --
        matching the sync behavior of abandoning a stream without calling
        ``close()``. No ``CancelledError`` handler is added here on purpose:
        reacting to cancellation would change the close/terminate semantics,
        which is out of scope for this fix. A stream neither consumed nor
        closed keeps its executor thread (non-daemon, like every
        ThreadPoolExecutor thread) blocked until the process exits; the same
        is true of an abandoned sync stream's subprocess, and closing the
        client terminates the child, unblocking the read.
        """
        if self._closed:
            raise StopAsyncIteration
        executor = self._async_executor
        if executor is None:
            executor = self._async_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="agy-stream"
            )
        stream_exhausted, chunk = await asyncio.get_running_loop().run_in_executor(
            executor, self._pull_chunk
        )
        if stream_exhausted:
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        """Async close for ``async with contextlib.aclosing(stream)`` consumers.

        Deliberately synchronous inside (``self.close()``). Reasons: Hermes'
        own ``_close_chunk_stream(chunks, allow_aclose=True)`` prefers the
        plain ``close`` attribute anyway, so this exists for direct-await
        consumers and ``aclosing``, not for the Hermes wire; close() is
        bounded (process terminate + ``wait(2)`` + kill fallback); routing it
        through the DEFAULT executor would re-introduce the shared-executor
        occupancy that ``__anext__`` avoids, and routing it through THIS
        stream's executor would deadlock when close() runs on that very
        thread (see ``close``).
        """
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.is_worker:
            # Terminate the worker only if it is still OURS. The interrupted
            # path runs at an arbitrary later time on an arbitrary thread --
            # close() called by a GC finalizer long after this stream was
            # abandoned -- and by then client._worker_proc may be a fresh
            # worker serving a later turn. Killing it would take down a
            # healthy live request; a stream whose worker was replaced must
            # leave the current one alone (issue #10 follow-up).
            if not self._finished and self.proc is self.client._worker_proc:
                self._interrupted = True
                self.client._terminate_worker()
            self._release_worker_lock_once()
        else:
            with self.client._lock:
                self.client._active_processes.discard(self.proc)
            self.client._terminate_process(self.proc)
        # Retire the private async executor, if the async path created one.
        # Same discipline as the exhaustion path in __next__ (see
        # _retire_executor); a second call is a harmless no-op because an
        # already-shut-down executor accepts shutdown() again.
        self._retire_executor()

    def _resolve_truncated(self, params: dict[str, Any], native: str) -> dict[str, Any] | None:
        """Full tool arguments from agy's transcript when the stream event only carried a preview."""
        from pathlib import Path
        dirs = [getattr(self.client, "_isolated_gemini_dir", None),
                Path.home() / ".gemini" / "antigravity-cli"]
        full = _native_tools.restore_truncated(params, native, self.conversation_id or "", [d for d in dirs if d])
        if full is None:
            logger.warning("Antigravity native tool '%s' arrived with truncated arguments and the full text "
                           "was not found in the transcript; not executing a partial command.", native)
        return full

    def _make_chunk(
        self,
        *,
        content: str | None = None,
        tool_calls: list[Any] | None = None,
        finish_reason: str | None = None,
    ) -> Any:
        delta = SimpleNamespace(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            reasoning=None,
            reasoning_content=None,
        )
        choice = SimpleNamespace(
            index=0,
            delta=delta,
            finish_reason=finish_reason,
        )
        return SimpleNamespace(
            id=self.conversation_id or f"agy-{int(time.time() * 1000)}",
            choices=[choice],
            model=self.model,
            usage=None,
        )

    def _usage_totals(self, usage_data: dict[str, Any]) -> tuple[int, int, int, int]:
        """(input, output, total, cached) token counts for the usage chunk.

        Persistent worker sessions report cumulative usage, so worker streams
        forward per-turn deltas against their session baseline. Oneshot
        processes already report per-turn usage and are forwarded raw; they
        never touch the worker baseline.
        """
        if self.is_worker and self.usage_baseline is not None:
            deltas = _delta_worker_usage(usage_data, self.usage_baseline)
            return (
                deltas.input_tokens,
                deltas.output_tokens,
                deltas.total_tokens,
                deltas.cache_read_tokens,
            )
        input_tokens = int(usage_data.get("input_tokens", 0) or 0)
        output_tokens = int(usage_data.get("output_tokens", 0) or 0)
        total_tokens = int(usage_data.get("total_tokens", input_tokens + output_tokens) or 0)
        cached_tokens = int(usage_data.get("cache_read_tokens", 0) or 0)
        return input_tokens, output_tokens, total_tokens, cached_tokens

    def _empty_result_message(
        self,
        *,
        saw_result: bool,
        status: str,
        process_exit: int | None,
        has_evidence: bool,
    ) -> str:
        """Detail for a turn that ended without proving success.

        One formatter for both branches so the oneshot and worker paths raise
        the same style of message: Hermes' retry/fallback logic treats these
        like the other transient ``Antigravity execution failed: ...``
        failures. ``process_exit`` is ``None`` when the process is still
        alive (a worker that outlived the broken turn).

        ``has_evidence`` (output already produced this turn) splits the
        concluded-turn case in two: with output, the status merely failed to
        be a success one ("unsuccessful"/"unconfirmed result" -- calling
        that "empty" would misreport a turn whose partial text was already
        delivered); without output the result genuinely carried nothing
        ("empty result", the wording the pre-existing tests pin).
        """
        if saw_result:
            if has_evidence:
                detail = (
                    f"unsuccessful result (status='{status}')"
                    if status
                    else "unconfirmed result (no status reported)"
                )
            elif status:
                detail = f"empty result (status='{status}')"
            else:
                detail = "empty result (no status reported)"
            if process_exit not in (None, 0):
                detail += f", process exited with return code {process_exit}"
            return detail
        if process_exit is None:
            return "no result event, process still alive"
        return f"no result event, process exited with return code {process_exit}"

    def _stream_generator(self) -> Iterator[Any]:
        deadline = time.monotonic() + self.timeout
        text_buffer = ""
        usage_data: dict[str, Any] = {}
        has_tool_calls = False
        has_content = False
        in_tool_call = False
        error_msg = ""
        status = ""
        # Positive evidence about how the stream ended: did a `result`
        # event conclude the turn, and did the run abort early because
        # agy attempted a native tool invocation (security neutralization)?
        saw_result = False
        neutralized_tool_step = False
        success = False
        start_time = time.monotonic()

        gemini_dir = getattr(self.client, "_isolated_gemini_dir", None)
        watchdog_stop = threading.Event()
        watchdog_thread: threading.Thread | None = None

        if gemini_dir:
            def _watchdog_loop() -> None:
                while not watchdog_stop.wait(timeout=0.3):
                    if time.monotonic() - start_time < 1.0:
                        continue
                    quota_err = _check_early_quota_error(gemini_dir, min_mtime=start_time)
                    if quota_err:
                        self._early_error = quota_err
                        logger.warning(
                            "Antigravity process hit early quota limit: %s; terminating to prevent hang.",
                            quota_err,
                        )
                        self.client._terminate_process(self.proc)
                        break

            watchdog_thread = threading.Thread(
                target=_watchdog_loop,
                name="agy-quota-watchdog",
                daemon=True,
            )
            watchdog_thread.start()

        try:
            while time.monotonic() < deadline:
                if self._early_error:
                    raise RuntimeError(f"Antigravity model error: {self._early_error}")

                line = self.proc.stdout.readline() if self.proc.stdout else ""
                if self._early_error:
                    raise RuntimeError(f"Antigravity model error: {self._early_error}")

                if not line:
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.01)
                    continue

                line = line.strip()
                if not line.startswith("{"):
                    continue

                try:
                    event = json.loads(line)
                except Exception:
                    continue

                watchdog_stop.set()

                event_type = event.get("event")
                if not self.conversation_id:
                    self.conversation_id = event.get("conversation_id", "")

                if event_type == "init":
                    init_data = event.get("init", {})
                    if not self.conversation_id:
                        self.conversation_id = init_data.get("conversation_id", "")

                elif event_type == "step_update":
                    step = event.get("step_update", {})
                    if not self.conversation_id:
                        self.conversation_id = step.get("conversation_id", "")
                    if step.get("step_type") == "tool":
                        call = _native_tools.translate(step, self.tool_names, index=0,
                                                       resolve=self._resolve_truncated) if not has_tool_calls else None
                        if call is not None:
                            logger.info("Antigravity native tool '%s' re-issued as Hermes tool '%s'.",
                                        step.get("tool_name"), call.function.name)
                            has_tool_calls = True
                            text_buffer = ""
                            yield self._make_chunk(tool_calls=[call])
                            self.close()
                            neutralized_tool_step = True
                            break
                        logger.warning(
                            "Antigravity attempted native tool invocation '%s'; neutralizing to prevent host execution.",
                            step.get("tool_name"),
                        )
                        self.close()
                        # Recorded so the post-loop positive-evidence check
                        # does not treat this security abort as an empty
                        # failed turn: the process is terminated right here
                        # on purpose, and the empty completion assembled
                        # afterwards is the pinned behavior (see
                        # test_native_tool_step_neutralization_in_stream).
                        neutralized_tool_step = True
                        break
                    if "usage" in step:
                        usage_data = step["usage"]

                    text_delta = step.get("text_delta")
                    if text_delta:
                        if not self.has_tools:
                            has_content = True
                            yield self._make_chunk(content=text_delta)
                        else:
                            text_buffer += text_delta
                            while text_buffer:
                                if in_tool_call:
                                    end_idx = text_buffer.find("</tool_call>")
                                    if end_idx != -1:
                                        full_end = end_idx + len("</tool_call>")
                                        block = text_buffer[:full_end]
                                        text_buffer = text_buffer[full_end:]
                                        in_tool_call = False
                                        parsed_calls, extra_text = _parse_tool_block(block)
                                        if parsed_calls:
                                            has_tool_calls = True
                                            for call_delta in parsed_calls:
                                                yield self._make_chunk(tool_calls=[call_delta])
                                        if extra_text and not has_tool_calls:
                                            has_content = True
                                            yield self._make_chunk(content=extra_text)
                                    else:
                                        break
                                else:
                                    idx = text_buffer.find("<tool_call")
                                    if idx != -1:
                                        if idx > 0 and not has_tool_calls:
                                            safe_text = text_buffer[:idx]
                                            has_content = True
                                            yield self._make_chunk(content=safe_text)
                                        text_buffer = text_buffer[idx:]
                                        in_tool_call = True
                                    else:
                                        k = _longest_tool_call_prefix_match(text_buffer)
                                        if k > 0:
                                            safe_text = text_buffer[:-k]
                                            if safe_text and not has_tool_calls:
                                                has_content = True
                                                yield self._make_chunk(content=safe_text)
                                            text_buffer = text_buffer[-k:]
                                            break
                                        else:
                                            if not has_tool_calls:
                                                has_content = True
                                                yield self._make_chunk(content=text_buffer)
                                            text_buffer = ""

                elif event_type == "result":
                    res = event.get("result", {})
                    if not self.conversation_id:
                        self.conversation_id = res.get("conversation_id", "")
                    status = res.get("status", "")
                    if "usage" in res:
                        usage_data = res["usage"]
                    if "error" in res:
                        error_msg = res["error"]
                    saw_result = True
                    # Fold a response that never streamed as deltas into the
                    # buffer so the post-loop flush emits it as content.
                    final_resp = res.get("response", "")
                    if final_resp and not has_content and not has_tool_calls and not text_buffer:
                        text_buffer = final_resp
                    # POSITIVE success evidence only: status SUCCESS *and* an
                    # answer that actually exists (streamed content, tool
                    # calls, buffered text, or a response field) *and* no
                    # error field. Absence of "ERROR" is NOT evidence: a
                    # persistent worker can emit a result with status
                    # CANCELLED, "" or an unknown value while carrying no
                    # response (observed live when agy's pubsub channel was
                    # killed mid-turn and the worker survived), and such a
                    # turn must fail the request instead of being recorded as
                    # an empty answer. Usage presence is deliberately NOT
                    # evidence: a legitimate turn (especially a worker's
                    # first) may report no usage at all.
                    #
                    # `not error_msg` is evaluated HERE, at the origin of the
                    # success verdict, and not only at the post-loop
                    # `if error_msg` raise: a turn that fails on its error
                    # field must NOT reach the finally's success branch,
                    # which would append the FAILED turn to the worker
                    # history (and skip the worker teardown the failure
                    # deserves). Fail-closed on the field's name alone --
                    # agy's contract for "error" on a SUCCESS result is
                    # unverified -- and it mirrors `if error_msg` exactly:
                    # the same falsy test, no extra validation invented.
                    success = (
                        status == "SUCCESS"
                        and not error_msg
                        and bool(has_content or has_tool_calls or text_buffer or final_resp)
                    )
                    break

            if text_buffer and not has_tool_calls:
                if self.has_tools or "<tool_call>" in text_buffer:
                    parsed_calls, extra_text = _parse_tool_block(text_buffer)
                    if parsed_calls:
                        has_tool_calls = True
                        for call_delta in parsed_calls:
                            yield self._make_chunk(tool_calls=[call_delta])
                    if extra_text and not has_tool_calls:
                        has_content = True
                        yield self._make_chunk(content=extra_text)
                    elif not parsed_calls:
                        has_content = True
                        yield self._make_chunk(content=text_buffer)
                else:
                    has_content = True
                    yield self._make_chunk(content=text_buffer)

            if not self.is_worker:
                try:
                    self.proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self.client._terminate_process(self.proc)

                stderr_out = self.proc.stderr.read() if self.proc.stderr else ""
                # Raw poll(), NOT `poll() or 0`: a process that is still
                # alive (None) must stay None. The old coercion fabricated
                # "exited 0" for it, and the empty-result message then named
                # a return code for a process that had never exited.
                # _empty_result_message already formats None as "process
                # still alive"; the guard below uses the same
                # `not in (None, 0)` test as the worker branch.
                returncode = self.proc.poll()

                # `not neutralized_tool_step`: a security neutralization a few
                # lines above (unmappable native tool step) deliberately
                # calls self.close(), which terminates THIS process on
                # purpose -- the resulting nonzero exit code is the expected
                # effect of that termination, not evidence of a crash. Without
                # this guard every neutralization was misreported as
                # "Antigravity execution failed: ... exited with return code
                # N", Hermes retried the turn up to 3 times (each retry
                # re-triggered the same neutralization), and the turn failed
                # with "didn't answer after 3 attempts" -- confirmed by
                # errors.log/agent.log correlation: 100% of these exit-code
                # failures are preceded within ~1s by the neutralization
                # warning (see repro_neutralization_crash.py, task
                # t_004f4153). Mirrors the guard already on the "empty
                # result" raise below (`if not neutralized_tool_step and not
                # success:`), which this one was missing.

                if self._early_error:
                    raise RuntimeError(f"Antigravity model error: {self._early_error}")

                # A result carrying an "error" field fails the turn whatever
                # its status says (status was not the only failure channel:
                # agy can report a killed pubsub channel with a non-ERROR
                # status), and a terminal ERROR status fails it with no field
                # at all -- the pre-PR `if status == "ERROR"` route, restored
                # for its user-visible wording (a bare
                # "Antigravity model error: "). Wording, not routing: that
                # message and the generic empty-result raise below classify
                # identically for Hermes (FailoverReason.unknown, retryable,
                # no fallback), so the user-visible text is what is pinned.
                # Checked before the quota and exit-code raises below, where
                # the pre-PR status check sat. Fail-closed on the field's
                # name alone -- agy's contract for a non-empty "error" on a
                # SUCCESS result is unverified -- and mirrored in the success
                # verdict above, so this failed turn can never reach the
                # worker-history update.
                if error_msg or status == "ERROR":
                    raise RuntimeError(f"Antigravity model error: {error_msg}")

                # `not in (None, 0)`, not `!= 0`: with the raw poll() above,
                # None means "still alive", and an alive process that
                # produced nothing is the empty-result raise's job below,
                # not this exit-code one. A real nonzero exit keeps the
                # quota/exit-code raise's priority over the generic one.
                if not neutralized_tool_step and not has_tool_calls and not has_content and returncode not in (None, 0):
                    quota_err = _check_early_quota_error(gemini_dir, min_mtime=start_time)
                    if quota_err:
                        raise RuntimeError(f"Antigravity model error: {quota_err}")
                    err_detail = error_msg or stderr_out.strip() or f"Process exited with return code {returncode}"
                    raise RuntimeError(f"Antigravity execution failed: {err_detail}")

                # Precedence: this positive-evidence raise comes AFTER the
                # quota and exit-code checks above because those are the more
                # specific failure signals. The old code fell through here
                # when returncode == 0 and no result event had been seen,
                # emitting a finish chunk and Hermes then recorded an empty
                # assistant turn as a legitimate response.
                #
                # Success is now REQUIRED to emit finish/usage, the security
                # neutralization above being the only exception (pinned by
                # test_native_tool_step_neutralization_in_stream): a partial
                # answer with no result event used to slip through on the
                # strength of its own deltas (saw_result False, has_content
                # True) and was delivered as a complete stop turn while the
                # broken worker lived on. No result event is never a
                # legitimate conclusion, and the deltas already yielded
                # cannot be retracted -- Hermes aggregates a stream that
                # raises mid-way, so failing the turn is the only honest
                # outcome.
                if not neutralized_tool_step and not success:
                    raise RuntimeError(
                        "Antigravity execution failed: "
                        + self._empty_result_message(
                            saw_result=saw_result,
                            status=status,
                            process_exit=returncode,
                            has_evidence=has_content or has_tool_calls,
                        )
                    )
            else:
                if self._early_error:
                    raise RuntimeError(f"Antigravity model error: {self._early_error}")

                # A result carrying an "error" field fails the turn whatever
                # its status says. This is the incident shape: agy's pubsub
                # channel died mid-turn, the worker survived (poll() None),
                # and the result event carried neither a response nor an
                # "ERROR" status. The terminal ERROR status still fails the
                # turn with no field at all -- the pre-PR
                # `if status == "ERROR"` route, restored for its user-visible
                # wording (bare "Antigravity model error: "); wording, not
                # routing, because both messages classify identically for
                # Hermes (FailoverReason.unknown, retryable, no fallback).
                # Either way success is False (the verdict demands SUCCESS),
                # so this failed turn can never reach the worker-history
                # update; the deltas already yielded cannot be retracted --
                # Hermes aggregates a stream that raises mid-way.
                if error_msg or status == "ERROR":
                    raise RuntimeError(f"Antigravity model error: {error_msg}")

                worker_exit = self.proc.poll()
                # `not neutralized_tool_step`: see the matching comment on the
                # oneshot-path exit-code check above -- a deliberate
                # self.close() from a security neutralization must not be
                # reported as this worker crashing. This was the unguarded
                # twin of the (correctly guarded) "empty result" raise below
                # and is the confirmed root cause of the antigravity-oauth
                # fast-crash ("worker process exited with return code 1" /
                # "didn't answer after 3 attempts"), task t_004f4153.
                if not neutralized_tool_step and not has_tool_calls and not has_content and worker_exit not in (None, 0):
                    quota_err = _check_early_quota_error(gemini_dir, min_mtime=start_time)
                    if quota_err:
                        raise RuntimeError(f"Antigravity model error: {quota_err}")
                    # Surface whatever agy wrote to stderr before this crash:
                    # without it, Hermes could not tell "agy answered
                    # nothing" from "agy printed its own error and this
                    # raise threw it away" (task t_004f4153 finding -- this
                    # message used to carry zero diagnostic detail, unlike
                    # the oneshot path's equivalent check above, which
                    # already reads stderr on its own exit-code raise). The
                    # worker is dead at this point (worker_exit is not
                    # None), so draining its stderr pipe here is a bounded,
                    # one-time read, not a hot-path cost.
                    worker_stderr = (self.proc.stderr.read() if self.proc.stderr else "").strip()
                    detail = f"worker process exited with return code {worker_exit}"
                    if worker_stderr:
                        detail += f"; stderr: {worker_stderr}"
                    raise RuntimeError(f"Antigravity execution failed: {detail}")

                # Positive-evidence verdict, placed AFTER the quota and
                # exit-code checks (the more specific signals) and BEFORE the
                # usage/baseline finalization below. The old code fell
                # through here whenever the worker was alive (poll() None) or
                # had exited 0 with a non-ERROR status, so an empty failed
                # turn was emitted as a legitimate response AND (success was
                # True) appended to the worker history.
                #
                # Baseline (usage accounting) is deliberately NOT advanced on
                # this raise path: the turn failed, but agy did spend tokens.
                # Advancing it here would change no outcome -- the usage
                # chunk is never yielded on a raised turn, so the spent
                # tokens reach no consumer either way -- and NOT advancing is
                # provably safe: the finally below closes this worker
                # (success is False), and client._terminate_worker_locked /
                # _get_or_spawn_worker reset _worker_usage_baseline on
                # termination and on respawn, so the stale snapshot is
                # discarded together with the dead session instead of
                # poisoning the respawned worker's deltas. Oneshot streams
                # carry no baseline at all.
                #
                # Success is now REQUIRED to emit finish/usage, the security
                # neutralization above being the only exception (pinned by
                # test_native_tool_step_neutralization_in_stream): partial
                # deltas with no result event used to slip through on the
                # strength of their own content (saw_result False,
                # has_content True), sealing the turn as a complete stop AND
                # leaving the broken worker alive because _finished was
                # already True when close() ran. No result event is never a
                # legitimate conclusion, and the deltas already yielded
                # cannot be retracted -- Hermes aggregates a stream that
                # raises mid-way -- so the turn fails, and the finally's
                # close() terminates the worker that broke it.
                if not neutralized_tool_step and not success:
                    raise RuntimeError(
                        "Antigravity execution failed: "
                        + self._empty_result_message(
                            saw_result=saw_result,
                            status=status,
                            process_exit=worker_exit,
                            has_evidence=has_content or has_tool_calls,
                        )
                    )

            finish_reason = "tool_calls" if has_tool_calls else "stop"

            # Resolve usage (advancing the cumulative worker baseline) BEFORE
            # emitting the finish-reason chunk. The common client pattern
            # `for chunk in stream: if finish_reason: break` abandons the
            # stream right here without calling close(), finalizing this
            # generator at this yield: the usage chunk below is then never
            # received, but the turn's tokens were already spent, so the
            # baseline must not wait for that chunk or the next turn's delta
            # absorbs them. There is no yield between this computation and
            # the finish-reason yield, so chunk emission order is unchanged.
            input_tokens, output_tokens, total_tokens, cached_tokens = self._usage_totals(usage_data)

            yield self._make_chunk(finish_reason=finish_reason)

            usage = SimpleNamespace(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=total_tokens,
                prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
            )
            yield SimpleNamespace(
                id=self.conversation_id or f"agy-{int(time.time() * 1000)}",
                choices=[],
                model=self.model,
                usage=usage,
            )
            self._finished = True
        except Exception:
            # A turn that already set success=True at its result event can
            # still fail afterwards: _early_error surfacing in the post-loop
            # (the watchdog race), a quota/exit-code raise, or _usage_totals
            # raising. Left as-is, the finally below would take the success
            # branch for such a turn: appending the FAILED exchange to the
            # worker history and releasing the worker request lock while the
            # worker is still alive -- close() only runs afterwards, from
            # __next__, once the exception propagates. Resetting the verdict
            # here funnels every failure through close(), which terminates
            # the worker and only then releases the lock.
            #
            # Exception, not BaseException: GeneratorExit (a consumer
            # abandoning the stream after the finish chunk, or the GC
            # finalizer) is deliberately NOT reset -- the pinned success
            # semantics of an abandoned successful stream (worker survives,
            # history recorded) must survive this handler.
            success = False
            raise
        finally:
            watchdog_stop.set()
            if watchdog_thread and watchdog_thread.is_alive():
                watchdog_thread.join(timeout=0.2)
            if self.is_worker:
                if success and not self._interrupted:
                    self.client._update_worker_history(self.messages)
                    self._release_worker_lock_once()
                else:
                    self.close()
            else:
                self.close()


def collect_stream_completion(stream: AntigravityStream) -> Any:
    """Consume an AntigravityStream completely and assemble a non-streaming ChatCompletion object.

    The returned object is a :class:`_AwaitableCompletion`: identical in shape
    to the plain ``SimpleNamespace`` this used to return, plus awaitable
    (``await`` yields the very same object) so the async wire can
    ``await client.chat.completions.create(...)`` without a TypeError.
    """
    conversation_id = ""
    model = stream.model
    content_parts: list[str] = []
    tool_calls: list[Any] = []
    finish_reason: str | None = None
    usage: Any = None

    for chunk in stream:
        if not conversation_id and getattr(chunk, "id", None):
            conversation_id = chunk.id
        if getattr(chunk, "model", None):
            model = chunk.model
        if getattr(chunk, "usage", None):
            usage = chunk.usage

        for choice in getattr(chunk, "choices", []):
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = getattr(choice, "delta", None)
            if delta:
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        tool_calls.append(
                            SimpleNamespace(
                                id=getattr(tc, "id", "call_1"),
                                type="function",
                                function=SimpleNamespace(
                                    name=getattr(tc.function, "name", ""),
                                    arguments=getattr(tc.function, "arguments", "{}"),
                                ),
                            )
                        )

    full_content = "".join(content_parts).strip() or None

    # Fallback tool call extraction if tools weren't pre-configured in stream
    if not tool_calls and full_content and "<tool_call>" in full_content:
        extracted_calls, cleaned = _parse_tool_block(full_content)
        if extracted_calls:
            tool_calls = extracted_calls
            full_content = cleaned or None
            finish_reason = "tool_calls"

    message = SimpleNamespace(
        role="assistant",
        content=full_content,
        tool_calls=tool_calls if tool_calls else None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    choice = SimpleNamespace(
        index=0,
        message=message,
        finish_reason=finish_reason or ("tool_calls" if tool_calls else "stop"),
    )
    if not usage:
        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )

    return _AwaitableCompletion(
        id=conversation_id or f"agy-{int(time.time() * 1000)}",
        choices=[choice],
        usage=usage,
        model=model,
    )
