# SpectraBot

Background service that scans a configured list of GitHub repos for open PRs,
reviews each one with a headless coding agent, and posts the review (approve /
request-changes / comment) back to GitHub.

- **Review engine:** a headless agent CLI — `claude`, `codex`, or `opencode`, selected in config
- **Posting:** as a configured developer who is *not* the PR author (so no one reviews their own PR), via the `gh` CLI — or as your own ambient `gh` user if no developers are configured
- **Schedule:** `systemd --user` timer on Linux, `launchd` agent on macOS
- **State:** each PR is reviewed at most once, and the state file records which developer posted that review

All tool data lives under `~/.spectrabot/`.

> **Looking for the interactive `/pr-review` slash command?**
> [`pr-review.md`](pr-review.md) is the canonical, tool-agnostic review spec.
> [`integrations/`](integrations/) holds drop-in wrappers for Claude Code,
> Codex, and OpenCode — all generated from the canonical by
> [`scripts/sync-integrations.sh`](scripts/sync-integrations.sh). See
> [`integrations/README.md`](integrations/README.md) for install steps.

---

## 1. Prerequisites

| Tool      | Purpose                              | Check                                |
|-----------|--------------------------------------|--------------------------------------|
| Python ≥ 3.11 | runs `scan.py` (needs stdlib `tomllib`) | `python3 -c 'import tomllib'` |
| `gh`      | lists PRs and posts reviews          | `gh --version`                       |
| review engine | generates the review — one of `claude`, `codex`, `opencode` (see `[engine]` in config) | `<engine> --version` |

### GitHub auth and scopes

SpectraBot posts reviews via the `gh` CLI, so `gh` needs PR write scope on the
repos you point it at:

```sh
gh auth login                  # if not already logged in
gh auth refresh -s repo        # ensure "repo" scope (covers pull request reviews)
gh auth status                 # confirm it picked up the new scope
```

For repos in organizations with SSO, you'll also need to authorize the token
for that org (GitHub prompts you the first time `gh` is denied).

If you configure per-developer tokens (`[[github.developers]]`) or a
`self_token` override — see [section 3](#3-configure) — the same applies to
**each** of those tokens: every developer token and `self_token` needs `repo`
scope, plus SSO authorization for any SSO-protected org it will post in.
Ambient `gh auth` only covers the single-user fallback.

### Review engine auth

SpectraBot drives whichever engine you set as `[engine] name` — `claude`,
`codex`, or `opencode`. Each authenticates independently (e.g. `claude` reads
`~/.claude/`; `codex` and `opencode` have their own logins). If you can run the
engine non-interactively from a normal terminal and get a reply, SpectraBot
can too.

---

## 2. Install

```sh
git clone <this-repo> spectrabot
cd spectrabot
./install.sh
```

What `install.sh` does, in order:

1. Verifies `python3 ≥ 3.11`, `gh`, and `claude` exist.
2. Warns (does not fail) if `gh auth status` shows you're not logged in.
3. Creates `~/.spectrabot/{bin,lib,state,logs}/`.
4. Installs files:
   - `bin/spectrabot` → `~/.spectrabot/bin/spectrabot`
   - `lib/scan.py` + `lib/review_prompt.md` → `~/.spectrabot/lib/`
   - `config/config.example.toml` → `~/.spectrabot/config.toml`
     (only if no config exists yet — reinstalls preserve your edits)
5. Symlinks `~/.local/bin/spectrabot` → `~/.spectrabot/bin/spectrabot` so the
   command is on your `PATH`.
6. Installs and **starts** the schedule for your OS:
   - **Linux:** `~/.config/systemd/user/spectrabot.{service,timer}`,
     then `systemctl --user enable --now spectrabot.timer`
   - **macOS:** `~/Library/LaunchAgents/com.spectrabot.scan.plist`,
     then `launchctl load`

### Install flags

| Flag           | Effect                                              |
|----------------|-----------------------------------------------------|
| (none)         | Full install, schedule enabled.                     |
| `--no-enable`  | Install files but don't enable the timer/agent. Useful for trying it manually first. |
| `--uninstall`  | Shortcut for `./uninstall.sh`.                      |

### Linux: enable lingering

By default, systemd user timers stop when you log out. For an always-on
background service, enable lingering once per machine:

```sh
sudo loginctl enable-linger $USER
```

After this, the timer keeps firing even when you're not logged in. The
installer prints this hint if it's not set.

### macOS: full-disk / accessibility

`launchd` runs the agent in your user context — no extra permissions needed
for the scanner itself. If `claude` reads repos outside your home directory
and macOS prompts for access, grant it once via System Settings → Privacy &
Security.

### PATH

The installer symlinks the entry point to `~/.local/bin/spectrabot`. If that
isn't already on your `PATH`, add it to your shell rc:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
```

This only affects running `spectrabot` interactively. The schedule invokes it
by absolute path, so it works either way.

---

## 3. Configure

Edit `~/.spectrabot/config.toml`:

```toml
[github]
repos = [
  "myorg/api-server",
  "myorg/web-app",
]

# Optional: developer credentials SpectraBot can post reviews as. Each PR is
# reviewed by the first developer in this list whose login is NOT the PR
# author ("first eligible, not the author"), so no one reviews their own PR.
# List your team in the order you want them tried.
[[github.developers]]
login = "alice"
token = "ghp_REPLACE_ME_ALICE"

[[github.developers]]
login = "bob"
token = "ghp_REPLACE_ME_BOB"

[review]
# "auto"    — map verdict to APPROVE / REQUEST_CHANGES / COMMENT
# "comment" — always file as COMMENT regardless of verdict
mode = "auto"

# GitHub logins whose PRs are skipped (e.g. bots you don't want reviewed).
skip_authors = ["dependabot[bot]", "renovate[bot]"]

# What to do when every configured developer IS the PR's own author, so no
# one is eligible to post a non-self review:
#   "comment" — post the review as comment-only (default)
#   "skip"    — skip the PR entirely, posting nothing
author_fallback = "comment"

# Optional self-token override (see "Self-token repo override" below).
# self_token = "ghp_REPLACE_ME_SELF"
self_review_repos = []

# Hard cap per scheduled scan. Protects against runaway token spend if a
# stack of PRs lands at once. Set to 0 for no cap.
max_prs_per_scan = 10

# Skip PRs whose unified diff exceeds this many lines (too big to review
# meaningfully in one shot). 0 disables the limit.
max_diff_lines = 4000

[engine]
# Which agent CLI runs the review: "claude", "codex", or "opencode".
name = "claude"
# Optional: pin a specific engine binary. Empty = look `name` up on PATH.
bin = ""
# Optional: extra args appended to the engine invocation,
# e.g. (claude) ["--model", "claude-sonnet-4-6"].
extra_args = []
# Per-PR timeout in seconds.
timeout_seconds = 600
```

The engine is invoked non-interactively per PR: `claude -p <prompt>`,
`codex exec <prompt>`, or `opencode run <prompt>`. The review prompt is
identical across engines; only the JSON block in the engine's stdout is
consumed, so incidental log output is tolerated. If a new engine emits noisy
stdout that confuses parsing, use `extra_args` to quiet it.

A PR is reviewed when **all** of these are true:

- Open
- Not a draft
- Not already approved by anyone (`reviewDecision != APPROVED`)
- Author not in `skip_authors`
- Not already recorded in the state file (one review per PR, lifetime)
- Diff is at or below `max_diff_lines`
- Per-scan cap `max_prs_per_scan` not yet hit

### Who posts the review

**No `[[github.developers]]` configured (single-user fallback).** SpectraBot
posts every review with whatever identity `gh` is already authenticated as. If
that user authored the PR, the event is automatically downgraded to `COMMENT`
even in `auto` mode — GitHub doesn't allow self-approval.

**One or more `[[github.developers]]` configured.** Each developer entry has a
`login` and a `token`. For each PR, SpectraBot picks the **first developer in
config order whose `login` is not the PR author** and posts the review under
that developer's token — so no one ever reviews their own PR. List your team
in the order you want them tried; if a developer's token has been revoked or
lost repo access, SpectraBot falls through to the next eligible developer.

Configured tokens are validated at startup: SpectraBot resolves each token to
a GitHub login and warns on a mismatch or an unusable token (see
[Troubleshooting](#6-troubleshooting)).

**Author fallback.** When *every* configured developer is the PR's own author
(e.g. a solo repo, or a PR by the only listed developer), no one is eligible
to post a non-self review. `[review] author_fallback` decides what happens:

- `"comment"` (default) — post the review as comment-only, under the author's
  own token. The review still appears; it just can't be an approval.
- `"skip"` — skip the PR entirely and post nothing. A reason is logged.

`skip_authors` is independent of reviewer selection: it only filters which PRs
get reviewed at all. A developer listed in both `skip_authors` and
`[[github.developers]]` can still be selected to *review* other people's PRs.

### Self-token repo override

Sometimes you want specific repos reviewed under your *own* identity rather
than the developer rotation. `[review] self_token` and `self_review_repos`
handle that:

```toml
[review]
self_token = "ghp_REPLACE_ME_SELF"
self_review_repos = ["myorg/infra", "myorg/private-tooling"]
```

- Any repo in `self_review_repos` is reviewed with `self_token` instead of the
  `[[github.developers]]` rotation. This override takes precedence — a repo in
  both `[github] repos` and `self_review_repos` uses the `self_token` path.
- On an override repo, if the PR author equals `self_token`'s own login, the
  event is downgraded to `COMMENT` regardless of verdict (you can't approve
  your own PR). The `author_fallback` setting does **not** apply to override
  repos — `self_token` is always the reviewer there.
- `self_review_repos` defaults to empty, which leaves the multi-developer
  behavior unchanged. If `self_review_repos` is non-empty, `self_token` must
  be set or SpectraBot aborts at startup with a clear error.

`self_token` needs `repo` scope (and SSO authorization where applicable), just
like every developer token. Keep `config.toml` private — `chmod 600`.

### Editing the prompt

The review prompt lives at `~/.spectrabot/lib/review_prompt.md` after install.
Edit it in place to change tone or what the model focuses on. Reinstalling
overwrites it, so keep edits in this repo's `lib/review_prompt.md` if you want
them to survive `./install.sh`.

### Review verdict and severity rubric

`lib/review_prompt.md` asks the model to assign each inline comment one of
five severities (`blocker` / `critical` / `high` / `medium` / `nitpick`) and
derive the top-level `recommendation` mechanically:

- Any **blocker** → `request-changes`
- No blockers, any **critical** → `request-changes`
- Otherwise → `approve` (or `comment` if there are only open questions)

`scan.py` then maps `recommendation` to a GitHub review event via the `mode`
setting above. The `counts` and per-comment `severity` fields in the JSON
output are surfaced in the review body and inline comments — `scan.py` reads
the existing `recommendation` / `summary` / `inline_comments` keys and ignores
the extras, so the contract is additive.

The same rubric drives the interactive [`pr-review.md`](pr-review.md) spec
used by the slash command. Edit the canonical there to keep the
human-readable template in sync with the bot's behavior, then run
`./scripts/sync-integrations.sh` to regenerate the per-tool wrappers.

---

## 4. Operate

### Run manually

```sh
spectrabot                                   # scan everything in config
spectrabot --dry-run                         # don't post — print payloads
spectrabot --repo myorg/api                  # only this repo
spectrabot --repo myorg/api --pr 1234        # only this PR
spectrabot --repo myorg/api --pr 1234 --force --dry-run   # ignore state
```

`--dry-run` fetches the diff and calls `claude`, so it still costs tokens —
it just doesn't POST to GitHub.

### Check schedule status

```sh
# Linux
systemctl --user list-timers spectrabot.timer     # next/last fire times
systemctl --user status spectrabot.service        # last run result

# macOS
launchctl list | grep spectrabot                   # PID column = "-" between runs
```

### Logs

```sh
# Linux
journalctl --user -u spectrabot.service -f         # live
journalctl --user -u spectrabot.service --since "1 hour ago"

# macOS
tail -f ~/.spectrabot/logs/scan.log
```

Each line is timestamped (`2026-05-14T18:55:52Z ...`). Per-PR failures are
logged but don't fail the scan — the timer fires again on its normal cadence.

### Trigger a scan immediately

```sh
# Linux
systemctl --user start spectrabot.service

# macOS
launchctl kickstart -k gui/$UID/com.spectrabot.scan
```

### Change scan frequency

The timer ships at a 5-minute interval (`OnUnitInactiveSec=5min`).

- **Linux:** edit `~/.config/systemd/user/spectrabot.timer`, change
  `OnUnitInactiveSec=5min`, then `systemctl --user daemon-reload &&
  systemctl --user restart spectrabot.timer`.
- **macOS:** edit `~/Library/LaunchAgents/com.spectrabot.scan.plist`, change
  `StartInterval` (seconds), then `launchctl unload <plist> && launchctl load <plist>`.

On Linux, `OnUnitInactiveSec` is measured from when the **previous scan
finishes**, not on a fixed wall clock — so the real gap between scans is the
configured interval *plus* each scan's runtime (typically a few seconds).
`OnUnitInactiveSec=5min` therefore means "5 minutes after the last scan
ends," not "every scan starts on a :00/:05/:10 boundary." For a fixed
wall-clock cadence regardless of scan duration, replace `OnUnitInactiveSec`
with `OnCalendar` instead — e.g. `OnCalendar=*:0/5` for every 5 minutes on the
clock — then `daemon-reload` and restart the timer as above.

Editing the installed timer takes effect immediately, but `./install.sh`
overwrites it from `service/spectrabot.timer`. To make an interval change
survive reinstalls, edit `service/spectrabot.timer` in this repo as well.

### Pause / resume

```sh
# Linux
systemctl --user stop  spectrabot.timer            # pause
systemctl --user start spectrabot.timer            # resume
systemctl --user disable spectrabot.timer          # don't start at boot

# macOS
launchctl unload ~/Library/LaunchAgents/com.spectrabot.scan.plist   # pause
launchctl load   ~/Library/LaunchAgents/com.spectrabot.scan.plist   # resume
```

### State file

`~/.spectrabot/state/reviewed.json` is a JSON object keyed by
`owner/repo#number`:

```json
{
  "myorg/api-server#1234": {
    "head_sha": "abc123def456...",
    "reviewed_at": "2026-05-14T18:55:52+00:00",
    "url": "https://github.com/myorg/api-server/pull/1234",
    "reviewer": "alice"
  }
}
```

The `reviewer` field records which developer's token posted the review.
Legacy entries written before multi-developer support omit it and still load
fine.

Operations:

- **Re-review one PR:** delete its entry, or pass `--force` for a single run.
- **Re-review everything:** `rm ~/.spectrabot/state/reviewed.json`.
- **Audit what's been reviewed:** `jq 'keys' ~/.spectrabot/state/reviewed.json`.

The file isn't synced between machines. If you install on a second host, it
will re-review everything it sees as open.

### Update the tool

From the repo checkout:

```sh
git pull
./install.sh                                       # safe to re-run
```

Reinstalling preserves your config and state. It does overwrite the prompt at
`~/.spectrabot/lib/review_prompt.md` — keep prompt edits in this repo.

---

## 5. Environment variables

You usually don't need these. They exist for tests and weird layouts.

| Variable             | Default                          | Purpose                                       |
|----------------------|----------------------------------|-----------------------------------------------|
| `SPECTRABOT_HOME`    | `~/.spectrabot`                  | Root of all tool data.                        |
| `SPECTRABOT_LIB`     | `$SPECTRABOT_HOME/lib`           | Where `scan.py` is loaded from.               |
| `SPECTRABOT_CONFIG`  | `$SPECTRABOT_HOME/config.toml`   | Config file path.                             |
| `SPECTRABOT_STATE`   | `$SPECTRABOT_HOME/state/reviewed.json` | State file path.                        |

Example — run from a source checkout without installing:

```sh
SPECTRABOT_HOME=/tmp/sb-test \
SPECTRABOT_LIB=$PWD/lib \
./bin/spectrabot --dry-run
```

---

## 6. Troubleshooting

**`config not found at ~/.spectrabot/config.toml`**
You didn't run `./install.sh`, or you ran it as a different user. Re-run it.

**`gh` not authenticated**
`gh auth login` and then `gh auth refresh -s repo`. Confirm with
`gh auth status`. If your org uses SSO, you'll see a "Configure SSO" hint —
follow it.

**Timer runs but `claude: command not found` in journal**
The systemd unit sets `PATH=%h/.local/bin:%h/.pyenv/shims:/usr/local/bin:/usr/bin:/bin`.
If your `claude` lives elsewhere, either symlink it into `~/.local/bin/` or
edit `~/.config/systemd/user/spectrabot.service` to add the right directory,
then `systemctl --user daemon-reload`.

**Reviews post the body but inline comments don't appear**
The scanner retries body-only when GitHub rejects inline comments (usually
because the model picked a line outside the diff). Look for "inline comments
rejected" in the logs. Tightening the prompt or shrinking `max_diff_lines`
usually helps.

**"Validation Failed" on a PR you authored**
You can't approve your own PR. SpectraBot already downgrades to `COMMENT` in
that case; if you still see this, the PR's author login changed (org
transfer, etc.) and the check doesn't match. Add it to `skip_authors`.

**Same PR reviewed twice**
The state file might be missing or unwritable. `ls -l ~/.spectrabot/state/`
should show `reviewed.json` owned by you and writable.

**`WARN: developer <login> token resolves to <other> (login mismatch)`**
The `token` for that `[[github.developers]]` entry authenticates as a
different GitHub user than its configured `login`. The token is still usable
(SpectraBot keeps it), but the `login` is what's used to decide "not the
author" — fix the entry so `login` matches the token's real user.

**`WARN: developer <login> token invalid — skipping`**
That developer's token didn't authenticate at all (revoked, expired, or
missing scope). SpectraBot drops that developer from selection for the run and
carries on with the remaining valid developers — it does not abort the scan.
The same WARN is logged for a bad `self_token`; override repos are then
skipped until the token is fixed.

**A PR was reviewed as `COMMENT` even though the diff looks approvable**
When *every* configured developer is the PR's author, no one is eligible to
post a non-self review, so with `author_fallback = "comment"` (the default)
the review is downgraded to comment-only. Add another developer to
`[[github.developers]]`, or set `author_fallback = "skip"` if you'd rather
those PRs be skipped entirely.

**Costs ran higher than expected**
Lower `max_prs_per_scan`, raise the timer interval, or set `max_diff_lines`
lower. Each scan touches every repo once but only invokes `claude` for PRs
that pass all filters.

---

## 7. Uninstall

```sh
./uninstall.sh           # remove binaries + schedule, keep config/state/logs
./uninstall.sh --purge   # also remove ~/.spectrabot entirely
```

---

## 8. Layout

Source repo:

```
spectrabot/
├── bin/spectrabot                    # entry point (installed to ~/.spectrabot/bin/)
├── lib/scan.py                       # main scanner
├── lib/review_prompt.md              # prompt fed to claude -p (headless bot)
├── pr-review.md                      # canonical interactive review spec
├── integrations/                     # generated /pr-review slash commands
│   ├── README.md
│   ├── claude-code/{prefix.md,commands/pr-review.md}
│   ├── codex/{prefix.md,prompts/pr-review.md}
│   └── opencode/{prefix.md,command/pr-review.md}
├── scripts/sync-integrations.sh      # regenerates integrations/ from pr-review.md
├── config/config.example.toml        # template config
├── service/spectrabot.service        # systemd oneshot
├── service/spectrabot.timer          # systemd timer
├── service/com.spectrabot.scan.plist # launchd agent
├── install.sh
└── uninstall.sh
```

`install.sh` only installs the headless service (`bin/`, `lib/`, `service/`,
`config/`). The interactive slash command lives entirely in `integrations/` —
symlink the wrapper for your tool into its expected path (see
[`integrations/README.md`](integrations/README.md)).

Installed layout (target machine):

```
~/.spectrabot/
├── bin/spectrabot                    # entry point
├── lib/scan.py
├── lib/review_prompt.md
├── config.toml                       # your config (preserved on reinstall)
├── state/reviewed.json               # state
└── logs/scan.log                     # log (macOS only; Linux uses journal)

~/.local/bin/spectrabot               # → symlink to ~/.spectrabot/bin/spectrabot
~/.config/systemd/user/spectrabot.{service,timer}   # Linux schedule
~/Library/LaunchAgents/com.spectrabot.scan.plist    # macOS schedule
```
