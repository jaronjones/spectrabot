#!/usr/bin/env bash
# Regenerate per-tool /pr-review wrappers from the canonical pr-review.md.
#
# For each tool we cat:
#   integrations/<tool>/prefix.md         (tool-specific frontmatter)
# followed by the canonical body with {{TARGET}} replaced by the tool's
# argument placeholder, written to the path the tool expects.
#
# Edit pr-review.md (canonical) and the prefix.md files. Then re-run this.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CANON="pr-review.md"
[[ -f $CANON ]] || { echo "missing $CANON at repo root" >&2; exit 1; }

generate() {
  local tool=$1 target=$2 out=$3
  local prefix="integrations/$tool/prefix.md"
  [[ -f $prefix ]] || { echo "missing $prefix" >&2; exit 1; }
  mkdir -p -- "$(dirname -- "$out")"
  {
    cat -- "$prefix"
    # Strip canonical-only blocks (meta docs that shouldn't appear in wrappers),
    # then substitute the target placeholder. `|` delimiter avoids `/`-in-path
    # escaping headaches.
    sed -e '/<!-- SYNC-STRIP-START -->/,/<!-- SYNC-STRIP-END -->/d' \
        -e "s|{{TARGET}}|${target//|/\\|}|g" \
        -- "$CANON"
  } > "$out"
  echo "wrote $out"
}

generate claude-code '$ARGUMENTS' 'integrations/claude-code/commands/pr-review.md'
generate codex       '$1'          'integrations/codex/prompts/pr-review.md'
generate opencode    '$ARGUMENTS' 'integrations/opencode/command/pr-review.md'
