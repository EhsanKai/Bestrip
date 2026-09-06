#!/usr/bin/env bash
# Path-drift preflight. Run this before ANY write, in every agent and session.
#
# Why this exists: git worktrees cannot write into each other - they share only
# the object store. Every "my isolated worktree modified the main checkout"
# incident is therefore path drift, not a git defect: a stray `cd`, or an agent
# scoped to a worktree using an absolute path back into the main tree. This
# check is cheap and catches it before the write, not after.
#
# Usage:  scripts/preflight_paths.sh [expected-repo-root]
# Exit 0 = safe to write. Non-zero = STOP, you are not where you think you are.

set -uo pipefail
EXPECTED="${1:-/Users/a/Documents/BesTrip/Bestrip}"

CWD="$(pwd -P)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo '<not-a-repo>')"
COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo '?')"
GITDIR="$(git rev-parse --path-format=absolute --git-dir 2>/dev/null || echo '?')"
BRANCH="$(git branch --show-current 2>/dev/null || echo '<detached>')"
HEAD_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"

# A worktree has git-dir != git-common-dir; the main checkout has them equal.
if [ "$GITDIR" = "$COMMON" ]; then KIND="main checkout"; else KIND="WORKTREE"; fi

echo "cwd      : $CWD"
echo "repo root: $ROOT"
echo "kind     : $KIND"
echo "branch   : $BRANCH @ $HEAD_SHA"
echo "dirty    : $(git status --porcelain 2>/dev/null | wc -l | tr -d ' ') entries"

fail=0
[ "$ROOT" = "$EXPECTED" ] || { echo "FAIL: repo root is not the expected tree ($EXPECTED)"; fail=1; }
[ "$CWD" = "$ROOT" ]      || { echo "WARN: cwd is not the repo root (relative paths will not land where you expect)"; }
case "$CWD" in
  */.claude/worktrees/*) echo "FAIL: cwd is inside an agent worktree"; fail=1 ;;
esac

[ "$fail" -eq 0 ] && echo "OK: safe to write" || echo "STOP: do not write from here"
exit "$fail"
