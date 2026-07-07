#!/usr/bin/env bash
# deacon: enumerate LIVE (in-progress) `ark-*` tmux sessions, map each to its
# source repo, and SKIP sessions whose run has already completed.
#
# ARK runs every pipeline in a tmux session named `ark-<slug>`, with the agent
# in pane 0 whose cwd is `<repo>/.git/ark-worktrees/<slug>`. The repo root is
# therefore the path preceding `/.git/ark-worktrees/`. That repo's `.tcorp/` is
# where a cheating finding for this run gets filed.
#
# Liveness — the load-bearing filter. ARK deliberately NEVER kills the tmux
# session when a run finishes (the session lingers so the branch stays
# inspectable), so "session exists" does NOT mean "run in progress." The
# definitive completion signal is in ark.py: every terminal outcome (succeeded,
# implement/verify loop exhausted, review-fix loop exhausted, or timed out) ends
# by calling `archive_run()`, which SWEEPS `.ark/` clean — it moves SPEC.md,
# verify-*.mk, REVIEW.md, etc. out of `<worktree>/.ark/` into
# `<worktree>/.ark/archive/<timestamp>/`. So:
#
#   run in progress  ⟺  `<worktree>/.ark/SPEC.md` exists (top-level, not archive)
#   run completed     ⟺  `.ark/` swept: no top-level SPEC.md (only .ark/archive/)
#
# This is the same check `ark continue` uses to decide a run is over. We emit a
# session ONLY if its worktree still has a live, un-swept `.ark/SPEC.md`.
#
# Emits one TSV line per LIVE session:
#   <session>\t<repo_root>\t<worktree_path>\t<tcorp_dir>
# tcorp_dir is <repo_root>/.tcorp/tickets (may not exist yet).
#
# Prints nothing (exit 0) if no ark run is in progress.

set -euo pipefail

command -v tmux >/dev/null 2>&1 || { echo "deacon: tmux not found" >&2; exit 0; }
tmux has-session 2>/dev/null || tmux ls >/dev/null 2>&1 || exit 0

tmux list-sessions -F '#{session_name}' 2>/dev/null \
  | grep -E '^ark-' \
  | while IFS= read -r sess; do
      # cwd of the agent pane (pane 0)
      cwd=$(tmux display-message -p -t "${sess}:0.0" '#{pane_current_path}' 2>/dev/null || true)
      [ -n "$cwd" ] || continue
      case "$cwd" in
        */.git/ark-worktrees/*)
          repo="${cwd%%/.git/ark-worktrees/*}"
          worktree="${cwd%%/.git/ark-worktrees/*}/.git/ark-worktrees/${cwd##*/.git/ark-worktrees/}"
          # Normalize: everything after ark-worktrees/ up to the first slash is
          # the slug dir; the worktree root is repo/.git/ark-worktrees/<slug>.
          rest="${cwd#*/.git/ark-worktrees/}"
          slug="${rest%%/*}"
          worktree="${repo}/.git/ark-worktrees/${slug}"
          ;;
        *)
          # Not in a worktree (attach/idle shell): best-effort repo + treat the
          # cwd as the worktree so the liveness check below can look for .ark/.
          repo=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || echo "$cwd")
          worktree="$cwd"
          ;;
      esac

      # LIVENESS: only in-progress runs carry a top-level .ark/SPEC.md. A
      # completed run has had .ark/ swept into .ark/archive/, so SPEC.md is
      # gone from the top level. Skip completed (and never-started) runs.
      [ -f "${worktree}/.ark/SPEC.md" ] || continue

      printf '%s\t%s\t%s\t%s\n' "$sess" "$repo" "$worktree" "${repo}/.tcorp/tickets"
    done
