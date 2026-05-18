"""Unit tests for lib/scan.py.

Run from the repo root with:
    python3 -m unittest discover -s tests
"""

import os
import subprocess
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


class ParseSelfReviewTest(unittest.TestCase):
    def test_absent_returns_no_override(self):
        """No [review] self-token config leaves multi-developer behavior."""
        self.assertEqual(
            scan.parse_self_review({}), {"token": None, "repos": []}
        )
        self.assertEqual(
            scan.parse_self_review({"review": {}}),
            {"token": None, "repos": []},
        )

    def test_empty_self_review_repos_returns_no_override(self):
        cfg = {"review": {"self_review_repos": []}}
        self.assertEqual(
            scan.parse_self_review(cfg), {"token": None, "repos": []}
        )

    def test_token_and_repos_are_read(self):
        cfg = {
            "review": {
                "self_token": "t-self",
                "self_review_repos": ["octocat/hello", "octocat/world"],
            }
        }
        self.assertEqual(
            scan.parse_self_review(cfg),
            {"token": "t-self", "repos": ["octocat/hello", "octocat/world"]},
        )

    def test_repos_without_token_aborts(self):
        cfg = {"review": {"self_review_repos": ["octocat/hello"]}}
        with self.assertRaises(SystemExit) as ctx:
            scan.parse_self_review(cfg)
        message = str(ctx.exception)
        self.assertIn("self_token", message)
        self.assertIn("self_review_repos", message)

    def test_token_without_repos_is_allowed(self):
        cfg = {"review": {"self_token": "t-self"}}
        self.assertEqual(
            scan.parse_self_review(cfg), {"token": "t-self", "repos": []}
        )

    def test_non_list_self_review_repos_aborts(self):
        cfg = {"review": {"self_token": "t", "self_review_repos": "octocat/hello"}}
        with self.assertRaises(SystemExit):
            scan.parse_self_review(cfg)

    def test_non_string_repo_entry_aborts(self):
        cfg = {"review": {"self_token": "t", "self_review_repos": [123]}}
        with self.assertRaises(SystemExit):
            scan.parse_self_review(cfg)


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


class GitTokenTest(unittest.TestCase):
    def test_git_without_token_inherits_ambient_env(self):
        """Omitting token must not pass an env kwarg (ambient git credentials)."""
        with mock.patch("scan.subprocess.check_output", return_value="ok") as co:
            scan.git(["status"])
        self.assertNotIn("env", co.call_args.kwargs)

    def test_git_with_token_injects_extraheader(self):
        with mock.patch("scan.subprocess.check_output", return_value="ok") as co:
            scan.git(["status"], token="t-secret")
        env = co.call_args.kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraheader")
        self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic "))

    def test_git_token_is_not_passed_in_argv(self):
        with mock.patch("scan.subprocess.check_output", return_value="ok") as co:
            scan.git(["status"], token="t-secret")
        self.assertNotIn("t-secret", co.call_args.args[0])

    def test_git_with_token_leaves_parent_environment_unchanged(self):
        before = os.environ.get("GIT_CONFIG_COUNT")
        with mock.patch("scan.subprocess.check_output", return_value="ok"):
            scan.git(["status"], token="t-secret")
        self.assertEqual(os.environ.get("GIT_CONFIG_COUNT"), before)

    def test_git_token_env_keeps_other_parent_vars(self):
        with mock.patch.dict(os.environ, {"SPECTRA_MARKER": "keep"}):
            with mock.patch("scan.subprocess.check_output", return_value="ok") as co:
                scan.git(["status"], token="t-secret")
            self.assertEqual(co.call_args.kwargs["env"]["SPECTRA_MARKER"], "keep")

    def test_git_forwards_cwd(self):
        with mock.patch("scan.subprocess.check_output", return_value="ok") as co:
            scan.git(["status"], cwd=Path("/tmp/repo"))
        self.assertEqual(co.call_args.kwargs["cwd"], "/tmp/repo")


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


class ValidateSelfTokenTest(unittest.TestCase):
    """validate_self_token resolves the operator's self_token to a login,
    failing soft (WARN + None) when the token is bad."""

    def test_unset_token_returns_none_without_calling_gh(self):
        with mock.patch.object(scan, "gh") as g:
            self.assertIsNone(scan.validate_self_token(None))
            self.assertIsNone(scan.validate_self_token(""))
        g.assert_not_called()

    def test_valid_token_resolves_login_using_that_token(self):
        with mock.patch.object(scan, "gh", return_value="operator\n") as g:
            self.assertEqual(scan.validate_self_token("t-self"), "operator")
        self.assertEqual(g.call_args.kwargs.get("token"), "t-self")

    def test_invalid_token_returns_none_and_warns(self):
        err = scan.subprocess.CalledProcessError(1, ["gh", "api", "user"])
        with mock.patch.object(scan, "gh", side_effect=err):
            with mock.patch.object(scan, "log") as logged:
                self.assertIsNone(scan.validate_self_token("t-bad"))
        warned = " ".join(str(c.args[0]) for c in logged.call_args_list)
        self.assertIn("self_token", warned)


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


class ReviewerCandidatesTest(unittest.TestCase):
    DEVS = [
        {"login": "alice", "token": "t-alice"},
        {"login": "bob", "token": "t-bob"},
    ]

    @staticmethod
    def _pr(author):
        return {"author": {"login": author}}

    def test_no_developers_yields_single_ambient_candidate(self):
        self.assertEqual(
            scan.reviewer_candidates(self._pr("carol"), [], "spectrabot"),
            {
                "kind": "ambient",
                "candidates": [{"login": "spectrabot", "token": None}],
            },
        )

    def test_non_author_developers_in_config_order(self):
        result = scan.reviewer_candidates(self._pr("carol"), self.DEVS, "viewer")
        self.assertEqual(result["kind"], "developer")
        self.assertEqual(result["candidates"], self.DEVS)

    def test_pr_author_is_dropped_from_candidates(self):
        result = scan.reviewer_candidates(self._pr("alice"), self.DEVS, "viewer")
        self.assertEqual(result["kind"], "developer")
        self.assertEqual(result["candidates"], [{"login": "bob", "token": "t-bob"}])

    def test_all_developers_are_author_returns_all_as_fallback(self):
        """Author-fallback: every dev is the author, so all are candidates."""
        only_alice = [{"login": "alice", "token": "t-alice"}]
        result = scan.reviewer_candidates(self._pr("alice"), only_alice, "viewer")
        self.assertEqual(result["kind"], "author_fallback")
        self.assertEqual(result["candidates"], only_alice)

    def test_skip_authors_is_not_consulted(self):
        """reviewer_candidates takes no skip list — a developer is still
        selectable even if their login also appears in skip_authors."""
        result = scan.reviewer_candidates(self._pr("carol"), self.DEVS, "viewer")
        self.assertEqual(result["candidates"][0]["login"], "alice")


class ReviewOnePrTest(unittest.TestCase):
    PR = {"number": 7, "headRefOid": "sha123", "author": {"login": "carol"}}
    CFG = {"review": {"mode": "auto"}}

    def _run(self, candidates, read_token=None, dry_run=False, post=None):
        review = {"recommendation": "approve", "summary": "looks good"}
        with mock.patch.object(
            scan, "fetch_pr_context", return_value=({"title": "t"}, "diff\n")
        ) as fetch, mock.patch.object(
            scan, "invoke_engine", return_value=review
        ), mock.patch.object(
            scan, "post_review", side_effect=post
        ) as posted, mock.patch.object(
            scan, "celebrate_approval"
        ):
            result = scan.review_one_pr(
                "owner/repo", self.PR, "prompt", self.CFG,
                candidates, read_token, dry_run,
            )
        return result, fetch, posted

    def test_posts_with_first_candidates_token(self):
        candidates = [{"login": "alice", "token": "t-alice"}]
        _, _, posted = self._run(candidates)
        self.assertEqual(posted.call_count, 1)
        self.assertEqual(posted.call_args.kwargs["token"], "t-alice")

    def test_reads_use_the_read_token(self):
        candidates = [{"login": "alice", "token": "t-alice"}]
        _, fetch, _ = self._run(candidates, read_token="t-read")
        self.assertEqual(fetch.call_args.kwargs["token"], "t-read")

    def test_falls_through_to_next_developer_on_posting_failure(self):
        candidates = [
            {"login": "alice", "token": "t-alice"},
            {"login": "bob", "token": "t-bob"},
        ]
        post = [RuntimeError("revoked token"), None]
        _, _, posted = self._run(candidates, post=post)
        self.assertEqual(posted.call_count, 2)
        self.assertEqual(
            [c.kwargs["token"] for c in posted.call_args_list],
            ["t-alice", "t-bob"],
        )

    def test_fails_pr_only_when_every_candidate_fails(self):
        candidates = [
            {"login": "alice", "token": "t-alice"},
            {"login": "bob", "token": "t-bob"},
        ]
        post = [RuntimeError("alice bad"), RuntimeError("bob bad")]
        with self.assertRaises(RuntimeError) as ctx:
            self._run(candidates, post=post)
        self.assertIn("alice", str(ctx.exception))
        self.assertIn("bob", str(ctx.exception))

    def test_dry_run_posts_nothing_and_logs_reviewer_login(self):
        candidates = [{"login": "alice", "token": "t-alice"}]
        with mock.patch.object(scan, "log") as logged:
            _, _, posted = self._run(candidates, dry_run=True)
        posted.assert_not_called()
        logged_text = " ".join(str(c.args[0]) for c in logged.call_args_list)
        self.assertIn("alice", logged_text)

    def test_result_records_posting_developer_login(self):
        """The result's reviewer field is the developer that actually posted."""
        candidates = [{"login": "alice", "token": "t-alice"}]
        result, _, _ = self._run(candidates)
        self.assertEqual(result["reviewer"], "alice")

    def test_result_reviewer_is_the_fallthrough_developer(self):
        """When the first candidate fails, reviewer is the one that succeeded."""
        candidates = [
            {"login": "alice", "token": "t-alice"},
            {"login": "bob", "token": "t-bob"},
        ]
        post = [RuntimeError("revoked token"), None]
        result, _, _ = self._run(candidates, post=post)
        self.assertEqual(result["reviewer"], "bob")

    def test_dry_run_result_records_first_candidate_login(self):
        candidates = [{"login": "alice", "token": "t-alice"}]
        result, _, _ = self._run(candidates, dry_run=True)
        self.assertEqual(result["reviewer"], "alice")

    def _run_oversized(self, candidates, dry_run=False, post=None):
        """Run with a diff that exceeds max_diff_lines (capped at 2 here)."""
        cfg = {"review": {"mode": "auto", "max_diff_lines": 2}}
        big_diff = "line\n" * 20
        with mock.patch.object(
            scan, "fetch_pr_context", return_value=({"title": "t"}, big_diff)
        ), mock.patch.object(
            scan, "invoke_engine"
        ) as engine, mock.patch.object(
            scan, "post_review", side_effect=post
        ) as posted, mock.patch.object(
            scan, "celebrate_approval"
        ):
            result = scan.review_one_pr(
                "owner/repo", self.PR, "prompt", cfg,
                candidates, None, dry_run,
            )
        return result, posted, engine

    def test_oversized_pr_posts_size_comment_without_running_engine(self):
        candidates = [{"login": "alice", "token": "t-alice"}]
        result, posted, engine = self._run_oversized(candidates)
        engine.assert_not_called()
        payload = posted.call_args.args[2]
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["body"], scan.OVERSIZED_PR_COMMENT)
        self.assertEqual(result["verdict"], "too-large")

    def test_oversized_pr_dry_run_posts_nothing(self):
        candidates = [{"login": "alice", "token": "t-alice"}]
        result, posted, _ = self._run_oversized(candidates, dry_run=True)
        posted.assert_not_called()
        self.assertEqual(result["verdict"], "too-large")


class FetchSpectrabotThreadsTest(unittest.TestCase):
    """fetch_spectrabot_threads matches threads opened by any configured
    developer login, not a single viewer."""

    @staticmethod
    def _graphql(thread_authors):
        """Build a graphql response whose review threads' first comments are
        authored by the given logins. `thread_authors` is a list of
        (login, isResolved, isOutdated) tuples."""
        nodes = [
            {
                "isResolved": resolved,
                "isOutdated": outdated,
                "comments": {"nodes": [{"author": {"login": login}}]},
            }
            for login, resolved, outdated in thread_authors
        ]
        return {
            "data": {
                "repository": {
                    "pullRequest": {"reviewThreads": {"nodes": nodes}}
                }
            }
        }

    def test_matches_any_configured_developer_login(self):
        response = self._graphql([
            ("alice", True, False),
            ("bob", False, False),
            ("carol", False, False),  # PR author, not a developer
        ])
        with mock.patch.object(scan, "gh_json", return_value=response):
            threads = scan.fetch_spectrabot_threads(
                "owner/repo", 1, {"alice", "bob"}
            )
        self.assertEqual(
            threads,
            [
                {"resolved": True, "outdated": False},
                {"resolved": False, "outdated": False},
            ],
        )

    def test_ignores_threads_from_unknown_authors(self):
        response = self._graphql([("stranger", True, False)])
        with mock.patch.object(scan, "gh_json", return_value=response):
            threads = scan.fetch_spectrabot_threads(
                "owner/repo", 1, {"alice", "bob"}
            )
        self.assertEqual(threads, [])

    def test_forwards_read_token(self):
        with mock.patch.object(scan, "gh_json", return_value={}) as gj:
            scan.fetch_spectrabot_threads(
                "owner/repo", 1, {"alice"}, token="t-read"
            )
        self.assertEqual(gj.call_args.kwargs.get("token"), "t-read")


class PreviouslyReviewedActionTest(unittest.TestCase):
    PR = {"number": 5, "headRefOid": "sha-new"}

    def test_resolved_threads_yield_approve(self):
        entry = {
            "head_sha": "sha-old",
            "verdict": "request-changes",
            "inline_comment_count": 2,
        }
        with mock.patch.object(
            scan, "fetch_spectrabot_threads",
            return_value=[{"resolved": True, "outdated": False}],
        ) as ft:
            action = scan.previously_reviewed_action(
                "owner/repo", self.PR, entry, {"alice", "bob"}, token="t-read"
            )
        self.assertEqual(action, "approve")
        # The configured developer logins are threaded through unchanged.
        self.assertEqual(ft.call_args.args[2], {"alice", "bob"})
        self.assertEqual(ft.call_args.kwargs.get("token"), "t-read")

    def test_unresolved_threads_with_advanced_head_yield_rescan(self):
        entry = {
            "head_sha": "sha-old",
            "verdict": "request-changes",
            "inline_comment_count": 1,
        }
        with mock.patch.object(
            scan, "fetch_spectrabot_threads",
            return_value=[{"resolved": False, "outdated": False}],
        ):
            action = scan.previously_reviewed_action(
                "owner/repo", self.PR, entry, {"alice"}
            )
        self.assertEqual(action, "rescan")


class ApproveResolvedPrTest(unittest.TestCase):
    def test_posts_with_first_candidate_token_and_approves(self):
        candidates = [{"login": "alice", "token": "t-alice"}]
        with mock.patch.object(scan, "post_review") as posted, \
                mock.patch.object(scan, "celebrate_approval"):
            result = scan.approve_resolved_pr(
                "owner/repo", 9, "sha", candidates, "carol", "auto", False
            )
        self.assertEqual(result, {"event": "APPROVE", "reviewer": "alice"})
        self.assertEqual(posted.call_args.kwargs["token"], "t-alice")

    def test_author_fallback_downgrades_to_comment(self):
        """When the only candidate is the PR author, the event is COMMENT."""
        candidates = [{"login": "alice", "token": "t-alice"}]
        with mock.patch.object(scan, "post_review"):
            result = scan.approve_resolved_pr(
                "owner/repo", 9, "sha", candidates, "alice", "auto", False
            )
        self.assertEqual(result["event"], "COMMENT")
        self.assertEqual(result["reviewer"], "alice")

    def test_comment_mode_forces_comment(self):
        candidates = [{"login": "alice", "token": "t-alice"}]
        with mock.patch.object(scan, "post_review"):
            result = scan.approve_resolved_pr(
                "owner/repo", 9, "sha", candidates, "carol", "comment", False
            )
        self.assertEqual(result["event"], "COMMENT")

    def test_falls_through_to_next_developer_on_posting_failure(self):
        candidates = [
            {"login": "alice", "token": "t-alice"},
            {"login": "bob", "token": "t-bob"},
        ]
        with mock.patch.object(
            scan, "post_review",
            side_effect=[RuntimeError("revoked token"), None],
        ) as posted, mock.patch.object(scan, "celebrate_approval"):
            result = scan.approve_resolved_pr(
                "owner/repo", 9, "sha", candidates, "carol", "auto", False
            )
        self.assertEqual(result["reviewer"], "bob")
        self.assertEqual(
            [c.kwargs["token"] for c in posted.call_args_list],
            ["t-alice", "t-bob"],
        )

    def test_fails_only_when_every_candidate_fails(self):
        candidates = [
            {"login": "alice", "token": "t-alice"},
            {"login": "bob", "token": "t-bob"},
        ]
        with mock.patch.object(
            scan, "post_review",
            side_effect=[RuntimeError("alice bad"), RuntimeError("bob bad")],
        ):
            with self.assertRaises(RuntimeError) as ctx:
                scan.approve_resolved_pr(
                    "owner/repo", 9, "sha", candidates, "carol", "auto", False
                )
        self.assertIn("alice", str(ctx.exception))
        self.assertIn("bob", str(ctx.exception))

    def test_dry_run_posts_nothing_and_records_first_candidate(self):
        candidates = [{"login": "alice", "token": "t-alice"}]
        with mock.patch.object(scan, "post_review") as posted:
            result = scan.approve_resolved_pr(
                "owner/repo", 9, "sha", candidates, "carol", "auto", True
            )
        posted.assert_not_called()
        self.assertEqual(result, {"event": "APPROVE", "reviewer": "alice"})


class SelfTokenFollowupTest(unittest.TestCase):
    """US-013: on a self-token override repo, the follow-up approval flow
    recognizes self_token's own review threads and posts re-reviews under
    self_token."""

    def test_fetch_threads_recognizes_self_login(self):
        """When self_token's login is in the logins set, its own review
        threads are matched (main() adds it for override repos)."""
        nodes = [
            {
                "isResolved": True,
                "isOutdated": False,
                "comments": {"nodes": [{"author": {"login": "operator"}}]},
            }
        ]
        response = {
            "data": {
                "repository": {
                    "pullRequest": {"reviewThreads": {"nodes": nodes}}
                }
            }
        }
        with mock.patch.object(scan, "gh_json", return_value=response):
            threads = scan.fetch_spectrabot_threads(
                "owner/repo", 1, {"alice", "operator"}
            )
        self.assertEqual(threads, [{"resolved": True, "outdated": False}])

    def test_previously_reviewed_action_approves_on_resolved_self_threads(self):
        """previously_reviewed_action returns approve when self_token's
        threads are all resolved on an override repo."""
        entry = {
            "head_sha": "sha-old",
            "verdict": "request-changes",
            "inline_comment_count": 1,
        }
        with mock.patch.object(
            scan, "fetch_spectrabot_threads",
            return_value=[{"resolved": True, "outdated": False}],
        ) as ft:
            action = scan.previously_reviewed_action(
                "owner/repo", {"number": 3, "headRefOid": "sha-new"},
                entry, {"operator"}, token="t-self",
            )
        self.assertEqual(action, "approve")
        self.assertIn("operator", ft.call_args.args[2])
        self.assertEqual(ft.call_args.kwargs.get("token"), "t-self")

    def test_approve_posts_under_self_token_and_records_reviewer(self):
        """approve_resolved_pr posts the override-repo re-review under
        self_token and records its login as the reviewer."""
        candidates = [{"login": "operator", "token": "t-self"}]
        with mock.patch.object(scan, "post_review") as posted, \
                mock.patch.object(scan, "celebrate_approval"):
            result = scan.approve_resolved_pr(
                "owner/repo", 9, "sha", candidates, "carol", "auto", False
            )
        self.assertEqual(posted.call_args.kwargs["token"], "t-self")
        self.assertEqual(result, {"event": "APPROVE", "reviewer": "operator"})

    def test_approve_downgrades_to_comment_when_author_is_self(self):
        """An override PR authored by the operator downgrades to COMMENT —
        the same author-equals-self_token rule as the main scan (US-012)."""
        candidates = [{"login": "operator", "token": "t-self"}]
        with mock.patch.object(scan, "post_review"):
            result = scan.approve_resolved_pr(
                "owner/repo", 9, "sha", candidates, "operator", "auto", False
            )
        self.assertEqual(result["event"], "COMMENT")
        self.assertEqual(result["reviewer"], "operator")


class SecretRedactionTest(unittest.TestCase):
    """log() must never emit a registered developer token."""

    def setUp(self):
        self._saved = set(scan._SECRET_TOKENS)
        scan._SECRET_TOKENS.clear()

    def tearDown(self):
        scan._SECRET_TOKENS.clear()
        scan._SECRET_TOKENS.update(self._saved)

    def test_register_secret_ignores_empty_values(self):
        scan.register_secret("")
        scan.register_secret(None)
        self.assertEqual(scan._SECRET_TOKENS, set())

    def test_redact_secrets_replaces_registered_token(self):
        scan.register_secret("ghp_supersecret")
        redacted = scan._redact_secrets("auth failed for ghp_supersecret here")
        self.assertNotIn("ghp_supersecret", redacted)
        self.assertIn("***", redacted)

    def test_redact_secrets_leaves_unrelated_text_untouched(self):
        scan.register_secret("ghp_supersecret")
        self.assertEqual(
            scan._redact_secrets("posting as alice"), "posting as alice"
        )

    def test_log_scrubs_token_from_stdout(self):
        """A token that sneaks into a log message is replaced before output."""
        scan.register_secret("ghp_leaked")
        with mock.patch("builtins.print") as printed, mock.patch.object(
            scan, "LOG_DIR", Path(os.devnull).parent / "spectrabot-test-logs"
        ), mock.patch.object(scan.Path, "mkdir"), mock.patch(
            "builtins.open", mock.mock_open()
        ):
            scan.log("token invalid: ghp_leaked")
        printed_text = " ".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertNotIn("ghp_leaked", printed_text)
        self.assertIn("***", printed_text)


class ListOpenPrsWithFallbackTest(unittest.TestCase):
    """The read path mirrors the posting path's fall-through: one developer
    losing access to a repo must not drop that repo from the whole scan."""

    CANDIDATES = [("alice", "t-alice"), ("bob", "t-bob")]

    @staticmethod
    def _auth_error():
        return subprocess.CalledProcessError(1, ["gh", "pr", "list"])

    def test_first_working_token_is_used(self):
        with mock.patch.object(
            scan, "list_open_prs", return_value=[{"number": 1}]
        ) as listed:
            token, prs = scan.list_open_prs_with_fallback(
                "o/r", self.CANDIDATES
            )
        self.assertEqual(token, "t-alice")
        self.assertEqual(prs, [{"number": 1}])
        self.assertEqual(listed.call_count, 1)

    def test_falls_through_to_next_token_on_auth_failure(self):
        with mock.patch.object(
            scan, "list_open_prs",
            side_effect=[self._auth_error(), [{"number": 2}]],
        ) as listed:
            token, prs = scan.list_open_prs_with_fallback(
                "o/r", self.CANDIDATES
            )
        self.assertEqual(token, "t-bob")
        self.assertEqual(prs, [{"number": 2}])
        self.assertEqual(listed.call_count, 2)

    def test_raises_when_every_token_fails(self):
        with mock.patch.object(
            scan, "list_open_prs",
            side_effect=[self._auth_error(), self._auth_error()],
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                scan.list_open_prs_with_fallback("o/r", self.CANDIDATES)


class MainOrchestrationTest(unittest.TestCase):
    """Integration coverage for main()'s wiring: override branching, the
    author-fallback skip, repo-set dedup, and reviewer-candidate selection."""

    DEVS = [
        {"login": "alice", "token": "t-alice"},
        {"login": "bob", "token": "t-bob"},
    ]

    @staticmethod
    def _pr(number, author, draft=False):
        return {
            "number": number,
            "headRefOid": f"sha{number}",
            "isDraft": draft,
            "author": {"login": author},
            "reviewDecision": "REVIEW_REQUIRED",
            "title": f"PR {number}",
            "url": f"https://example.test/{number}",
        }

    def _run_main(self, cfg, prs_by_repo, developers=None, self_login=None):
        """Drive main() with the gh/engine boundary mocked. Returns
        (return_code, reviewed) where reviewed is a list of
        (repo, pr_number, [candidate logins]) for each review_one_pr call."""
        developers = self.DEVS if developers is None else developers
        reviewed = []

        def fake_review(repo, pr, prompt_text, cfg_, candidates, read_tok, dry):
            reviewed.append(
                (repo, pr["number"], [c["login"] for c in candidates])
            )
            return {
                "verdict": "comment",
                "inline_count": 0,
                "reviewer": candidates[0]["login"],
            }

        prompt = mock.Mock()
        prompt.read_text.return_value = "prompt"
        with mock.patch.object(sys, "argv", ["spectrabot"]), \
             mock.patch.object(scan, "prune_old_logs"), \
             mock.patch.object(scan, "log"), \
             mock.patch.object(scan, "load_config", return_value=cfg), \
             mock.patch.object(
                 scan, "validate_developers", return_value=developers), \
             mock.patch.object(
                 scan, "validate_self_token", return_value=self_login), \
             mock.patch.object(scan, "load_state", return_value={}), \
             mock.patch.object(scan, "save_state"), \
             mock.patch.object(
                 scan, "viewer_login", return_value="spectrabot"), \
             mock.patch.object(scan, "PROMPT_PATH", prompt), \
             mock.patch.object(
                 scan, "list_open_prs",
                 side_effect=lambda repo, token=None: prs_by_repo.get(repo, [])), \
             mock.patch.object(
                 scan, "review_one_pr", side_effect=fake_review):
            rc = scan.main()
        return rc, reviewed

    def test_normal_multi_developer_pr_reviewed_by_non_author(self):
        cfg = {
            "github": {"repos": ["o/app"], "developers": self.DEVS},
            "review": {},
        }
        rc, reviewed = self._run_main(cfg, {"o/app": [self._pr(1, "carol")]})
        self.assertEqual(rc, 0)
        self.assertEqual(reviewed, [("o/app", 1, ["alice", "bob"])])

    def test_author_fallback_skip_excludes_own_pr(self):
        only_alice = [{"login": "alice", "token": "t-alice"}]
        cfg = {
            "github": {"repos": ["o/app"], "developers": only_alice},
            "review": {"author_fallback": "skip"},
        }
        prs = {"o/app": [self._pr(1, "alice"), self._pr(2, "carol")]}
        rc, reviewed = self._run_main(cfg, prs, developers=only_alice)
        self.assertEqual(rc, 0)
        # PR 1 (authored by the only developer) is skipped; PR 2 is reviewed.
        self.assertEqual(reviewed, [("o/app", 2, ["alice"])])

    def test_override_repo_uses_self_token_and_is_scanned(self):
        cfg = {
            "github": {"repos": ["o/app"], "developers": self.DEVS},
            "review": {
                "self_token": "t-self",
                "self_review_repos": ["o/infra"],
            },
        }
        prs = {
            "o/app": [self._pr(1, "carol")],
            "o/infra": [self._pr(2, "dave")],
        }
        rc, reviewed = self._run_main(cfg, prs, self_login="operator")
        self.assertEqual(rc, 0)
        # o/infra is reviewed under the self_token even though it is absent
        # from [github] repos; o/app keeps the developer rotation.
        self.assertEqual(
            reviewed,
            [("o/app", 1, ["alice", "bob"]), ("o/infra", 2, ["operator"])],
        )

    def test_no_developers_falls_back_to_ambient_identity(self):
        cfg = {"github": {"repos": ["o/app"]}, "review": {}}
        rc, reviewed = self._run_main(
            cfg, {"o/app": [self._pr(1, "carol")]}, developers=[]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(reviewed, [("o/app", 1, ["spectrabot"])])


if __name__ == "__main__":
    unittest.main()
