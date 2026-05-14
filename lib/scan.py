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
import re
import shutil
import subprocess
import sys
import time
import tomllib
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


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {msg}", flush=True)


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


def parse_claude_output(text: str) -> dict:
    """Pull the JSON block out of Claude's response."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"no JSON object in claude output (first 500 chars): {text[:500]}")


def invoke_claude(prompt_text: str, pr_meta: dict, diff: str, cfg: dict) -> dict:
    user_prompt = (
        f"{prompt_text}\n\n"
        f"## PR Metadata\n```json\n{json.dumps(pr_meta, indent=2)}\n```\n\n"
        f"## Unified Diff\n```diff\n{diff}\n```\n"
    )
    claude_bin = cfg.get("claude", {}).get("bin") or shutil.which("claude") or "claude"
    extra_args = cfg.get("claude", {}).get("extra_args", [])
    timeout = cfg.get("claude", {}).get("timeout_seconds", 600)
    cmd = [claude_bin, "-p", user_prompt, "--output-format", "text", *extra_args]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"claude exit {res.returncode}: {res.stderr.strip()[:500]}"
        )
    return parse_claude_output(res.stdout)


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
            log(f"  inline comments rejected, retrying body-only: {proc.stderr.strip()[:200]}")
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


def review_one_pr(
    repo: str,
    pr: dict,
    prompt_text: str,
    cfg: dict,
    viewer: str,
    dry_run: bool,
) -> None:
    pr_number = pr["number"]
    head_sha = pr["headRefOid"]
    is_own = pr["author"]["login"] == viewer
    meta, diff = fetch_pr_context(repo, pr_number)

    max_diff = cfg.get("review", {}).get("max_diff_lines", 4000)
    diff_lines = diff.count("\n")
    if max_diff and diff_lines > max_diff:
        raise RuntimeError(f"diff too large ({diff_lines} > {max_diff} lines)")

    review = invoke_claude(prompt_text, meta, diff, cfg)
    mode = cfg.get("review", {}).get("mode", "auto")
    event = decide_event(review.get("recommendation", "comment"), is_own, mode)
    payload = build_review_payload(review, head_sha, event)

    log(f"  verdict={review.get('recommendation')!r} event={event} inline={len(payload.get('comments', []))}")

    if dry_run:
        log(f"  [dry-run] would POST review:\n{json.dumps(payload, indent=2)[:2000]}")
        return

    post_review(repo, pr_number, payload)


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

            if not args.force and pr_id in state:
                log(f"[{pr_id}] skip: already reviewed @ {state[pr_id].get('head_sha', '?')[:8]}")
                continue

            if max_prs and reviewed_this_run >= max_prs:
                log(f"[{pr_id}] skip: max_prs_per_scan ({max_prs}) reached")
                continue

            log(f"[{pr_id}] reviewing @ {pr['headRefOid'][:8]}: {pr['title']}")
            try:
                review_one_pr(repo, pr, prompt_text, cfg, viewer, args.dry_run)
                if not args.dry_run:
                    state[pr_id] = {
                        "head_sha": pr["headRefOid"],
                        "reviewed_at": datetime.now(timezone.utc).isoformat(),
                        "url": pr["url"],
                    }
                    save_state(state)
                reviewed_this_run += 1
                log(f"[{pr_id}] done")
            except subprocess.TimeoutExpired:
                log(f"[{pr_id}] FAILED: claude timed out")
            except Exception as e:
                log(f"[{pr_id}] FAILED: {e}")

    log(f"scan complete: {reviewed_this_run} reviewed in {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
