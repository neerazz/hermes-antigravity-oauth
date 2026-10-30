import subprocess
import time
import unittest
from unittest.mock import MagicMock
from stream import AntigravityStream

class TestStreamGeneratorPerformance(unittest.TestCase):
    def test_stream_generator_avoids_spin_loop_latency_on_eof(self):
        """
        Verify that _stream_generator detects EOF immediately without artificial latency.
        In the old implementation, an empty line from stdout caused a time.sleep(0.01)
        before checking proc.poll() or continuing, creating a ~10ms minimum delay on exit.
        """
        import sys
        import json

        # A simple process that writes a valid result JSON and then exits, closing stdout.
        # This prevents the "no result event" RuntimeError.
        result_json = json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "ok"
            }
        })

        proc = subprocess.Popen(
            [sys.executable, "-c", f"import sys; print('{result_json}'); sys.stdout.flush()"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Mock client to prevent external calls
        mock_client = MagicMock()
        mock_client._isolated_gemini_dir = None

        stream = AntigravityStream(
            proc=proc,
            client=mock_client,
            model="test-model",
            timeout=5.0
        )

        # Let the child finish first so the timer measures only EOF detection,
        # not interpreter startup (which alone can exceed 50 ms on a loaded host).
        proc.wait(timeout=10)

        start_time = time.monotonic()

        # Iterate over the generator. It should yield the content and finish chunk.
        chunks = []
        try:
            for chunk in stream._stream_generator():
                chunks.append(chunk)
        except StopIteration:
            pass

        duration = time.monotonic() - start_time

        # Old implementation with time.sleep(0.01) might spin for a bit.
        # The new implementation should take < 5ms. We assert it's less than 50ms
        # to be safe against CI jitter, but usually it's ~0.5ms.
        self.assertLess(duration, 0.05, f"Generator took too long to detect EOF: {duration}s")

        # Verify it finished correctly
        self.assertTrue(stream._finished)
        self.assertTrue(len(chunks) >= 2) # content chunk and completion chunk

if __name__ == '__main__':
    unittest.main()
