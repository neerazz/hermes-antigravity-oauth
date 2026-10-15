"""agy-native tool steps become Hermes tool calls; unmappable ones stay neutralized."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

import native_tools  # noqa: E402
from client import AntigravityClient  # noqa: E402

TERMINAL_TOOL = [{"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}]


def _step(name, params, idx=2):
    return {"step_index": idx, "state": "ACTIVE", "step_type": "tool", "tool_name": name,
            "tool_info": {"name": name, "parameters": params}}


class TranslateTests(unittest.TestCase):
    def test_run_command_maps_to_terminal(self):
        call = native_tools.translate(_step("run_command", {"CommandLine": "echo hi", "Cwd": "/w"}), {"terminal"}, 0)
        self.assertEqual(call.function.name, "terminal")
        self.assertEqual(json.loads(call.function.arguments), {"command": "echo hi", "workdir": "/w"})
        self.assertEqual(call.id, "agy_2_run_command")

    def test_unavailable_hermes_tool_is_not_mapped(self):
        self.assertIsNone(native_tools.translate(_step("run_command", {"CommandLine": "x"}), {"read_file"}, 0))

    def test_unknown_native_tool_is_not_mapped(self):
        self.assertIsNone(native_tools.translate(_step("browser_click_element", {"x": 1}), {"terminal"}, 0))

    def test_missing_parameters_is_not_mapped(self):
        self.assertIsNone(native_tools.translate({"step_type": "tool", "tool_name": "run_command"}, {"terminal"}, 0))

    def test_view_file_and_search(self):
        c = native_tools.translate(_step("view_file", {"AbsolutePath": "/a.py", "StartLine": 5}), {"read_file"}, 0)
        self.assertEqual(json.loads(c.function.arguments), {"path": "/a.py", "offset": 5})
        c = native_tools.translate(_step("grep_search", {"Query": "foo", "SearchPath": "/r"}), {"search_files"}, 0)
        self.assertEqual(json.loads(c.function.arguments), {"pattern": "foo", "target": "content", "path": "/r"})


class StreamBridgeTests(unittest.TestCase):
    def _run(self, events, tools):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = AntigravityClient(cwd=tmp.name)
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout.readline.side_effect = [json.dumps(e) + "\n" for e in events] + [""] * 50
        proc.stdin = MagicMock()
        # Hermetic: never probe the host keyring (on Linux that probe itself goes through Popen).
        with patch("client.is_authenticated", return_value=True), \
                patch("subprocess.Popen", return_value=proc), patch.object(client, "_terminate_process") as term:
            chunks = list(client.chat.completions.create(model="claude-sonnet-4-6", tools=tools, stream=True,
                                                         messages=[{"role": "user", "content": "run it"}]))
        return chunks, term, proc

    def test_native_run_command_is_reissued_as_hermes_terminal_call(self):
        events = [{"event": "init", "conversation_id": "c"},
                  {"event": "step_update", "step_update": _step("run_command", {"CommandLine": "echo SOUL_42"})}]
        chunks, term, proc = self._run(events, TERMINAL_TOOL)
        term.assert_called_once_with(proc)  # agy never gets to execute it
        calls = [tc for ch in chunks for tc in (ch.choices[0].delta.tool_calls or [])]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "terminal")
        self.assertEqual(json.loads(calls[0].function.arguments)["command"], "echo SOUL_42")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "tool_calls")

    def test_unmappable_native_tool_still_neutralized_without_call(self):
        events = [{"event": "step_update", "step_update": _step("browser_click_element", {"x": 1})}]
        chunks, term, proc = self._run(events, TERMINAL_TOOL)
        term.assert_called_once_with(proc)
        calls = [tc for ch in chunks for tc in (ch.choices[0].delta.tool_calls or [])]
        self.assertEqual(calls, [])


def _transcript(root, conversation_id, tool_calls):
    logs = Path(root) / "brain" / conversation_id / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    lines = [json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "x"}),
             json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "tool_calls": tool_calls})]
    (logs / "transcript_full.jsonl").write_text("\n".join(lines) + "\n")


class TruncatedPreviewTests(unittest.TestCase):
    """agy stream events cut string params to 512 chars + an ellipsis (live-verified 2026-10-12)."""

    FULL = "python3 -c \"" + "print('x')\n" * 120 + "print('END')\""

    def preview(self):
        return self.FULL[:512] + native_tools.ELLIPSIS

    def test_preview_is_detected_only_at_the_agy_limit(self):
        self.assertEqual(native_tools.truncated_keys({"CommandLine": self.preview(), "Cwd": "/tmp"}), ["CommandLine"])
        self.assertEqual(native_tools.truncated_keys({"CommandLine": "echo hi" + native_tools.ELLIPSIS}), [])

    def test_full_command_is_restored_from_the_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            _transcript(d, "conv1", [{"name": "run_command", "args": {"CommandLine": self.FULL, "Cwd": "/tmp"}}])
            full = native_tools.restore_truncated({"CommandLine": self.preview(), "Cwd": "/tmp"},
                                                  "run_command", "conv1", [Path(d)], wait=0)
        self.assertEqual(full["CommandLine"], self.FULL)

    def test_mismatching_transcript_or_missing_file_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            _transcript(d, "conv1", [{"name": "run_command", "args": {"CommandLine": "rm -rf /tmp/other " + "x" * 600}}])
            self.assertIsNone(native_tools.restore_truncated({"CommandLine": self.preview()}, "run_command",
                                                             "conv1", [Path(d)], wait=0))
            self.assertIsNone(native_tools.restore_truncated({"CommandLine": self.preview()}, "run_command",
                                                             "nope", [Path(d)], wait=0))

    def test_translate_never_emits_a_cut_command(self):
        step = _step("run_command", {"CommandLine": self.preview(), "Cwd": "/tmp"})
        self.assertIsNone(native_tools.translate(step, {"terminal"}, 0))
        self.assertIsNone(native_tools.translate(step, {"terminal"}, 0, resolve=lambda p, n: None))
        call = native_tools.translate(step, {"terminal"}, 0,
                                      resolve=lambda p, n: dict(p, CommandLine=self.FULL))
        self.assertEqual(json.loads(call.function.arguments)["command"], self.FULL)

    def test_transcript_written_slightly_after_the_event_is_waited_for(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            timer = threading.Timer(0.3, _transcript, args=(d, "conv2",
                                    [{"name": "run_command", "args": {"CommandLine": self.FULL}}]))
            timer.start()
            full = native_tools.restore_truncated({"CommandLine": self.preview()}, "run_command",
                                                  "conv2", [Path(d)], wait=2)
            timer.join()
        self.assertEqual(full["CommandLine"], self.FULL)


if __name__ == "__main__":
    unittest.main()
