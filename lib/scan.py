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


def log(msg: str, level: str = "info") -> None:
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


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def gh(args: list[str], **kwargs) -> str:
    return subprocess.check_output(["gh", *args], text=True, **kwargs)


def gh_json(args: list[str]) -> object:
    out = gh(args).strip()
    return json.loads(out) if out else None


def viewer_login() -> str:
    return gh(["api", "user", "--jq", ".login"]).strip()


def list_open_prs(repo: str) -> list[dict]:
    return gh_json(
        [
            "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--json", "number,headRefOid,isDraft,author,reviewDecision,title,url",
            "--limit", "100",
        ]
    ) or []


def fetch_pr_context(repo: str, pr_number: int) -> tuple[dict, str]:
    meta = gh_json(
        [
            "pr", "view", str(pr_number),
            "--repo", repo,
            "--json",
            "title,body,author,baseRefName,headRefName,additions,deletions,changedFiles,files,url",
        ]
    )
    diff = gh(["pr", "diff", str(pr_number), "--repo", repo])
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


def decide_event(recommendation: str, is_own_pr: bool, mode: str) -> str:
    if mode == "comment" or is_own_pr:
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


def post_review(repo: str, pr_number: int, payload: dict) -> None:
    owner, name = repo.split("/", 1)
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
            )
            if proc2.returncode != 0:
                raise RuntimeError(f"gh api review failed: {proc2.stderr.strip()[:500]}")
            return
        raise RuntimeError(f"gh api review failed: {proc.stderr.strip()[:500]}")


def fetch_spectrabot_threads(repo: str, pr_number: int, viewer: str) -> list[dict]:
    """Return the review threads on a PR whose first comment was authored by
    `viewer` (i.e. SpectraBot's own threads). Each item is
    {"resolved": bool, "outdated": bool}."""
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
    ]) or {}
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
        if author != viewer:
            continue
        threads.append({
            "resolved": bool(t.get("isResolved")),
            "outdated": bool(t.get("isOutdated")),
        })
    return threads


def previously_reviewed_action(
    repo: str, pr: dict, state_entry: dict, viewer: str
) -> str:
    """For a PR already in state, decide whether to skip, approve, or rescan.

    Returns "skip" | "approve" | "rescan".
    """
    head_changed = state_entry.get("head_sha") != pr["headRefOid"]
    verdict = state_entry.get("verdict")
    inline_count = state_entry.get("inline_comment_count")

    if verdict is None:
        # Legacy entry from before the schema was extended.
        return "rescan" if head_changed else "skip"

    if verdict not in ("request-changes", "comment"):
        # Previously approved (or an unexpected verdict). Re-review only when
        # new commits have landed since — an unchanged head has nothing left
        # to re-evaluate.
        return "rescan" if head_changed else "skip"

    if not inline_count:
        return "rescan" if head_changed else "skip"

    try:
        threads = fetch_spectrabot_threads(repo, pr["number"], viewer)
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
    is_own_pr: bool,
    mode: str,
    dry_run: bool,
) -> str:
    """Post a re-review for a PR whose prior SpectraBot comments are resolved
    or outdated. Returns the GitHub event used ("APPROVE" or "COMMENT")."""
    event = "COMMENT" if (mode == "comment" or is_own_pr) else "APPROVE"
    verb = "Approving" if event == "APPROVE" else "Acknowledging resolution"
    body = (
        f"Previously raised comments are resolved or outdated. {verb}.\n\n"
        "---\n_Automated re-review by SpectraBot._"
    )
    payload = {"commit_id": head_sha, "event": event, "body": body}
    if dry_run:
        log(f"  [dry-run] would POST review:\n{json.dumps(payload, indent=2)}")
        return event
    post_review(repo, pr_number, payload)
    if event == "APPROVE":
        celebrate_approval(f"{repo}#{pr_number}")
    return event


def review_one_pr(
    repo: str,
    pr: dict,
    prompt_text: str,
    cfg: dict,
    viewer: str,
    dry_run: bool,
) -> dict:
    pr_number = pr["number"]
    head_sha = pr["headRefOid"]
    is_own = pr["author"]["login"] == viewer
    meta, diff = fetch_pr_context(repo, pr_number)

    max_diff = cfg.get("review", {}).get("max_diff_lines", 4000)
    diff_lines = diff.count("\n")
    if max_diff and diff_lines > max_diff:
        raise RuntimeError(f"diff too large ({diff_lines} > {max_diff} lines)")

    review = invoke_engine(prompt_text, meta, diff, cfg)
    mode = cfg.get("review", {}).get("mode", "auto")
    verdict = review.get("recommendation", "comment")
    event = decide_event(verdict, is_own, mode)
    payload = build_review_payload(review, head_sha, event)
    inline_count = len(payload.get("comments", []))

    log(f"  verdict={verdict!r} event={event} inline={inline_count}")

    if not dry_run:
        post_review(repo, pr_number, payload)
        if event == "APPROVE":
            celebrate_approval(f"{repo}#{pr_number}")
    else:
        log(f"  [dry-run] would POST review:\n{json.dumps(payload, indent=2)[:2000]}")

    return {"verdict": verdict, "inline_count": inline_count}


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
    state = load_state()
    prompt_text = PROMPT_PATH.read_text()

    try:
        viewer = viewer_login()
    except subprocess.CalledProcessError as e:
        sys.exit(f"`gh` not authenticated? {e}")

    repos = [args.repo] if args.repo else cfg.get("github", {}).get("repos", [])
    if not repos:
        log("no repos configured; nothing to do")
        return 0

    skip_authors = set(cfg.get("review", {}).get("skip_authors", []))
    max_prs = cfg.get("review", {}).get("max_prs_per_scan", 10)
    reviewed_this_run = 0
    start = time.time()

    for repo in repos:
        try:
            prs = list_open_prs(repo)
        except subprocess.CalledProcessError as e:
            log(f"[{repo}] gh pr list failed: {e}")
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
                action = previously_reviewed_action(repo, pr, state[pr_id], viewer)

            if action == "skip":
                log(f"[{pr_id}] skip: already reviewed @ {state[pr_id].get('head_sha', '?')[:8]}")
                continue

            if max_prs and reviewed_this_run >= max_prs:
                log(f"[{pr_id}] skip: max_prs_per_scan ({max_prs}) reached")
                continue

            if action == "approve":
                log(f"[{pr_id}] prior comments resolved — approving")
                try:
                    mode = cfg.get("review", {}).get("mode", "auto")
                    is_own = pr["author"]["login"] == viewer
                    event = approve_resolved_pr(
                        repo, pr["number"], pr["headRefOid"], is_own, mode, args.dry_run
                    )
                    if not args.dry_run:
                        state[pr_id] = {
                            **state[pr_id],
                            "head_sha": pr["headRefOid"],
                            "reviewed_at": datetime.now(timezone.utc).isoformat(),
                            "verdict": "approve" if event == "APPROVE" else state[pr_id].get("verdict"),
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
                result = review_one_pr(repo, pr, prompt_text, cfg, viewer, args.dry_run)
                if not args.dry_run:
                    state[pr_id] = {
                        "head_sha": pr["headRefOid"],
                        "reviewed_at": datetime.now(timezone.utc).isoformat(),
                        "url": pr["url"],
                        "verdict": result["verdict"],
                        "inline_comment_count": result["inline_count"],
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
