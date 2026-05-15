"""Unit tests for lib/scan.py — the encoding and non-blocking-fetch fixes.

Run from the repo root with:

    python -m unittest discover

These tests make no real network calls: the Chuck Norris joke API is patched.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _REPO_ROOT / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import scan  # noqa: E402


class _FakeResponse:
    """Minimal stand-in for the object urllib.request.urlopen returns."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _join_celebrate_threads(timeout: float = 10.0) -> None:
    """Join any background joke-fetch threads so they don't leak across tests."""
    for t in threading.enumerate():
        if t.name == "celebrate-approval":
            t.join(timeout)


class ScanTestBase(unittest.TestCase):
    """Redirects scan's log dir to a temp dir so tests touch no real files."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._log_dir = Path(self._tmp.name)
        self._log_dir_patch = mock.patch.object(scan, "LOG_DIR", self._log_dir)
        self._log_dir_patch.start()

    def tearDown(self) -> None:
        _join_celebrate_threads()
        self._log_dir_patch.stop()
        self._tmp.cleanup()

    def read_log(self) -> str:
        return "".join(
            p.read_text(encoding="utf-8")
            for p in sorted(self._log_dir.glob("spectrabot-*.log"))
        )


class LogEncodingTests(ScanTestBase):
    def test_non_ascii_message_round_trips_as_utf8(self) -> None:
        msg = "café 日本語 🎉 — review approved"
        try:
            scan.log(msg)
        except UnicodeEncodeError as e:  # pragma: no cover - failure path
            self.fail(f"log() raised UnicodeEncodeError on non-ASCII input: {e}")

        log_files = list(self._log_dir.glob("spectrabot-*.log"))
        self.assertEqual(len(log_files), 1, "exactly one daily log file expected")
        # Decoding strictly as UTF-8 proves the bytes on disk are valid UTF-8.
        contents = log_files[0].read_bytes().decode("utf-8")
        self.assertIn(msg, contents)


class NonBlockingFetchTests(ScanTestBase):
    def test_celebrate_approval_returns_without_blocking(self) -> None:
        release = threading.Event()

        def slow_urlopen(req: object, timeout: float | None = None) -> _FakeResponse:
            # Block until released to simulate a slow / unreachable joke API.
            release.wait(timeout=10)
            return _FakeResponse(b'{"value": "Chuck wins"}')

        with mock.patch.object(scan.urllib.request, "urlopen", slow_urlopen):
            start = time.monotonic()
            scan.celebrate_approval("owner/repo#1")
            elapsed = time.monotonic() - start
            # The caller must return immediately; the fetch is on a daemon thread.
            self.assertLess(elapsed, 1.0, "celebrate_approval() blocked the caller")
            release.set()
            _join_celebrate_threads()

    def test_failed_fetch_is_swallowed(self) -> None:
        def failing_urlopen(req: object, timeout: float | None = None) -> _FakeResponse:
            raise OSError("simulated network failure")

        result: str | None
        with mock.patch.object(scan.urllib.request, "urlopen", failing_urlopen):
            try:
                result = scan.fetch_chuck_norris_joke()
            except Exception as e:  # noqa: BLE001 - the point is nothing escapes
                self.fail(f"fetch_chuck_norris_joke() raised: {e}")

        self.assertIsNone(result)
        self.assertIn("WARN", self.read_log())

    def test_successful_fetch_logs_joke_level_line(self) -> None:
        joke = "Chuck Norris can divide by zero."
        payload = json.dumps({"value": joke}).encode()

        def ok_urlopen(req: object, timeout: float | None = None) -> _FakeResponse:
            return _FakeResponse(payload)

        with mock.patch.object(scan.urllib.request, "urlopen", ok_urlopen):
            scan.celebrate_approval("owner/repo#7")
            _join_celebrate_threads()

        contents = self.read_log()
        self.assertIn("JOKE", contents)
        self.assertIn(f"[owner/repo#7] Chuck Norris says: {joke}", contents)


if __name__ == "__main__":
    unittest.main()
