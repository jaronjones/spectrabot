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

Skip nits about formatting, naming preferences, or things a linter would catch.
Be specific — reference exact lines. If the PR looks clean, say so.

## Output format

Respond with EXACTLY ONE fenced JSON code block and nothing else. The JSON must
match this shape:

```json
{
  "recommendation": "approve" | "request-changes" | "comment",
  "summary": "Markdown body for the overall review. 2-6 sentences. Explain the verdict.",
  "inline_comments": [
    {
      "path": "path/to/file.py",
      "line": 42,
      "side": "RIGHT",
      "body": "Specific comment about this line."
    }
  ]
}
```

Rules:
- `recommendation`:
  - `"approve"` only if you would merge this as-is.
  - `"request-changes"` if there is a real bug, security issue, or breaking change.
  - `"comment"` for "looks fine but here are some thoughts" or pure questions.
- `inline_comments[].line` is the line number in the **new** version of the file
  (the right side of the diff), not a position in the diff hunk.
- `inline_comments[].side` should almost always be `"RIGHT"`. Use `"LEFT"` only
  when commenting on a deleted line that you specifically want to discuss.
- Only attach inline comments to lines that actually appear in the diff. Do not
  invent line numbers.
- 0–8 inline comments is plenty. If there's nothing line-specific to say, leave
  the array empty and put your thoughts in `summary`.
- Output nothing outside the fenced JSON block. No prose preamble, no trailing
  commentary.
