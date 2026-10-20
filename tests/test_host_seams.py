"""Host seam contract: the Hermes names this plugin reads must still exist.

The live-reasoning presentation is fail-open: when a host seam disappears it
silently disables itself and the completion is unaffected. The realistic
regression is therefore a rename or move in the host, and this suite catches
that in the default CI without executing any Hermes runtime: executable seams
are imported directly (they need no installed packages); dependency-heavy
modules are only LOCATED via importlib and checked at source level.

Delivery behavior itself is covered by tests/test_live_reasoning_adapter.py
against an agy-style stand-in agent, and by scripts/verify_live_reasoning.py
against the real producer.
"""
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

# Executable seams (stdlib-importable): used by live_reasoning._resolve_agent
# and by the lease's profile fencing.
from agent.subagent_lifecycle import bind_subagent_parent, get_active_subagent_parent
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


def _package_dir(package: str) -> Path:
    # find_spec on a TOP-LEVEL name locates without executing the module.
    spec = importlib.util.find_spec(package)
    assert spec is not None and spec.origin is not None, package
    return Path(spec.origin).parent


def _package_text(package: str) -> str:
    return '\n'.join(p.read_text(encoding='utf-8') for p in _package_dir(package).glob('*.py'))


class ExecutableSeamTests(unittest.TestCase):
    def test_subagent_parent_binding_roundtrip(self):
        agent = NS(session_id='seam')
        before = get_active_subagent_parent()
        with bind_subagent_parent(agent):
            self.assertIs(get_active_subagent_parent(), agent)
        self.assertIs(get_active_subagent_parent(), before)

    def test_profile_home_override_roundtrip(self):
        # live_reasoning._home() fences leases by the resolved profile home.
        base = str(get_hermes_home().resolve())
        token = set_hermes_home_override(Path('/tmp/agy-seam-home'))
        try:
            self.assertEqual(str(get_hermes_home()), '/tmp/agy-seam-home')
        finally:
            reset_hermes_home_override(token)
        self.assertEqual(str(get_hermes_home().resolve()), base)


class SourceSeamTests(unittest.TestCase):
    """Each entry names a host definition next to the plugin code that reads it."""

    def assert_in_source(self, text, needles, where):
        for needle in needles:
            self.assertIn(needle, text, f'{where}: missing host seam {needle!r}')

    def test_middleware_registration_and_dispatch(self):
        plugins = (_package_dir('hermes_cli')/'plugins.py').read_text(encoding='utf-8')
        # live_reasoning.register/ensure_registered import these names from
        # hermes_cli.plugins (PluginManifest is a re-export there).
        self.assert_in_source(plugins, ['PluginContext', 'PluginManifest',
                                        'def get_plugin_manager', 'def register_middleware'], 'hermes_cli/plugins.py')
        middleware = (_package_dir('hermes_cli')/'middleware.py').read_text(encoding='utf-8')
        self.assert_in_source(middleware, ['def run_llm_execution_middleware',
                                           'LLM_EXECUTION_MIDDLEWARE = "llm_execution"'], 'hermes_cli/middleware.py')

    def test_nonstream_dispatch_invokes_middleware_with_fence_kwargs(self):
        turn = (_package_dir('agent')/'turn_api_call.py').read_text(encoding='utf-8')
        # execution_middleware's identity fences come from this call site.
        self.assert_in_source(turn, ['run_llm_execution_middleware(', 'turn_id=turn_id',
                                     'api_request_id=api_request_id', 'session_id=agent.session_id',
                                     'model=agent.model', 'provider=agent.provider',
                                     'def _should_stream'], 'agent/turn_api_call.py')
        # Auxiliary projections stay excluded via the aux_task fence.
        aux = (_package_dir('agent')/'auxiliary_client.py').read_text(encoding='utf-8')
        self.assert_in_source(aux, ['aux_task='], 'agent/auxiliary_client.py')

    def test_tui_live_session_lookup(self):
        gateway = _package_text('tui_gateway')
        # live_reasoning._live_session reads these off tui_gateway.server
        # (definitions may live in sibling modules rebound onto server globals).
        self.assert_in_source(gateway, ['def _find_live_session_by_key', 'def _launch_home'],
                              'tui_gateway package')

    def test_agent_lease_attributes(self):
        agent_text = _package_text('agent') + (_package_dir('agent').parent/'run_agent.py').read_text(encoding='utf-8')
        # _Lease.current() reads these attributes off the live AIAgent.
        self.assert_in_source(agent_text, ['_current_turn_id', '_current_api_request_id',
                                           '_model_request_active', '_mute_notification_reply',
                                           'reasoning_callback', '_stream_callback',
                                           'stream_delta_callback'], 'agent package')

    def test_provider_registry(self):
        providers = (_package_dir('providers')/'__init__.py').read_text(encoding='utf-8')
        # plugin __init__ registers through these; discovery reads model-providers.
        self.assert_in_source(providers, ['def register_provider', 'def get_provider_profile',
                                          'model-providers'], 'providers/__init__.py')


if __name__ == '__main__':
    unittest.main()
