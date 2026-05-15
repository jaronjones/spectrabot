# SpectraBot

Background service that scans a configured list of GitHub repos for open PRs,
reviews each one with a headless coding agent, and posts the review (approve /
request-changes / comment) back to GitHub.

- **Review engine:** a headless agent CLI — `claude`, `codex`, or `opencode`, selected in config
- **Posting:** as your own GitHub user, via the `gh` CLI
- **Schedule:** `systemd --user` timer on Linux, `launchd` agent on macOS
- **State:** each PR is reviewed at most once (tracked in a JSON state file)

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

SpectraBot posts reviews as your user, so `gh` needs PR write scope on the
repos you point it at:

```sh
gh auth login                  # if not already logged in
gh auth refresh -s repo        # ensure "repo" scope (covers pull request reviews)
gh auth status                 # confirm it picked up the new scope
```

For repos in organizations with SSO, you'll also need to authorize the token
for that org (GitHub prompts you the first time `gh` is denied).

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

[review]
# "auto"    — map verdict to APPROVE / REQUEST_CHANGES / COMMENT
# "comment" — always file as COMMENT regardless of verdict
mode = "auto"

# GitHub logins whose PRs are skipped (e.g. bots you don't want reviewed).
skip_authors = ["dependabot[bot]", "renovate[bot]"]

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

If you authored the PR, the event is automatically downgraded to `COMMENT`
even in `auto` mode — GitHub doesn't allow self-approval.

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

- **Linux:** edit `~/.config/systemd/user/spectrabot.timer`, change
  `OnUnitInactiveSec=10min`, then `systemctl --user daemon-reload &&
  systemctl --user restart spectrabot.timer`.
- **macOS:** edit `~/Library/LaunchAgents/com.spectrabot.scan.plist`, change
  `StartInterval` (seconds), then `launchctl unload <plist> && launchctl load <plist>`.

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
    "url": "https://github.com/myorg/api-server/pull/1234"
  }
}
```

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
