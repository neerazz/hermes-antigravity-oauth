"""`/usage` hook tests: agy's quota readout mapped onto Hermes' account-usage snapshot.

agy is replaced by a fake executable script, so the real Google session is never touched.
"""

import copy
import dataclasses
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

import usage  # noqa: E402

# Verbatim `agy -p /usage --output-format json` response from agy 1.2.14.
LIVE_RESPONSE = {
    "conversation_id": "",
    "status": "SUCCESS",
    "response": "Gemini Models\tWeekly Limit Remaining\t67%\t2026-10-09T06:03:23Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-09T15:05:59Z\n",
    "duration_seconds": 0,
    "num_turns": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0},
    "command": {
        "name": "usage",
        "data": {
            "description": "Within each group, models share a weekly limit.",
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Models within this group: Gemini Flash, Gemini Pro",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "description": "You have used some of your weekly limit, it will fully refresh in 6 days, 14 hours.",
                            "window": "weekly",
                            "remaining_fraction": 0.6726695895195007,
                            "reset_time": "2026-10-09T06:03:23Z",
                        }
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 1,
                            "reset_time": "2026-10-09T15:05:59Z",
                        }
                    ],
                },
            ],
        },
    },
}

# Logs every invocation (args, cwd, updater switch), then answers like agy. Python, not shell, so
# the same fake runs on Windows. Modes that leave a process behind record PIDs before anything else,
# so a test never races the fake's own startup.
FAKE_AGY = r"""
import os, subprocess, sys, time

args = sys.argv[1:]
mode = os.environ.get("FAKE_AGY_MODE", "")
with open(os.environ["FAKE_AGY_LOG"], "a", encoding="utf-8") as log:
    update = os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE", "")
    log.write(" ".join(args) + " | cwd=" + os.getcwd() + " | update=" + update + "\n")


def leave_child_holding_stdout():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["FAKE_AGY_PIDS"], "w", encoding="utf-8") as pids:
        pids.write(f"{child.pid} {os.getpid()}")


if args[:1] == ["--version"]:
    if mode == "version-hang":
        time.sleep(60)
    print(os.environ["FAKE_AGY_VERSION"])
    sys.exit(1 if mode == "version-fail" else 0)
if mode == "hang":
    leave_child_holding_stdout()
    time.sleep(60)
if mode == "orphan-pipe":
    leave_child_holding_stdout()
    sys.exit(0)
with open(os.environ["FAKE_AGY_RESPONSE"], encoding="utf-8") as response:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(response.read())
if mode == "fail":
    print("Authentication required", file=sys.stderr)
    sys.exit(1)
"""
# Python and Windows process startup is slow on CI runners; budgets in the timing tests get this slack.
TIMING_SLACK_S = 3.0


def write_fake_agy(directory):
    """An executable fake agy in `directory`: a shebang script on POSIX, a .cmd shim on Windows."""
    if os.name == "nt":
        script = directory / "fake_agy.py"
        script.write_text(FAKE_AGY, encoding="utf-8")
        shim = directory / "agy.cmd"
        # cmd.exe reads batch files in the OEM code page; CI and temp paths are ASCII.
        shim.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return shim
    shim = directory / "agy"
    shim.write_text(f"#!{sys.executable}\n" + FAKE_AGY, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim


def response_with(**changes):
    body = copy.deepcopy(LIVE_RESPONSE)
    body.update(changes)
    return body


def data_with_groups(*groups):
    return {"groups": list(groups)}


def bucket(**fields):
    base = {"id": "b", "window": "weekly", "remaining_fraction": 0.5, "reset_time": "2026-10-09T06:03:23Z"}
    base.update(fields)
    return base


class ParseUsageResponseTests(unittest.TestCase):
    def test_live_response_is_accepted_whole(self):
        self.assertEqual(usage.parse_usage_response(json.dumps(LIVE_RESPONSE)), LIVE_RESPONSE)

    def test_rejects_response_that_ran_a_model_turn(self):
        self.assertIsNone(usage.parse_usage_response(json.dumps(response_with(num_turns=1))))

    def test_rejects_response_without_turn_count(self):
        body = response_with()
        del body["num_turns"]
        self.assertIsNone(usage.parse_usage_response(json.dumps(body)))

    def test_rejects_non_usage_command(self):
        body = response_with(command={"name": "model", "data": {"groups": []}})
        self.assertIsNone(usage.parse_usage_response(json.dumps(body)))

    def test_rejects_unsuccessful_status(self):
        self.assertIsNone(usage.parse_usage_response(json.dumps(response_with(status="ERROR"))))

    def test_rejects_missing_command_data(self):
        body = response_with(command={"name": "usage"})
        self.assertIsNone(usage.parse_usage_response(json.dumps(body)))

    def test_malformed_output_returns_none(self):
        for stdout in ("", "Gemini Models\tWeekly Limit Remaining\t67%", "[1, 2]", "null", "{"):
            with self.subTest(stdout=stdout):
                self.assertIsNone(usage.parse_usage_response(stdout))


class UsageWindowsTests(unittest.TestCase):
    def test_live_groups_map_to_short_labelled_windows(self):
        windows = usage.usage_windows(LIVE_RESPONSE["command"]["data"])
        self.assertEqual([w.label for w in windows], ["Gemini 7d", "Claude/GPT 7d"])
        self.assertAlmostEqual(windows[0].used_percent, 32.73304104804993)
        self.assertEqual(windows[1].used_percent, 0.0)
        self.assertEqual(windows[0].reset_at, datetime(2026, 10, 9, 6, 3, 23, tzinfo=timezone.utc))
        self.assertEqual(windows[1].reset_at, datetime(2026, 10, 9, 15, 5, 59, tzinfo=timezone.utc))
        self.assertIn("weekly limit", windows[0].detail)
        self.assertIsNone(windows[1].detail)

    def test_used_percent_is_inverse_of_remaining_fraction(self):
        cases = {0: 100.0, 0.25: 75.0, 1: 0.0, 1.2: 0.0, -0.1: 100.0}
        for remaining, used in cases.items():
            with self.subTest(remaining=remaining):
                data = data_with_groups({"name": "Gemini Models", "buckets": [bucket(remaining_fraction=remaining)]})
                self.assertAlmostEqual(usage.usage_windows(data)[0].used_percent, used)

    def test_bucket_without_numeric_fraction_is_skipped(self):
        for remaining in (None, "0.5", True, [0.5]):
            with self.subTest(remaining=remaining):
                data = data_with_groups({"name": "Gemini Models", "buckets": [bucket(remaining_fraction=remaining)]})
                self.assertEqual(usage.usage_windows(data), [])

    def test_unparseable_reset_time_keeps_window(self):
        for reset in (None, "", "next week", 1760000000):
            with self.subTest(reset=reset):
                data = data_with_groups({"name": "Gemini Models", "buckets": [bucket(reset_time=reset)]})
                (window,) = usage.usage_windows(data)
                self.assertIsNone(window.reset_at)
                self.assertEqual(window.used_percent, 50.0)

    def test_reset_time_formats(self):
        cases = {
            "2026-10-09T06:03:23": datetime(2026, 10, 9, 6, 3, 23, tzinfo=timezone.utc),
            "2026-10-09T06:03:23.123456789Z": datetime(2026, 10, 9, 6, 3, 23, 123456, tzinfo=timezone.utc),
            "2026-10-09T08:03:23+02:00": datetime(2026, 10, 9, 6, 3, 23, tzinfo=timezone.utc),
        }
        for reset, expected in cases.items():
            with self.subTest(reset=reset):
                data = data_with_groups({"name": "Gemini Models", "buckets": [bucket(reset_time=reset)]})
                self.assertEqual(usage.usage_windows(data)[0].reset_at, expected)

    def test_unknown_window_name_is_kept_verbatim(self):
        data = data_with_groups({"name": "Gemini Models", "buckets": [bucket(window="monthly")]})
        self.assertEqual(usage.usage_windows(data)[0].label, "Gemini monthly")

    def test_missing_group_name_and_window_fall_back_to_bucket_id(self):
        data = data_with_groups({"buckets": [bucket(window=None, id="gemini-weekly")]})
        self.assertEqual(usage.usage_windows(data)[0].label, "gemini-weekly")

    def test_colliding_labels_are_made_unique(self):
        data = data_with_groups(
            {"name": "Gemini Models", "buckets": [bucket(id="flash"), bucket(id="pro"), bucket(id=None)]}
        )
        labels = [w.label for w in usage.usage_windows(data)]
        self.assertEqual(labels, ["Gemini 7d", "Gemini 7d (pro)", "Gemini 7d (2)"])

    def test_generated_label_never_reuses_an_existing_one(self):
        data = data_with_groups(
            {"name": "Gemini Models", "buckets": [bucket(id="flash")]},
            {"name": "Gemini 7d (pro)", "buckets": [bucket(window=None)]},
            {"name": "Gemini Models", "buckets": [bucket(id="pro")]},
        )
        labels = [w.label for w in usage.usage_windows(data)]
        self.assertEqual(labels, ["Gemini 7d", "Gemini 7d (pro)", "Gemini 7d (2)"])
        self.assertEqual(len(set(labels)), len(labels))

    def test_malformed_structure_yields_no_windows(self):
        for data in ({}, {"groups": None}, {"groups": "x"}, {"groups": [None, 1, {"buckets": "x"}]}):
            with self.subTest(data=data):
                self.assertEqual(usage.usage_windows(data), [])


class ParseAgyVersionTests(unittest.TestCase):
    def test_versions(self):
        cases = {
            "1.2.14\n": (1, 2, 14),
            "agy version 1.1.11 (build abc)": (1, 1, 11),
            "dev": None,
            "": None,
            None: None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(usage.parse_agy_version(text), expected)


class FakeAgyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.agy = write_fake_agy(self.tmp)
        self.log = self.tmp / "invocations.log"
        self.log.touch()
        self.response = self.tmp / "response.json"
        self.write_response(LIVE_RESPONSE)
        self.pids = self.tmp / "pids"
        env = {
            "FAKE_AGY_LOG": str(self.log),
            "FAKE_AGY_RESPONSE": str(self.response),
            "FAKE_AGY_PIDS": str(self.pids),
            "FAKE_AGY_VERSION": "1.2.14",
            "FAKE_AGY_MODE": "",
            "AGY_CLI_DISABLE_AUTO_UPDATE": "",
        }
        for patcher in (
            patch.dict(os.environ, env),
            patch("usage.is_authenticated", return_value=True),
            patch("usage.resolve_agy_command", return_value=str(self.agy)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_response(self, body):
        self.response.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    def invocations(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def usage_invocations(self):
        return [line for line in self.invocations() if line.startswith("-p /usage")]

    def invocation_cwd(self, line):
        return Path(line.split("cwd=")[1].split(" |")[0])

    def assert_processes_gone(self):
        deadline = time.monotonic() + 5
        while not self.pids.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.pids.exists(), "fake agy never recorded its PIDs")
        pids = [int(pid) for pid in self.pids.read_text(encoding="utf-8").split()]
        self.assertEqual(len(pids), 2)
        alive = pids
        while alive and time.monotonic() < deadline:
            alive = [pid for pid in alive if _pid_alive(pid)]
            time.sleep(0.05)
        self.assertEqual(alive, [], "agy or its child survived the timeout")

    def test_report_from_live_response(self):
        report = usage.fetch_usage_report()
        self.assertIsNotNone(report)
        self.assertEqual([w.label for w in report.windows], ["Gemini 7d", "Claude/GPT 7d"])
        self.assertEqual(report.raw, LIVE_RESPONSE)

    def test_non_ascii_output_is_decoded_as_utf8(self):
        body = copy.deepcopy(LIVE_RESPONSE)
        body["command"]["data"]["groups"][0]["buckets"][0]["description"] = "Refreshes in 6 days — 33% used ✓"
        self.write_response(body)
        report = usage.fetch_usage_report()
        self.assertEqual(report.windows[0].detail, "Refreshes in 6 days — 33% used ✓")

    def test_unauthenticated_never_spawns_agy(self):
        with patch("usage.is_authenticated", return_value=False), \
             patch("usage.subprocess.Popen", side_effect=AssertionError("spawned agy")):
            self.assertIsNone(usage.fetch_usage_report())

    def test_old_agy_version_skips_usage_probe(self):
        os.environ["FAKE_AGY_VERSION"] = "1.1.10"
        self.assertIsNone(usage.fetch_usage_report())
        self.assertEqual(len(self.invocations()), 1)
        self.assertEqual(self.usage_invocations(), [])

    def test_unparseable_version_skips_usage_probe(self):
        os.environ["FAKE_AGY_VERSION"] = "dev-build"
        self.assertIsNone(usage.fetch_usage_report())
        self.assertEqual(self.usage_invocations(), [])

    def test_failing_version_command_skips_usage_probe(self):
        os.environ["FAKE_AGY_MODE"] = "version-fail"
        self.assertIsNone(usage.fetch_usage_report())
        self.assertEqual(self.usage_invocations(), [])

    def test_hanging_version_command_skips_usage_probe(self):
        os.environ["FAKE_AGY_MODE"] = "version-hang"
        started = time.monotonic()
        with patch("usage.VERSION_TIMEOUT_S", 0.5):
            self.assertIsNone(usage.fetch_usage_report())
        self.assertLess(time.monotonic() - started, 0.5 + usage.DRAIN_TIMEOUT_S + TIMING_SLACK_S)
        self.assertEqual(self.usage_invocations(), [])

    def test_minimum_version_runs_usage_probe(self):
        os.environ["FAKE_AGY_VERSION"] = "1.1.11"
        self.assertIsNotNone(usage.fetch_usage_report())
        self.assertEqual(len(self.usage_invocations()), 1)

    def test_exhausted_budget_spawns_nothing(self):
        with patch("usage.USAGE_BUDGET_S", 0), \
             patch("usage.subprocess.Popen", side_effect=AssertionError("spawned agy")):
            self.assertIsNone(usage.fetch_usage_report())

    def test_missing_agy_returns_none(self):
        with patch("usage.resolve_agy_command", return_value=str(self.tmp / "missing" / "agy")):
            self.assertIsNone(usage.fetch_usage_report())

    def test_spawn_failure_returns_none(self):
        with patch("usage.subprocess.Popen", side_effect=PermissionError("denied")):
            self.assertIsNone(usage.run_agy(str(self.agy), usage.USAGE_ARGS, time.monotonic() + 5))

    def test_nonzero_exit_returns_none_even_with_parseable_output(self):
        os.environ["FAKE_AGY_MODE"] = "fail"
        self.assertIsNone(usage.fetch_usage_report())

    def test_model_turn_response_returns_none(self):
        self.write_response(response_with(num_turns=1))
        self.assertIsNone(usage.fetch_usage_report())

    def test_response_without_buckets_returns_none(self):
        self.write_response(response_with(command={"name": "usage", "data": {"groups": []}}))
        self.assertIsNone(usage.fetch_usage_report())

    def test_command_line_cwd_and_env(self):
        with patch("usage.subprocess.Popen", wraps=subprocess.Popen) as popen:
            self.assertIsNotNone(usage.fetch_usage_report())
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [str(self.agy), "-p", "/usage", "--output-format", "json", "--print-timeout", "6s"])
        self.assertNotIn("--disable-slash-commands", argv)
        for call in popen.call_args_list:
            self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["encoding"], "utf-8")

        version_line, usage_line = self.invocations()
        self.assertTrue(version_line.startswith("--version"))
        for line in (version_line, usage_line):
            self.assertIn("update=true", line)
            cwd = self.invocation_cwd(line)
            self.assertTrue(cwd.name.startswith("hermes-agy-usage-"))
            self.assertNotEqual(cwd.resolve(), Path.cwd().resolve())
            self.assertFalse(cwd.exists(), "private cwd must be removed after the run")

    def test_timeout_kills_process_tree_within_budget(self):
        os.environ["FAKE_AGY_MODE"] = "hang"
        started = time.monotonic()
        with patch("usage.USAGE_BUDGET_S", 1.5):
            self.assertIsNone(usage.fetch_usage_report())
        self.assertLess(time.monotonic() - started, 1.5 + usage.DRAIN_TIMEOUT_S + TIMING_SLACK_S)
        self.assert_processes_gone()
        self.assertFalse(self.invocation_cwd(self.usage_invocations()[0]).exists())

    def test_unkillable_survivor_holding_pipe_does_not_block_return(self):
        os.environ["FAKE_AGY_MODE"] = "orphan-pipe"
        self.addCleanup(self.kill_recorded_pids)
        started = time.monotonic()
        with patch("usage.USAGE_BUDGET_S", 1.5), patch("usage._windows_job_runner", return_value=None), \
             patch("usage._kill_agy_tree"):
            self.assertIsNone(usage.fetch_usage_report())
        self.assertLess(time.monotonic() - started, 1.5 + usage.DRAIN_TIMEOUT_S + TIMING_SLACK_S)

    def kill_recorded_pids(self):
        if not self.pids.exists():
            return
        for pid in self.pids.read_text(encoding="utf-8").split():
            try:
                os.kill(int(pid), 9)
            except OSError:
                pass

    def test_exited_leader_with_child_holding_pipe_is_killed(self):
        # The case taskkill /T cannot reach; on Windows only the Job Object runner handles it.
        if os.name == "nt" and usage._windows_job_runner() is None:
            self.skipTest("needs Hermes' Job Object runner (hermes_cli._subprocess_compat)")
        os.environ["FAKE_AGY_MODE"] = "orphan-pipe"
        self.addCleanup(self.kill_recorded_pids)
        started = time.monotonic()
        with patch("usage.USAGE_BUDGET_S", 1.5):
            self.assertIsNone(usage.fetch_usage_report())
        self.assertLess(time.monotonic() - started, 1.5 + usage.DRAIN_TIMEOUT_S + TIMING_SLACK_S)
        self.assert_processes_gone()


class WindowsJobRunnerTests(unittest.TestCase):
    """Which runner run_agy picks, and what it hands Hermes' Job Object runner."""

    def test_posix_never_uses_job_runner(self):
        with patch.object(usage.os, "name", "posix"):
            self.assertIsNone(usage._windows_job_runner())

    @staticmethod
    def current_runner(argv, *, timeout, errors="replace", env=None, cwd=None, raise_on_spawn_failure=False):
        """Signature of Hermes' bounded_probe_run from 0.21.4 on."""

    @staticmethod
    def pre_0214_runner(argv, *, timeout, errors="replace", env=None):
        """Signature of bounded_probe_run up to Hermes 0.21.3: no cwd."""

    def hermes_modules(self, runner=current_runner, *, compat=True, backend=True):
        module = ModuleType("hermes_cli._subprocess_compat")
        module.bounded_probe_run = runner
        return {
            "hermes_cli._subprocess_compat": module if compat else None,
            "hermes_cli.local_runtime.processes": ModuleType("hermes_cli.local_runtime.processes") if backend else None,
        }

    def test_windows_uses_hermes_runner_when_available(self):
        modules = self.hermes_modules()
        with patch.object(usage.os, "name", "nt"), patch.dict(sys.modules, modules):
            self.assertIs(usage._windows_job_runner(), modules["hermes_cli._subprocess_compat"].bounded_probe_run)

    def test_windows_without_hermes_runner_falls_back(self):
        with patch.object(usage.os, "name", "nt"), patch.dict(sys.modules, self.hermes_modules(compat=False)):
            self.assertIsNone(usage._windows_job_runner())

    def test_windows_runner_without_job_backend_falls_back(self):
        # e.g. psutil missing: the runner would import fine but fail every spawn.
        with patch.object(usage.os, "name", "nt"), patch.dict(sys.modules, self.hermes_modules(backend=False)):
            self.assertIsNone(usage._windows_job_runner())

    def test_windows_runner_with_incompatible_signature_falls_back(self):
        modules = self.hermes_modules(self.pre_0214_runner)
        with patch.object(usage.os, "name", "nt"), patch.dict(sys.modules, modules):
            self.assertIsNone(usage._windows_job_runner())

    def test_windows_backend_import_crash_falls_back(self):
        compat = self.hermes_modules()["hermes_cli._subprocess_compat"]
        with patch.object(usage.os, "name", "nt"), \
             patch("usage.importlib.import_module", side_effect=[compat, OSError("ctypes load failed")]):
            self.assertIsNone(usage._windows_job_runner())

    def test_run_agy_hands_argv_cwd_env_and_timeout_to_job_runner(self):
        calls = []

        def runner(argv, *, timeout, env, cwd):
            calls.append({"argv": argv, "timeout": timeout, "env": env, "cwd": Path(cwd), "cwd_existed": Path(cwd).is_dir()})
            return subprocess.CompletedProcess(argv, 0, "stdout", "")

        with patch("usage._windows_job_runner", return_value=runner), \
             patch("usage.subprocess.Popen", side_effect=AssertionError("bypassed the job runner")):
            self.assertEqual(usage.run_agy("agy", usage.USAGE_ARGS, time.monotonic() + 5), "stdout")
        (call,) = calls
        self.assertEqual(call["argv"], ["agy", *usage.USAGE_ARGS])
        self.assertTrue(0 < call["timeout"] <= 5)
        self.assertEqual(call["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        self.assertTrue(call["cwd_existed"])
        self.assertTrue(call["cwd"].name.startswith("hermes-agy-usage-"))
        self.assertFalse(call["cwd"].exists(), "private cwd must be removed after the run")

    def test_run_agy_rejects_job_runner_timeout_and_failure(self):
        for result in (None, subprocess.CompletedProcess(["agy"], 1, "stdout", "")):
            with self.subTest(result=result), patch("usage._windows_job_runner", return_value=lambda *a, **k: result):
                self.assertIsNone(usage.run_agy("agy", usage.USAGE_ARGS, time.monotonic() + 5))

    def test_exhausted_budget_never_reaches_job_runner(self):
        runner = MagicMock()
        with patch("usage._windows_job_runner", return_value=runner):
            self.assertIsNone(usage.run_agy("agy", usage.USAGE_ARGS, time.monotonic() - 1))
        runner.assert_not_called()


class WindowsKillPathTests(unittest.TestCase):
    """The nt branches, exercised on any host by faking os.name and the process handle."""

    def exited_leader(self):
        proc = MagicMock()
        proc.pid = 4242
        proc.poll.return_value = 0
        return proc

    def test_taskkill_runs_with_timeout_even_after_leader_exited(self):
        with patch.object(usage.os, "name", "nt"), patch("usage.subprocess.run") as run:
            usage._kill_agy_tree(self.exited_leader())
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["taskkill", "/F", "/T", "/PID", "4242"])
        self.assertEqual(run.call_args.kwargs["timeout"], usage.DRAIN_TIMEOUT_S)

    def test_taskkill_failure_is_swallowed(self):
        for error in (OSError("no taskkill"), subprocess.TimeoutExpired("taskkill", 1)):
            with self.subTest(error=error), patch.object(usage.os, "name", "nt"), \
                 patch("usage.subprocess.run", side_effect=error):
                usage._kill_agy_tree(self.exited_leader())

    def test_drain_never_closes_pipe_a_reader_thread_holds(self):
        proc = self.exited_leader()
        proc.communicate.side_effect = subprocess.TimeoutExpired("agy", usage.DRAIN_TIMEOUT_S)
        with patch.object(usage.os, "name", "nt"):
            usage._drain(proc)
        proc.stdout.close.assert_not_called()

    def test_drain_closes_pipe_on_posix(self):
        proc = self.exited_leader()
        proc.communicate.side_effect = subprocess.TimeoutExpired("agy", usage.DRAIN_TIMEOUT_S)
        with patch.object(usage.os, "name", "posix"):
            usage._drain(proc)
        proc.stdout.close.assert_called_once()


def _pid_alive(pid):
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed grandchild lingers as a zombie until init reaps it; that is not alive.
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                               check=False).stdout.strip()
    except OSError:
        return True
    return bool(state) and not state.startswith("Z")


def _windows_pid_alive(pid):
    # os.kill(pid, 0) is not a liveness probe on Windows: any signal but the CTRL events means
    # TerminateProcess. Ask for the exit code instead.
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87  # OpenProcess's answer for a pid that no longer exists
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied and the like mean it exists; only a vanished pid counts as dead.
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _hermes_account_usage():
    """Hermes' real account-usage module, or None where its runtime deps are absent (plugin CI)."""
    try:
        import agent.account_usage as module
    except ImportError:
        return None
    return module


def _hermes_hook_deadline_s():
    """PLUGIN_USAGE_HOOK_DEADLINE_S read from Hermes' source, so plugin CI needs no Hermes deps."""
    import ast
    import importlib.util

    spec = importlib.util.find_spec("agent")
    assert spec and spec.submodule_search_locations  # noqa: S101 — Hermes must be on sys.path
    source = Path(next(iter(spec.submodule_search_locations)), "account_usage.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PLUGIN_USAGE_HOOK_DEADLINE_S" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("PLUGIN_USAGE_HOOK_DEADLINE_S not found in agent/account_usage.py")


def _stub_account_usage():
    # Mirrors the two dataclasses the hook constructs, so the adapter is still exercised when
    # Hermes' runtime deps (httpx, ruamel.yaml) are not installed.
    @dataclasses.dataclass(frozen=True)
    class AccountUsageWindow:
        label: str
        used_percent: float | None = None
        reset_at: datetime | None = None
        detail: str | None = None

    @dataclasses.dataclass(frozen=True)
    class AccountUsageSnapshot:
        provider: str
        source: str
        fetched_at: datetime
        windows: tuple = ()
        raw: dict | None = None

    module = ModuleType("agent.account_usage")
    module.AccountUsageWindow = AccountUsageWindow
    module.AccountUsageSnapshot = AccountUsageSnapshot
    return module


class ProfileHookTests(unittest.TestCase):
    PACKAGE = "antigravity_usage_entry"

    @classmethod
    def setUpClass(cls):
        # Load the package entry the way Hermes does, so `from .usage import ...` resolves.
        import importlib
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            cls.PACKAGE, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
        )
        assert spec and spec.loader  # noqa: S101 — test bootstrap, not production
        cls.package = importlib.util.module_from_spec(spec)
        sys.modules[cls.PACKAGE] = cls.package
        cls.addClassCleanup(cls._unload_package)
        spec.loader.exec_module(cls.package)
        cls.profile = cls.package.antigravity_profile
        cls.package_usage = importlib.import_module(f"{cls.PACKAGE}.usage")

    @classmethod
    def _unload_package(cls):
        for name in [name for name in sys.modules if name == cls.PACKAGE or name.startswith(cls.PACKAGE + ".")]:
            del sys.modules[name]

    def setUp(self):
        if _hermes_account_usage() is None:
            patcher = patch.dict(sys.modules, {"agent.account_usage": _stub_account_usage()})
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_hook_maps_report_onto_hermes_snapshot(self):
        report = live_report()
        with patch.object(self.package_usage, "fetch_usage_report", return_value=report):
            snapshot = self.profile.fetch_account_usage()

        from agent.account_usage import AccountUsageWindow

        self.assertEqual(snapshot.provider, "antigravity-oauth")
        self.assertEqual(snapshot.source, "agy_usage")
        self.assertEqual(snapshot.fetched_at.tzinfo, timezone.utc)
        self.assertEqual(snapshot.raw, report.raw)
        self.assertTrue(all(isinstance(w, AccountUsageWindow) for w in snapshot.windows))
        self.assertEqual(
            [(w.label, w.used_percent, w.reset_at, w.detail) for w in snapshot.windows],
            [tuple(w) for w in report.windows],
        )

    def test_hook_returns_none_without_report(self):
        with patch.object(self.package_usage, "fetch_usage_report", return_value=None):
            self.assertIsNone(self.profile.fetch_account_usage())

    @unittest.skipIf(_hermes_account_usage() is None, "needs Hermes runtime deps (httpx, ruamel.yaml)")
    def test_hermes_dispatcher_reaches_hook(self):
        from agent.account_usage import fetch_account_usage

        with patch("providers.get_provider_profile", return_value=self.profile), \
             patch.object(self.package_usage, "fetch_usage_report", return_value=live_report()):
            result = fetch_account_usage("antigravity-oauth")
        self.assertIsNotNone(result)
        self.assertTrue(result.available)
        self.assertEqual([w.label for w in result.windows], ["Gemini 7d", "Claude/GPT 7d"])

    def test_budget_kill_and_drain_end_before_hermes_hook_deadline(self):
        worst_case_s = usage.USAGE_BUDGET_S + 2 * usage.DRAIN_TIMEOUT_S  # taskkill, then drain
        self.assertLess(worst_case_s, _hermes_hook_deadline_s())


def live_report():
    data = LIVE_RESPONSE["command"]["data"]
    return usage.UsageReport(windows=tuple(usage.usage_windows(data)), raw=LIVE_RESPONSE)


if __name__ == "__main__":
    unittest.main()
