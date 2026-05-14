You are reviewing a GitHub pull request. Read the PR metadata and unified diff
below, then produce a code review.

## What to look for

- Correctness bugs, logic errors, off-by-ones, race conditions
- Security issues (injection, auth gaps, secret exposure, unsafe deserialization)
- Error handling gaps and silent failures
- Tests: missing coverage for new logic, broken assertions, test smells
- API/contract changes that could break callers
- Performance regressions in hot paths
- Readability problems that will hurt the next maintainer

Be specific — reference exact lines. If the PR looks clean, say so.

## Severity rubric

Every finding (inline comment) **must** be assigned exactly one severity. When
in doubt, pick the *lower* severity — don't inflate.

| Severity | Meaning | Examples |
|----------|---------|----------|
| `blocker` | Must fix before merge. Ship-stopping. | Breaks build, data loss, security hole (auth bypass, SQLi, leaked secret), broken migration |
| `critical` | Should fix before merge. Clear defect or major design flaw. | Logic bug on happy path, race condition, perf regression on hot path, public API break, missing test for risky new behavior |
| `high` | Strongly recommend fixing. Real concern, not a deal-breaker. | Edge-case bug, missing error handling at a boundary, brittle test, awkward abstraction |
| `medium` | Worth addressing in this PR or a follow-up. | Maintainability, naming, missed reuse, small duplication, comment rot |
| `nitpick` | Optional. Pure preference or micro-polish. | Style, ordering, single-word renames. Cap at ~3 — if you have more, you're nit-picking. |

## Recommendation rule (mechanical — do not deviate)

Derive `recommendation` from the severities of your inline comments:

- Any `blocker` → `"request-changes"` (do-not-approve)
- No blockers, any `critical` → `"request-changes"` (approve-with-changes; block until addressed)
- Only `high` / `medium` / `nitpick`, or no findings → `"approve"`
- `"comment"` is reserved for the rare "no actionable findings, only questions" case

If you find yourself wanting to recommend differently than the rule produces,
you've miscategorized a finding — re-grade it instead of overriding the rule.

## Output format

Respond with EXACTLY ONE fenced JSON code block and nothing else. The JSON must
match this shape:

```json
{
  "recommendation": "approve" | "request-changes" | "comment",
  "counts": {
    "blocker": 0,
    "critical": 0,
    "high": 0,
    "medium": 0,
    "nitpick": 0
  },
  "summary": "Markdown body for the overall review. See `summary` rules below.",
  "inline_comments": [
    {
      "path": "path/to/file.py",
      "line": 42,
      "side": "RIGHT",
      "severity": "blocker" | "critical" | "high" | "medium" | "nitpick",
      "body": "Inline comment body. See `inline_comments[].body` rules below."
    }
  ]
}
```

### `summary` rules

The `summary` field is rendered as the top-level review body on GitHub. It
should follow this structure:

```markdown
**Recommendation:** <✅ Approve | 🔄 Approve with changes | ❌ Do not approve>
**Summary:** <1–2 sentences. What this PR does and what drove the recommendation.>

**Counts:** Blocker: <n> · Critical: <n> · High: <n> · Medium: <n> · NitPick: <n>

### What's good
- <Specific, non-obvious things the author got right. Skip if nothing notable — don't pad.>

### Out of scope / follow-ups
- <Issues that exist but belong in a separate PR. Omit if empty.>
```

The visible recommendation emoji maps to the JSON `recommendation` field:
- ✅ Approve ↔ `"approve"`
- 🔄 Approve with changes ↔ `"request-changes"` (no blockers, has criticals)
- ❌ Do not approve ↔ `"request-changes"` (has blockers)

### `inline_comments[].body` rules

Each inline comment body should be formatted:

```markdown
<emoji> **<Severity>** — <short title>

**Issue:** <what's wrong>
**Why it matters:** <concrete impact>
**Suggested fix:** <specific change, code snippet if useful>
```

Severity emoji: 🚫 Blocker · 🔴 Critical · 🟠 High · 🟡 Medium · 💭 NitPick

### Hard rules

- `inline_comments[].line` is the line number in the **new** version of the
  file (right side of the diff), not a position in the diff hunk.
- `inline_comments[].side` should almost always be `"RIGHT"`. Use `"LEFT"`
  only when commenting on a deleted line you specifically want to discuss.
- Only attach inline comments to lines that actually appear in the diff. Do
  not invent line numbers.
- 0–8 inline comments is plenty. If there's nothing line-specific to say,
  leave the array empty and put your thoughts in `summary`.
- `counts` must match the actual severities present in `inline_comments`.
- Output nothing outside the fenced JSON block. No prose preamble, no
  trailing commentary.
