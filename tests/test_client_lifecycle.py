"""Regression tests for client lifecycle robustness (issues #10 and #4).

Two pre-existing hazards pinned here:

Issue #10 -- cyclic-GC self-deadlock on client._lock
    A suspended ``AntigravityStream._stream_generator`` forms a reference
    cycle with the stream (``stream._generator`` <-> frame <-> stream), so
    an ABANDONED stream (consumer stopped iterating without ``close()``) is
    reclaimable only by the cyclic GC, whose pass can fire while the SAME
    thread already holds the non-reentrant ``client._lock`` (e.g. inside
    ``client.close()``). The GC-finalized generator's ``finally`` then runs
    ``stream.close()`` -> ``client._terminate_worker()`` or the worker
    success path's ``client._update_worker_history()``, both of which do
    ``with self._lock`` on the thread that already holds it. Fix: an RLock
    (see client.AntigravityClient.__init__). The tests below reproduce the
    hazard deterministically -- a helper thread holds ``_lock`` and runs
    ``gc.collect()``, so the finalizer re-enters the lock on that same
    thread -- and guard every wait with a watchdog timeout so a regression
    FAILS the test instead of hanging pytest.

Issue #4 -- Windows temp-dir cleanup PermissionError (WinError 32)
    On Windows, ``taskkill /F /T`` is asynchronous, so a freshly killed agy
    child can still hold ``conversations/*.db`` when
    ``TemporaryDirectory.cleanup()`` runs. Fix: bounded retries with short
    backoff plus a forced-removal fallback in ``_remove_temp_dir``. All
    tests are mocked; live Windows verification by the reporter is pending
    on the issue.

Deliberate difference from the other suites: the clients here are NOT
registered for ``addCleanup(client.close)``. In the bug-present case the
deadlocked helper thread holds ``client._lock`` forever, so calling
``close()`` from a cleanup hook would block pytest forever; the tests
close the client explicitly on the success path only, and the per-test
TemporaryDirectory cleanup still removes the workspace.
"""

import contextlib
import gc
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import warnings
import weakref
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add plugin parent dir to sys.path
plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

from client import (
    _TEMP_DIR_CLEANUP_ATTEMPTS,
    _TEMP_DIR_CLEANUP_BACKOFF_SECONDS,
    _force_rmtree,
    AntigravityClient,
)
from stream import AntigravityStream

MODEL = "gemini-3.8-flash-high"
TURN_MESSAGES = [{"role": "user", "content": "hello agy"}]

# Watchdog budget for the gc-under-lock reproduction of issue #10. A
# deadlocked finalizer blocks the collecting thread indefinitely, so the
# join must bail out and let the test fail; 5s is far more than a healthy
# gc.collect() on this payload takes (milliseconds) yet far less than the
# pytest run budget.
_GC_WATCHDOG_TIMEOUT_SECONDS = 5.0

# Bounded retries of the hazard scenario. Two verified mechanisms can
# leave a scenario's generator unfinalized on its FIRST attempt in a
# fresh process, which is why the scenario is retried until the expected
# finalization is observed:
#   (i) the stream's agy-quota-watchdog daemon holds a loop closure over
#       the stream, so the abandoned cycle stays reachable until that
#       thread exits -- _await_quota_watchdog_exit is load-bearing for
#       exactly this reason;
#   (ii) CPython's nested-collect guard: a gc.collect() that runs while
#        another thread's collection is in progress returns 0 immediately
#        without collecting (verified: under the plain-Lock mutation the
#        success-path scenario failed via "finalizer never ran" rather
#        than the watchdog for this reason).
# The retry cannot mask the deadlock under test: a hung finalizer never
# returns from collect, so _collect_gc_under_lock returns False and
# self.fail() raises out of _run_hazard_scenario, and the retry loop is
# never entered.
_HAZARD_SCENARIO_ATTEMPTS = 3

# Budget for the stream's quota-watchdog thread to exit once its stop
# event is set (it polls the event every 0.3s), see
# CyclicGcLockReentranceTests._await_quota_watchdog_exit.
_QUOTA_WATCHDOG_EXIT_TIMEOUT_SECONDS = 2.0


def _worker_turn_lines(conversation_id: str, text: str, usage: dict | None = None) -> list[str]:
    """stream-json lines for one successful persistent-worker turn."""
    events = [
        {"event": "init", "conversation_id": conversation_id},
        {"event": "step_update", "step_update": {"text_delta": text}},
        {
            "event": "result",
            "result": {"status": "SUCCESS", "response": text, "usage": usage or {}},
        },
    ]
    return [json.dumps(event) + "\n" for event in events] + [""]


def _mock_proc(lines: list[str], *, alive: bool = True) -> MagicMock:
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stderr = io.StringIO("")
    proc.poll.return_value = None if alive else 0
    proc.wait.return_value = 0
    proc.stdout.readline.side_effect = lines
    return proc


def _pin_host_dependent_seams(test: unittest.TestCase) -> None:
    """Pin every host-dependent seam, same rationale as the main suite."""
    patchers = (
        patch("client.is_authenticated", return_value=True),
        patch("process.resolve_real_token_path", return_value=None),
        patch("client.resolve_agy_command", return_value="agy"),
        # like the token/auth seams: isolated homes must not link real keychains
        patch("process._link_macos_keychains"),
    )
    for patcher in patchers:
        patcher.start()
        test.addCleanup(patcher.stop)


class CyclicGcLockReentranceTests(unittest.TestCase):
    """Issue #10: a GC-finalized abandoned stream must never self-deadlock.

    Semantics pinned (unchanged by the fix): finalizing an abandoned
    mid-turn stream takes the interrupted path -- the worker is terminated,
    exactly as an explicit ``stream.close()`` would -- while finalizing one
    abandoned after a successful turn keeps the worker alive and records the
    history via ``_update_worker_history``. What must NOT happen is the
    finalizer blocking forever on ``client._lock``.
    """

    def setUp(self) -> None:
        _pin_host_dependent_seams(self)

    def _client(self) -> AntigravityClient:
        # Explicit cwd: close() never removes it (the TemporaryDirectory
        # does, LIFO after this body). No addCleanup(client.close) -- see
        # the module docstring for why that would be unsafe here.
        temp_dir = tempfile.TemporaryDirectory(prefix="hermes_agy_test_")
        self.addCleanup(temp_dir.cleanup)
        return AntigravityClient(cwd=temp_dir.name)

    def _await_quota_watchdog_exit(self) -> None:
        """Wait for the stream's quota watchdog thread to finish.

        ``_stream_generator`` starts an ``agy-quota-watchdog`` daemon whose
        loop closure references the stream, so an abandoned stream only
        becomes cyclic-GC-collectable once that thread exits. The exit
        itself is deterministic: the first parsed stream-json event sets the
        stop event, and the loop polls it every 0.3s, so the
        post-abandonment steady state arrives within ~0.3s. Joining it here
        removes that race from the reproduction instead of hoping a
        ``time.sleep`` was long enough.
        """
        for thread in threading.enumerate():
            if thread.name == "agy-quota-watchdog":
                thread.join(timeout=_QUOTA_WATCHDOG_EXIT_TIMEOUT_SECONDS)
                self.assertFalse(
                    thread.is_alive(),
                    "agy-quota-watchdog thread did not exit; the abandoned "
                    "stream cannot be reclaimed deterministically.",
                )

    def _collect_gc_under_lock(self, client: AntigravityClient) -> bool:
        """One watchdog-protected hazard attempt.

        A helper thread acquires ``client._lock`` and runs ``gc.collect()``
        while holding it, so the cyclic collector finalizes the abandoned
        stream generator ON THAT SAME THREAD -- whose ``finally`` re-acquires
        ``client._lock`` (interrupted path: ``stream.close()`` ->
        ``_terminate_worker``; success path: ``_update_worker_history``). A
        plain Lock self-deadlocks there, an RLock does not.

        Returns True when the collection finished inside the watchdog
        budget, False when it hung. The thread is a daemon, so a hung run
        can never block pytest teardown; the caller must not touch
        ``client._lock`` after False.
        """
        finished = threading.Event()

        def _collect_under_lock() -> None:
            with client._lock:
                gc.collect()
            finished.set()

        collector = threading.Thread(
            target=_collect_under_lock, name="agy-gc-hazard", daemon=True
        )
        collector.start()
        collector.join(timeout=_GC_WATCHDOG_TIMEOUT_SECONDS)
        return finished.is_set()

    def _run_hazard_scenario(self, *, stop_at_finish_chunk: bool):
        """One full hazard iteration.

        Create a worker stream, consume part of its turn, abandon it
        WITHOUT ``close()``, then cyclically collect while the helper
        thread holds ``client._lock``.

        Returns ``(client, proc, finalizer_observed)``. On a watchdog trip
        (the deadlock) this fails loudly instead of returning: the finalizer
        hung holding ``client._lock``, so the client is intentionally left
        untouched (retrying cannot clear a deadlock).

        Automatic collection is disabled around the whole scenario so the
        ONLY collection is the explicit, watchdog-guarded one; otherwise an
        arbitrary gen-2 pass could finalize the generator on the main thread
        (with no lock held, so no deadlock) and the test would pass
        vacuously.
        """
        client = self._client()
        if stop_at_finish_chunk:
            proc = _mock_proc(
                _worker_turn_lines(
                    "conv-1", "answer one", usage={"input_tokens": 100, "output_tokens": 10}
                )
            )
        else:
            proc = _mock_proc(_worker_turn_lines("conv-1", "answer one"))

        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            with patch("subprocess.Popen", return_value=proc):
                stream = client.chat.completions.create(
                    model=MODEL, messages=list(TURN_MESSAGES), stream=True
                )
                if not stop_at_finish_chunk:
                    first_chunk = next(stream)
                    self.assertEqual(first_chunk.choices[0].delta.content, "answer one")
                    del first_chunk
                else:
                    # The common OpenAI consumer pattern `if finish_reason:
                    # break`: the trailing usage chunk is never consumed and
                    # the generator stays suspended with success=True.
                    finish_chunk = None
                    for _ in range(10):
                        chunk = next(stream)
                        if any(
                            getattr(choice, "finish_reason", None)
                            for choice in getattr(chunk, "choices", [])
                        ):
                            finish_chunk = chunk
                            break
                        del chunk
                    self.assertIsNotNone(
                        finish_chunk, "stream never emitted a finish_reason chunk"
                    )
                    self.assertIsNone(getattr(finish_chunk, "usage", None))
                    del finish_chunk
                self._await_quota_watchdog_exit()
                # Abandon WITHOUT close(), exactly like a consumer that moved
                # on. The only reference left is the cycle
                # stream._generator <-> frame <-> stream, which only the
                # cyclic collector can reclaim.
                del stream
                collected = self._collect_gc_under_lock(client)
        finally:
            if gc_was_enabled:
                gc.enable()

        if not collected:
            if stop_at_finish_chunk:
                blocked_on = "_update_worker_history (success path)"
            else:
                blocked_on = "stream.close -> _terminate_worker (interrupted path)"
            self.fail(
                "cyclic GC self-deadlock on client._lock (issue #10): "
                "gc.collect() run while the collecting thread already held "
                f"client._lock did not finish within {_GC_WATCHDOG_TIMEOUT_SECONDS}s "
                f"-- the abandoned stream's finalizer blocks re-acquiring "
                f"client._lock ({blocked_on}). The client is intentionally "
                "left untouched; this run leaks only its mock worker and "
                "per-test temp dir."
            )

        if stop_at_finish_chunk:
            finalizer_observed = client._worker_history == TURN_MESSAGES
        else:
            finalizer_observed = client._worker_proc is None
        return client, proc, finalizer_observed

    def test_gc_finalized_abandoned_midturn_stream_does_not_self_deadlock(self):
        client = proc = None
        finalizer_observed = False
        previous_client = None
        for _ in range(_HAZARD_SCENARIO_ATTEMPTS):
            # S4: an attempt whose finalizer was not observed left its
            # (mock) worker running; close it before the next attempt so
            # intermediate retries do not leak live workers.
            if previous_client is not None:
                previous_client.close()
            client, proc, finalizer_observed = self._run_hazard_scenario(
                stop_at_finish_chunk=False
            )
            previous_client = client
            if finalizer_observed:
                break
        # A scenario whose finalizer was never observed proves nothing about
        # the deadlock (see _HAZARD_SCENARIO_ATTEMPTS for why retries exist).
        self.assertTrue(
            finalizer_observed,
            "the abandoned mid-turn stream's finalizer never ran, so this "
            "test proves nothing about the client._lock deadlock",
        )

        # The finalizer took the interrupted path (turn never finished), so
        # it terminated the worker. Nothing else in this test terminates
        # it, which proves the finalizer really ran inside the helper's
        # gc.collect() -- the assertion is what makes this test meaningful.
        self.assertIsNone(client._worker_proc)
        self.assertFalse(client.is_closed)
        self.assertFalse(client._worker_lock.locked())

        acquired = client._lock.acquire(timeout=_GC_WATCHDOG_TIMEOUT_SECONDS)
        self.assertTrue(acquired, "client._lock was not released after the GC pass")
        client._lock.release()

        # The client remains usable: the next turn respawns a worker.
        respawn_proc = _mock_proc(_worker_turn_lines("conv-2", "answer two"))
        with patch("subprocess.Popen", return_value=respawn_proc):
            res = client.chat.completions.create(
                model=MODEL, messages=list(TURN_MESSAGES), stream=False
            )
        self.assertEqual(res.choices[0].message.content, "answer two")
        self.assertIs(client._worker_proc, respawn_proc)
        client.close()

    def test_gc_finalized_abandoned_successful_stream_does_not_self_deadlock(self):
        client = proc = None
        finalizer_observed = False
        previous_client = None
        for _ in range(_HAZARD_SCENARIO_ATTEMPTS):
            # S4: see the mid-turn test -- close the previous attempt's
            # client so an unobserved retry does not leak its worker.
            if previous_client is not None:
                previous_client.close()
            client, proc, finalizer_observed = self._run_hazard_scenario(
                stop_at_finish_chunk=True
            )
            previous_client = client
            if finalizer_observed:
                break
        self.assertTrue(
            finalizer_observed,
            "the abandoned successful stream's finalizer never ran, so this "
            "test proves nothing about the client._lock deadlock",
        )

        # Success semantics kept by the finalizer: the worker survives and
        # the turn is recorded, and the baseline advanced before the
        # finish_reason chunk was even emitted.
        self.assertIs(client._worker_proc, proc)
        self.assertEqual(client._worker_history, TURN_MESSAGES)
        self.assertEqual(client._worker_usage_baseline, {"input_tokens": 100, "output_tokens": 10})
        self.assertFalse(client.is_closed)
        self.assertFalse(client._worker_lock.locked())

        acquired = client._lock.acquire(timeout=_GC_WATCHDOG_TIMEOUT_SECONDS)
        self.assertTrue(acquired, "client._lock was not released after the GC pass")
        client._lock.release()

        client.close()


class WorkspaceCleanupTests(unittest.TestCase):
    """Issue #4: close() must survive a locked workspace on Windows."""

    def setUp(self) -> None:
        _pin_host_dependent_seams(self)

    def _client_with_live_worker(self) -> tuple[AntigravityClient, MagicMock]:
        """A client owning its real TemporaryDirectory plus one live worker.

        The non-streaming turn runs to success, which leaves the persistent
        worker alive and releases the request lock, so the ensuing close()
        has a live process to terminate and a full workspace to clean.
        """
        proc = _mock_proc(_worker_turn_lines("conv-clean", "answer one"))
        with patch("subprocess.Popen", return_value=proc):
            client = AntigravityClient()
            self.assertIsNotNone(client._temp_dir)
            res = client.chat.completions.create(
                model=MODEL, messages=list(TURN_MESSAGES), stream=False
            )
        self.assertEqual(res.choices[0].message.content, "answer one")
        self.assertIsNotNone(client._worker_proc)
        return client, proc

    def test_transient_cleanup_permission_error_is_retried(self):
        client, proc = self._client_with_live_worker()
        temp_dir = client._temp_dir
        cleanup = MagicMock(side_effect=[PermissionError(32, "WinError 32"), None])
        rmtree = MagicMock()
        with (
            patch.object(temp_dir, "cleanup", cleanup),
            patch("shutil.rmtree", rmtree),
            patch("time.sleep") as sleep,
        ):
            client.close()  # must not raise

        self.assertEqual(cleanup.call_count, 2)  # one retry, then success
        self.assertEqual(sleep.call_count, 1)
        sleep.assert_called_once_with(_TEMP_DIR_CLEANUP_BACKOFF_SECONDS[0])
        # Path-filtered (see _rmtree_calls_targeting): a stray 3.11
        # tempfile finalizer must not fail a test about OUR call.
        self.assertEqual(
            self._rmtree_calls_targeting(rmtree, temp_dir.name),
            [],
            f"the retry succeeded, so no forced removal targeted the "
            f"workspace; ALL rmtree calls: {rmtree.call_args_list}",
        )
        self.assertTrue(client.is_closed)
        self.assertIsNone(client._temp_dir)
        self.assertTrue(proc.terminate.called)
        # cleanup is mocked, so the real directory outlives the client.
        self.addCleanup(shutil.rmtree, temp_dir.name, ignore_errors=True)

    def test_persistent_cleanup_permission_error_falls_back_to_forced_removal(self):
        client, proc = self._client_with_live_worker()
        temp_dir = client._temp_dir
        workspace_path = temp_dir.name
        cleanup = MagicMock(side_effect=PermissionError(32, "WinError 32"))
        rmtree = MagicMock()  # leaves the path in place: _force_rmtree must
        # then chmod-sweep and retry, which is the two-pass shape below.
        with (
            patch.object(temp_dir, "cleanup", cleanup),
            patch("shutil.rmtree", rmtree),
            patch("time.sleep") as sleep,
        ):
            client.close()  # must not raise even when cleanup never succeeds

        self.assertEqual(cleanup.call_count, _TEMP_DIR_CLEANUP_ATTEMPTS)
        self.assertEqual(sleep.call_count, _TEMP_DIR_CLEANUP_ATTEMPTS - 1)
        # Path-filtered, not a total count -- see _rmtree_calls_targeting:
        # on 3.11 a stray tempfile finalizer can reach this mock on
        # Windows (observed as a third, foreign-shaped call), and a total
        # count would fail for a call that has nothing to do with us.
        self.assertEqual(
            [
                (c.args, c.kwargs)
                for c in self._rmtree_calls_targeting(rmtree, workspace_path)
            ],
            [((workspace_path,), {"ignore_errors": True})] * 2,
            f"two-pass forced removal of the workspace; ALL rmtree calls: {rmtree.call_args_list}",
        )
        # The client is fully closed, never half-closed.
        self.assertTrue(client.is_closed)
        self.assertIsNone(client._temp_dir)
        self.assertIsNone(client._worker_proc)
        self.assertTrue(proc.terminate.called)
        self.addCleanup(shutil.rmtree, workspace_path, ignore_errors=True)

    def test_non_oserror_cleanup_failure_forces_removal_without_retry(self):
        """A non-OSError from cleanup() must reach the forced removal (W1).

        The loop catches every exception, but only OSError (the
        handle-still-held symptoms) earns a retry; anything else breaks
        straight to the forced pass. Under an OSError-only loop this
        exception skipped the forced removal entirely: the outer guard in
        _remove_temp_dir swallowed it and the whole workspace leaked while
        close() looked successful.
        """
        client, proc = self._client_with_live_worker()
        temp_dir = client._temp_dir
        workspace_path = temp_dir.name
        cleanup = MagicMock(side_effect=ValueError("not an OS error"))
        rmtree = MagicMock()
        with (
            patch.object(temp_dir, "cleanup", cleanup),
            patch("shutil.rmtree", rmtree),
            patch("time.sleep") as sleep,
        ):
            client.close()  # must not raise

        self.assertEqual(cleanup.call_count, 1)  # no retry for non-OSError
        sleep.assert_not_called()
        # Path-filtered two-pass contract -- see _rmtree_calls_targeting.
        self.assertEqual(
            [
                (c.args, c.kwargs)
                for c in self._rmtree_calls_targeting(rmtree, workspace_path)
            ],
            [((workspace_path,), {"ignore_errors": True})] * 2,
            f"the non-OSError failure still forced removal; ALL rmtree calls: {rmtree.call_args_list}",
        )
        self.assertTrue(client.is_closed)
        self.assertIsNone(client._temp_dir)
        self.assertTrue(proc.terminate.called)
        self.addCleanup(shutil.rmtree, workspace_path, ignore_errors=True)

    def test_workspace_cleanup_runs_outside_client_lock(self):
        """Phase 2 must not hold client._lock (mutation survivor, W3).

        Termination already happened in phase 1; holding the lock through
        a retrying cleanup would block every other request for up to the
        whole retry budget on a dying client. The probe runs on a HELPER
        thread because _lock is an RLock: a same-thread acquire would
        succeed even while close() legitimately holds it, so only a
        foreign thread can observe the mutation.
        """
        client, proc = self._client_with_live_worker()
        temp_dir = client._temp_dir
        lock_observed_free = []

        def _probe_lock_from_other_thread() -> None:
            acquired = threading.Event()

            def _acquire() -> None:
                if client._lock.acquire(timeout=1.0):
                    client._lock.release()
                    acquired.set()

            prober = threading.Thread(target=_acquire, daemon=True)
            prober.start()
            prober.join(timeout=2.0)
            lock_observed_free.append(acquired.is_set())

        cleanup = MagicMock(side_effect=_probe_lock_from_other_thread)
        with (
            patch.object(temp_dir, "cleanup", cleanup),
            patch("shutil.rmtree", MagicMock()),
            patch("time.sleep"),
        ):
            client.close()

        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(
            lock_observed_free,
            [True],
            "cleanup ran while another thread could not take client._lock: "
            "phase 2 is executing inside the locked region",
        )
        self.assertTrue(client.is_closed)
        self.addCleanup(shutil.rmtree, temp_dir.name, ignore_errors=True)

    def test_forced_removal_chmods_survivors_and_retries(self):
        """_force_rmtree's second pass after a chmod sweep (S1).

        shutil.rmtree(ignore_errors=True) alone is weaker than the
        tempfile._rmtree behind cleanup(): it silently skips read-only
        subtrees, which is the residual-leak shape of issue #4. When the
        first forced pass leaves the path behind, the sweep must grant
        owner write/search permission to every surviving entry and force
        a second pass. Mocked so the assertion is about the sequence, not
        the filesystem.
        """
        with tempfile.TemporaryDirectory(prefix="hermes_agy_force_") as survivor_root:
            nested = Path(survivor_root) / "conversations" / "sub"
            nested.mkdir(parents=True)
            victim = nested / "workspace.db"
            victim.write_text("x", encoding="utf-8")
            original_rmtree = shutil.rmtree
            calls: list[str] = []
            chmodded: list[str] = []

            def _fake_rmtree(path, *args, **kwargs):
                if path != survivor_root:
                    # A 3.11 tempfile finalizer can reach this patch
                    # window on Windows (see _rmtree_calls_targeting); it
                    # is not our call and must not perturb the sequence.
                    return original_rmtree(path, *args, **kwargs)
                calls.append(path)
                if len(calls) == 1:
                    return None  # first pass "fails" to remove anything
                return original_rmtree(path, *args, **kwargs)

            real_chmod = os.chmod

            def _fake_chmod(path, mode, *args, **kwargs):
                chmodded.append(str(path))
                return real_chmod(path, mode, *args, **kwargs)

            with (
                patch("client.shutil.rmtree", side_effect=_fake_rmtree),
                patch("client.os.chmod", side_effect=_fake_chmod),
            ):
                _force_rmtree(survivor_root)  # must not raise

            self.assertEqual(calls, [survivor_root, survivor_root])
            # Every surviving entry (both dirs + the file) got a permission bump.
            self.assertEqual(len(chmodded), 3)
            self.assertTrue(
                all(Path(c).is_relative_to(survivor_root) for c in chmodded),
                "the permission sweep touched entries outside the workspace",
            )
            # The second pass really removed the tree.
            self.assertFalse(os.path.exists(survivor_root))

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "root bypasses directory permissions, so the read-only sweep would be vacuous",
    )
    def test_forced_removal_reclaims_real_read_only_subtree(self):
        """Real-FS proof for S1: a read-only subtree does not survive.

        Skipped as root (DAC is bypassed, nothing to reclaim); CI runners
        are non-root, so this runs there.
        """
        with tempfile.TemporaryDirectory(prefix="hermes_agy_ro_") as root:
            locked_dir = Path(root) / "conversations"
            locked_dir.mkdir()
            victim = locked_dir / "session.db"
            victim.write_text("x", encoding="utf-8")
            os.chmod(locked_dir, 0o500)  # read+execute only: rmtree fails
            with self.assertRaises(PermissionError):
                shutil.rmtree(root)  # sanity: the plain removal really fails
            os.chmod(locked_dir, 0o500)

            _force_rmtree(root)  # must not raise

            self.assertFalse(os.path.exists(root), "read-only subtree survived the forced removal")

    @staticmethod
    def _rmtree_calls_targeting(rmtree: MagicMock, workspace_path: str) -> list:
        """The subset of a patched rmtree's calls that targeted the workspace.

        On Python 3.11 -- the version of BOTH CI legs -- ``tempfile._rmtree``
        delegates to ``shutil.rmtree`` (CPython 3.11 ``Lib/tempfile.py``:
        ``_shutil.rmtree(name, onerror=onerror)``), while 3.12 binds a
        frozen ``_rmtree = shutil.rmtree`` alias at import time. So on
        3.11 an unrelated ``TemporaryDirectory`` finalizer that happens
        to fire inside a ``patch("shutil.rmtree", ...)`` window lands in
        the same mock: the windows-latest leg observed exactly that -- a
        third call with a foreign ``onerror=`` shape, on a
        finalizer-timing path. Which TemporaryDirectory fires, and on
        which leg, is timing-dependent and NOT identifiable from Linux;
        nobody has proven the culprit instance, so none is named. The
        path filter pins OUR contract -- the forced passes on the
        workspace -- and every failure message dumps the full call list,
        so a genuine regression stays diagnosable.
        """
        return [c for c in rmtree.call_args_list if c.args and c.args[0] == workspace_path]

    @staticmethod
    def _force_with_residual_survivors(path: str) -> None:
        """Run ``_force_rmtree`` with the first pass neutralized.

        The #4 residual shape -- pass 1 leaves survivors, so the sweep
        runs -- modelled deterministically everywhere: as root a
        read-only workspace would NOT stop the real first pass (DAC is
        bypassed), so pass 1 is a no-op instead of a permission trick.
        Pass 2 is the real removal. The attempt counter lives in this
        call's closure, never shared between tests.
        """
        real_rmtree = shutil.rmtree
        state = {"passes": 0}

        def _first_pass_leaves_survivors(target, *args, **kwargs):
            state["passes"] += 1
            if state["passes"] == 1:
                return None
            return real_rmtree(target, *args, **kwargs)

        with patch("client.shutil.rmtree", side_effect=_first_pass_leaves_survivors):
            _force_rmtree(path)

    def test_forced_removal_does_not_widen_permissions_through_a_symlink(self):
        """W-A: a surviving token SYMLINK must never chmod its target.

        ``setup_isolated_home`` links the user's REAL OAuth token into the
        isolated HOME (symlink first, process.py), and that HOME lives
        inside this workspace. An ``os.stat``/``os.path.isdir`` sweep
        FOLLOWED the link and widened the real 0400 token to 0600 --
        out-of-scope permission changes on user data, firing on exactly
        the residual shape issue #4 is about. The sweep now uses ``lstat``
        and skips links. The assertion is on the MODE, so it holds under
        root (DAC bypassed) as much as under a normal uid.
        """
        with tempfile.TemporaryDirectory(prefix="hermes_agy_out_") as outside:
            token = Path(outside) / "real-token"
            token.write_text("secret", encoding="utf-8")
            os.chmod(token, 0o400)
            # Baseline read back through the PLATFORM's own normalization:
            # POSIX reports 0o400, Windows synthesizes 0o444 from
            # FILE_ATTRIBUTE_READONLY (there is no 0o400 there). Comparing
            # against the baseline instead of a hardcoded literal keeps
            # the test portable, while a widening sweep still fails on
            # both (POSIX 0o600, Windows 0o666 -- see the round's
            # portability simulation).
            baseline = stat.S_IMODE(os.stat(token).st_mode)
            with tempfile.TemporaryDirectory(prefix="hermes_agy_ws_") as workspace:
                os.symlink(token, Path(workspace) / "token-link")
                self._force_with_residual_survivors(workspace)

                self.assertFalse(
                    os.path.exists(workspace),
                    "the forced removal left the workspace behind",
                )
                self.assertEqual(
                    stat.S_IMODE(os.stat(token).st_mode),
                    baseline,
                    "the sweep followed the token symlink and widened the "
                    "user's real token outside the workspace",
                )

    def test_forced_removal_does_not_widen_permissions_through_a_hardlink(self):
        """W-A, the ``os.link`` fallback shape (same guarantee as symlinks).

        A hardlink shares the token's inode: chmodding it through
        ``os.stat`` widened the real 0400 token to 0600 exactly like the
        symlink case. ``st_nlink > 1`` marks the shared inode, so the
        sweep skips it for the same reason it skips symlinks.

        The workspace-survival assertion is platform-branched, and that
        is a documented platform semantic rather than a test fudge: on
        POSIX, unlinking needs only parent-directory write, so the swept
        workspace disappears. On Windows, DeleteFile refuses a file
        carrying FILE_ATTRIBUTE_READONLY, and clearing that attribute
        would modify the shared file record -- i.e. the user's real
        token -- the exact out-of-scope widening W-A forbids. The sweep
        therefore leaves the readonly hardlink (and its parent chain)
        behind THERE, deliberately: agy writes tokens writable, and
        issue #4's actual conversations/*.db shape (nlink=1) is removed
        normally on Windows too. The Windows branch pins exactly that
        residual -- nothing but the undeletable token link survives, its
        mode and content untouched.
        """
        with tempfile.TemporaryDirectory(prefix="hermes_agy_out_") as outside:
            token = Path(outside) / "real-token"
            token.write_text("secret", encoding="utf-8")
            os.chmod(token, 0o400)
            baseline = stat.S_IMODE(os.stat(token).st_mode)
            with tempfile.TemporaryDirectory(prefix="hermes_agy_ws_") as workspace:
                os.link(token, Path(workspace) / "token-link")
                self.assertEqual(os.lstat(Path(workspace) / "token-link").st_nlink, 2)
                self._force_with_residual_survivors(workspace)

                self.assertEqual(
                    stat.S_IMODE(os.stat(token).st_mode),
                    baseline,
                    "the sweep chmodded the hardlinked token inode and "
                    "widened the user's real token outside the workspace",
                )
                self.assertEqual(
                    token.read_text(encoding="utf-8"),
                    "secret",
                    "the sweep disturbed the token's contents",
                )
                if os.name == "nt":
                    self._assert_only_the_linked_token_survives(workspace, token)
                else:
                    self.assertFalse(
                        os.path.exists(workspace),
                        "the forced removal left the workspace behind",
                    )

    def _assert_only_the_linked_token_survives(self, workspace: str, token: Path) -> None:
        """Windows: the only tolerated residue is the undeletable hardlink.

        See the hardlink test's docstring for why the remnant exists.
        Anything else surviving -- a different file, extra entries --
        would be a genuine leak and must fail.
        """
        if not os.path.exists(workspace):
            return  # removed outright: nothing to justify
        residuals = [
            Path(dirpath) / name
            for dirpath, _dirnames, filenames in os.walk(workspace)
            for name in filenames
        ]
        self.assertTrue(
            residuals,
            "the workspace shell survived on Windows without the hardlink "
            "that explains it",
        )
        for residual in residuals:
            self.assertTrue(
                os.path.samefile(residual, token),
                "unexpected survivor inside the workspace: "
                f"{residual} is not the hardlinked token the sweep "
                "documents as Windows-undeletable",
            )

    def test_forced_removal_does_not_escape_through_a_symlinked_directory(self):
        """S-1: the sweep must not recurse THROUGH a linked directory.

        ``_force_rmtree`` promises ``os.walk`` runs with the default
        ``followlinks=False``, so recursion cannot escape the workspace.
        The two sibling tests pin the leaf shapes (a symlinked file, a
        hardlinked file); this pins the recursion itself. With the
        mutated ``os.walk(path, followlinks=True)`` the walk descends
        through a workspace symlink that points at an outside directory
        and chmod-widens the 0400 file inside it -- the same W-A failure
        mode through the one shape the siblings do not cover. The
        assertion is on the MODE, so it holds under root (DAC bypassed)
        just like the siblings.
        """
        with tempfile.TemporaryDirectory(prefix="hermes_agy_out_") as outside:
            secret = Path(outside) / "real-token"
            secret.write_text("secret", encoding="utf-8")
            os.chmod(secret, 0o400)
            baseline = stat.S_IMODE(os.stat(secret).st_mode)
            with tempfile.TemporaryDirectory(prefix="hermes_agy_ws_") as workspace:
                os.symlink(outside, Path(workspace) / "linked-dir")
                self._force_with_residual_survivors(workspace)

                self.assertFalse(
                    os.path.exists(workspace),
                    "the forced removal left the workspace behind",
                )
                self.assertEqual(
                    stat.S_IMODE(os.stat(secret).st_mode),
                    baseline,
                    "the walk recursed through the symlinked directory and "
                    "widened a file outside the workspace",
                )
                self.assertTrue(
                    os.path.isdir(outside),
                    "the sweep escaped the workspace through the linked "
                    "directory and touched its contents",
                )

    def test_terminate_processes_before_workspace_cleanup(self):
        client, _proc = self._client_with_live_worker()
        temp_dir = client._temp_dir
        workspace_path = temp_dir.name
        events: list[str] = []
        with (
            patch(
                "client.terminate_process",
                side_effect=lambda proc: events.append("terminate"),
            ),
            patch.object(
                temp_dir, "cleanup", side_effect=lambda: events.append("cleanup")
            ),
            patch("time.sleep"),
        ):
            client.close()
        # Ordering is load-bearing (issue #4): removing the workspace while
        # agy still holds conversations/*.db is exactly the WinError 32 race.
        self.assertEqual(events, ["terminate", "cleanup"])
        self.assertTrue(client.is_closed)
        self.addCleanup(shutil.rmtree, workspace_path, ignore_errors=True)


# Stand-in persistent worker for ExitWithoutCloseTests: holds a file open
# inside the workspace, as agy holds conversations/*.db, and appends to a
# heartbeat file OUTSIDE the workspace for as long as it runs. Self-exits
# after 30s so a failing run cannot leave it behind for long.
_HOLDER_CODE = textwrap.dedent(
    """
    import sys, time
    held = open(sys.argv[1], "w")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with open(sys.argv[2], "a") as beat:
            beat.write(".")
        time.sleep(0.05)
    """
)

# Child interpreter for ExitWithoutCloseTests: builds a client, adopts the
# stand-in as its persistent worker, and exits WITHOUT close() -- the way
# `hermes chat --oneshot` leaves.
_EXIT_WITHOUT_CLOSE_CHILD = textwrap.dedent(
    """
    import json, os, subprocess, sys, time
    plugin_dir, holder_python, holder_code, beat = sys.argv[1:5]
    sys.path.insert(0, plugin_dir)
    import process
    # Same seams the in-process suites pin: never link the real token or keychains.
    process.resolve_real_token_path = lambda: None
    process._link_macos_keychains = lambda isolated_home: None
    import client
    c = client.AntigravityClient(command="agy")
    conversations = os.path.join(c._cwd, "home", ".gemini", "antigravity-cli", "conversations")
    os.makedirs(conversations, exist_ok=True)
    holder = subprocess.Popen(
        [holder_python, "-c", holder_code, os.path.join(conversations, "x.db"), beat],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **process._own_process_group(),
    )
    deadline = time.monotonic() + 10
    while not os.path.exists(beat) and time.monotonic() < deadline:
        time.sleep(0.05)
    with c._lock:  # adopt it exactly as _get_or_spawn_worker does
        c._worker_proc = holder
        c._active_processes.add(holder)
    print(json.dumps({"workspace": c._cwd, "pid": holder.pid}), flush=True)
    """
)


def _kill_pid(pid: int) -> None:
    """Best-effort kill of a stand-in worker that outlived its test."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


class ExitWithoutCloseTests(unittest.TestCase):
    """A client its host never closes is closed at interpreter exit.

    Exit-path gap reported in GitHub issue neerazz/hermes-antigravity-oauth#4:
    ``hermes chat --oneshot`` exits without closing the provider client.
    The only cleanup left was TemporaryDirectory's weakref finalizer, which
    ran while the worker still held conversations/*.db: a WinError 32
    traceback at exit and a leaked workspace on Windows, and a worker left
    running on every platform.
    """

    def test_unclosed_client_is_closed_at_interpreter_exit(self):
        scratch = tempfile.mkdtemp(prefix="hermes_agy_exit_test_")
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        beat = os.path.join(scratch, "heartbeat")
        # On Windows a venv python.exe is a launcher that runs the real
        # interpreter as a child; terminating the launcher would leave the
        # file holder running, so the holder uses the base interpreter.
        holder_python = getattr(sys, "_base_executable", None) or sys.executable
        result = subprocess.run(
            [sys.executable, "-c", _EXIT_WITHOUT_CLOSE_CHILD,
             str(plugin_dir), holder_python, _HOLDER_CODE, beat],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout.strip().splitlines()[-1])
        self.addCleanup(shutil.rmtree, info["workspace"], ignore_errors=True)
        self.addCleanup(_kill_pid, info["pid"])  # cleanups run LIFO: kill first

        beats = os.path.getsize(beat)
        time.sleep(0.5)  # ten heartbeats if the worker were still running
        self.assertEqual(os.path.getsize(beat), beats, "worker still running after exit")
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(os.path.exists(info["workspace"]), "workspace leaked")

    def test_exit_hook_is_weak_and_removed_by_close(self):
        _pin_host_dependent_seams(self)
        with patch("client.atexit") as fake_atexit:
            closed = AntigravityClient()
            closed.close()
            dropped = AntigravityClient()
        closed_hook, dropped_hook = (call.args[0] for call in fake_atexit.register.call_args_list)
        # close() removes its own hook: a long-running host does not pile them up.
        fake_atexit.unregister.assert_called_once_with(closed_hook)
        # The hook never keeps an unclosed client alive ...
        ref = weakref.ref(dropped)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)  # left unclosed on purpose
            del dropped
            gc.collect()
        self.assertIsNone(ref())
        # ... and running it for a collected client is a silent no-op.
        dropped_hook()


class StaleStreamFinalizationTests(unittest.TestCase):
    """W2: a stream finalized long after its turn must be inert.

    Same root cause as issue #10: the RLock lets a GC-finalized
    generator's ``finally`` run to completion instead of deadlocking, so
    cleanup that used to (accidentally) be blocked by the deadlock now
    actually executes -- at an arbitrary later time, on an arbitrary
    thread, against state that may have moved on. Two ownership guards
    keep that execution inert:

    * ``_worker_lock_held`` -- the lock is released only by the stream
      whose turn actually acquired it, never via a ``locked()`` probe
      (``threading.Lock`` is not owner-bound, so a probe cannot tell "my
      lock" from "someone else's" and a stale finalizer would unlock a
      live turn underneath its owner).
    * a proc-identity check -- ``close()`` terminates the worker only if
      ``self.proc`` is still the client's current worker, so a stale
      stream cannot kill the fresh worker serving a later turn.
    """

    def setUp(self) -> None:
        _pin_host_dependent_seams(self)
        temp_dir = tempfile.TemporaryDirectory(prefix="hermes_agy_test_")
        self.addCleanup(temp_dir.cleanup)
        self.client = AntigravityClient(cwd=temp_dir.name)

    def _stream(self, *, proc, finished=False, lock=None, lock_held=False):
        # The pair is passed only together (N-A): no lock means neither
        # argument, so a oneshot-style construction cannot trip the
        # constructor's pairing guard.
        pair = {}
        if lock is not None:
            pair = {"worker_lock": lock, "worker_lock_held": lock_held}
        stream = AntigravityStream(
            proc=proc,
            client=self.client,
            model=MODEL,
            timeout=30.0,
            is_worker=True,
            messages=list(TURN_MESSAGES),
            **pair,
        )
        # The generator is never consumed here: these tests drive the
        # cleanup paths directly, which is exactly what a GC finalizer
        # reaches into.
        stream._finished = finished
        return stream

    def test_completed_abandoned_stream_does_not_release_a_live_turns_lock(self):
        # A completed-and-abandoned stream: its success path already ran
        # (flag cleared) and the object is only awaiting the cyclic GC.
        # Meanwhile a live turn (H) owns the request lock. Under the old
        # locked() probe, the finalizer's close() released H's lock
        # underneath it, letting a third request write to the same worker.
        live_turn_lock = threading.Lock()
        self.assertTrue(live_turn_lock.acquire(blocking=False))
        try:
            stream = self._stream(
                proc=MagicMock(), finished=True, lock=live_turn_lock, lock_held=False
            )
            stream.close()
            self.assertTrue(
                live_turn_lock.locked(),
                "a finalized stream released a request lock owned by a live turn",
            )
        finally:
            live_turn_lock.release()

    def test_stale_stream_close_does_not_terminate_the_current_worker(self):
        # The stream's worker was replaced: _worker_proc is a fresh worker
        # serving a later turn. Terminating it would take down a healthy
        # live request.
        stale_proc = MagicMock()
        current_proc = MagicMock()
        self.client._worker_proc = current_proc
        stream = self._stream(proc=stale_proc, finished=False)
        stream.close()
        current_proc.terminate.assert_not_called()
        self.assertIs(self.client._worker_proc, current_proc)

    def test_midturn_stream_close_still_terminates_its_own_worker(self):
        # Preserved behavior: this stream's proc IS the client's current
        # worker and the turn never finished, so closing it mid-turn
        # terminates the worker exactly as before.
        worker_proc = MagicMock()
        self.client._worker_proc = worker_proc
        stream = self._stream(proc=worker_proc, finished=False)
        stream.close()
        worker_proc.terminate.assert_called_once()
        self.assertIsNone(self.client._worker_proc)

    def test_worker_lock_is_released_exactly_once(self):
        # The flag discipline: the owning stream releases its lock on the
        # first cleanup that reaches it and never again -- a finalizer
        # racing (or following) the owner's own release cannot
        # double-release, and releasing an unlocked Lock would raise.
        class _RecordingLock:
            def __init__(self) -> None:
                self._lock = threading.Lock()
                self.releases = 0

            def acquire(self, **kwargs):
                return self._lock.acquire(**kwargs)

            def release(self) -> None:
                self.releases += 1
                self._lock.release()

            def locked(self) -> bool:
                return self._lock.locked()

        lock = _RecordingLock()
        self.assertTrue(lock.acquire(blocking=False))
        stream = self._stream(proc=MagicMock(), finished=False, lock=lock, lock_held=True)
        stream.close()
        self.assertEqual(lock.releases, 1)
        self.assertFalse(lock.locked())
        self.assertFalse(stream._worker_lock_held)
        stream.close()  # idempotent close: still exactly one release
        self.assertEqual(lock.releases, 1)

    def test_worker_lock_and_flag_must_be_passed_together(self):
        """N-A: the worker_lock/worker_lock_held pairing is loud.

        A future site passing worker_lock WITHOUT the flag would never
        release it -- a permanent _worker_lock leak, every later turn
        silently degraded to the oneshot path. The constructor refuses
        either half of the pair instead of defaulting into that.
        """
        base = dict(
            proc=MagicMock(),
            client=self.client,
            model=MODEL,
            timeout=30.0,
            is_worker=True,
        )
        with self.assertRaises(ValueError):
            AntigravityStream(worker_lock=threading.Lock(), **base)  # flag omitted
        with self.assertRaises(ValueError):
            AntigravityStream(worker_lock_held=True, **base)  # lock omitted
        # The valid pairings still construct.
        self.assertIsNotNone(AntigravityStream(**base))  # neither passed
        with_lock = threading.Lock()
        self.assertIsNotNone(
            AntigravityStream(worker_lock=with_lock, worker_lock_held=True, **base)
        )


class _RealPollFakeProc:
    """A Popen stand-in with REAL post-terminate() poll() semantics.

    ``MagicMock().poll.return_value`` is hardcoded forever -- it never
    reflects that terminate()/kill() was actually called, so a mocked test
    using one cannot distinguish "still alive" from "we just killed this on
    purpose" the way a real ``subprocess.Popen`` does. This fake flips
    poll() from None to a real exit code only once terminate()/kill() runs,
    which is exactly the fidelity needed to pin the bug below.
    """

    def __init__(self, lines, exit_code=1):
        self._lines = list(lines) + [""]
        self._idx = 0
        self._terminated = False
        self._exit_code = exit_code
        self.stdin = MagicMock()
        self.stderr = MagicMock(read=lambda: "")
        self.stdout = MagicMock(readline=self._readline)
        self.pid = 999999

    def _readline(self):
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return (line + "\n") if line else ""
        return ""

    def poll(self):
        return self._exit_code if self._terminated else None

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._terminated = True

    def wait(self, timeout=None):
        if not self._terminated:
            raise TimeoutError("not terminated")
        return self._exit_code


class TestNeutralizedToolStepDoesNotMisreportAsACrash(unittest.TestCase):
    """Regression for the antigravity-oauth fast-crash (task t_004f4153).

    Root cause (confirmed via errors.log/agent.log correlation on gaia):
    every "Antigravity execution failed: worker process exited with return
    code 1" failure is preceded within ~1s by exactly one "...neutralizing
    to prevent host execution." warning. stream.py's step_update handler
    deliberately tears down the worker on an unmappable native tool step
    (``self.close()``) and sets ``neutralized_tool_step = True`` -- but the
    exit-code check right after it (`if not has_tool_calls and not
    has_content and worker_exit not in (None, 0): raise ...`) did not carry
    the same ``not neutralized_tool_step`` guard already present on the
    sibling "empty result" raise, so the deliberate teardown's own nonzero
    exit code was reported as a crash every time. Fixed by adding the
    missing guard on both the worker-path and oneshot-path checks.

    ``test_native_tool_step_neutralization_in_stream`` in
    ``test_antigravity_plugin.py`` does not catch this: its mock hardcodes
    ``poll.return_value = None`` forever and stubs ``_terminate_process``
    out entirely, so poll() never reflects a real post-kill exit code. This
    test uses ``_RealPollFakeProc`` instead, which flips poll() to a real
    nonzero code only once terminate() actually runs -- reproducing the
    real race.
    """

    def setUp(self) -> None:
        _pin_host_dependent_seams(self)
        temp_dir = tempfile.TemporaryDirectory(prefix="hermes_agy_test_")
        self.addCleanup(temp_dir.cleanup)
        self.client = AntigravityClient(cwd=temp_dir.name)
        self.addCleanup(self.client.close)

    def test_unmappable_native_tool_step_does_not_raise(self):
        events = [
            json.dumps({"event": "init", "conversation_id": "test-neutralize"}),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {"name": "run_command", "parameters": {"CommandLine": "echo hi"}},
                },
            }),
        ]
        proc = _RealPollFakeProc(events, exit_code=1)
        self.client._worker_proc = proc
        self.client._worker_model = MODEL
        self.client._worker_effort = None
        self.client._active_processes.add(proc)
        # Point real termination at the fake so close() ACTUALLY flips
        # poll() like a real killed child would (see _RealPollFakeProc).
        self.client._terminate_process = lambda p: p.terminate()

        stream = AntigravityStream(
            proc=proc,
            client=self.client,
            model=MODEL,
            timeout=30.0,
            tools=[],  # no "terminal" tool offered -> run_command is unmappable
            is_worker=True,
            worker_lock=self.client._worker_lock,
            worker_lock_held=True,
            messages=list(TURN_MESSAGES),
            usage_baseline={},
        )

        chunks = list(stream)  # must not raise
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].choices[0].finish_reason, "stop")
        self.assertTrue(proc.poll() not in (None, 0), "fake must report a real post-kill exit code")

    def test_genuine_worker_crash_surfaces_stderr_in_the_raised_message(self):
        """A REAL crash (no neutralization) must still raise -- and now with detail.

        Companion to the neutralization regression above: this pins the
        other half of the task t_004f4153 finding -- the worker-path
        exit-code raise used to carry ZERO diagnostic detail (unlike the
        oneshot path's equivalent check, which already read stderr), so a
        genuine agy crash was indistinguishable from the neutralization
        false-positive this file's other test fixes. Both must raise with
        the stderr content now included; only the neutralization case must
        not raise at all.
        """
        proc = _RealPollFakeProc([], exit_code=1)  # no events at all: agy died immediately
        proc.stderr = MagicMock(read=lambda: "agy: fatal: some real startup failure\n")
        self.client._worker_proc = proc
        self.client._worker_model = MODEL
        self.client._worker_effort = None
        self.client._active_processes.add(proc)
        self.client._terminate_process = lambda p: p.terminate()
        proc.terminate()  # already "dead" before the stream even starts reading

        stream = AntigravityStream(
            proc=proc,
            client=self.client,
            model=MODEL,
            timeout=30.0,
            tools=[],
            is_worker=True,
            worker_lock=self.client._worker_lock,
            worker_lock_held=True,
            messages=list(TURN_MESSAGES),
            usage_baseline={},
        )

        with self.assertRaises(RuntimeError) as ctx:
            list(stream)
        message = str(ctx.exception)
        self.assertIn("worker process exited with return code 1", message)
        self.assertIn("agy: fatal: some real startup failure", message)


if __name__ == "__main__":
    unittest.main()
