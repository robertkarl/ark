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

- All agents run in a tmux session you can attach to: `tmux attach -t ark-*`
- Creates an `ark/<slug>` git branch for isolation
- Agents commit as they go on the feature branch
- Resumable: re-run the same command and it skips completed steps
- Archives results to `.ark/archive/` after each run

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `ARK_MODEL` | `opus` | Model for all claude invocations |
| `ARK_SKIP_PERMISSIONS` | `1` | Set to `0` to remove `--dangerously-skip-permissions` |

## Design principles

- One file, stdlib only, no dependencies
- The orchestrator is not an LLM — it's a dumb script
- Each step is a small function
- Fresh context at every step (no accumulated confusion)
- Agents write to disk, script reads from disk
