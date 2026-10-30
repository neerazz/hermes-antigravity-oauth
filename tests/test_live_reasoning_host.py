"""Host integration without network: actual provider discovery, dispatch, TUI gates.

Only the agy subprocess producer and UI write_json egress are substituted.
The live script independently exercises the real agy producer.
"""
import importlib
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import os
import tempfile

# Hermetic HERMES_HOME holding only this checkout as an installed plugin, so
# provider discovery finds the code under test (not a host-installed copy, and
# not nothing at all on a clean CI runner).
_HERMES_HOME = tempfile.mkdtemp(prefix='agy_host_test_')
os.makedirs(os.path.join(_HERMES_HOME, 'plugins'))
os.symlink(Path(__file__).resolve().parent.parent, os.path.join(_HERMES_HOME, 'plugins', 'antigravity-oauth'))
os.environ['HERMES_HOME'] = _HERMES_HOME
# Only the agy producer is substituted below; credential resolution still needs
# an executable path, which a clean CI runner does not have.
_FAKE_AGY = os.path.join(_HERMES_HOME, 'agy')
with open(_FAKE_AGY, 'w') as _f:
    _f.write('#!/bin/sh\nexit 0\n')
os.chmod(_FAKE_AGY, 0o755)
os.environ['AGY_CLI_PATH'] = _FAKE_AGY

# Fallback for a local run without PYTHONPATH; never shadow an explicit checkout.
sys.path.append(str(Path.home()/'.hermes'/'hermes-agent'))
import hermes_bootstrap
from hermes_cli.plugins_discovery import suppress_plugin_discovery

# Avoid unrelated default-profile plugins during isolated regression execution.
with suppress_plugin_discovery():
    from run_agent import AIAgent
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.turn_api_call import _should_stream, perform_api_call
    from agent.notification_presentation import notification_turn
    from hermes_cli.plugins import get_plugin_manager
    from providers import get_provider_profile
    from tui_gateway import server


class HostPathTests(unittest.TestCase):
    def setUp(self):
        self.scope = suppress_plugin_discovery(); self.scope.__enter__()
        self.addCleanup(self.scope.__exit__,None,None,None)
        self.profile = get_provider_profile('antigravity-oauth')
        self.module = importlib.import_module(type(self.profile).__module__)
        self.bridge = importlib.import_module(type(self.profile).__module__+'.live_reasoning')
        self.client_module = importlib.import_module(type(self.profile).__module__+'.client')
        self.frames = []
        self.sid = 'adapter-regression'
        self.agent = AIAgent(provider='antigravity-oauth',model='gemini-4-argon',base_url='agy://local',
            api_mode='chat_completions',session_id='adapter-regression-store',platform='tui',
            enabled_toolsets=[],disabled_toolsets=['all'],quiet_mode=True,skip_context_files=True,
            skip_memory=True,skip_background_review=True,load_soul_identity=False,
            reasoning_callback=server._agent_cbs(self.sid)['reasoning_callback'],
            stream_delta_callback=lambda text: None)
        self.addCleanup(self.agent.client.close)
        server._sessions[self.sid] = {'agent':self.agent,'running':True,'show_reasoning':True,'profile_home':None}
        self.addCleanup(server._sessions.pop,self.sid,None)
        self.agent._current_turn_id = 'turn'
        self.agent._current_api_request_id = 'req'
        self.request = self.agent._build_api_kwargs([{'role':'user','content':'fixture'}],tools_for_api=[])
        self.assertEqual(self.request['_agy_session_id'],self.agent.session_id)
        self.assertFalse(_should_stream(self.agent))
        self.assertEqual(self.profile.auth_type,'external_process')
        self.spy = patch.object(server,'write_json',side_effect=lambda f:self.frames.append(f) or True)
        self.spy.start();self.addCleanup(self.spy.stop)

    def proc(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdin = io.StringIO()
        proc.stdout = io.StringIO('\n'.join(json.dumps(x) for x in [
            {'event':'init','init':{'conversation_id':'fixture'}},
            {'event':'step_update','step_update':{'thinking_delta':'private thinking'}},
            {'event':'text_delta','text_delta':'answer'},
            {'event':'result','result':{'status':'SUCCESS','response':'answer',
                'usage':{'input_tokens':3,'output_tokens':4,'total_tokens':7,'thinking_tokens':2}}},
        ])+'\n')
        proc.stderr = io.StringIO()
        return proc

    def perform(self):
        with patch.object(self.client_module,'is_authenticated',return_value=True), \
             patch.object(self.client_module.AntigravityClient,'_get_or_spawn_worker',return_value=self.proc()), \
             bind_subagent_parent(self.agent):
            return perform_api_call(self.agent,api_kwargs=self.request,_original_api_kwargs=dict(self.request),
                _llm_middleware_trace=[],_moa_prepared_request=None,_retry=NS(),thinking_spinner=None,
                retry_count=0,api_call_count=1,api_request_id='req',effective_task_id='task',turn_id='turn',interrupted=False).response

    def reasoning_frames(self):
        return [f for f in self.frames if f.get('params',{}).get('type') == 'reasoning.delta']

    def test_actual_nonstream_dispatch_and_no_post_response_duplicate(self):
        response = self.perform()
        self.assertEqual(len(self.reasoning_frames()),1)
        self.assertEqual(response.choices[0].message.reasoning_content,'private thinking')
        self.assertEqual(response.choices[0].message.content,'answer')
        from agent.chat_completion_helpers import _assistant_reasoning_text
        self.assertEqual(_assistant_reasoning_text(self.agent,response.choices[0].message),'private thinking')
        self.assertEqual(len(self.reasoning_frames()),1)
        self.assertEqual(response.usage.total_tokens,7)

    def test_hidden_and_muted_keep_response_but_no_event(self):
        server._sessions[self.sid]['show_reasoning'] = False
        self.assertEqual(self.perform().choices[0].message.reasoning_content,'private thinking')
        self.assertEqual(self.reasoning_frames(),[])
        server._sessions[self.sid]['show_reasoning'] = True
        with notification_turn(self.agent,muted=True,session_id=self.sid):
            self.assertEqual(self.perform().choices[0].message.reasoning_content,'private thinking')
        self.assertEqual(self.reasoning_frames(),[])

    def test_stream_true_uses_chunks_without_adapter_duplicate(self):
        self.agent._model_request_active.set()
        with patch.object(self.client_module,'is_authenticated',return_value=True), \
             patch.object(self.client_module.AntigravityClient,'_get_or_spawn_worker',return_value=self.proc()), \
             bind_subagent_parent(self.agent):
            def downstream(req):
                # Even a downstream middleware switching to stream=True cannot
                # cause the collector to emit; native consumer owns delivery.
                stream = self.agent.client.chat.completions.create(**{**req,'stream':True})
                chunks = list(stream)
                self.assertTrue(any(getattr(c.delta,'reasoning_content',None)
                    for chunk in chunks for c in chunk.choices))
            self.bridge.execution_middleware(request=self.request,next_call=downstream,
                provider='antigravity-oauth',session_id=self.agent.session_id,turn_id='turn',api_request_id='req',model=self.agent.model)
        self.assertEqual(self.reasoning_frames(),[])

    def test_provider_lifecycle_projection_idempotent_and_reinstalls_after_disposal(self):
        manager = get_plugin_manager()
        self.assertTrue(self.bridge._registrations[manager].active)
        before = len(manager._middleware['llm_execution'])
        self.profile.build_api_kwargs_extras(session_id='x')
        self.assertEqual(len(manager._middleware['llm_execution']),before)
        self.bridge._registrations[manager].dispose()
        self.profile.build_api_kwargs_extras(session_id='x')
        self.assertTrue(self.bridge._registrations[manager].active)
        self.assertEqual(len(manager._middleware['llm_execution']),before)
        self.assertNotIn('_agy_session_id',self.profile.build_api_kwargs_extras()[1])

    def test_launch_profile_lookup_and_closing_guard(self):
        live = self.bridge._live_session(self.agent.session_id,self.bridge._home())
        self.assertEqual(live[0],self.sid)
        server._sessions[self.sid]['_closing'] = True
        self.assertEqual(self.perform().choices[0].message.reasoning_content,'private thinking')
        self.assertEqual(self.reasoning_frames(),[])


    def test_oneshot_nonstream_collector_delivers_once(self):
        self.agent._model_request_active.set()
        client = self.agent.client
        client._worker_lock.acquire()
        try:
            with patch.object(self.client_module,'is_authenticated',return_value=True), \
                 patch.object(self.client_module.subprocess,'Popen',return_value=self.proc()), \
                 bind_subagent_parent(self.agent):
                response = self.bridge.execution_middleware(request=self.request,
                    next_call=lambda req:client.chat.completions.create(**req),
                    provider='antigravity-oauth',session_id=self.agent.session_id,
                    turn_id='turn',api_request_id='req',model=self.agent.model)
            self.assertEqual(response.choices[0].message.reasoning_content,'private thinking')
            self.assertEqual(len(self.reasoning_frames()),1)
        finally:
            client._worker_lock.release()


if __name__ == '__main__':
    unittest.main()
