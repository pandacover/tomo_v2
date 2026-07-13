import io
import unittest
from unittest.mock import patch

from tomo_core import latency_trace


class LatencyTraceTests(unittest.TestCase):
    trace_env = {"TOMO_LATENCY_TRACE": "1", "TOMO_LATENCY_TRACE_KEY": "test-latency-trace-key-at-least-32-bytes"}

    def test_disabled_is_silent(self):
        output = io.StringIO()

        with patch.dict("os.environ", {}, clear=True), patch("sys.stderr", output):
            latency_trace.emit("generation-secret", "dispatch_start", elapsed_ms=0)

        self.assertEqual(output.getvalue(), "")

    def test_enabled_emits_allowlisted_format_with_stable_opaque_handle(self):
        output = io.StringIO()

        with patch.dict("os.environ", self.trace_env, clear=True), patch("sys.stderr", output):
            latency_trace.emit("generation-secret", "dispatch_start", elapsed_ms=12, attempt=1)
            latency_trace.emit("generation-secret", "dispatch_start", elapsed_ms=13, attempt=2)

        lines = output.getvalue().splitlines()
        self.assertRegex(lines[0], r"^\[DEBUG-latency-v1\] phase=dispatch_start outcome=ok elapsed_ms=12 attempt=1 trace=[0-9a-f]{12}$")
        self.assertEqual(lines[0].rsplit("trace=", 1)[1], lines[1].rsplit("trace=", 1)[1])
        self.assertNotIn("generation-secret", output.getvalue())

    def test_unsafe_fields_are_rejected_without_output(self):
        output = io.StringIO()

        with patch.dict("os.environ", self.trace_env, clear=True), patch("sys.stderr", output):
            latency_trace.emit("generation-secret", "dispatch_start", elapsed_ms=1, text="private text")
            latency_trace.emit("generation-secret", "untrusted phase", elapsed_ms=1)
            latency_trace.emit("generation-secret", "dispatch_start", elapsed_ms=1, attempt="private text")

        self.assertEqual(output.getvalue(), "")

    def test_missing_trace_key_is_silent(self):
        output = io.StringIO()

        with patch.dict("os.environ", {"TOMO_LATENCY_TRACE": "1"}, clear=True), patch("sys.stderr", output):
            latency_trace.emit("generation-secret", "dispatch_start", elapsed_ms=1)

        self.assertEqual(output.getvalue(), "")

    def test_sink_failure_never_escapes(self):
        class BrokenSink:
            def write(self, _value):
                raise OSError("sink unavailable")

            def flush(self):
                raise OSError("sink unavailable")

        with patch.dict("os.environ", self.trace_env, clear=True), patch("sys.stderr", BrokenSink()):
            latency_trace.emit("generation-secret", "dispatch_start", elapsed_ms=1)
