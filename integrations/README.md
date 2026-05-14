# Integrations

Drop-in `/pr-review` slash command for Claude Code, Codex, and OpenCode. Each
wrapper is **generated** from the canonical [`../pr-review.md`](../pr-review.md)
by [`../scripts/sync-integrations.sh`](../scripts/sync-integrations.sh) — do
not hand-edit the generated files.

```
spectrabot/
├── pr-review.md                                 ← source of truth
├── scripts/sync-integrations.sh                 ← regenerates wrappers
└── integrations/
    ├── claude-code/
    │   ├── prefix.md                            ← frontmatter only (hand-edit)
    │   └── commands/pr-review.md                ← GENERATED
    ├── codex/
    │   ├── prefix.md                            ← (hand-edit)
    │   └── prompts/pr-review.md                 ← GENERATED
    └── opencode/
        ├── prefix.md                            ← frontmatter only (hand-edit)
        └── command/pr-review.md                 ← GENERATED
```

## Editing the spec

1. Edit `../pr-review.md` (the canonical content) and/or the relevant
   `<tool>/prefix.md` (tool-specific frontmatter only).
2. Run `./scripts/sync-integrations.sh` from the repo root.
3. Commit both the canonical edit and the regenerated wrappers.

The canonical uses `{{TARGET}}` as the argument placeholder. The sync script
substitutes it per tool (`$ARGUMENTS` for Claude Code / OpenCode, `$1` for
Codex). Blocks between `<!-- SYNC-STRIP-START -->` and `<!-- SYNC-STRIP-END -->`
in the canonical are stripped from generated wrappers — use these to write
notes that only make sense in the source file.

## Install (pick the one for your tool)

### Claude Code

```sh
# User-scoped (available in every project)
ln -sf "$(pwd)/integrations/claude-code/commands/pr-review.md" \
       ~/.claude/commands/pr-review.md

# Or project-scoped (only in this repo)
mkdir -p .claude/commands
ln -sf ../../integrations/claude-code/commands/pr-review.md \
       .claude/commands/pr-review.md
```

Invoke with `/pr-review`, `/pr-review 1234`, or `/pr-review my-branch`.

### Codex

```sh
mkdir -p ~/.codex/prompts
ln -sf "$(pwd)/integrations/codex/prompts/pr-review.md" \
       ~/.codex/prompts/pr-review.md
```

Invoke with `/pr-review` in the Codex TUI. Codex passes positional args as
`$1`, `$2`, ... — pass the PR number as `/pr-review 1234`.

### OpenCode

```sh
# Global
mkdir -p ~/.config/opencode/command
ln -sf "$(pwd)/integrations/opencode/command/pr-review.md" \
       ~/.config/opencode/command/pr-review.md

# Or project-scoped
mkdir -p .opencode/command
ln -sf ../../integrations/opencode/command/pr-review.md \
       .opencode/command/pr-review.md
```

Invoke with `/pr-review` in OpenCode.
