#!/usr/bin/env python3
"""SpectraBot — scan configured GitHub repos for open PRs and review them
with Claude.

Idempotent: tracks reviewed PRs in a state file so each PR is reviewed at most
once. Designed to be invoked on a schedule (systemd timer / launchd).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SPECTRABOT_HOME = Path(
    os.environ.get("SPECTRABOT_HOME", "~/.spectrabot")
).expanduser()
CONFIG_PATH = Path(
    os.environ.get("SPECTRABOT_CONFIG", SPECTRABOT_HOME / "config.toml")
).expanduser()
STATE_PATH = Path(
    os.environ.get("SPECTRABOT_STATE", SPECTRABOT_HOME / "state" / "reviewed.json")
).expanduser()
LIB_DIR = Path(__file__).resolve().parent
PROMPT_PATH = LIB_DIR / "review_prompt.md"

LOG_DIR = Path(
    os.environ.get("SPECTRABOT_LOG_DIR", SPECTRABOT_HOME / "logs")
).expanduser()
LOG_RETENTION_DAYS = 7
CHUCK_NORRIS_API = "https://api.chucknorris.io/jokes/random"
JOKE_CATEGORIES = ("dev", "science")

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_ANSI = {
    "dim": "\033[2m", "reset": "\033[0m",
    "info": "\033[36m",     # cyan
    "success": "\033[32m",  # green
    "warn": "\033[33m",     # yellow
    "error": "\033[31m",    # red
    "joke": "\033[35m",     # magenta
}
_LEVEL_TAGS = {
    "info": "INFO", "success": "OK", "warn": "WARN",
    "error": "ERROR", "joke": "JOKE",
}


def _log_file() -> Path:
    return LOG_DIR / f"spectrabot-{datetime.now(timezone.utc):%Y-%m-%d}.log"


# Token values registered at startup (see `register_secret`). `log()` scrubs
# any of these from every message — defense in depth so a developer token can
# never reach the log file or stdout, even via an exception message we don't
# control (e.g. a `gh` error). Only login values should ever appear in output.
_SECRET_TOKENS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a token so `log()` redacts it from all output. Called once per
    configured developer token at startup, before any scanning."""
    if value:
        _SECRET_TOKENS.add(value)


def _redact_secrets(text: str) -> str:
    """Replace every registered token in `text` with a placeholder."""
    for secret in _SECRET_TOKENS:
        if secret in text:
            text = text.replace(secret, "***")
    return text


def log(msg: str, level: str = "info") -> None:
    msg = _redact_secrets(msg)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tag = _LEVEL_TAGS.get(level, "INFO")
    plain = f"{ts} {tag:<5} {msg}"

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_log_file(), "a") as f:
            f.write(plain + "\n")
    except OSError as e:
        print(f"{ts} WARN  log file write failed: {e}", flush=True)

    if _USE_COLOR:
        color = _ANSI.get(level, _ANSI["info"])
        line = (
            f"{_ANSI['dim']}{ts}{_ANSI['reset']} "
            f"{color}{tag:<5}{_ANSI['reset']} {msg}"
        )
    else:
        line = plain
    print(line, flush=True)


def prune_old_logs() -> None:
    """Delete spectrabot-YYYY-MM-DD.log files older than LOG_RETENTION_DAYS."""
    if not LOG_DIR.exists():
        return
    today = datetime.now(timezone.utc).date()
    for path in LOG_DIR.glob("spectrabot-*.log"):
        m = re.fullmatch(r"spectrabot-(\d{4}-\d{2}-\d{2})\.log", path.name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - file_date).days > LOG_RETENTION_DAYS:
            try:
                path.unlink()
            except OSError:
                pass


def fetch_chuck_norris_joke() -> str | None:
    """Fetch a random Chuck Norris joke from a curated category. Returns None
    on any failure."""
    try:
        category = random.choice(JOKE_CATEGORIES)
        req = urllib.request.Request(
            f"{CHUCK_NORRIS_API}?category={category}",
            headers={"Accept": "application/json", "User-Agent": "SpectraBot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        return (data.get("value") or "").strip() or None
    except Exception as e:
        log(f"chuck norris joke fetch failed: {e}", level="warn")
        return None


def celebrate_approval(pr_id: str) -> None:
    joke = fetch_chuck_norris_joke()
    if joke:
        log(f"[{pr_id}] Chuck Norris says: {joke}", level="joke")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"config not found at {CONFIG_PATH}\n"
            "Run install.sh, or copy config/config.example.toml there and edit."
        )
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


VALID_AUTHOR_FALLBACK = ("comment", "skip")


def parse_developers(cfg: dict) -> list[dict]:
    """Read [[github.developers]] into an ordered list of
    {"login": str, "token": str}, preserving config order.

    Returns an empty list when none are configured — SpectraBot then keeps its
    single-user behavior, reviewing with whatever identity `gh` is already
    authenticated as. Aborts with a clear error when an entry is missing its
    login or token.
    """
    raw = cfg.get("github", {}).get("developers", []) or []
    developers: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            sys.exit(
                f"config error: github.developers entry #{i + 1} must be a "
                "table with a login and token"
            )
        login = entry.get("login")
        token = entry.get("token")
        missing = [
            field
            for field, value in (("login", login), ("token", token))
            if not value
        ]
        if missing:
            label = f"'{login}'" if login else f"#{i + 1}"
            sys.exit(
                f"config error: github.developers entry {label} is missing "
                f"required field(s): {', '.join(missing)}"
            )
        developers.append({"login": login, "token": token})
    return developers


def parse_author_fallback(cfg: dict) -> str:
    """Read [review] author_fallback — 'comment' (default) or 'skip'.

    Decides what happens when every configured developer is the PR's own
    author, so no one is eligible to post a non-self review.
    """
    value = cfg.get("review", {}).get("author_fallback", "comment")
    if value not in VALID_AUTHOR_FALLBACK:
        sys.exit(
            "config error: review.author_fallback must be one of "
            f"{', '.join(VALID_AUTHOR_FALLBACK)}; got {value!r}"
        )
    return value


def parse_self_review(cfg: dict) -> dict:
    """Read the [review] self-token override.

    Returns {"token": str | None, "repos": [str, ...]}:
      - token: the operator's own GitHub token, used to review the repos in
        `repos` with the operator's own identity instead of the
        [[github.developers]] rotation. None when unset.
      - repos: 'owner/repo' strings reviewed with `token`; empty when unset,
        which leaves the multi-developer behavior unchanged.

    Aborts when `repos` is non-empty but `token` is unset — those repos would
    have no identity to review with.
    """
    review = cfg.get("review", {})
    token = review.get("self_token") or None
    raw_repos = review.get("self_review_repos", []) or []
    if not isinstance(raw_repos, list) or not all(
        isinstance(r, str) for r in raw_repos
    ):
        sys.exit(
            "config error: review.self_review_repos must be a list of "
            "'owner/repo' strings"
        )
    repos = list(raw_repos)
    if repos and not token:
        sys.exit(
            "config error: review.self_review_repos is set but "
            "review.self_token is missing — set review.self_token to the "
            "operator's GitHub token, or clear review.self_review_repos"
        )
    return {"token": token, "repos": repos}


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def _gh_env(token: str | None) -> dict | None:
    """Environment for a gh subprocess. When `token` is set, return a copy of
    the parent environment with GH_TOKEN overridden; the parent environment
    itself is never mutated. When `token` is None, return None so the
    subprocess inherits the parent environment unchanged (ambient auth)."""
    if not token:
        return None
    return {**os.environ, "GH_TOKEN": token}


def gh(args: list[str], token: str | None = None, **kwargs) -> str:
    env = _gh_env(token)
    if env is not None:
        kwargs["env"] = env
    return subprocess.check_output(["gh", *args], text=True, **kwargs)


def gh_json(args: list[str], token: str | None = None) -> object:
    out = gh(args, token=token).strip()
    return json.loads(out) if out else None


def viewer_login() -> str:
    return gh(["api", "user", "--jq", ".login"]).strip()


def validate_developers(developers: list[dict]) -> list[dict]:
    """Resolve each configured developer's token via `gh api user` and return
    the developers usable for reviewer selection.

    A developer whose token is invalid or unauthorized is logged with a WARN
    and dropped from the returned list — the scan continues with the rest. A
    developer whose token authenticates but resolves to a different login than
    configured is logged with a WARN yet kept, since the token still works.
    Validation never aborts the scan.
    """
    valid: list[dict] = []
    for dev in developers:
        login = dev["login"]
        try:
            resolved = gh(
                ["api", "user", "--jq", ".login"], token=dev["token"]
            ).strip()
        except subprocess.CalledProcessError as e:
            log(
                f"developer {login!r}: token invalid or unauthorized — "
                f"excluding from reviewer selection ({e})",
                level="warn",
            )
            continue
        if resolved != login:
            log(
                f"developer {login!r}: token resolves to login {resolved!r}, "
                "not the configured login — check config.toml",
                level="warn",
            )
        valid.append(dev)
    return valid


def validate_self_token(token: str | None) -> str | None:
    """Resolve the operator's self_token to its GitHub login via `gh api user`.

    Returns the resolved login, or None when the token is unset or fails to
    authenticate. A token that fails is logged with a WARN; the scan continues
    and the self-review override repos that depend on it are skipped rather
    than crashing — mirrors `validate_developers`' fail-soft behavior.
    """
    if not token:
        return None
    try:
        return gh(["api", "user", "--jq", ".login"], token=token).strip()
    except subprocess.CalledProcessError as e:
        log(
            "review.self_token is invalid or unauthorized — self-review "
            f"override repos cannot be reviewed ({e})",
            level="warn",
        )
        return None


def reviewer_candidates(
    pr: dict, developers: list[dict], viewer: str | None
) -> dict:
    """Decide who may post `pr`'s review — the single source of truth for
    reviewer eligibility.

    `developers` is the validated, ordered developer list (see
    `validate_developers`). `viewer` is the ambient `gh` login, used only when
    no developers are configured. `skip_authors` is intentionally NOT
    consulted: that list only decides which PRs get reviewed at all, so a
    developer who also appears in skip_authors can still review.

    Returns {"kind": str, "candidates": [{"login": str, "token": str | None}]}:
      kind "ambient"
          no developers configured — a single ambient-auth candidate (token
          None) posting as whatever `gh` is already authenticated as.
      kind "developer"
          one or more eligible non-author developers, in config order — never
          the PR author, so an APPROVE stays allowed.
      kind "author_fallback"
          every configured developer is the PR author; the candidates are the
          developers as-is, the review posts as the author and `decide_event`
          downgrades it to COMMENT.

    `review_one_pr` tries `candidates` in order, falling through to the next
    when a developer's token fails to post (revoked token, lost repo scope),
    and fails the PR only when every candidate fails. `candidates[0]` is the
    primary reviewer.
    """
    if not developers:
        return {
            "kind": "ambient",
            "candidates": [{"login": viewer, "token": None}],
        }
    author = pr["author"]["login"]
    eligible = [dev for dev in developers if dev["login"] != author]
    if eligible:
        return {"kind": "developer", "candidates": eligible}
    return {"kind": "author_fallback", "candidates": list(developers)}


def list_open_prs(repo: str, token: str | None = None) -> list[dict]:
    return gh_json(
        [
            "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--json", "number,headRefOid,isDraft,author,reviewDecision,title,url",
            "--limit", "100",
        ],
        token=token,
    ) or []


def list_open_prs_with_fallback(
    repo: str, read_candidates: list[tuple[str, str | None]]
) -> tuple[str | None, list[dict]]:
    """List `repo`'s open PRs, trying each read identity in config order.

    `read_candidates` is an ordered list of (label, token) pairs. Returns
    `(token, prs)` for the first identity that can list the repo, so the same
    token is reused for the rest of the repo's read-only calls. Raises the
    final `CalledProcessError` when every identity fails.

    This mirrors the posting path's per-candidate fall-through: a developer who
    has lost access to one repo no longer drops that repo from the whole scan
    as long as another configured developer still has access.
    """
    last_error: subprocess.CalledProcessError | None = None
    for label, token in read_candidates:
        try:
            return token, list_open_prs(repo, token=token)
        except subprocess.CalledProcessError as e:
            last_error = e
            log(f"[{repo}] gh pr list failed as {label}: {e}", level="warn")
    raise last_error


def fetch_pr_context(
    repo: str, pr_number: int, token: str | None = None
) -> tuple[dict, str]:
    meta = gh_json(
        [
            "pr", "view", str(pr_number),
            "--repo", repo,
            "--json",
            "title,body,author,baseRefName,headRefName,additions,deletions,changedFiles,files,url",
        ],
        token=token,
    )
    diff = gh(["pr", "diff", str(pr_number), "--repo", repo], token=token)
    return meta, diff


# Non-interactive invocation shape per supported engine CLI. The review prompt
# is passed as a single argv element between `before` and `after`.
ENGINE_SPECS = {
    "claude": {"before": ["-p"], "after": ["--output-format", "text"]},
    "codex": {"before": ["exec"], "after": []},
    "opencode": {"before": ["run"], "after": []},
}


def parse_review_output(text: str) -> dict:
    """Pull the JSON block out of the engine's response."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"no JSON object in engine output (first 500 chars): {text[:500]}")


def invoke_engine(prompt_text: str, pr_meta: dict, diff: str, cfg: dict) -> dict:
    user_prompt = (
        f"{prompt_text}\n\n"
        f"## PR Metadata\n```json\n{json.dumps(pr_meta, indent=2)}\n```\n\n"
        f"## Unified Diff\n```diff\n{diff}\n```\n"
    )
    eng = cfg.get("engine", {})
    name = eng.get("name", "claude")
    spec = ENGINE_SPECS.get(name)
    if spec is None:
        raise RuntimeError(
            f"unknown engine {name!r}; valid: {', '.join(sorted(ENGINE_SPECS))}"
        )
    engine_bin = eng.get("bin") or shutil.which(name) or name
    extra_args = eng.get("extra_args", [])
    timeout = eng.get("timeout_seconds", 600)
    cmd = [engine_bin, *spec["before"], user_prompt, *spec["after"], *extra_args]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"{name} exit {res.returncode}: {res.stderr.strip()[:500]}"
        )
    return parse_review_output(res.stdout)


def decide_event(
    recommendation: str, reviewer_login: str, pr_author: str, mode: str
) -> str:
    """Map the engine's recommendation to a GitHub review event.

    The review is posted by `reviewer_login` — the developer chosen by
    `reviewer_candidates`, or the ambient `gh` identity when no developers are
    configured. A reviewer who is not the PR author may APPROVE. When the
    reviewer *is* the PR author (the author-fallback case, or an ambient
    single-user setup reviewing its own PR) the event is forced to COMMENT so
    no one approves their own PR. `mode == "comment"` forces COMMENT for every
    PR regardless of verdict.
    """
    if mode == "comment" or reviewer_login == pr_author:
        return "COMMENT"
    if recommendation == "approve":
        return "APPROVE"
    if recommendation == "request-changes":
        return "REQUEST_CHANGES"
    return "COMMENT"


def build_review_payload(review: dict, head_sha: str, event: str) -> dict:
    body = review.get("summary", "").strip() or "_(no summary)_"
    body += "\n\n---\n_Automated review by SpectraBot._"
    comments = []
    for c in review.get("inline_comments", []) or []:
        if not all(k in c for k in ("path", "line", "body")):
            continue
        comments.append(
            {
                "path": c["path"],
                "line": int(c["line"]),
                "side": c.get("side", "RIGHT"),
                "body": c["body"],
            }
        )
    payload = {"commit_id": head_sha, "event": event, "body": body}
    if comments:
        payload["comments"] = comments
    return payload


def post_review(repo: str, pr_number: int, payload: dict, token: str | None = None) -> None:
    owner, name = repo.split("/", 1)
    env = _gh_env(token)
    proc = subprocess.run(
        [
            "gh", "api",
            f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
            "--method", "POST",
            "--input", "-",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        # If inline comments fail (e.g. line not in diff), retry without them.
        if "comments" in payload and (
            "pull_request_review_comment" in proc.stderr
            or "line must be part of the diff" in proc.stderr.lower()
            or "Unprocessable Entity" in proc.stderr
        ):
            log(f"  inline comments rejected, retrying body-only: {proc.stderr.strip()[:200]}", level="warn")
            retry = {k: v for k, v in payload.items() if k != "comments"}
            retry["body"] += "\n\n_(inline comments dropped — line refs didn't match the diff)_"
            proc2 = subprocess.run(
                [
                    "gh", "api",
                    f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
                    "--method", "POST",
                    "--input", "-",
                ],
                input=json.dumps(retry),
                text=True,
                capture_output=True,
                env=env,
            )
            if proc2.returncode != 0:
                raise RuntimeError(f"gh api review failed: {proc2.stderr.strip()[:500]}")
            return
        raise RuntimeError(f"gh api review failed: {proc.stderr.strip()[:500]}")


def fetch_spectrabot_threads(
    repo: str, pr_number: int, logins: set[str], token: str | None = None
) -> list[dict]:
    """Return the review threads on a PR whose first comment was authored by
    any of `logins` (i.e. SpectraBot's own threads).

    `logins` is the set of configured developer logins — any of them may have
    posted a prior review — or the ambient `gh` identity when no developers
    are configured. Each item is {"resolved": bool, "outdated": bool}."""
    owner, name = repo.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "pullRequest(number:$number){"
        "reviewThreads(first:100){nodes{"
        "isResolved isOutdated "
        "comments(first:1){nodes{author{login}}}"
        "}}}}}"
    )
    out = gh_json([
        "api", "graphql",
        "-f", f"query={query}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"number={pr_number}",
    ], token=token) or {}
    nodes = (
        (out.get("data") or {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
        or []
    )
    threads = []
    for t in nodes:
        comments = (t.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        author = (comments[0].get("author") or {}).get("login")
        if author not in logins:
            continue
        threads.append({
            "resolved": bool(t.get("isResolved")),
            "outdated": bool(t.get("isOutdated")),
        })
    return threads


def previously_reviewed_action(
    repo: str, pr: dict, state_entry: dict, logins: set[str],
    token: str | None = None,
) -> str:
    """For a PR already in state, decide whether to skip, approve, or rescan.

    `logins` is the set of identities whose review threads count as
    SpectraBot's own (see `fetch_spectrabot_threads`).

    Returns "skip" | "approve" | "rescan".
    """
    head_changed = state_entry.get("head_sha") != pr["headRefOid"]
    verdict = state_entry.get("verdict")
    inline_count = state_entry.get("inline_comment_count")

    if verdict is None:
        # Legacy entry from before the schema was extended.
        return "rescan" if head_changed else "skip"

    if verdict not in ("request-changes", "comment"):
        # "approve" should be filtered upstream via reviewDecision; be defensive.
        return "skip"

    if not inline_count:
        return "rescan" if head_changed else "skip"

    try:
        threads = fetch_spectrabot_threads(repo, pr["number"], logins, token=token)
    except Exception as e:
        log(f"  graphql fetch failed; treating as unresolved: {e}", level="warn")
        return "rescan" if head_changed else "skip"

    if not threads:
        # We recorded inline comments but none survive on GitHub — dismissed or
        # deleted by a maintainer. Nothing left to be unresolved.
        log(f"  no SpectraBot threads found (recorded {inline_count}); treating as resolved")
        return "approve"

    if all(t["resolved"] or t["outdated"] for t in threads):
        return "approve"
    return "rescan" if head_changed else "skip"


def approve_resolved_pr(
    repo: str,
    pr_number: int,
    head_sha: str,
    candidates: list[dict],
    pr_author: str,
    mode: str,
    dry_run: bool,
) -> dict:
    """Post a re-review for a PR whose prior SpectraBot comments are resolved
    or outdated. Returns {"event": str, "reviewer": str} — the GitHub event
    used ("APPROVE" or "COMMENT") and the login that actually posted.

    `candidates` is the ordered list from `reviewer_candidates`: the re-review
    posts under the first candidate's token, falling through to the next when
    posting fails (revoked token, lost repo scope) and failing the PR only
    when every candidate fails — the same fall-through `review_one_pr` applies.
    Downgrades to COMMENT when the selected reviewer is the PR author so no one
    approves their own PR — the same rule `decide_event` applies."""
    reviewer_login = candidates[0]["login"]
    event = (
        "COMMENT"
        if (mode == "comment" or reviewer_login == pr_author)
        else "APPROVE"
    )
    verb = "Approving" if event == "APPROVE" else "Acknowledging resolution"
    body = (
        f"Previously raised comments are resolved or outdated. {verb}.\n\n"
        "---\n_Automated re-review by SpectraBot._"
    )
    payload = {"commit_id": head_sha, "event": event, "body": body}
    if dry_run:
        log(
            f"  [dry-run] would post review as {reviewer_login}:\n"
            f"{json.dumps(payload, indent=2)}"
        )
        return {"event": event, "reviewer": reviewer_login}

    errors = []
    for candidate in candidates:
        try:
            post_review(repo, pr_number, payload, token=candidate["token"])
        except Exception as e:
            errors.append(f"{candidate['login']}: {e}")
            log(
                f"  posting as {candidate['login']} failed; "
                f"trying next eligible developer: {e}",
                level="warn",
            )
            continue
        if event == "APPROVE":
            celebrate_approval(f"{repo}#{pr_number}")
        return {"event": event, "reviewer": candidate["login"]}

    raise RuntimeError(
        "every eligible developer failed to post the re-review "
        f"({'; '.join(errors)})"
    )


def review_one_pr(
    repo: str,
    pr: dict,
    prompt_text: str,
    cfg: dict,
    candidates: list[dict],
    read_token: str | None,
    dry_run: bool,
) -> dict:
    """Review a single PR and post the result.

    `candidates` is the ordered list from `reviewer_candidates`: the review is
    posted under the first candidate's token, falling through to the next
    candidate when posting fails (revoked token, lost repo scope) and failing
    the PR only when every candidate fails. `read_token` authenticates the
    read-only `gh pr view`/`pr diff` calls.
    """
    pr_number = pr["number"]
    head_sha = pr["headRefOid"]
    reviewer_login = candidates[0]["login"]
    meta, diff = fetch_pr_context(repo, pr_number, token=read_token)

    max_diff = cfg.get("review", {}).get("max_diff_lines", 4000)
    diff_lines = diff.count("\n")
    if max_diff and diff_lines > max_diff:
        raise RuntimeError(f"diff too large ({diff_lines} > {max_diff} lines)")

    review = invoke_engine(prompt_text, meta, diff, cfg)
    mode = cfg.get("review", {}).get("mode", "auto")
    verdict = review.get("recommendation", "comment")
    event = decide_event(verdict, reviewer_login, pr["author"]["login"], mode)
    payload = build_review_payload(review, head_sha, event)
    inline_count = len(payload.get("comments", []))

    log(f"  verdict={verdict!r} event={event} inline={inline_count}")

    if dry_run:
        log(
            f"  [dry-run] would post review as {reviewer_login}:\n"
            f"{json.dumps(payload, indent=2)[:2000]}"
        )
        return {
            "verdict": verdict,
            "inline_count": inline_count,
            "reviewer": reviewer_login,
        }

    errors = []
    for candidate in candidates:
        try:
            post_review(repo, pr_number, payload, token=candidate["token"])
        except Exception as e:
            errors.append(f"{candidate['login']}: {e}")
            log(
                f"  posting as {candidate['login']} failed; "
                f"trying next eligible developer: {e}",
                level="warn",
            )
            continue
        if event == "APPROVE":
            celebrate_approval(f"{repo}#{pr_number}")
        return {
            "verdict": verdict,
            "inline_count": inline_count,
            "reviewer": candidate["login"],
        }

    raise RuntimeError(
        "every eligible developer failed to post the review "
        f"({'; '.join(errors)})"
    )


def should_skip(pr: dict, skip_authors: set[str]) -> str | None:
    if pr["isDraft"]:
        return "draft"
    if pr["author"]["login"] in skip_authors:
        return f"author {pr['author']['login']} in skip_authors"
    if pr["reviewDecision"] == "APPROVED":
        return "already approved"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="spectrabot",
        description="SpectraBot — scan configured GitHub repos for PRs to review.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Don't post reviews; print payload instead.")
    ap.add_argument("--repo", help="Scan only this repo (owner/name), overriding config.")
    ap.add_argument("--pr", type=int, help="Review only this PR number. Requires --repo.")
    ap.add_argument("--force", action="store_true", help="Ignore state file; re-review PRs already marked done.")
    args = ap.parse_args()

    if args.pr and not args.repo:
        ap.error("--pr requires --repo")

    prune_old_logs()
    cfg = load_config()
    # Validate the multi-developer config at startup so misconfiguration is
    # caught before any scanning. Consumed by reviewer selection in later work.
    # Register every developer token first so `log()` scrubs it from output,
    # including any error logged during validation itself.
    parsed_developers = parse_developers(cfg)
    for dev in parsed_developers:
        register_secret(dev["token"])
    developers = validate_developers(parsed_developers)
    # If developers were configured but every token failed validation, abort
    # rather than silently reverting to ambient `gh` auth — an operator who set
    # up a developer rotation should not unknowingly post under their own
    # identity after a mass token expiry.
    if parsed_developers and not developers:
        sys.exit(
            "config error: all configured [[github.developers]] tokens "
            "failed validation — fix or remove them"
        )
    author_fallback = parse_author_fallback(cfg)
    # Repos that bypass the developer rotation and are reviewed with the
    # operator's own self_token. `self_login` is that token's resolved GitHub
    # login (None when the token is unset or fails to authenticate).
    self_review = parse_self_review(cfg)
    register_secret(self_review["token"])
    self_login = validate_self_token(self_review["token"])
    override_repos = set(self_review["repos"])
    state = load_state()
    prompt_text = PROMPT_PATH.read_text()

    # Ambient `gh auth` is only consulted as the no-developers fallback, so
    # resolve `viewer` lazily — a pure multi-developer deployment need not run
    # `gh auth login` at all.
    if developers:
        viewer = None
    else:
        try:
            viewer = viewer_login()
        except subprocess.CalledProcessError as e:
            sys.exit(f"`gh` not authenticated? {e}")

    # Logins whose prior review threads count as SpectraBot's own — any
    # configured developer may have posted an earlier review, or the ambient
    # `gh` identity when no developers are configured.
    reviewer_logins = {dev["login"] for dev in developers} or {viewer}

    if args.repo:
        repos = [args.repo]
    else:
        config_repos = cfg.get("github", {}).get("repos", [])
        # self_review_repos must be scanned even when absent from [github] repos
        # — they're reviewed with self_token via the is_override branch below.
        repos = list(dict.fromkeys([*config_repos, *self_review["repos"]]))
    if not repos:
        log("no repos configured; nothing to do")
        return 0

    skip_authors = set(cfg.get("review", {}).get("skip_authors", []))
    max_prs = cfg.get("review", {}).get("max_prs_per_scan", 10)
    reviewed_this_run = 0
    start = time.time()

    for repo in repos:
        # Override repos bypass the [[github.developers]] rotation entirely:
        # every PR is read and reviewed with the operator's own self_token.
        is_override = repo in override_repos
        if is_override and self_login is None:
            log(
                f"[{repo}] skip: review.self_token did not validate — this "
                "self-review override repo cannot be reviewed",
                level="warn",
            )
            continue
        # Threads opened by the operator's own self_token also count as
        # SpectraBot's own on an override repo, so the follow-up approval flow
        # detects resolved comments there. `self_login` is non-None here —
        # override repos with an unvalidated self_token were skipped above.
        repo_reviewer_logins = (
            reviewer_logins | {self_login} if is_override else reviewer_logins
        )
        # Read-only gh calls (pr list/view/diff, graphql) run as the override's
        # self_token, or — for normal repos — the first configured developer
        # that can actually read this repo, falling through the rotation so one
        # developer's lost access doesn't drop the repo. None means ambient auth.
        if is_override:
            read_candidates = [(self_login, self_review["token"])]
        elif developers:
            read_candidates = [(d["login"], d["token"]) for d in developers]
        else:
            read_candidates = [("ambient gh auth", None)]
        try:
            repo_read_token, prs = list_open_prs_with_fallback(
                repo, read_candidates
            )
        except subprocess.CalledProcessError:
            log(f"[{repo}] skip: no configured token could list its PRs",
                level="error")
            continue

        for pr in prs:
            if args.pr and pr["number"] != args.pr:
                continue
            pr_id = f"{repo}#{pr['number']}"

            skip_reason = should_skip(pr, skip_authors)
            if skip_reason:
                log(f"[{pr_id}] skip: {skip_reason}")
                continue

            action = "review"
            if not args.force and pr_id in state:
                action = previously_reviewed_action(
                    repo, pr, state[pr_id], repo_reviewer_logins,
                    token=repo_read_token,
                )

            if action == "skip":
                log(f"[{pr_id}] skip: already reviewed @ {state[pr_id].get('head_sha', '?')[:8]}")
                continue

            if max_prs and reviewed_this_run >= max_prs:
                log(f"[{pr_id}] skip: max_prs_per_scan ({max_prs}) reached")
                continue

            # Decide who would post this PR's review. The event decision below
            # is made against this selected reviewer, not a single global user.
            # An override repo always posts as the operator's own self_token,
            # bypassing the developer rotation and the author-fallback rule.
            if is_override:
                selection_kind = "self_override"
                candidates = [
                    {"login": self_login, "token": self_review["token"]}
                ]
            else:
                selection = reviewer_candidates(pr, developers, viewer)
                selection_kind = selection["kind"]
                candidates = selection["candidates"]
            reviewer_login = candidates[0]["login"]
            if selection_kind == "self_override":
                log(
                    f"[{pr_id}] selected reviewer: {reviewer_login} "
                    "(self-token override)"
                )
            elif selection_kind == "author_fallback":
                log(
                    f"[{pr_id}] selected reviewer: {reviewer_login} "
                    "(PR author — no other eligible developer configured)"
                )
            else:
                log(f"[{pr_id}] selected reviewer: {reviewer_login}")

            # The only eligible reviewer is the PR author and the operator
            # chose to skip rather than downgrade to COMMENT.
            if selection_kind == "author_fallback" and author_fallback == "skip":
                log(
                    f"[{pr_id}] skip: only configured developer is the PR "
                    "author and review.author_fallback is 'skip'"
                )
                continue

            if action == "approve":
                log(f"[{pr_id}] prior comments resolved — approving")
                try:
                    mode = cfg.get("review", {}).get("mode", "auto")
                    result = approve_resolved_pr(
                        repo, pr["number"], pr["headRefOid"], candidates,
                        pr["author"]["login"], mode, args.dry_run
                    )
                    event = result["event"]
                    if not args.dry_run:
                        state[pr_id] = {
                            **state[pr_id],
                            "head_sha": pr["headRefOid"],
                            "reviewed_at": datetime.now(timezone.utc).isoformat(),
                            "verdict": "approve" if event == "APPROVE" else state[pr_id].get("verdict"),
                            "reviewer": result["reviewer"],
                        }
                        save_state(state)
                    reviewed_this_run += 1
                    log(f"[{pr_id}] done ({event})", level="success")
                except Exception as e:
                    log(f"[{pr_id}] FAILED approve: {e}", level="error")
                continue

            if action == "rescan":
                log(f"[{pr_id}] re-reviewing: prior comments unresolved and head advanced")

            log(f"[{pr_id}] reviewing @ {pr['headRefOid'][:8]}: {pr['title']}")
            try:
                result = review_one_pr(
                    repo, pr, prompt_text, cfg, candidates, repo_read_token,
                    args.dry_run
                )
                if not args.dry_run:
                    state[pr_id] = {
                        "head_sha": pr["headRefOid"],
                        "reviewed_at": datetime.now(timezone.utc).isoformat(),
                        "url": pr["url"],
                        "verdict": result["verdict"],
                        "inline_comment_count": result["inline_count"],
                        "reviewer": result["reviewer"],
                    }
                    save_state(state)
                reviewed_this_run += 1
                log(f"[{pr_id}] done", level="success")
            except subprocess.TimeoutExpired:
                log(f"[{pr_id}] FAILED: claude timed out", level="error")
            except Exception as e:
                log(f"[{pr_id}] FAILED: {e}", level="error")

    log(f"scan complete: {reviewed_this_run} reviewed in {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
