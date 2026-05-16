"""Unit tests for lib/scan.py.

Run from the repo root with:
    python3 -m unittest discover -s tests
"""

import os
import sys
import unittest
from unittest import mock
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


class GhTokenTest(unittest.TestCase):
    def test_gh_without_token_inherits_ambient_env(self):
        """Omitting token must not pass an env kwarg (ambient gh auth)."""
        with mock.patch("scan.subprocess.check_output", return_value="ok") as co:
            scan.gh(["api", "user"])
        self.assertNotIn("env", co.call_args.kwargs)

    def test_gh_with_token_sets_gh_token_in_subprocess_env(self):
        with mock.patch("scan.subprocess.check_output", return_value="ok") as co:
            scan.gh(["api", "user"], token="t-secret")
        env = co.call_args.kwargs["env"]
        self.assertEqual(env["GH_TOKEN"], "t-secret")

    def test_gh_with_token_leaves_parent_environment_unchanged(self):
        before = os.environ.get("GH_TOKEN")
        with mock.patch("scan.subprocess.check_output", return_value="ok"):
            scan.gh(["api", "user"], token="t-secret")
        self.assertEqual(os.environ.get("GH_TOKEN"), before)

    def test_gh_token_env_keeps_other_parent_vars(self):
        with mock.patch.dict(os.environ, {"SPECTRA_MARKER": "keep"}):
            with mock.patch("scan.subprocess.check_output", return_value="ok") as co:
                scan.gh(["api", "user"], token="t-secret")
            self.assertEqual(co.call_args.kwargs["env"]["SPECTRA_MARKER"], "keep")

    def test_gh_json_forwards_token(self):
        with mock.patch.object(scan, "gh", return_value='{"login": "alice"}') as g:
            result = scan.gh_json(["api", "user"], token="t-secret")
        self.assertEqual(result, {"login": "alice"})
        self.assertEqual(g.call_args.kwargs.get("token"), "t-secret")


class PostReviewTokenTest(unittest.TestCase):
    def _ok_proc(self):
        return mock.Mock(returncode=0, stdout="", stderr="")

    def test_post_review_without_token_inherits_ambient_env(self):
        with mock.patch("scan.subprocess.run", return_value=self._ok_proc()) as run:
            scan.post_review("owner/repo", 1, {"event": "COMMENT", "body": "hi"})
        self.assertIsNone(run.call_args.kwargs["env"])

    def test_post_review_with_token_sets_gh_token(self):
        with mock.patch("scan.subprocess.run", return_value=self._ok_proc()) as run:
            scan.post_review(
                "owner/repo", 1, {"event": "COMMENT", "body": "hi"}, token="t-secret"
            )
        self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "t-secret")

    def test_post_review_retry_path_reuses_token(self):
        """Body-only retry must run under the same developer token."""
        rejected = mock.Mock(
            returncode=1, stdout="", stderr="line must be part of the diff"
        )
        calls = [rejected, self._ok_proc()]
        with mock.patch("scan.subprocess.run", side_effect=calls) as run:
            scan.post_review(
                "owner/repo",
                1,
                {
                    "event": "COMMENT",
                    "body": "hi",
                    "comments": [{"path": "f", "line": 1, "body": "x"}],
                },
                token="t-secret",
            )
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["GH_TOKEN"], "t-secret")


class ValidateDevelopersTest(unittest.TestCase):
    DEVS = [
        {"login": "alice", "token": "t-alice"},
        {"login": "bob", "token": "t-bob"},
    ]

    def test_empty_list_returns_empty(self):
        with mock.patch.object(scan, "gh") as g:
            self.assertEqual(scan.validate_developers([]), [])
        g.assert_not_called()

    def test_each_developer_resolved_with_own_token(self):
        with mock.patch.object(scan, "gh", side_effect=["alice", "bob"]) as g:
            result = scan.validate_developers(self.DEVS)
        self.assertEqual(result, self.DEVS)
        tokens = [c.kwargs.get("token") for c in g.call_args_list]
        self.assertEqual(tokens, ["t-alice", "t-bob"])

    def test_invalid_token_excluded_without_crashing(self):
        def fake_gh(args, token=None, **kwargs):
            if token == "t-bob":
                raise scan.subprocess.CalledProcessError(1, ["gh", *args])
            return "alice"

        with mock.patch.object(scan, "gh", side_effect=fake_gh):
            result = scan.validate_developers(self.DEVS)
        self.assertEqual(result, [{"login": "alice", "token": "t-alice"}])

    def test_login_mismatch_kept_but_warned(self):
        with mock.patch.object(scan, "gh", side_effect=["someone-else", "bob"]):
            with mock.patch.object(scan, "log") as logged:
                result = scan.validate_developers(self.DEVS)
        self.assertEqual(result, self.DEVS)
        warned = " ".join(
            str(c.args[0]) for c in logged.call_args_list
        )
        self.assertIn("alice", warned)
        self.assertIn("someone-else", warned)

    def test_one_bad_token_leaves_remaining_developers_usable(self):
        def fake_gh(args, token=None, **kwargs):
            if token == "t-alice":
                raise scan.subprocess.CalledProcessError(1, ["gh", *args])
            return "bob"

        with mock.patch.object(scan, "gh", side_effect=fake_gh):
            result = scan.validate_developers(self.DEVS)
        self.assertEqual(result, [{"login": "bob", "token": "t-bob"}])


class SelectReviewerTest(unittest.TestCase):
    DEVS = [
        {"login": "alice", "token": "t-alice"},
        {"login": "bob", "token": "t-bob"},
    ]

    @staticmethod
    def _pr(author):
        return {"author": {"login": author}}

    def test_no_developers_falls_back_to_ambient(self):
        result = scan.select_reviewer(self._pr("carol"), [])
        self.assertEqual(result, {"kind": "ambient", "developer": None})

    def test_first_non_author_developer_in_config_order(self):
        result = scan.select_reviewer(self._pr("carol"), self.DEVS)
        self.assertEqual(result["kind"], "developer")
        self.assertEqual(result["developer"], self.DEVS[0])

    def test_skips_author_and_picks_next_developer(self):
        result = scan.select_reviewer(self._pr("alice"), self.DEVS)
        self.assertEqual(result["kind"], "developer")
        self.assertEqual(result["developer"], self.DEVS[1])

    def test_all_developers_are_author_signals_fallback(self):
        only_alice = [{"login": "alice", "token": "t-alice"}]
        result = scan.select_reviewer(self._pr("alice"), only_alice)
        self.assertEqual(result["kind"], "author_fallback")
        self.assertEqual(result["developer"], only_alice[0])

    def test_skip_authors_is_not_consulted(self):
        """A developer is selectable even if their login is a skip_author —
        select_reviewer takes no skip list and must not filter on one."""
        result = scan.select_reviewer(self._pr("carol"), self.DEVS)
        self.assertEqual(result["developer"]["login"], "alice")


class DecideEventTest(unittest.TestCase):
    """decide_event decides the GitHub event against the *selected reviewer*,
    not a single global viewer."""

    def test_non_author_reviewer_maps_verdict_normally(self):
        """An eligible non-author reviewer may APPROVE."""
        self.assertEqual(
            scan.decide_event("approve", "alice", "carol", "auto"), "APPROVE"
        )
        self.assertEqual(
            scan.decide_event("request-changes", "alice", "carol", "auto"),
            "REQUEST_CHANGES",
        )
        self.assertEqual(
            scan.decide_event("comment", "alice", "carol", "auto"), "COMMENT"
        )

    def test_unknown_verdict_falls_back_to_comment(self):
        self.assertEqual(
            scan.decide_event("???", "alice", "carol", "auto"), "COMMENT"
        )

    def test_reviewer_is_author_forced_to_comment(self):
        """Author-fallback / ambient self-review: reviewer == author, so the
        verdict is downgraded to COMMENT regardless of recommendation."""
        self.assertEqual(
            scan.decide_event("approve", "alice", "alice", "auto"), "COMMENT"
        )
        self.assertEqual(
            scan.decide_event("request-changes", "alice", "alice", "auto"),
            "COMMENT",
        )

    def test_comment_mode_forces_comment_for_all_prs(self):
        self.assertEqual(
            scan.decide_event("approve", "alice", "carol", "comment"), "COMMENT"
        )
        self.assertEqual(
            scan.decide_event("request-changes", "alice", "carol", "comment"),
            "COMMENT",
        )


if __name__ == "__main__":
    unittest.main()
