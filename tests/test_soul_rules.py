"""SOUL.md reaches agy as workspace rules (GEMINI.md), and the stdin prompt stops duplicating it."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

import soul  # noqa: E402
from prompt import _PROMPT_PREAMBLE, _format_messages_as_prompt, _render_message_content  # noqa: E402

SOUL_TEXT = "# SOUL.md - Test Persona\n\nVerdict first. Never open with pleasantries. Canary: MANGO-31."


class SoulRulesTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.ws = tempfile.mkdtemp()
        Path(self.home, "SOUL.md").write_text(SOUL_TEXT, encoding="utf-8")
        p = patch.dict(os.environ, {"HERMES_HOME": self.home})
        p.start()
        self.addCleanup(p.stop)
        q = patch("soul.hermes_soul_path", return_value=Path(self.home) / "SOUL.md")
        q.start()
        self.addCleanup(q.stop)

    def test_soul_is_prepended_when_system_prompt_lacks_it(self):
        rules = soul.build_rules(["You are a helpful assistant."], _PROMPT_PREAMBLE)
        self.assertIn("MANGO-31", rules)
        self.assertLess(rules.index("MANGO-31"), rules.index("helpful assistant"))
        self.assertIn("<tool_call>", rules)

    def test_soul_not_duplicated_when_hermes_already_injected_it(self):
        rules = soul.build_rules([SOUL_TEXT + "\n\nMore Hermes instructions."], _PROMPT_PREAMBLE)
        self.assertEqual(rules.count("MANGO-31"), 1)
        self.assertNotIn("## Persona (SOUL.md)", rules)

    def test_missing_soul_file_still_writes_system_prompt(self):
        Path(self.home, "SOUL.md").unlink()
        rules = soul.build_rules(["System text only."], _PROMPT_PREAMBLE)
        self.assertIn("System text only.", rules)
        self.assertNotIn("Persona", rules)

    def test_write_rules_is_idempotent_and_reports_change(self):
        d1 = soul.write_rules(self.ws, "a\n")
        self.assertEqual(soul.write_rules(self.ws, "a\n"), d1)
        self.assertNotEqual(soul.write_rules(self.ws, "b\n"), d1)
        self.assertEqual(Path(self.ws, "GEMINI.md").read_text(), "b\n")

    def test_prompt_omits_system_and_preamble_when_rules_in_workspace(self):
        messages = [{"role": "system", "content": "SYSTEM-XYZ"}, {"role": "user", "content": "hi"}]
        inline = _format_messages_as_prompt(messages)
        ruled = _format_messages_as_prompt(messages, rules_in_workspace=True)
        self.assertIn("SYSTEM-XYZ", inline)
        self.assertIn(_PROMPT_PREAMBLE[0], inline)
        self.assertNotIn("SYSTEM-XYZ", ruled)
        self.assertNotIn(_PROMPT_PREAMBLE[0], ruled)
        self.assertIn("hi", ruled)

    def test_agy_429_trigger_lines_are_reworded(self):
        prompt = "\n".join((
            "For help, see https://hermes-agent.nousresearch.com/docs for details.",
            "# Google model operational directives",
            "<!-- End Hermes runtime environment -->",
            "When asked to build something, the deliverable is a working artifact backed by real "
            "tool output — not a description of one.",
            "Reporting a blocker honestly is always better than inventing a result.",
            "Mid-turn, the user can steer you: Hermes delivers their message as a standalone user message right after the latest tool results, wrapped exactly as:",
        ))
        rules = soul.build_rules([prompt], _PROMPT_PREAMBLE)
        for marker in (
            "https://hermes-agent.nousresearch.com/docs",
            "# Google model operational directives",
            "<!-- End Hermes runtime environment -->",
            "the deliverable is a working artifact backed by real tool output",
            "Reporting a blocker honestly is always better than inventing a result.",
            "Mid-turn, the user can steer you: Hermes delivers their message as a standalone user message right after the latest tool results, wrapped exactly as:",
        ):
            self.assertNotIn(marker, rules)
        self.assertIn("the official documentation", rules)
        self.assertIn("## Provider-specific guidance", rules)
        self.assertIn("produce it for real and report what actually ran", rules)
        self.assertIn("Say plainly when something is blocked.", rules)
        self.assertIn("A separate runtime marker may carry the next real user message.", rules)

    def test_opt_out_env(self):
        with patch.dict(os.environ, {"ANTIGRAVITY_WORKSPACE_RULES": "0"}):
            self.assertFalse(soul.rules_enabled())
        self.assertTrue(soul.rules_enabled())

    def test_system_parts_extraction(self):
        parts = soul.system_parts_of([{"role": "system", "content": "S1"}, {"role": "user", "content": "U"}],
                                     _render_message_content)
        self.assertEqual(parts, ["S1"])


if __name__ == "__main__":
    unittest.main()
