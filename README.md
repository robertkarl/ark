# ark

A dumb Python orchestrator for LLM agents. No LLM in the orchestration loop.

The problem: when you tell an LLM "you're the orchestrator, have subagents plan, review, implement" — it inevitably tries to write code, negotiate, or go off-script. ark solves this by making the orchestrator a plain Python script that runs a fixed pipeline.

## Install

```
ln -sf $(pwd)/ark.py ~/bin/ark
```

Requires: `python3`, `tmux`, `claude` (Claude Code CLI), `codex` (optional, for adversarial review and `--driver codex`).

## Usage

```
echo 'Add a health check endpoint' | ark new
echo 'Add a health check endpoint' | ark new --driver codex
ark archive my-label
ark help
```

## Pipeline

```
cat feature.txt | ark new
```

1. **spec** — agent explores codebase, writes `.ark/SPEC.md` with numbered acceptance criteria
2. **review-spec** — fresh agent reviews spec for ambiguity, missing edge cases
3. **encode** — fresh agent writes a verification Makefile (`.ark/verify-*.mk`)
4. **review-make** — fresh agent reviews Makefile for impossible/vacuous checks
5. **implement** — agent implements the spec (up to 3 attempts)
6. **verify** — runs `make -k` on the Makefile, writes `REVIEW.md`
7. **fix-make** — if verify fails, fresh agent fixes Makefile structural issues
8. **reimplement** — fresh agent gets spec + review, tries again
9. *(loop 5-8 up to 3 times)*
10. **adversarial** — claude + codex review in parallel, write findings
11. **land** — fresh agent addresses critical/major findings

Each agent gets fresh context. No state bleeds between steps. The Python script decides what runs next — not an LLM.

## How it works

- Each run executes in its own [git worktree](https://git-scm.com/docs/git-worktree)
  — a separate working directory linked to the same repository — so **multiple
  runs can proceed in parallel on one repository** without fighting over the
  working tree, branches, or `.ark/` artifacts.
- The worktree is created at a deterministic path under
  `.git/ark-worktrees/<slug>`, with the `ark/<slug>` branch checked out. All
  file edits, commits, `.ark/` artifacts, and verification happen there — the
  **repository-root working tree is never touched** (no stashing, no branch
  switching, uncommitted changes are left alone).
- All agents run in a tmux session you can attach to: `tmux attach -t ark-<slug>`
  (the session name is derived from the slug, so concurrent runs don't collide).
- Agents commit as they go on the `ark/<slug>` branch. When done, ark prints the
  worktree path and the `git merge ark/<slug>` command — the branch is visible
  and mergeable from the repository root via normal git, no merge required to see
  the commits.
- Resumable while in progress: re-run the same command (or `ark continue`) and
  it reuses the existing worktree and branch, skipping completed steps based on
  the artifacts in that worktree's `.ark/`. With several runs in flight,
  disambiguate from the repository root with `ark continue <slug>`. (Archiving a
  run ends it — see below.)
- A run's worktree persists after the run finishes so its branch stays
  inspectable and mergeable from the repository root. Stale registrations (a
  worktree directory deleted out from under git) are detected and pruned
  automatically on the next run for that slug, so they don't accumulate as
  orphans. Remove a finished run's worktree yourself with
  `git worktree remove .git/ark-worktrees/<slug>` once you've merged it.
- `ark archive [label]` is **terminal**: it sweeps the whole run out of `.ark/`
  into `.ark/archive/<timestamp>/`. An archived run is gone — it's no longer
  discoverable by `ark continue` or resumable. Start a fresh run to revisit the
  feature.

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `ARK_MODEL` | `opus` | Model for all claude invocations |
| `ARK_SKIP_PERMISSIONS` | `1` | Set to `0` to remove `--dangerously-skip-permissions` |
| `ARK_IDLE_TIMEOUT` | `900` | Seconds a step may make **no progress** (no change to watched output) before it is declared hung. A step that keeps writing output is never killed. |
| `ARK_STEP_TIMEOUT` | `86400` | Absolute wall-clock ceiling per step, regardless of progress — a final backstop (24h). |

ark watches each run's worktree output area (`results/` and the worktree
generally) while an implement/reimplement step runs. As long as that output
keeps advancing, the step is considered alive and is never killed mid-write — a
multi-hour job that steadily appends to an append-only file survives up to the
hard ceiling. A step is timed out only when it makes no progress for
`ARK_IDLE_TIMEOUT` seconds (or hits `ARK_STEP_TIMEOUT`). A timed-out step is
reported as an explicit **timeout** — it never advances to verification and
never renders a PASS/FAIL verdict against partial state. Both env vars must be
positive integers; a non-numeric or non-positive value makes ark fail fast at
startup naming the offending variable.

## Design principles

- One file, stdlib only, no dependencies
- The orchestrator is not an LLM — it's a dumb script
- Each step is a small function
- Fresh context at every step (no accumulated confusion)
- Agents write to disk, script reads from disk
