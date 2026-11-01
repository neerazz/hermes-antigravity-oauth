"""Offline cache/refresh regressions; CLI and authentication are mocked."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usage
from test_account_usage import LIVE_RESPONSE

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)

def report():
    body = copy.deepcopy(LIVE_RESPONSE)
    body['command']['data']['groups'][0]['buckets'].append({
        'id': 'gemini-5h', 'window': '5h', 'remaining_fraction': .05,
        'reset_time': '2026-10-02T05:00:00Z'})
    return usage.UsageReport(tuple(usage.usage_windows(body['command']['data'])), body)

class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # macOS /var is a symlink; the product's state-dir guard (correctly)
        # refuses symlink ancestors, so resolve the scratch root first.
        self.home = Path(os.path.realpath(self.tmp.name))
        self.env = patch.dict(os.environ, {'HERMES_HOME': str(self.home)})
        self.env.start(); self.addCleanup(self.env.stop)

    def refresh(self):
        with patch.object(usage, 'fetch_usage_report', return_value=report()):
            return usage.refresh_usage(now=NOW)

    def test_missing_status_is_read_only_and_never_probes(self):
        with patch.object(usage, 'fetch_usage_report', side_effect=AssertionError('CLI')), \
             patch.object(usage, 'is_authenticated', side_effect=AssertionError('auth')), \
             patch.object(usage.subprocess, 'Popen', side_effect=AssertionError('process')):
            self.assertEqual(usage.read_usage_status(now=NOW)['snapshot']['freshness'], 'missing')
            self.assertIsNone(usage.read_cached_usage_report(now=NOW))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_refresh_writes_sanitized_private_cache_and_reads_both_windows(self):
        self.assertTrue(self.refresh()['ok'])
        status = usage.read_usage_status(now=NOW)['snapshot']
        self.assertEqual(status['freshness'], 'fresh')
        self.assertEqual({m['id'] for m in status['meters']}, {'gemini-5h', 'gemini-weekly', '3p-weekly'})
        self.assertNotIn('response', usage.cache_path().read_text())
        if os.name != 'nt':
            self.assertEqual(usage.cache_path().stat().st_mode & 0o777, 0o600)
        cached = usage.read_cached_usage_report(now=NOW)
        self.assertEqual(cached.fetched_at, NOW)

    def test_populated_cached_reads_never_spawn_or_probe_and_keep_observation(self):
        self.refresh()
        with patch.object(usage, 'fetch_usage_report', side_effect=AssertionError('CLI')), \
             patch.object(usage, 'is_authenticated', side_effect=AssertionError('auth')), \
             patch.object(usage.subprocess, 'Popen', side_effect=AssertionError('process')):
            for _ in range(3):
                cached = usage.read_cached_usage_report(now=NOW)
                self.assertEqual(cached.fetched_at, NOW)
                self.assertEqual(len(cached.windows), 3)

    def test_interprocess_refresh_lock_refuses_before_cli(self):
        with usage._refresh_lock(usage.cache_path()):
            code = "import usage; from unittest.mock import patch; p=patch.object(usage,'fetch_usage_report',side_effect=AssertionError('CLI')); p.start(); print(usage.refresh_usage()['error']['code'])"
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'refresh_busy')

    def test_atomic_publication_failure_retains_original_complete_bytes(self):
        self.refresh(); before = usage.cache_path().read_bytes()
        with patch.object(usage, 'fetch_usage_report', return_value=report()), \
             patch.object(usage.os, 'replace', side_effect=OSError('disk unavailable')):
            self.assertFalse(usage.refresh_usage(now=NOW)['ok'])
        self.assertEqual(usage.cache_path().read_bytes(), before)
        self.assertEqual(list(usage.cache_path().parent.glob('.quota-*')), [])

    def test_weekly_only_never_invents_five_hour_data(self):
        r = report(); r.raw['command']['data']['groups'][0]['buckets'].pop()
        with patch.object(usage, 'fetch_usage_report', return_value=r):
            usage.refresh_usage(now=NOW)
        status = usage.read_usage_status(now=NOW)['snapshot']
        self.assertFalse(any(m['id'].endswith('5h') for m in status['meters']))
        self.assertTrue(all(not p['available'] for p in status['pools']))

    def test_argon_is_optional_third_pool_and_absent_without_group(self):
        self.assertTrue(self.refresh()['ok'])
        self.assertNotIn('Gemini 4 Argon', [p['pool'] for p in usage.read_usage_status(now=NOW)['snapshot']['pools']])
        r = report()
        r.raw['command']['data']['groups'].append({'name': 'Gemini 4 Argon', 'buckets': [
            {'id': 'gemini-argon-5h', 'window': '5h', 'remaining_fraction': .6, 'reset_time': '2026-10-02T05:00:00Z'},
            {'id': 'gemini-argon-wtu-weekly', 'window': 'weekly', 'remaining_fraction': .4, 'reset_time': '2026-10-08T00:00:00Z'}]})
        with patch.object(usage, 'fetch_usage_report', return_value=r):
            self.assertTrue(usage.refresh_usage(now=NOW)['ok'])
        status = usage.read_usage_status(now=NOW)['snapshot']
        argon = [m for m in status['meters'] if m['group'] == 'Gemini 4 Argon']
        self.assertEqual({m['id'] for m in argon}, {'gemini-argon-5h', 'gemini-argon-wtu-weekly'})
        pools = {p['pool']: p['available'] for p in status['pools']}
        self.assertTrue(pools['Gemini 4 Argon'])
        self.assertIn('Gemini Models', pools); self.assertIn('Claude and GPT models', pools)
        self.assertIsNotNone(usage.read_cached_usage_report(now=NOW))

    def test_argon_rename_and_id_drift_map_to_canonical_without_inventing_5h(self):
        r = report()
        r.raw['command']['data']['groups'].append({'name': 'Argon (Preview)', 'buckets': [
            {'id': 'argon_weekly', 'window': 'weekly', 'remaining_fraction': .3, 'reset_time': '2026-10-08T00:00:00Z'}]})
        with patch.object(usage, 'fetch_usage_report', return_value=r):
            self.assertTrue(usage.refresh_usage(now=NOW)['ok'])
        status = usage.read_usage_status(now=NOW)['snapshot']
        argon = [m for m in status['meters'] if m['group'] == 'Gemini 4 Argon']
        self.assertEqual([m['id'] for m in argon], ['gemini-argon-wtu-weekly'])
        self.assertTrue({p['pool']: p['available'] for p in status['pools']}['Gemini 4 Argon'])
        self.assertEqual({m['id'] for m in status['meters'] if m['group'] != 'Gemini 4 Argon'},
                         {'gemini-5h', 'gemini-weekly', '3p-weekly'})

    def test_stale_due_and_clock_regression_never_claim_refill(self):
        self.refresh()
        for when in (NOW + timedelta(minutes=30), NOW - timedelta(seconds=1), NOW + timedelta(hours=6)):
            view = usage.read_usage_status(now=when)['snapshot']
            self.assertTrue(view['stale'])
            self.assertFalse(any(p['available'] for p in view['pools']))
        due = usage.read_usage_status(now=NOW + timedelta(hours=6))['snapshot']
        meter = next(m for m in due['meters'] if m['id'] == 'gemini-5h')
        self.assertTrue(meter['reset_due'])
        self.assertEqual(meter['remaining_fraction'], .05)

    def test_failure_retains_values_but_marks_health_error(self):
        self.refresh()
        with patch.object(usage, 'fetch_usage_report', return_value=None):
            self.assertFalse(usage.refresh_usage(now=NOW + timedelta(seconds=1))['ok'])
        view = usage.read_usage_status(now=NOW + timedelta(seconds=2))['snapshot']
        self.assertEqual(view['freshness'], 'error')
        self.assertEqual(view['observed_at'], NOW.isoformat())
        self.assertEqual(len(view['meters']), 3)

    def test_concurrent_refresh_is_refused_without_another_cli_call(self):
        entered, release = threading.Event(), threading.Event()
        def held():
            entered.set(); self.assertTrue(release.wait(3)); return report()
        with patch.object(usage, 'fetch_usage_report', side_effect=held) as fetch:
            worker = threading.Thread(target=lambda: usage.refresh_usage(now=NOW))
            worker.start(); self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(usage.refresh_usage(now=NOW)['error']['code'], 'refresh_busy')
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(usage.read_usage_status(now=NOW)['snapshot']['freshness'], 'missing')
            finally:
                release.set(); worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_symlink_cache_and_lock_refuse_without_touching_target(self):
        outside = self.home / 'untouched'; outside.write_text('keep')
        for name in ('latest.json', 'refresh.lock'):
            with self.subTest(name=name):
                path = usage.cache_path().parent / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    path.unlink()
                path.symlink_to(outside)
                with patch.object(usage, 'fetch_usage_report', side_effect=AssertionError('CLI')):
                    self.assertFalse(usage.refresh_usage(now=NOW)['ok'])
                self.assertEqual(outside.read_text(), 'keep')
                path.unlink()

    def test_corrupt_oversized_and_nonfinite_cache_is_unverified(self):
        path = usage.cache_path(); path.parent.mkdir(parents=True)
        for value in ('{', 'x' * (usage.MAX_CACHE_BYTES + 1), '{"schema_version":1,"observed_at":"NaN"}'):
            path.write_text(value)
            self.assertEqual(usage.read_usage_status(now=NOW)['snapshot']['freshness'], 'error')

    def test_parser_rejects_nonfinite_fraction(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            data = {'groups': [{'name': 'Gemini Models', 'buckets': [{'window': '5h', 'remaining_fraction': value}]}]}
            self.assertEqual(usage.usage_windows(data), [])

    def test_invalid_refresh_marks_retained_observation_unverified(self):
        self.refresh()
        invalid = report()
        invalid.raw['command']['data']['groups'][0]['buckets'][0]['remaining_fraction'] = float('nan')
        with patch.object(usage, 'fetch_usage_report', return_value=invalid):
            self.assertFalse(usage.refresh_usage(now=NOW)['ok'])
        self.assertEqual(usage.read_usage_status(now=NOW)['snapshot']['freshness'], 'error')

    def test_symlink_ancestor_is_refused_before_creating_directories(self):
        target = self.home / 'target'; target.mkdir()
        (self.home / 'plugin-data').symlink_to(target, target_is_directory=True)
        with patch.object(usage, 'fetch_usage_report', side_effect=AssertionError('CLI')):
            self.assertFalse(usage.refresh_usage(now=NOW)['ok'])
        self.assertEqual(list(target.iterdir()), [])

    def test_five_hour_label_and_zero_token_guard(self):
        self.assertIn('Gemini 5h', [w.label for w in report().windows])
        for turns in (True, '0', None):
            body = copy.deepcopy(LIVE_RESPONSE); body['num_turns'] = turns
            self.assertIsNone(usage.parse_usage_response(json.dumps(body)))
        body = copy.deepcopy(LIVE_RESPONSE); body['usage']['input_tokens'] = 1
        self.assertIsNone(usage.parse_usage_response(json.dumps(body)))

if __name__ == '__main__':
    unittest.main()
