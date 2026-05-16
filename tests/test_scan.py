"""Unit tests for lib/scan.py.

Run from the repo root with:
    python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import scan  # noqa: E402


class ParseDevelopersTest(unittest.TestCase):
    def test_absent_returns_empty(self):
        """No [[github.developers]] keeps single-user / ambient-auth behavior."""
        self.assertEqual(scan.parse_developers({}), [])
        self.assertEqual(scan.parse_developers({"github": {}}), [])

    def test_empty_returns_empty(self):
        self.assertEqual(
            scan.parse_developers({"github": {"developers": []}}), []
        )

    def test_valid_developers_preserve_config_order(self):
        cfg = {
            "github": {
                "developers": [
                    {"login": "alice", "token": "t-alice"},
                    {"login": "bob", "token": "t-bob"},
                ]
            }
        }
        self.assertEqual(
            scan.parse_developers(cfg),
            [
                {"login": "alice", "token": "t-alice"},
                {"login": "bob", "token": "t-bob"},
            ],
        )

    def test_missing_token_aborts_naming_entry(self):
        cfg = {"github": {"developers": [{"login": "alice"}]}}
        with self.assertRaises(SystemExit) as ctx:
            scan.parse_developers(cfg)
        message = str(ctx.exception)
        self.assertIn("alice", message)
        self.assertIn("token", message)

    def test_missing_login_aborts(self):
        cfg = {"github": {"developers": [{"token": "t-x"}]}}
        with self.assertRaises(SystemExit) as ctx:
            scan.parse_developers(cfg)
        self.assertIn("login", str(ctx.exception))


class ParseAuthorFallbackTest(unittest.TestCase):
    def test_default_is_comment(self):
        self.assertEqual(scan.parse_author_fallback({}), "comment")
        self.assertEqual(scan.parse_author_fallback({"review": {}}), "comment")

    def test_skip_is_allowed(self):
        cfg = {"review": {"author_fallback": "skip"}}
        self.assertEqual(scan.parse_author_fallback(cfg), "skip")

    def test_invalid_value_aborts(self):
        cfg = {"review": {"author_fallback": "bogus"}}
        with self.assertRaises(SystemExit):
            scan.parse_author_fallback(cfg)


if __name__ == "__main__":
    unittest.main()
