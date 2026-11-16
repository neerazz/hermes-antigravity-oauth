"""Read-only DB contract and real-pipe live delivery regression coverage."""
import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from thinking import ThinkingReader, decode_thinking, MAX_PAYLOAD
from stream import AntigravityStream
from client import AntigravityClient


def varint(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def field(n, data):
    return varint(n * 8 + 2) + varint(len(data)) + data


def payload(text, raw=None):
    inner = field(3, text.encode())
    if raw is not None:
        inner += field(16, raw.encode())
    return field(20, inner)


class LiveThinkingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'conversations').mkdir()

    def db(self, name='exact'):
        path = self.root / 'conversations' / (name + '.db')
        c = sqlite3.connect(path)
        c.execute('CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, step_payload BLOB)')
        c.commit()
        c.close()
        return path

    def put(self, path, idx, text, raw=None):
        c = sqlite3.connect(path)
        c.execute('INSERT OR REPLACE INTO steps VALUES (?,15,1,?)', (idx, payload(text, raw)))
        c.commit()
        c.close()

    def test_decoder_prefers_raw_and_validates_entire_message(self):
        self.assertEqual(decode_thinking(payload('summary', 'raw reasoning')), 'raw reasoning')
        self.assertEqual(decode_thinking(payload('fallback', '')), 'fallback')
        for bad in [b'\0', b'\x0e', b'\x09x', b'\x12\x09x', b'\x80'*11,
                    payload('valid') + b'\0', field(20, field(3, b'\xff')),
                    varint(20*8)+b'\x01', b'x'*(MAX_PAYLOAD+1)]:
            with self.subTest(size=len(bad)), self.assertRaises((ValueError, UnicodeError)):
                decode_thinking(bad)

    def test_baseline_exact_conversation_and_append_only(self):
        path = self.db()
        other = self.db('other')
        self.put(path, 1, 'STALE')
        reader = ThinkingReader.before_request(self.root)
        self.put(other, 2, 'OTHER WORKER')
        self.put(path, 1, 'STALE MUTATED')
        self.put(path, 2, 'new')
        self.assertEqual(reader.poll('exact'), ['new'])
        self.assertEqual(reader.poll('exact'), [])
        self.put(path, 2, 'new growth')
        self.assertEqual(reader.poll('exact'), [' growth'])
        self.put(path, 2, 'new')
        self.assertEqual(reader.poll('exact'), [])
        self.put(path, 2, 'new growth again')
        self.put(path, 3, 'next step')
        self.assertEqual(reader.poll('exact'), ['next step'])
        self.assertIn('replacement', reader.diagnostics)
        reader.close()
        self.assertEqual(reader.poll('exact'), [])
        self.assertEqual(reader.poll('../other'), [])

    def test_missing_database_is_not_created_and_late_creation_works(self):
        reader = ThinkingReader.before_request(self.root)
        self.assertEqual(reader.poll('new'), [])
        self.assertFalse((self.root / 'conversations/new.db').exists())
        path = self.db('new')
        self.put(path, 0, 'live')
        self.assertEqual(reader.poll('new'), ['live'])

    def test_baseline_failure_disables_optional_reading(self):
        (self.root / 'conversations/bad.db').write_bytes(b'not a db')
        reader = ThinkingReader.before_request(self.root)
        self.assertTrue(reader.closed)
        self.assertEqual(reader.poll('bad'), [])
        self.assertEqual(reader.diagnostics, {'baseline'})

    def test_locked_or_malformed_data_only_degrades_reasoning(self):
        path = self.db()
        reader = ThinkingReader.before_request(self.root)
        c = sqlite3.connect(path)
        c.execute('BEGIN EXCLUSIVE')
        # Exercise the real lock, but check the configured SQLite wait rather
        # than CI wall time, which also includes scheduling/filesystem delays.
        with patch('thinking.sqlite3.connect', wraps=sqlite3.connect) as connect:
            self.assertEqual(reader.poll('exact'), [])
        connect.assert_called_once_with(
            path.absolute().as_uri() + '?mode=ro', uri=True, timeout=.05)
        c.rollback()
        c.execute('INSERT INTO steps VALUES (1,15,1,?)', (b'\0',))
        c.commit()
        c.close()
        self.assertEqual(reader.poll('exact'), [])
        self.put(path, 1, 'recovered')
        self.assertEqual(reader.poll('exact'), ['recovered'])
        self.assertLessEqual(len(reader.diagnostics), 4)

    def pipe_stream(self, timeout=2):
        read_fd, write_fd = os.pipe()
        out = os.fdopen(read_fd, 'r')
        writer = os.fdopen(write_fd, 'w', buffering=1)
        self.addCleanup(out.close)
        self.addCleanup(writer.close)
        proc = MagicMock()
        proc.stdout = out
        proc.stderr = io.StringIO('')
        proc.poll.return_value = None
        client = MagicMock()
        client._worker_proc = proc
        client._worker_conversation_id = 'exact'
        client._isolated_gemini_dir = None
        client._terminate_worker.side_effect = lambda: writer.close()
        lock = threading.Lock()
        lock.acquire()
        reader = ThinkingReader.before_request(self.root)
        stream = AntigravityStream(proc=proc, client=client, model='test', timeout=timeout,
            is_worker=True, worker_lock=lock, worker_lock_held=True, thinking_reader=reader)
        self.addCleanup(stream.close)
        return stream, reader, writer, lock, client

    def test_stream_thinking_delta_closes_db_sidechannel(self):
        # Models/agy builds that emit thinking_delta on stream-json must not
        # have the same reasoning delivered again from the conversation DB.
        path = self.db()
        stream, reader, writer, lock, client = self.pipe_stream()
        writer.write(json.dumps({'event':'step_update','step_update':{'thinking_delta':'from stream'}})+'\n')
        chunk = next(stream)
        self.assertEqual(chunk.choices[0].delta.reasoning_content, 'from stream')
        self.assertTrue(reader.closed)
        self.put(path, 1, 'from stream')  # DB catches up with identical text
        writer.write(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'answer'}})+'\n')
        rest = list(stream)
        reasoning = [c.choices[0].delta.reasoning_content for c in rest if c.choices and c.choices[0].delta.reasoning_content]
        self.assertEqual(reasoning, [])
        self.assertEqual(''.join(c.choices[0].delta.content or '' for c in rest if c.choices), 'answer')

    def test_reasoning_precedes_any_stdout_and_result_reader_does_not_leak(self):
        path = self.db()
        stream, reader, writer, lock, client = self.pipe_stream()
        self.put(path, 1, 'live before stdout')
        chunk = next(stream)
        self.assertEqual(chunk.choices[0].delta.reasoning_content, 'live before stdout')
        self.assertIsNone(chunk.choices[0].delta.content)
        writer.write(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'answer'}})+'\n')
        list(stream)
        self.assertFalse(stream._reader_thread.is_alive())
        self.assertTrue(reader.closed)
        self.assertFalse(lock.locked())
        client._terminate_worker.assert_not_called()
        # Persistent worker remains writable and its next line wasn't stolen.
        writer.write('next-turn\n')
        self.assertEqual(stream.proc.stdout.readline(), 'next-turn\n')

    def test_timeout_closes_reader_and_releases_lock(self):
        stream, reader, writer, lock, client = self.pipe_stream(timeout=.15)
        with self.assertRaises(RuntimeError):
            list(stream)
        self.assertTrue(reader.closed)
        self.assertFalse(stream._reader_thread.is_alive())
        self.assertFalse(lock.locked())
        client._terminate_worker.assert_called_once()

    def test_async_cancel_closes_reader_and_releases_lock(self):
        stream, reader, writer, lock, client = self.pipe_stream()
        async def cancel():
            task = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(cancel())
        self.assertTrue(reader.closed)
        self.assertFalse(stream._reader_thread.is_alive())
        self.assertFalse(lock.locked())

    def test_client_snapshots_boundary_before_stdin_write(self):
        path = self.db()
        self.put(path, 1, 'stale')
        with patch('client.is_authenticated', return_value=True):
            client = AntigravityClient(cwd=str(self.root / 'client'))
            self.addCleanup(client.close)
            client._isolated_gemini_dir = self.root
            proc = MagicMock()
            proc.poll.return_value = None
            client._get_or_spawn_worker = lambda *args: proc
            client._sync_workspace_rules = lambda *args: False
            client._resolve_model_and_effort = lambda *args: ('test', None)
            proc.stdin.write.side_effect = lambda data: self.put(path, 2, 'arrived during send')
            stream = client.chat.completions.create(messages=[{'role':'user','content':'q'}],stream=True)
            self.addCleanup(stream.close)
            self.assertEqual(stream._thinking_reader.poll('exact'), ['arrived during send'])


if __name__ == '__main__':
    unittest.main()
