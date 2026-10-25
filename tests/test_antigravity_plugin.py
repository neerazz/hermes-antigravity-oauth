import contextlib
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

# Add plugin parent dir to sys.path
plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

try:
    from providers import get_provider_profile
except ImportError:
    # Skip tests if Hermes Agent checkout is not in PYTHONPATH
    raise unittest.SkipTest("Skipping test_antigravity_plugin.py: 'providers' module from Hermes Agent not found in PYTHONPATH.")

from client import (
    AntigravityClient,
    AntigravityStream,
    _format_messages_as_prompt,
    _render_message_content,
    is_authenticated,
    resolve_agy_command,
)
from process import _check_early_quota_error
from stream import _is_output_limit_error, _result_reports_output_limit

# Load the plugin's package entry as a real package (not a plain module) so its
# `from .client import ...` relative imports resolve. Hermes itself discovers a
# provider plugin only via $HERMES_HOME/plugins/model-providers/, so a suite
# run against a bare source tree would otherwise never call register_provider()
# and every get_provider_profile() lookup would return None.
import importlib.util as _ilu

_pkg_spec = _ilu.spec_from_file_location(
    "antigravity_plugin_entry",
    plugin_dir / "__init__.py",
    submodule_search_locations=[str(plugin_dir)],
)
assert _pkg_spec and _pkg_spec.loader  # noqa: S101 — test bootstrap, not production
_pkg = _ilu.module_from_spec(_pkg_spec)
sys.modules["antigravity_plugin_entry"] = _pkg
_pkg_spec.loader.exec_module(_pkg)

# How far past the real clock a patched ``time.monotonic`` reports, so a
# stream deadline expires without any wall-clock wait: the no-result tests
# below expire the generator's read loop deterministically instead of
# relying on a short timeout plus the loop's sleep polling.
_CLOCK_JUMP_SECONDS = 10 ** 6


@contextlib.contextmanager
def _mock_inherited_config(effort):
    # Keep offline provider tests independent of Hermes runtime dependencies.
    config = ModuleType("hermes_cli.config")
    load_config = MagicMock(return_value={"agent": {"reasoning_effort": effort}})
    setattr(config, "load_config_readonly", load_config)
    with patch.dict(sys.modules, {"hermes_cli.config": config}):
        yield load_config


class AntigravityPluginTests(unittest.TestCase):
    def setUp(self):
        # The suite must not depend on host state. Three host facts leak in
        # otherwise: a real ~/.gemini auth token (present on a dev box, absent
        # on CI), the resolved agy binary, whose candidate scan stats paths
        # under another account's home, and on macOS the tester's real
        # ~/Library/Keychains, which setup_isolated_home() would link into the
        # throwaway home. Pin all three for every test.
        patcher_auth = patch("client.is_authenticated", return_value=True)
        patcher_token = patch("client.resolve_real_token_path", return_value=None)
        patcher_cmd = patch("client.resolve_agy_command", return_value="agy")
        patcher_keychains = patch("process._link_macos_keychains")
        patcher_auth.start()
        patcher_token.start()
        patcher_cmd.start()
        patcher_keychains.start()
        self.addCleanup(patcher_auth.stop)
        self.addCleanup(patcher_token.stop)
        self.addCleanup(patcher_cmd.stop)
        self.addCleanup(patcher_keychains.stop)

    @staticmethod
    def _write_token(tmp_dir: str) -> Path:
        token = Path(tmp_dir) / "antigravity-oauth-token"
        token.write_text("token-content-123456", encoding="utf-8")
        return token
    def test_provider_registration(self):
        profile = get_provider_profile("antigravity-oauth")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "antigravity-oauth")
        # Distinct aliases so this plugin can coexist with antigravity-subscription-directsdk.
        self.assertIn("agy-oauth", profile.aliases)
        self.assertIn("google-antigravity", profile.aliases)
        self.assertIsNotNone(profile.auth_handler)
        self.assertEqual(profile.auth_type, "external_process")
        self.assertEqual(profile.api_mode, "chat_completions")

    def test_alias_lookup(self):
        profile = get_provider_profile("google-antigravity")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "antigravity-oauth")

    def test_command_resolution(self):
        # Deterministic: never touch the host (env/PATH/filesystem). Force
        # candidate fallback and verify the per-OS executable name. The OS
        # name is faked only inside the process module (wraps the real os
        # module) so pathlib's global os.name check keeps working.
        from process import resolve_agy_command as real_resolve
        env_clear = {"ANTIGRAVITY_COMMAND": "", "AGY_CLI_PATH": "", "ANTIGRAVITY_CLI_PATH": ""}
        with patch.dict(os.environ, env_clear, clear=False):
            with patch("process.shutil.which", return_value=None), patch(
                "process._is_existing_file", return_value=True
            ), patch("process.os", wraps=os) as mock_os:
                mock_os.access = lambda *args, **kwargs: True
                mock_os.name = "posix"
                cmd = real_resolve()
                self.assertTrue(cmd.endswith("agy"))
                self.assertFalse(cmd.endswith("agy.exe"))
                mock_os.name = "nt"
                cmd = real_resolve()
                self.assertTrue(cmd.endswith("agy.exe"))

    def test_command_resolution_env_var_returns_exact_path(self):
        # When ANTIGRAVITY_COMMAND points at an existing executable file it
        # must be returned verbatim, short-circuiting PATH and candidate
        # scan. Deterministic: real temp file, no host dependence.
        from process import resolve_agy_command as real_resolve

        with tempfile.TemporaryDirectory() as tmp:
            agy_bin = Path(tmp) / "agy-custom-bin"
            agy_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            if os.name != "nt":
                os.chmod(agy_bin, 0o755)
            env = {
                "ANTIGRAVITY_COMMAND": str(agy_bin),
                "AGY_CLI_PATH": "",
                "ANTIGRAVITY_CLI_PATH": "",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "process.shutil.which",
                side_effect=AssertionError("env var must win before the PATH scan"),
            ):
                self.assertEqual(real_resolve(), str(agy_bin))

    def test_command_resolution_env_var_skipped_when_not_executable(self):
        # The env var only wins if it points at an executable file. A
        # non-executable file must fall through to the rest of resolution
        # (the POSIX X_OK gate; os.access is pinned inside the process
        # module so the outcome cannot vary by host/root).
        from process import resolve_agy_command as real_resolve

        with tempfile.TemporaryDirectory() as tmp:
            plain_file = Path(tmp) / "not-executable"
            plain_file.write_text("dummy", encoding="utf-8")
            env = {
                "ANTIGRAVITY_COMMAND": str(plain_file),
                "AGY_CLI_PATH": "",
                "ANTIGRAVITY_CLI_PATH": "",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "process.shutil.which", return_value="/fake/agy-from-path"
            ), patch("process.os", wraps=os) as mock_os:
                mock_os.access = lambda *args, **kwargs: False
                mock_os.name = "posix"
                self.assertEqual(real_resolve(), "/fake/agy-from-path")

    def test_auth_check(self):
        # Auth must be determined by the token directory, not by host state.
        # Exercise both ends of the gate with a real temp token file: absent
        # (empty dir, the CI shape) and present (a token file, the dev shape).
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ANTIGRAVITY_CONFIG_DIR": tmp}, clear=False):
                self.assertFalse(is_authenticated())
        with tempfile.TemporaryDirectory() as tmp:
            self._write_token(tmp)
            with patch.dict(os.environ, {"ANTIGRAVITY_CONFIG_DIR": tmp}, clear=False):
                self.assertTrue(is_authenticated())

    def _windows_without_token_file(self, home):
        # Windows, no token file and no ANTIGRAVITY_CONFIG_DIR. The OS name
        # is faked only inside the process module (wraps the real os module)
        # so pathlib's global os.name check keeps working.
        env = {k: v for k, v in os.environ.items() if k != "ANTIGRAVITY_CONFIG_DIR"}

        @contextlib.contextmanager
        def fake_nt():
            with patch("process.os", wraps=os) as mock_os:
                mock_os.name = "nt"
                yield

        return (patch.dict(os.environ, env, clear=True), patch("process.Path.home", return_value=Path(home)),
                patch("process._is_existing_file", return_value=False), fake_nt())

    def test_auth_check_windows_credential_manager(self):
        # agy on Windows keeps its session in the Credential Manager, not in a
        # token file. Only the entry's presence is checked; the secret is never read.
        listed = SimpleNamespace(stdout=b"    Target: LegacyGeneric:target=gemini:antigravity\r\n", returncode=0)
        cases = [
            (listed, True),
            (SimpleNamespace(stdout=b"Target: LegacyGeneric:target=Gemini:Antigravity\r\n", returncode=0), True),
            # Bytes undefined in cp1252 around the target: no decoding happens, so nothing can raise.
            (SimpleNamespace(stdout=b"User: \x81\x8d\x90\r\n Target: gemini:antigravity\r\n", returncode=0), True),
            (SimpleNamespace(stdout=b"* NONE *\r\n", returncode=0), False),
            # A failing cmdkey can echo the requested target in its diagnostics: fail closed.
            (SimpleNamespace(stdout=b"gemini:antigravity: element not found\r\n", returncode=1), False),
            (subprocess.TimeoutExpired(["cmdkey"], 3), False),
            (FileNotFoundError(), False),
        ]
        for result, expected in cases:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as home, \
                    contextlib.ExitStack() as stack:
                for ctx in self._windows_without_token_file(home):
                    stack.enter_context(ctx)
                kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch("process.subprocess.run", **kwargs) as run:
                    self.assertIs(is_authenticated(), expected)
                    self.assertEqual(run.call_args.args[0], ["cmdkey", "/list:gemini:antigravity"])
                    call = run.call_args.kwargs
                    # Output is captured as bytes: no text mode, no encoding to get wrong.
                    self.assertFalse(call.get("text") or call.get("encoding") or call.get("universal_newlines"))
                    self.assertLessEqual(call.get("timeout"), 3)

    def test_auth_check_explicit_dir_wins_over_windows_credential(self):
        # ANTIGRAVITY_CONFIG_DIR is an override: an empty explicit dir stays
        # unauthenticated even when the Credential Manager has an entry.
        listed = SimpleNamespace(stdout=b"Target: LegacyGeneric:target=gemini:antigravity\r\n", returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            # The OS name is faked only inside the process module (wraps the
            # real os module) so pathlib's global os.name check keeps working.
            with patch.dict(os.environ, {"ANTIGRAVITY_CONFIG_DIR": tmp}, clear=False), \
                    patch("process.os", wraps=os) as mock_os, \
                    patch("process.subprocess.run", return_value=listed) as run:
                mock_os.name = "nt"
                self.assertFalse(is_authenticated())
                run.assert_not_called()

    def test_format_messages_prompt(self):
        messages = [
            {"role": "system", "content": "You are a helpful coding assistant."},
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "What is the weather?"},
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for city",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]
        prompt = _format_messages_as_prompt(messages, tools=tools)
        self.assertIn("Conversation transcript:", prompt)
        self.assertIn("System:\nYou are a helpful coding assistant.", prompt)
        self.assertIn("User:\nHello!", prompt)
        self.assertIn("Assistant:\nHi there!", prompt)
        self.assertIn("User:\nWhat is the weather?", prompt)
        self.assertIn("get_weather", prompt)

    def test_format_messages_with_tool_call_and_result(self):
        messages = [
            {"role": "user", "content": "Run calculator"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expr": "2+2"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_123", "name": "calculator", "content": "4"},
        ]
        prompt = _format_messages_as_prompt(messages)
        self.assertIn("<tool_call>", prompt)
        self.assertIn("call_123", prompt)
        self.assertIn("Tool Result (call_123):\n4", prompt)

    def test_format_messages_strips_hallucinated_tool_results_and_emphasizes_user_query(self):
        corrupted_messages = [
            {"role": "user", "content": "Initial query"},
            {
                "role": "assistant",
                "content": "Tool Result (call_1):\nfake data\nHere is fake summary",
                "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "real data"},
            {"role": "assistant", "content": "Real summary"},
            {"role": "user", "content": "Why is this not following thread?"},
        ]
        prompt = _format_messages_as_prompt(corrupted_messages)
        # Verify hallucinated tool result in assistant content was stripped
        self.assertNotIn("fake data", prompt)
        self.assertNotIn("Here is fake summary", prompt)
        # Verify real tool result is present
        self.assertIn("Tool Result (call_1):\nreal data", prompt)
        # Verify latest user request is highlighted at tail
        self.assertIn("### LATEST USER REQUEST TO ANSWER:\nUser:\nWhy is this not following thread?", prompt)
        self.assertIn("Do NOT repeat previous architectural summaries", prompt)

    def test_format_messages_latest_tool_results(self):
        messages = [
            {"role": "user", "content": "Do work"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result 1"},
        ]
        prompt = _format_messages_as_prompt(messages)
        self.assertIn("### LATEST TOOL RESULTS RECEIVED.", prompt)
        self.assertIn("Tool Result (c1):\nresult 1", prompt)

    def test_format_delta_prompt_user_vs_tool(self):
        from client import _format_delta_prompt
        # Delta ending with user
        user_delta = [{"role": "user", "content": "new user query"}]
        d_prompt = _format_delta_prompt(user_delta)
        self.assertIn("Respond directly and specifically to the latest user request above.", d_prompt)

        # Delta ending with tool
        tool_delta = [{"role": "tool", "tool_call_id": "t1", "content": "output"}]
        d_prompt2 = _format_delta_prompt(tool_delta)
        self.assertIn("Continue the conversation from the latest tool result.", d_prompt2)

    def test_format_messages_historical_tool_pruning(self):
        messages = [{"role": "user", "content": "Start session"}]
        # Create 12 tool results, each 1000 characters
        for idx in range(12):
            call_id = f"c_{idx}"
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": call_id, "function": {"name": "test_tool", "arguments": "{}"}}],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"output_{idx}_" + ("X" * 1000),
            })
        messages.append({"role": "user", "content": "What is the status?"})

        prompt = _format_messages_as_prompt(messages)
        # Older tools (indices 0 to 3) should be truncated
        for idx in range(4):
            self.assertIn(f"Tool Result (c_{idx}):", prompt)
            self.assertIn("[... tool output truncated:", prompt)

        # Recent tools (the last 8, indices 4 to 11) should remain intact (1000 X's)
        for idx in range(4, 12):
            self.assertIn(f"Tool Result (c_{idx}):\noutput_{idx}_" + ("X" * 1000), prompt)

    def test_format_messages_single_large_tool_capped(self):
        messages = [
            {"role": "user", "content": "Run big dump"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c_big", "function": {"name": "big", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c_big", "content": "START" + ("Y" * 25000) + "END"},
            {"role": "user", "content": "Analyze"},
        ]
        prompt = _format_messages_as_prompt(messages)
        self.assertIn("[... large tool output truncated: 25008 chars ...]", prompt)
        self.assertIn("START", prompt)
        self.assertIn("END", prompt)

    def test_format_delta_prompt_large_tool_capped(self):
        from client import _format_delta_prompt
        tool_delta = [{"role": "tool", "tool_call_id": "t_big", "content": "HEAD" + ("Z" * 25000) + "TAIL"}]
        d_prompt = _format_delta_prompt(tool_delta)
        self.assertIn("[... large tool output truncated: 25008 chars ...]", d_prompt)
        self.assertIn("HEAD", d_prompt)
        self.assertIn("TAIL", d_prompt)

    def test_format_messages_budget_preserves_latest_user_request_and_evicts_oldest(self):
        messages: list[dict[str, object]] = [{"role": "user", "content": "Turn 1 initial modeling request"}]
        for idx in range(40):
            call_id = f"call_blender_{idx}"
            messages.append({
                "role": "assistant",
                "content": f"Step {idx} reasoning",
                "tool_calls": [{
                    "id": call_id,
                    "function": {
                        "name": "mcp__blender__execute_blender_code",
                        "arguments": json.dumps({"code": f"# script_{idx}\n" + ("A" * 6000)}),
                    },
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"done_{idx}",
            })
        messages.append({"role": "assistant", "content": "Turn 1 final summary: 79 meshes complete."})
        latest_req = "Thanks, that works. Next, improve the material contrast on the remaining meshes first."
        messages.append({"role": "user", "content": latest_req})

        prompt = _format_messages_as_prompt(messages)
        self.assertLessEqual(len(prompt), 150000)
        self.assertLessEqual(len(prompt.encode("utf-8")), 160000)
        self.assertIn("### LATEST USER REQUEST TO ANSWER:", prompt)
        self.assertIn(latest_req, prompt)
        self.assertIn("Turn 1 final summary: 79 meshes complete.", prompt)
        self.assertIn("truncated 60", prompt)

        tight_prompt = _format_messages_as_prompt(messages, max_prompt_chars=40000, max_prompt_bytes=45000)
        self.assertLessEqual(len(tight_prompt), 40000)
        self.assertLessEqual(len(tight_prompt.encode("utf-8")), 45000)
        self.assertIn("### LATEST USER REQUEST TO ANSWER:", tight_prompt)
        self.assertIn(latest_req, tight_prompt)
        self.assertIn("Turn 1 final summary: 79 meshes complete.", tight_prompt)
        self.assertIn("earlier transcript messages omitted", tight_prompt)

    def test_format_messages_budget_pins_active_user_request_on_mid_turn_tool_respawn(self):
        active_user_req = "Improve the highlight and contrast of the remaining meshes first"
        messages: list[dict[str, object]] = [
            {"role": "user", "content": "Turn 1 old request"},
            {"role": "assistant", "content": "Turn 1 done"},
            {"role": "user", "content": active_user_req},
        ]
        for idx in range(15):
            cid = f"call_t2_{idx}"
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": cid, "function": {"name": "exec", "arguments": json.dumps({"code": "B" * 4000})}}],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": cid,
                "content": f"tool_out_{idx}_" + ("C" * 4000),
            })

        prompt = _format_messages_as_prompt(messages, max_prompt_chars=25000, max_prompt_bytes=28000)
        self.assertLessEqual(len(prompt), 25000)
        self.assertLessEqual(len(prompt.encode("utf-8")), 28000)
        self.assertIn(active_user_req, prompt)
        self.assertIn("### LATEST TOOL RESULTS RECEIVED.", prompt)
        self.assertIn("tool_out_14_", prompt)

    def test_mock_stream_tool_call_suppresses_trailing_hallucination(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        tool_call_obj = {"id": "call_stream_1", "type": "function", "function": {"name": "stream_tool", "arguments": json.dumps({"q": 42})}}
        fake_events = [
            json.dumps({"event": "init", "conversation_id": "stream-tool-hallucination"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": f"<tool_call>{json.dumps(tool_call_obj)}</tool_call>"}}),
            # Model continues generating hallucinated tool output in the same turn
            json.dumps({"event": "step_update", "step_update": {"text_delta": "\nTool Result (call_stream_1):\n{\"fake\": true}\nI am already done!"}}),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": f"<tool_call>{json.dumps(tool_call_obj)}</tool_call>\nTool Result (call_stream_1):\n{{\"fake\": true}}\nI am already done!",
                    "usage": {"input_tokens": 80, "output_tokens": 50, "total_tokens": 130},
                },
            }),
        ]

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = io.StringIO("\n".join(fake_events) + "\n")
        mock_proc.stderr = io.StringIO("")
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0

        tools = [{"type": "function", "function": {"name": "stream_tool"}}]

        with patch("subprocess.Popen", return_value=mock_proc):
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "call stream_tool"}],
                tools=tools,
                stream=True,
            )

            chunks = list(stream)
            # Verify tool chunk is yielded
            tool_chunks = [c for c in chunks if c.choices and c.choices[0].delta.tool_calls]
            self.assertEqual(len(tool_chunks), 1)
            self.assertEqual(tool_chunks[0].choices[0].delta.tool_calls[0].id, "call_stream_1")

            # Verify finish reason is tool_calls
            finish_chunks = [c for c in chunks if c.choices and c.choices[0].finish_reason]
            self.assertEqual(finish_chunks[0].choices[0].finish_reason, "tool_calls")

            # Verify hallucinated content after tool_call was NOT yielded in any chunk
            content_chunks = [c for c in chunks if c.choices and c.choices[0].delta.content]
            self.assertEqual(len(content_chunks), 0)

    def test_create_client_and_mock_turn(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        fake_events = [
            json.dumps({"event": "init", "conversation_id": "test-conv-1"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "Hello from mock"}}),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Hello from mock\n",
                    "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cache_read_tokens": 0},
                },
            }),
        ]

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = io.StringIO("\n".join(fake_events) + "\n")
        mock_proc.stderr = io.StringIO("")
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            res = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "Hello"}],
                stream=False,
            )

            self.assertEqual(res.id, "test-conv-1")
            self.assertEqual(res.choices[0].message.content, "Hello from mock")
            self.assertEqual(res.choices[0].finish_reason, "stop")
            self.assertEqual(res.usage.prompt_tokens, 100)
            self.assertEqual(res.usage.completion_tokens, 10)
            self.assertEqual(res.usage.total_tokens, 110)

    def test_mock_tool_call_turn(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        tool_call_obj = {"id": "call_99", "type": "function", "function": {"name": "test_tool", "arguments": json.dumps({"arg": 1})}}
        tool_call_body = f"<tool_call>{json.dumps(tool_call_obj)}</tool_call>"
        fake_events = [
            json.dumps({"event": "init", "conversation_id": "test-tool-conv"}),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": tool_call_body,
                    "usage": {"input_tokens": 200, "output_tokens": 50, "total_tokens": 250},
                },
            }),
        ]

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = io.StringIO("\n".join(fake_events) + "\n")
        mock_proc.stderr = io.StringIO("")
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            res = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "call test_tool"}],
                stream=False,
            )

            self.assertEqual(res.choices[0].finish_reason, "tool_calls")
            self.assertIsNone(res.choices[0].message.content)
            self.assertEqual(len(res.choices[0].message.tool_calls), 1)
            tc = res.choices[0].message.tool_calls[0]
            self.assertEqual(tc.id, "call_99")
            self.assertEqual(tc.function.name, "test_tool")
            self.assertEqual(json.loads(tc.function.arguments), {"arg": 1})

    def test_mock_stream_turn(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        fake_events = [
            json.dumps({"event": "init", "conversation_id": "stream-conv-1"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "Hello "}}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "stream!"}}),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Hello stream!\n",
                    "usage": {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55},
                },
            }),
        ]

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = io.StringIO("\n".join(fake_events) + "\n")
        mock_proc.stderr = io.StringIO("")
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "Hello"}],
                stream=True,
            )

            chunks = list(stream)
            # Should have: delta("Hello "), delta("stream!"), finish("stop"), usage
            self.assertGreaterEqual(len(chunks), 4)

            # First data chunk
            self.assertEqual(chunks[0].choices[0].delta.content, "Hello ")
            self.assertIsNone(chunks[0].choices[0].finish_reason)

            # Second data chunk
            self.assertEqual(chunks[1].choices[0].delta.content, "stream!")
            self.assertIsNone(chunks[1].choices[0].finish_reason)

            # Finish chunk
            self.assertEqual(chunks[2].choices[0].finish_reason, "stop")

            # Usage chunk
            self.assertEqual(chunks[3].choices, [])
            self.assertEqual(chunks[3].usage.total_tokens, 55)

    def test_mock_stream_tool_call(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        tool_call_obj = {"id": "call_stream_1", "type": "function", "function": {"name": "stream_tool", "arguments": json.dumps({"q": 42})}}
        fake_events = [
            json.dumps({"event": "init", "conversation_id": "stream-tool-conv"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": f"<tool_call>{json.dumps(tool_call_obj)}</tool_call>"}}),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": f"<tool_call>{json.dumps(tool_call_obj)}</tool_call>",
                    "usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
                },
            }),
        ]

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = io.StringIO("\n".join(fake_events) + "\n")
        mock_proc.stderr = io.StringIO("")
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0

        tools = [{"type": "function", "function": {"name": "stream_tool"}}]

        with patch("subprocess.Popen", return_value=mock_proc):
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "call stream_tool"}],
                tools=tools,
                stream=True,
            )

            chunks = list(stream)
            tool_chunks = [c for c in chunks if c.choices and c.choices[0].delta.tool_calls]
            self.assertEqual(len(tool_chunks), 1)
            tc_delta = tool_chunks[0].choices[0].delta.tool_calls[0]
            self.assertEqual(tc_delta.id, "call_stream_1")
            self.assertEqual(tc_delta.function.name, "stream_tool")
            self.assertEqual(json.loads(tc_delta.function.arguments), {"q": 42})

            # Check finish reason
            finish_chunks = [c for c in chunks if c.choices and c.choices[0].finish_reason]
            self.assertEqual(finish_chunks[0].choices[0].finish_reason, "tool_calls")

    # Current account IDs are offline fixtures, not a production catalog.
    _LIVE_IDS = (
        "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
        "claude-opus-5-5-low", "claude-opus-5-5-medium", "claude-opus-5-5-high",
        "claude-sonnet-5-5-low", "claude-sonnet-5-5-medium", "claude-sonnet-5-5-high",
        "gpt-oss-120b-medium",
    )
    _FUTURE_IDS = (
        "gemini-future-pro-max", "gemini-future-flash-xhigh", "orion-v2-reasoner-max",
        "Vendor/Model_vNext@Preview-high", "vendor/model_vnext@preview-high",
    )

    def test_base_model_and_effort_resolution(self):
        from models import resolve_model_and_effort
        for model in ("gemini-3.8-flash", "gemini-3.1-pro", "gemini-future-flash"):
            for effort in ("low", "medium", "high", "xhigh", "max"):
                with self.subTest(model=model, effort=effort):
                    self.assertEqual(resolve_model_and_effort(model, effort), (model, effort))
            self.assertEqual(resolve_model_and_effort(model, "none"), (model, "low"))
            self.assertEqual(resolve_model_and_effort(model, "invalid"), (model, None))
        with _mock_inherited_config("high") as config:
            for model in self._LIVE_IDS + self._FUTURE_IDS:
                for effort in (None, "low", "max"):
                    self.assertEqual(resolve_model_and_effort(model, effort), (model, None))
            self.assertEqual(resolve_model_and_effort(None), ("", None))
            self.assertEqual(resolve_model_and_effort("gemini-future-flash"), ("gemini-future-flash", None))
            config.assert_not_called()
        for model in ("flash", "default", "sonnet", "claude-sonnet-5-5", "gpt-oss-120b"):
            self.assertEqual(resolve_model_and_effort(model, "high"), (model, None))

    def test_profile_supported_reasoning_efforts(self):
        profile = get_provider_profile("antigravity-oauth")
        for model in self._LIVE_IDS + self._FUTURE_IDS + (None, "claude-sonnet-5-5"):
            self.assertEqual(profile.supported_reasoning_efforts(model), ())
            self.assertEqual(profile.build_api_kwargs_extras(model=model, reasoning_config={"effort": "max"}), ({}, {}))
        for model in ("gemini-3.8-flash", "gemini-future-pro"):
            self.assertEqual(profile.supported_reasoning_efforts(model), ("low", "medium", "high", "xhigh", "max"))
            self.assertEqual(profile.build_api_kwargs_extras(model=model, reasoning_config={"effort": "max"}), ({}, {"reasoning_effort": "max"}))
            self.assertEqual(profile.build_api_kwargs_extras(model=model, reasoning_config={"enabled": False}), ({}, {"reasoning_effort": "low"}))
            self.assertEqual(profile.build_api_kwargs_extras(model=model, reasoning_config={"effort": "invalid"}), ({}, {}))

    def _fetch_fixture(self, output, returncode=0):
        # Patch the package-qualified client used by the registered profile.
        with patch("antigravity_plugin_entry.client.resolve_agy_command", return_value="fixture-agy"), patch.object(
            _pkg.subprocess, "run", return_value=SimpleNamespace(stdout=output, returncode=returncode)
        ) as run:
            result = get_provider_profile("antigravity-oauth").fetch_models(timeout=2.5)
            run.assert_called_once_with(["fixture-agy", "models"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=2.5,
                                        creationflags=0x08000000 if os.name == "nt" else 0)
            return result

    def test_fetch_models_exact_native_ids(self):
        ids = self._LIVE_IDS + self._FUTURE_IDS
        table = "Fetching available models...\nID\tName\n" + "".join(f"{model}\tFixture display name\n" for model in ids + (ids[0],))
        self.assertEqual(self._fetch_fixture(table), list(ids))
        profile = get_provider_profile("antigravity-oauth")
        self.assertEqual(profile.fallback_models, ())
        self.assertEqual(profile.default_aux_model, "")

    def test_fetch_models_failure_is_not_empty_or_static_fallback(self):
        valid = "gemini-3.8-flash-high\tGemini Flash High\n"
        for output in (
            "Fetching available models...\n", "ID\tName\n", "model authentication failed\n",
            "error\tAuthentication required\n", "warning:\tLogin needed\n",
            "gemini-3.8-flash-high\n", "gemini-3.8-flash-high\t\n",
            " gemini-3.8-flash-high\tName\n", "not a model\tName\n",
            valid + "truncated-row\n", valid + "model-id\tName\textra\n",
        ):
            with self.subTest(output=output):
                self.assertIsNone(self._fetch_fixture(output))
        self.assertIsNone(self._fetch_fixture(valid, returncode=1))
        self.assertEqual(self._fetch_fixture(""), [])
        for error in (subprocess.TimeoutExpired("agy", 2.5), OSError("unavailable")):
            with patch("antigravity_plugin_entry.client.resolve_agy_command", return_value="fixture-agy"), patch.object(_pkg.subprocess, "run", side_effect=error):
                self.assertIsNone(get_provider_profile("antigravity-oauth").fetch_models(timeout=2.5))

    def test_native_model_worker_and_oneshot_argv(self):
        for model in self._LIVE_IDS + self._FUTURE_IDS:
            for oneshot in (False, True):
                with self.subTest(model=model, oneshot=oneshot), tempfile.TemporaryDirectory() as tmp:
                    client = AntigravityClient(cwd=tmp)
                    proc = MagicMock()
                    proc.poll.return_value = None
                    if oneshot:
                        client._worker_lock.acquire()
                    try:
                        with patch("client.subprocess.Popen", return_value=proc) as popen, patch("client.AntigravityStream"), patch.object(client, "_sync_workspace_rules", return_value=False), patch.object(client, "_terminate_process"), _mock_inherited_config("max") as config:
                            client.chat.completions.create(model=model, reasoning_effort="low", messages=[{"role": "user", "content": "fixture"}], stream=True)
                            expected = ["agy"] + ([] if oneshot else ["--input-format", "stream-json"]) + list(client._args) + ["--model", model]
                            self.assertEqual(popen.call_args.args[0], expected)
                            client.close()
                            config.assert_not_called()
                    finally:
                        if client._worker_lock.locked():
                            client._worker_lock.release()
                        client.close()

    def test_profile_get_model_context_length(self):
        profile = get_provider_profile("antigravity-oauth")
        for override in ("", "invalid", "0", "-10", "1.5"):
            with patch.dict(os.environ, {"ANTIGRAVITY_CONTEXT_LENGTH": override}):
                for model in self._LIVE_IDS + self._FUTURE_IDS:
                    self.assertIsNone(profile.get_model_context_length(model))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTIGRAVITY_CONTEXT_LENGTH", None)
            self.assertIsNone(profile.get_model_context_length("gpt-oss-120b-medium"))
        for override, expected in (("250000", 250_000), (" 1 ", 1)):
            with patch.dict(os.environ, {"ANTIGRAVITY_CONTEXT_LENGTH": override}):
                for model in self._LIVE_IDS + self._FUTURE_IDS:
                    self.assertEqual(profile.get_model_context_length(model), expected)

    def test_profile_classify_api_error(self):
        profile = get_provider_profile("antigravity-oauth")
        hook = profile.classify_api_error
        self.assertIsNotNone(hook)
        self.assertTrue(callable(hook))

        # Test subscriber fell behind / stalled
        verdict = hook(RuntimeError("subscriber fell behind updates, stalled for 5s"))
        self.assertEqual(verdict, {"reason": "context_overflow", "retryable": True, "should_compress": True})

        # Test empty result SUCCESS
        verdict2 = hook(RuntimeError("Antigravity execution failed: empty result (status='SUCCESS')"))
        self.assertEqual(verdict2, {"reason": "context_overflow", "retryable": True, "should_compress": True})

        # Test context canceled
        verdict3 = hook(RuntimeError("stream input cancelled: context canceled"))
        self.assertEqual(verdict3, {"reason": "context_overflow", "retryable": True, "should_compress": True})

        # Test unrelated error returns None
        verdict4 = hook(RuntimeError("Invalid API key or unauthorized"))
        self.assertIsNone(verdict4)

    def test_hermes_error_classifier_integration(self):
        try:
            from agent.error_classifier import classify_api_error, FailoverReason
        except ImportError:
            self.skipTest("Hermes agent not installed in test environment")

        err = RuntimeError("Antigravity model error: subscriber fell behind updates, stalled for 5s")
        classified = classify_api_error(err, provider="antigravity-oauth")
        self.assertEqual(classified.reason, FailoverReason.context_overflow)
        self.assertTrue(classified.retryable)
        self.assertTrue(classified.should_compress)

    def test_security_default_args_omit_dangerous_permissions(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        self.assertNotIn("--dangerously-skip-permissions", client._args)
        self.assertIn("--disable-slash-commands", client._args)
        self.assertIn("--output-format", client._args)
        profile = get_provider_profile("antigravity-oauth")
        self.assertNotIn("--dangerously-skip-permissions", profile.process_args)
        self.assertIn("--disable-slash-commands", profile.process_args)
        self.assertIn("--output-format", profile.process_args)

    def test_native_tool_step_neutralization_in_stream(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        fake_events = [
            json.dumps({"event": "init", "conversation_id": "test-sec-conv"}),
            json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "run_command"}}),
        ]
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout.readline.side_effect = [f"{line}\n" for line in fake_events] + [""]
        mock_proc.stdin = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc), patch.object(client, "_terminate_process") as mock_term:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            chunks = list(stream)
            mock_term.assert_called_once_with(mock_proc)

    def test_temp_directory_isolation_and_cleanup(self):
        client = AntigravityClient()
        temp_dir = client._cwd
        self.assertTrue(Path(temp_dir).is_dir())
        self.assertIn("hermes_agy_", temp_dir)
        client.close()
        self.assertFalse(Path(temp_dir).exists())

    def test_messages_match_prefix_and_delta(self):
        from client import _messages_match_prefix, _format_delta_prompt

        history = [
            {"role": "system", "content": "You are Hermes"},
            {"role": "user", "content": "Run command"},
        ]
        incoming_same = list(history)
        self.assertFalse(_messages_match_prefix(history, incoming_same))

        incoming_extended = list(history) + [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "sh", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        self.assertTrue(_messages_match_prefix(history, incoming_extended))

        delta = _format_delta_prompt(incoming_extended[2:])
        self.assertIn("<tool_call>", delta)
        self.assertIn("Tool Result (c1):\nok", delta)
        self.assertIn("Continue the conversation from the latest tool result.", delta)

    def test_session_worker_reuse_on_continuation(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = MagicMock()
        fake_events = [
            json.dumps({"event": "init", "conversation_id": "conv-worker"}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "done", "usage": {}}}),
        ]
        mock_proc.stdout.readline.side_effect = [f"{line}\n" for line in fake_events] * 2 + [""]

        with patch("subprocess.Popen", return_value=mock_proc):
            # Turn 1
            msgs1 = [{"role": "user", "content": "first turn"}]
            stream1 = client.chat.completions.create(model="gemini-3.8-flash", messages=msgs1, stream=True)
            list(stream1)
            self.assertEqual(len(client._worker_history), 1)

            # Turn 2: continuation
            msgs2 = msgs1 + [{"role": "tool", "tool_call_id": "t1", "content": "res"}]
            stream2 = client.chat.completions.create(model="gemini-3.8-flash", messages=msgs2, stream=True)
            list(stream2)
            self.assertEqual(len(client._worker_history), 2)
            # Proc was not terminated between turns
            mock_proc.terminate.assert_not_called()

        client.close()
        mock_proc.terminate.assert_called()

    @staticmethod
    def _scripted_proc(events: list[str], *, alive: bool) -> MagicMock:
        """Mock agy process replaying scripted stream-json events.

        Host-free: no real subprocess, zero quota state. Mirrors the fakes
        used by the mock-stream tests above; ``alive=False`` models a process
        that has already exited 0 (readline returns EOF, poll() == 0).

        ``pid`` is pinned to None on purpose: a MagicMock's default pid
        carries ``__index__() == 1``, so on POSIX any accidental route
        into the process-tree kill (a test forgetting _stubbed_kill_path,
        or a future choreography break) would signal the REAL process
        group 1. With pid=None the kill fallback raises TypeError
        instead -- POSIX branch only: ``os.killpg(None, ...)`` raises in
        CPython before any signal syscall, and TypeError is outside
        _kill_process_tree's caught set, so on POSIX the kill fails
        loud and host-safely. On Windows _kill_process_tree takes the
        "nt" branch and never reaches killpg: pid=None spawns a real
        ``taskkill /F /T /PID None`` (the literal string) whose failure
        is suppressed, so the pin alone there is neither loud nor
        host-free -- _stubbed_kill_path (the outermost guarantee,
        OS-independent) is what actually keeps taskkill/killpg off the
        host. Not 0 and not negative: killpg(0) means "the caller's own
        group" and killpg(-1) means "every process" -- both are real
        signals, not safe failures. No test here requires a valid pid.
        """
        proc = MagicMock()
        proc.pid = None
        proc.stdin = MagicMock()
        proc.stderr = io.StringIO("")
        proc.poll.return_value = None if alive else 0
        proc.wait.return_value = 0
        proc.stdout = io.StringIO("\n".join(events) + "\n")
        return proc

    @staticmethod
    @contextlib.contextmanager
    def _stubbed_kill_path():
        """Record -- and neuter -- the process-tree kill for fake procs.

        This stub is the OUTERMOST guarantee, and it is
        OS-independent: it replaces ``_kill_process_tree`` wholesale,
        and that function is the plugin's only route to a real kill
        (``os.killpg`` on POSIX, ``taskkill /F /T`` on Windows) --
        every teardown funnels through process.terminate_process, its
        sole caller. With it installed no killpg and no taskkill can
        run on ANY OS, whatever the fake's pid is. It patches the
        function on the ``process`` module rather than any ``os``
        attribute -- which is also what keeps it Windows-portable,
        where ``os.killpg`` does not exist and cannot be patched at
        all.

        The ``pid=None`` pin on _scripted_proc fakes is the inner,
        POSIX-only defense-in-depth: ``os.killpg(None, ...)`` raises
        TypeError in CPython before any signal syscall (TypeError is
        outside _kill_process_tree's caught set), so a POSIX kill route
        that bypassed this stub would fail loud instead of signaling a
        real group. The pin is load-bearing on POSIX: drop it and an
        unpinned MagicMock pid carries ``__index__() == 1``, so a
        ``_kill_process_tree`` that reached ``os.killpg`` would fire a
        signal at the REAL process group 1. On Windows the pin alone
        protects nothing -- the "nt" branch spawns a real
        ``taskkill /F /T /PID None`` and suppresses its failure --
        which is exactly why this stub, not the pin, is the host-free
        guarantee for fixtures that can reach the kill path.

        Live-oneshot fixtures additionally keep teardown on the
        graceful branch; this stub is defense in depth: if that
        choreography ever breaks, the test fails on the yielded record
        instead of reaching the kill at all.
        """
        kill_record: list = []
        with patch("process._kill_process_tree", side_effect=kill_record.append):
            yield kill_record

    @staticmethod
    @contextlib.contextmanager
    def _spy_update_worker_history(client: AntigravityClient):
        """Record whether the success path updated the worker history.

        ``wraps`` keeps the real update in place, so the spy observes the
        production call without changing behavior.
        """
        with patch.object(
            client, "_update_worker_history", wraps=client._update_worker_history
        ) as spy:
            yield spy

    def test_worker_result_with_cancelled_status_is_not_success(self):
        # Incident shape: agy's pubsub channel was killed mid-turn, the worker
        # survived (poll() -> None), and the result event carried no response
        # with a non-ERROR status. The turn must raise instead of emitting
        # finish_reason="stop" plus an empty response (which Hermes would
        # record as a legitimate answer), and the failed turn must NOT be
        # appended to the worker's conversation history.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-cancelled"}),
            json.dumps({"event": "result", "result": {"status": "CANCELLED"}}),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertIn("Antigravity execution failed", str(ctx.exception))
        self.assertIn("status='CANCELLED'", str(ctx.exception))
        history_spy.assert_not_called()
        # The failing turn closed (terminated) the worker instead.
        self.assertIsNone(client._worker_proc)
        self.assertEqual(client._worker_history, [])
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_worker_cancelled_status_with_partial_content_is_not_success(self):
        # Same incident shape WITH partial output: the pubsub channel died
        # mid-turn, so 'partial' streamed as deltas and the result arrived
        # CANCELLED carrying the same partial response. The turn must still
        # fail -- partial text is not a conclusion, and Hermes cannot unsee
        # deltas a stream already yielded -- and the message must not call
        # that output-empty: the status is simply not a success one.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-cancelled-partial"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "partial"}}),
            json.dumps({
                "event": "result",
                "result": {"status": "CANCELLED", "response": "partial"},
            }),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertIn("Antigravity execution failed", str(ctx.exception))
        self.assertIn("unsuccessful result", str(ctx.exception))
        self.assertIn("status='CANCELLED'", str(ctx.exception))
        history_spy.assert_not_called()
        self.assertIsNone(client._worker_proc)
        self.assertEqual(client._worker_history, [])
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_worker_unsuccessful_result_with_partial_content_reports_exit_code(self):
        # Same partial-output failure with the worker dead: the message must
        # name the status AND the return code, so an operator can tell "agy
        # answered, then died" from "agy never answered" at a glance.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-cancelled-dead"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "partial"}}),
            json.dumps({
                "event": "result",
                "result": {"status": "CANCELLED", "response": "partial"},
            }),
        ]
        proc = self._scripted_proc(events, alive=False)
        proc.poll.return_value = 7

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertIn("Antigravity execution failed", str(ctx.exception))
        self.assertIn("unsuccessful result", str(ctx.exception))
        self.assertIn("status='CANCELLED'", str(ctx.exception))
        self.assertIn("return code 7", str(ctx.exception))
        history_spy.assert_not_called()
        self.assertIsNone(client._worker_proc)
        self.assertEqual(client._worker_history, [])
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_worker_result_success_status_without_content_is_not_success(self):
        # SUCCESS is necessary but not sufficient: a status of SUCCESS with no
        # response, no streamed content and no usage is still an empty failed
        # turn and must raise rather than deliver an empty answer.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-empty-success"}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS"}}),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertIn("Antigravity execution failed", str(ctx.exception))
        self.assertIn("empty result", str(ctx.exception))
        self.assertIn("status='SUCCESS'", str(ctx.exception))
        history_spy.assert_not_called()
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_worker_result_error_field_fails_turn_regardless_of_status(self):
        # A result carrying an "error" field must fail the turn whatever its
        # status says -- the reported pubsub failure arrived with a
        # non-ERROR status, so error presence, not status, is the trigger.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-pubsub"}),
            json.dumps({
                "event": "result",
                "result": {"status": "CANCELLED", "error": "pubsub closed"},
            }),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertIn("Antigravity model error", str(ctx.exception))
        self.assertIn("pubsub closed", str(ctx.exception))
        history_spy.assert_not_called()
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_worker_success_status_with_error_field_fails_turn(self):
        # Origin fix for the corruption the error-field raise introduced:
        # status SUCCESS with a response AND a non-empty error field used to
        # raise (correctly) while success stayed True, so the finally branch
        # appended the FAILED turn to the worker history before the worker
        # was torn down. The error field must invalidate success at the
        # verdict itself, so a raising turn never reaches the history update.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        messages = [{"role": "user", "content": "hello"}]
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-success-error"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "partial answer"}}),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "partial answer",
                    "error": "pubsub closed",
                },
            }),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=messages,
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertIn("Antigravity model error", str(ctx.exception))
        self.assertIn("pubsub closed", str(ctx.exception))
        # The failed turn must not be recorded as a legitimate exchange.
        history_spy.assert_not_called()
        self.assertEqual(client._worker_history, [])
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_worker_error_status_without_error_field_fails_turn(self):
        # The pre-PR `if status == "ERROR"` route: a result with the
        # terminal ERROR status must still fail the turn even when it
        # carries no "error" field, with the exact prior wording (a bare
        # "Antigravity model error: "). success stays False (the verdict
        # demands SUCCESS), so the failed turn must not be appended to
        # the worker history and the broken worker must be torn down.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        messages = [{"role": "user", "content": "hello"}]
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-error-status"}),
            json.dumps({"event": "result", "result": {"status": "ERROR"}}),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=messages,
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertEqual(str(ctx.exception), "Antigravity model error: ")
        history_spy.assert_not_called()
        self.assertEqual(client._worker_history, [])
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_oneshot_error_status_without_error_field_fails_turn(self):
        # Same restored route on the oneshot path (the worker lock is held
        # by the test to force it, mirroring
        # test_oneshot_eof_without_result_event_is_not_success): the ERROR
        # status raise comes before the exit-code checks, and a clean exit
        # code 0 must not reroute the failure into an empty-result message.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        self.assertTrue(client._worker_lock.acquire(blocking=False))
        events = [
            json.dumps({"event": "init", "conversation_id": "oneshot-error-status"}),
            json.dumps({"event": "result", "result": {"status": "ERROR"}}),
        ]
        proc = self._scripted_proc(events, alive=False)
        self.assertEqual(proc.poll.return_value, 0)

        with patch("subprocess.Popen", return_value=proc):
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertEqual(str(ctx.exception), "Antigravity model error: ")
        proc.terminate.assert_called_once()
        client._worker_lock.release()
        client.close()

    def test_worker_output_token_limit_error_is_length_finish(self):
        # agy reports a reply cut at the output cap as a result "error".
        # It must reach Hermes as finish_reason="length" (truncation
        # recovery), not as a RuntimeError that Hermes misreads as a
        # context overflow. The cut reply is not a resumable exchange: no
        # history update, and the worker is retired.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-output-cap"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "partial answer"}}),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "error": "your previous response was cut off because it exceeded the output token limit",
                },
            }),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            chunks = list(stream)

        finish = [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason]
        self.assertEqual(finish, ["length"])
        text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
        self.assertEqual(text, "partial answer")
        history_spy.assert_not_called()
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_oneshot_output_token_limit_error_is_length_finish(self):
        # Structured path: a max-tokens finish reason on the result event is
        # enough even when the error text is not agy's truncation sentence.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        self.assertTrue(client._worker_lock.acquire(blocking=False))
        events = [
            json.dumps({"event": "init", "conversation_id": "oneshot-output-cap"}),
            json.dumps({
                "event": "result",
                "result": {"status": "ERROR", "error": "generation stopped", "finish_reason": "MAX_TOKENS"},
            }),
        ]
        proc = self._scripted_proc(events, alive=False)

        with patch("subprocess.Popen", return_value=proc):
            completion = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
                timeout=30.0,
            )

        self.assertEqual(completion.choices[0].finish_reason, "length")
        client._worker_lock.release()
        client.close()

    def test_output_limit_detection_matches_agy_sentences_only(self):
        for text in (
            "Your previous response was cut off because it exceeded the output token limit",
            "your response exceeded the maximum output token limit and was truncated. Do not repeat...",
            "Your previous response was cut off\nbecause it exceeded the output token limit.",
        ):
            with self.subTest(text=text):
                self.assertTrue(_is_output_limit_error(text))
        # Same phrase, unrelated agy auxiliary messages: not a response truncation.
        for text in (
            "Checkpoint summary exceeded the max output token limit of 8192",
            "intent prediction exceeded the max output token limit of 1024",
            "exceeded the output token limit",
            "", None, 42,
        ):
            with self.subTest(text=text):
                self.assertFalse(_is_output_limit_error(text))
        for result in (
            {"finish_reason": "MAX_TOKENS"}, {"finishReason": "max_tokens"},
            {"stop_reason": "max_tokens"}, {"metadata": {"finishReason": "length"}},
        ):
            with self.subTest(result=result):
                self.assertTrue(_result_reports_output_limit(result))
        for result in (
            {}, {"finish_reason": "STOP"}, {"finish_reason": None}, {"metadata": "MAX_TOKENS"},
            {"status": "ERROR", "error": "context canceled"}, None, "MAX_TOKENS",
        ):
            with self.subTest(result=result):
                self.assertFalse(_result_reports_output_limit(result))

    def test_worker_eof_without_result_event_is_not_success(self):
        # The dead-worker guard's blind spot: the worker exited 0, so
        # poll() == 0 passed `not in (None, 0)` and the empty stream used to
        # fall through as a successful empty turn.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        proc = self._scripted_proc([], alive=False)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertIn("Antigravity execution failed", str(ctx.exception))
        self.assertIn("no result event", str(ctx.exception))
        self.assertIn("return code 0", str(ctx.exception))
        history_spy.assert_not_called()
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_oneshot_eof_without_result_event_is_not_success(self):
        # Same blind spot for oneshots: exit code 0 passed
        # `returncode != 0`, so the empty stream used to be recorded as a
        # legitimate empty response. The worker lock is held by the test to
        # force the oneshot path (mirrors test_concurrent_fallback_to_oneshot).
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        self.assertTrue(client._worker_lock.acquire(blocking=False))
        proc = self._scripted_proc([], alive=False)

        with patch("subprocess.Popen", return_value=proc):
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        self.assertIn("Antigravity execution failed", str(ctx.exception))
        self.assertIn("no result event", str(ctx.exception))
        self.assertIn("return code 0", str(ctx.exception))
        proc.terminate.assert_called_once()
        client._worker_lock.release()
        client.close()

    def test_worker_partial_text_without_result_event_is_not_success(self):
        # The surviving-worker hazard: 'partial' streamed, the worker never
        # sent a result event and is still alive, so the read loop only ends
        # at the deadline (EOF -> poll() None -> sleep, repeatedly). The
        # partial deltas used to satisfy the old `not (has_content or
        # has_tool_calls)` escape, so the turn was sealed as
        # finish_reason="stop" AND the broken worker survived it (_finished
        # was already True when close() ran).
        # Deterministic without scheduler-dependent timing: a generous
        # timeout, and the first delta is consumed with the REAL clock
        # before the clock is patched -- the generator is then suspended
        # inside the read loop, its deadline is already booked, and the
        # quota watchdog is already stopped by the first parsed event, so
        # the patched clock can only expire this test's loop condition.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-partial-no-result"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "partial"}}),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            # First delta on the real clock, as established above.
            chunks = [next(stream)]
            # Every later clock read reports a time far past the deadline,
            # so the read loop ends the moment the generator resumes.
            expired_clock = time.monotonic() + _CLOCK_JUMP_SECONDS
            with patch("stream.time.monotonic", return_value=expired_clock):
                with self.assertRaises(RuntimeError) as ctx:
                    for chunk in stream:
                        chunks.append(chunk)

        # The partial text WAS delivered before the raise (Hermes aggregates
        # a stream that fails mid-way; delivered deltas cannot be retracted).
        # What must never happen is the finish chunk that would seal the
        # turn as complete.
        contents = "".join(
            c.choices[0].delta.content
            for c in chunks
            if c.choices and c.choices[0].delta.content
        )
        self.assertEqual(contents, "partial")
        self.assertEqual(
            [c for c in chunks if c.choices and c.choices[0].finish_reason],
            [],
        )
        self.assertIn("Antigravity execution failed", str(ctx.exception))
        self.assertIn("no result event", str(ctx.exception))
        self.assertIn("process still alive", str(ctx.exception))
        history_spy.assert_not_called()
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_oneshot_partial_text_without_result_event_is_not_success(self):
        # Same hazard on the oneshot path: partial deltas, no result event,
        # exit code 0. The worker lock is held by the test to force the
        # oneshot path (mirrors test_oneshot_eof_without_result_event_is_not_success).
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        self.assertTrue(client._worker_lock.acquire(blocking=False))
        events = [
            json.dumps({"event": "init", "conversation_id": "oneshot-partial-no-result"}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "partial"}}),
        ]
        proc = self._scripted_proc(events, alive=False)

        with patch("subprocess.Popen", return_value=proc):
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            chunks = []
            with self.assertRaises(RuntimeError) as ctx:
                for chunk in stream:
                    chunks.append(chunk)

        contents = "".join(
            c.choices[0].delta.content
            for c in chunks
            if c.choices and c.choices[0].delta.content
        )
        self.assertEqual(contents, "partial")
        self.assertEqual(
            [c for c in chunks if c.choices and c.choices[0].finish_reason],
            [],
        )
        self.assertIn("Antigravity execution failed", str(ctx.exception))
        self.assertIn("no result event", str(ctx.exception))
        self.assertIn("return code 0", str(ctx.exception))
        proc.terminate.assert_called_once()
        client._worker_lock.release()
        client.close()

    def test_oneshot_wait_timeout_with_live_process_reports_still_alive(self):
        # The fabricated exit code: the oneshot post-loop used to compute
        # `returncode = proc.poll() or 0`, so a still-alive process
        # (poll() None) was reported as having "exited with return code
        # 0" -- a return code for a process that never exited. Here the
        # bounded post-result wait times out, the process is STILL alive,
        # and no result event ever arrived, so the message must say the
        # process is still alive and never name a return code.
        # The worker lock is held by the test to force the oneshot path
        # (mirrors test_oneshot_eof_without_result_event_is_not_success).
        # Deterministic without wall-clock waits: the generator books its
        # deadline and start snapshot from the clock first (two calls),
        # every later clock read jumps strictly further past the real
        # clock than the previous one (see expiring_monotonic), so the
        # read loop ends within bounded calls instead of spinning on the
        # alive process for the wall-clock timeout.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        self.assertTrue(client._worker_lock.acquire(blocking=False))
        proc = self._scripted_proc([], alive=True)
        # Only the generator's bounded wait (timeout=3.0) times out; every
        # later wait -- the ones inside terminate_process (timeout=2) --
        # returns 0, so teardown stays on the graceful branch and the
        # process-tree kill is never reached. The outermost guarantee is
        # _stubbed_kill_path below: it replaces process._kill_process_tree
        # wholesale, so neither killpg (POSIX) nor taskkill (Windows) can
        # run, regardless of OS. The fake's pid=None pin is only the
        # POSIX inner layer (TypeError before the syscall); on Windows the
        # pin alone would spawn a real taskkill /PID "None" instead --
        # see _scripted_proc and _stubbed_kill_path.
        proc.wait.side_effect = itertools.chain(
            [subprocess.TimeoutExpired(cmd=["agy"], timeout=3.0)],
            itertools.repeat(0),
        )

        teardown_order: list[str] = []
        real_terminate = client._terminate_process

        def recording_terminate(terminated_proc: subprocess.Popen) -> None:
            teardown_order.append("terminate")
            return real_terminate(terminated_proc)

        real_monotonic = time.monotonic
        clock_calls = itertools.count()

        def expiring_monotonic() -> float:
            # First two reads real, so the deadline and the start
            # snapshot book from the actual clock and stay realistic.
            # Every later read jumps further past the real clock than the
            # previous one, which makes the clock strictly growing: a
            # deadline booked from ANY earlier read -- even if the two
            # realistic reads above were consumed before the booking by
            # a future code change -- is exceeded within bounded calls,
            # so the read loop can never spin forever. A fixed value
            # shared by every later read would instead hang the loop
            # whenever the booking itself landed past the jump.
            # The quota watchdog (the only other reader, on its own
            # thread) sees a huge elapsed time, finds no CLI logs in the
            # isolated home, and idles.
            calls = next(clock_calls)
            if calls < 2:
                return real_monotonic()
            return real_monotonic() + _CLOCK_JUMP_SECONDS * calls

        with self._stubbed_kill_path() as kill_record, \
                patch("subprocess.Popen", return_value=proc), \
                patch("stream.time.monotonic", side_effect=expiring_monotonic), \
                patch.object(client, "_terminate_process", side_effect=recording_terminate):
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

        message = str(ctx.exception)
        self.assertIn("Antigravity execution failed", message)
        self.assertIn("no result event", message)
        self.assertIn("process still alive", message)
        # A live process must never be reported as exited.
        self.assertNotIn("return code", message)
        self.assertNotIn("exited", message)
        # Teardown reached the live process, in order: once for the
        # timed-out wait (the pre-existing kill attempt) and once from
        # the stream's close(). The fixture never needed the
        # process-tree kill: the record stays empty, so no signal could
        # reach any pid.
        self.assertEqual(teardown_order, ["terminate", "terminate"])
        self.assertEqual(proc.terminate.call_count, 2)
        self.assertEqual(kill_record, [])
        client._worker_lock.release()
        client.close()

    def test_worker_failure_surfaces_in_non_streaming_mode(self):
        # stream=False robustness for the same failure: the raise must reach
        # collect_stream_completion's caller, and the client-side finally
        # must not double-release the worker lock the stream already
        # released (an unlocked release() would raise RuntimeError and mask
        # the real failure).
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-cancelled-nostream"}),
            json.dumps({"event": "result", "result": {"status": "CANCELLED"}}),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            with self.assertRaises(RuntimeError) as ctx:
                client.chat.completions.create(
                    model="gemini-3.8-flash-high",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=False,
                    timeout=30.0,
                )

        self.assertIn("Antigravity execution failed", str(ctx.exception))
        self.assertIn("status='CANCELLED'", str(ctx.exception))
        history_spy.assert_not_called()
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_worker_happy_path_result_success_still_updates_history(self):
        # Regression guard pinning the positive-evidence contract: status
        # SUCCESS WITH a response remains a success -- no raise, the worker
        # survives the turn, and its history is updated for continuation --
        # so a future tightening of the success check cannot silently turn
        # every healthy turn into a failure.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        messages = [{"role": "user", "content": "hello"}]
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-happy"}),
            json.dumps({
                "event": "result",
                "result": {"status": "SUCCESS", "response": "all good", "usage": {}},
            }),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=messages,
                stream=True,
                timeout=30.0,
            )
            chunks = list(stream)

        contents = "".join(
            c.choices[0].delta.content
            for c in chunks
            if c.choices and c.choices[0].delta.content
        )
        self.assertEqual(contents, "all good")
        self.assertEqual(
            [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason],
            ["stop"],
        )
        history_spy.assert_called_once_with(messages)
        # The worker survived the successful turn: same process, reusable.
        self.assertIs(client._worker_proc, proc)
        self.assertFalse(client._worker_lock.locked())
        proc.terminate.assert_not_called()
        # Only client teardown kills it.
        client.close()
        proc.terminate.assert_called_once()

    def test_post_result_usage_failure_does_not_record_failed_turn(self):
        # Post-result race window: success=True is fixed at the result
        # event, but a failure can still surface afterwards -- here
        # _usage_totals raising. Without a reset the finally would take the
        # success branch for this failed turn: appending it to the worker
        # history and releasing the request lock with the worker still
        # alive (close() only ran later, from __next__). The failed turn
        # must instead tear the worker down like any other failure. The
        # usage patch is installed for the consumption alone.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-usage-race"}),
            json.dumps({
                "event": "result",
                "result": {"status": "SUCCESS", "response": "ok", "usage": {}},
            }),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            with self.assertRaises(RuntimeError) as ctx:
                with patch.object(
                    AntigravityStream,
                    "_usage_totals",
                    side_effect=RuntimeError("synthetic usage failure"),
                ):
                    list(stream)

        self.assertIn("synthetic usage failure", str(ctx.exception))
        history_spy.assert_not_called()
        self.assertEqual(client._worker_history, [])
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_early_error_after_result_does_not_record_failed_turn(self):
        # Same race window through _early_error: the watchdog can flag a
        # quota failure after the result event was already parsed (success
        # True), and the post-loop check then raises for a turn the success
        # branch would have recorded. Deterministic without sleeps: the
        # result is consumed (first next() parses it and flushes the folded
        # response), then the flag is set on the stream before the generator
        # resumes into the post-loop checks.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        events = [
            json.dumps({"event": "init", "conversation_id": "conv-early-race"}),
            json.dumps({
                "event": "result",
                "result": {"status": "SUCCESS", "response": "ok", "usage": {}},
            }),
        ]
        proc = self._scripted_proc(events, alive=True)

        with patch("subprocess.Popen", return_value=proc), \
                self._spy_update_worker_history(client) as history_spy:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash-high",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                timeout=30.0,
            )
            first_chunk = next(stream)
            self.assertEqual(first_chunk.choices[0].delta.content, "ok")
            stream._early_error = "synthetic early quota error after result"
            with self.assertRaises(RuntimeError) as ctx:
                for _ in stream:
                    pass

        self.assertIn("Antigravity model error", str(ctx.exception))
        self.assertIn("synthetic early quota error", str(ctx.exception))
        history_spy.assert_not_called()
        self.assertEqual(client._worker_history, [])
        self.assertIsNone(client._worker_proc)
        proc.terminate.assert_called_once()
        self.assertFalse(client._worker_lock.locked())

    def test_concurrent_fallback_to_oneshot(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        # Acquire worker lock manually to simulate an in-progress stream
        self.assertTrue(client._worker_lock.acquire(blocking=False))

        mock_oneshot_proc = MagicMock()
        mock_oneshot_proc.poll.return_value = 0
        mock_oneshot_proc.stdin = MagicMock()
        mock_oneshot_proc.stderr = io.StringIO("")
        fake_events = [
            json.dumps({"event": "init", "conversation_id": "oneshot-conv"}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "oneshot response"}}),
        ]
        mock_oneshot_proc.stdout = io.StringIO("\n".join(fake_events) + "\n")

        with patch("subprocess.Popen", return_value=mock_oneshot_proc):
            res = client.chat.completions.create(
                model="gemini-3.8-flash",
                messages=[{"role": "user", "content": "concurrent"}],
                stream=False,
            )
            self.assertEqual(res.choices[0].message.content, "oneshot response")

        client._worker_lock.release()
        client.close()

    def test_isolated_home_and_token_symlink(self):
        token = self._write_token(tempfile.mkdtemp())
        # setup_isolated_home() resolves the token via the process module,
        # not through the client's static seam, so patch it where it is read.
        with patch("process.resolve_real_token_path", return_value=token):
            client = AntigravityClient()
            self.assertTrue(client._isolated_home.is_dir())
            self.assertTrue(client._isolated_gemini_dir.is_dir())
            symlinked_token = client._isolated_gemini_dir / "antigravity-oauth-token"
            self.assertTrue(symlinked_token.exists())
            temp_dir = client._cwd
            client.close()
            self.assertFalse(Path(temp_dir).exists())

    def test_windows_process_group_creation(self):
        from client import _own_process_group
        with patch("os.name", "nt"):
            flags = _own_process_group()
            self.assertEqual(flags, {"creationflags": 0x00000200 | 0x08000000})
        with patch("os.name", "posix"):
            flags = _own_process_group()
            self.assertEqual(flags, {"start_new_session": True})

    def test_windows_kill_process_tree(self):
        from client import _kill_process_tree
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 4321

        with patch("os.name", "nt"), patch("subprocess.run") as mock_run:
            _kill_process_tree(mock_proc)
            mock_run.assert_called_once_with(
                ["taskkill", "/F", "/T", "/PID", "4321"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=0x08000000,
            )

    def test_windows_background_helpers_hide_console(self):
        import auth
        import process
        import usage
        failed = subprocess.CompletedProcess([], 1, "", "")
        with patch("os.name", "nt"), patch("subprocess.run", return_value=failed) as run:
            auth.verify_session("agy")
            process._windows_credential_present()
            usage._kill_agy_tree(MagicMock(pid=4321))
        self.assertEqual([c.args[0][0] for c in run.call_args_list], ["agy", "cmdkey", "taskkill"])
        self.assertTrue(all(c.kwargs["creationflags"] == 0x08000000 for c in run.call_args_list))

    def test_child_env_windows_userprofile(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() — addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        env = client._child_env()
        self.assertIn("HOME", env)
        self.assertIn("USERPROFILE", env)
        self.assertEqual(env["HOME"], str(client._isolated_home))
        self.assertEqual(env["USERPROFILE"], str(client._isolated_home))
        self.assertEqual(env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        client.close()

    def test_token_linking_fallback_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            token_src = tmp_path / "src_token"
            token_src.write_text("token-content-123456", encoding="utf-8")

            # Simulate os.symlink failing (WinError 1314) and falling back to os.link
            with patch("process.resolve_real_token_path", return_value=token_src):
                with patch("process.os.symlink", side_effect=OSError("privilege not held")), \
                     patch("process.os.link") as mock_link:
                    client = AntigravityClient(cwd=tmp)
                    mock_link.assert_called_once()
                    client.close()

            # Simulate both symlink and hardlink failing (cross-device/filesystem), falling back to copy2
            with patch("process.resolve_real_token_path", return_value=token_src):
                with patch("process.os.symlink", side_effect=OSError("privilege not held")), \
                     patch("process.os.link", side_effect=OSError("cross-device link")), \
                     patch("process.shutil.copy2") as mock_copy:
                    client = AntigravityClient(cwd=tmp)
                    mock_copy.assert_called_once()
                    client.close()

    def test_check_early_quota_error_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log_dir = tmp_path / "log"
            log_dir.mkdir()
            log_file = log_dir / "cli-20260921_211950.log"
            sample_line = (
                "I0921 21:19:51.264799     299 run.go:395] Run: attempt 1 failed "
                "(RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
                "Please upgrade your subscription to increase your limits. Resets in 1h49m22s.), retrying in 4s\n"
            )
            log_file.write_text(sample_line, encoding="utf-8")

            # Verify extraction
            err = _check_early_quota_error(tmp_path)
            self.assertIsNotNone(err)
            self.assertIn("RESOURCE_EXHAUSTED (code 429)", err)
            self.assertIn("Individual quota reached", err)
            self.assertIn("Resets in 1h49m22s.", err)
            self.assertNotIn("retrying in", err)

            # Check min_mtime filter: future timestamp ignores old log
            err_future = _check_early_quota_error(tmp_path, min_mtime=time.time() + 100)
            self.assertIsNone(err_future)

    def test_early_quota_watchdog_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log_dir = tmp_path / "log"
            log_dir.mkdir()
            log_file = log_dir / "cli-20260921_999999.log"
            sample_line = (
                "I0921 21:19:51.264799     299 run.go:395] Run: attempt 1 failed "
                "(RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
                "Please upgrade your subscription to increase your limits. Resets in 1h49m22s.), retrying in 4s\n"
            )
            log_file.write_text(sample_line, encoding="utf-8")

            mock_client = MagicMock()
            mock_client._isolated_gemini_dir = tmp_path
            mock_client._terminate_process = MagicMock()

            # Pipe that blocks or returns nothing until terminated
            mock_proc = MagicMock()
            def fake_readline():
                for _ in range(50):
                    if mock_client._terminate_process.called:
                        return ""
                    time.sleep(0.05)
                return ""

            mock_proc.stdout.readline = fake_readline
            mock_proc.poll.return_value = None

            stream = AntigravityStream(
                proc=mock_proc,
                client=mock_client,
                model="gemini-3.8-flash",
                timeout=5.0,
            )

            with self.assertRaises(RuntimeError) as ctx:
                list(stream)

            self.assertIn("RESOURCE_EXHAUSTED", str(ctx.exception))
            self.assertIn("Individual quota reached", str(ctx.exception))
            mock_client._terminate_process.assert_called_with(mock_proc)


if __name__ == "__main__":
    unittest.main()


