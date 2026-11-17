"""Regression tests for fail-fast on a silent 429 / stuck turn.

Two failure shapes motivated these tests:

1. agy emits ``init`` roughly two seconds after spawn, *before* any model
   request. When the backend answers ``RESOURCE_EXHAUSTED (429)``, agy retries
   internally (8 attempts, ~140 s of exponential backoff) and writes the retry
   lines **only to its own log file**, never to stdout. The ``agy-quota-watchdog``
   thread deliberately stops on the first parsed JSON event — which is ``init``
   — because its closure keeps an abandoned stream from being GC-collectable
   (pinned by ``tests/test_client_lifecycle.py``). So the only place that could
   see the 429 was the stream's idle path, and it did not look. The turn ran to
   the client timeout and Hermes reported the useless
   ``Antigravity execution failed: no result event, process still alive``
   instead of the real cause.

2. A turn that never returns at all (no 429, no output) was unbounded: agy's
   ``--print-timeout`` defaults to ``0s``, meaning "wait until the turn
   completes".

Run standalone (``python tests/test_watchdog_fail_fast.py``) or with the
per-file loop documented in CONTRIBUTING.md.
"""

import io
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

import stream as stream_module
from stream import AntigravityStream

# The literal text agy writes to its isolated log when the backend 429s.
QUOTA_LINE = "Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Resource has been exhausted (e.g. check quota).), retrying in 4s"


class _ScriptedStdout:
    """Yields fixed lines, then blocks for ``block_seconds`` before EOF.

    Blocking (instead of returning EOF at once) is what keeps
    ``_stream_generator`` inside its read loop long enough for the watchdog to
    poll — the exact window the old code skipped.
    """

    def __init__(self, lines, block_seconds=0.0):
        self._lines = list(lines)
        self._deadline = None
        self._block_seconds = block_seconds

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._deadline is None:
            self._deadline = time.monotonic() + self._block_seconds
        # Block WITHOUT returning, exactly like agy when it is silent: stdout
        # stays open with no line, so the stream sees idle poll slices
        # (``_read_line`` -> None) and its inline 429 check runs.
        while time.monotonic() < self._deadline:
            time.sleep(0.05)
        return ""


class _FakeProc:
    def __init__(self, lines, block_seconds=0.0):
        self.stdout = _ScriptedStdout(lines, block_seconds)
        self.stderr = io.StringIO("")
        self.pid = 4242

    def poll(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("agy", timeout)

    def terminate(self):
        pass

    def kill(self):
        pass


class _FakeClient:
    _isolated_gemini_dir = "/tmp/hermes-agy-test"
    _worker_proc = None

    def __init__(self):
        self._lock = threading.RLock()
        self._active_processes = set()

    def _terminate_process(self, proc):
        pass

    def _terminate_worker(self):
        pass


def _init_line():
    return json.dumps({"event": "init", "init": {"model": "gemini-test"}}) + "\n"


def _result_line(**result):
    return json.dumps({"event": "result", "result": result}) + "\n"


class SilentQuotaFailFastTests(unittest.TestCase):
    """A silent 429 must surface as a quota error, not as a bare timeout."""

    def setUp(self):
        self._original = stream_module._check_early_quota_error
        self.calls = 0

    def tearDown(self):
        stream_module._check_early_quota_error = self._original

    def _fake_check(self, gemini_dir, min_mtime=None):
        self.calls += 1
        return QUOTA_LINE if self.calls >= 3 else None

    def test_silent_429_surfaces_as_quota_error_after_init(self):
        stream_module._check_early_quota_error = self._fake_check
        proc = _FakeProc([_init_line()], block_seconds=3.0)
        stream = AntigravityStream(
            proc=proc, client=_FakeClient(), model="gemini-test", timeout=15, is_worker=False
        )

        started = time.monotonic()
        with self.assertRaises(RuntimeError) as ctx:
            for _ in stream:
                pass

        message = str(ctx.exception)
        self.assertIn("RESOURCE_EXHAUSTED", message)
        self.assertNotIn("no result event", message)
        self.assertGreaterEqual(
            self.calls,
            2,
            "the stream never re-checked the agy log after init — the 429 would be missed",
        )
        self.assertLess(
            time.monotonic() - started,
            15,
            "watchdog did not fail fast; the turn ran to its timeout",
        )


class PrintTimeoutTests(unittest.TestCase):
    """``--print-timeout`` must bound a turn that never returns."""

    def test_print_timeout_result_is_reported_as_a_timeout_not_an_empty_result(self):
        proc = _FakeProc(
            [
                _init_line(),
                _result_line(
                    conversation_id="c1",
                    status="SUCCESS",
                    response="",
                    finished_reason="print_timeout",
                ),
            ],
            block_seconds=0.0,
        )
        stream = AntigravityStream(
            proc=proc, client=_FakeClient(), model="gemini-test", timeout=15, is_worker=False
        )

        with self.assertRaises(RuntimeError) as ctx:
            for _ in stream:
                pass

        message = str(ctx.exception)
        self.assertIn("print timeout", message)
        self.assertNotIn("empty result", message)


class PrintTimeoutArgTests(unittest.TestCase):
    """The ``--print-timeout`` argv is opt-out and unit-tolerant."""

    def setUp(self):
        from client import print_timeout_arg

        self.argv = print_timeout_arg
        self._original = __import__("os").environ.pop("ANTIGRAVITY_PRINT_TIMEOUT", None)

    def tearDown(self):
        import os

        os.environ.pop("ANTIGRAVITY_PRINT_TIMEOUT", None)
        if self._original is not None:
            os.environ["ANTIGRAVITY_PRINT_TIMEOUT"] = self._original

    def test_default_is_bounded(self):
        argv = self.argv()
        self.assertEqual(argv[0], "--print-timeout")
        self.assertTrue(argv[1].endswith("s"))

    def test_disabled_values(self):
        import os

        for value in ("0", "off", "none", "false", "no", ""):
            with self.subTest(value=value):
                os.environ["ANTIGRAVITY_PRINT_TIMEOUT"] = value
                self.assertEqual(self.argv(), [])

    def test_bare_seconds_get_a_unit(self):
        import os

        os.environ["ANTIGRAVITY_PRINT_TIMEOUT"] = "30"
        self.assertEqual(self.argv(), ["--print-timeout", "30s"])

    def test_explicit_unit_is_kept(self):
        import os

        os.environ["ANTIGRAVITY_PRINT_TIMEOUT"] = "2m"
        self.assertEqual(self.argv(), ["--print-timeout", "2m"])


if __name__ == "__main__":
    unittest.main()
