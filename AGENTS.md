# Agent instructions for the ark repo

## ark is human-only — do not invoke it

`ark` (`ark new`, `ark continue`, `ark archive`, `cat ticket | ark`) is a
human-driven orchestrator. **Agents must never run it.** It:

- spawns fresh-context Claude agents and drives a live tmux session for a long time,
- recurses if launched from inside an ark run (guarded by `ARK_RUNNING`, but don't rely on that),
- is easy to kill by accident — piping `ark new` into `head`/`tail` or backgrounding
  it SIGPIPE-kills the run mid-pipeline.

If the user wants an ark run, instruct the user to run the `ark` command
themselves at their terminal. Do not run it on their behalf, in the background,
or via a subagent.

## Editing ark itself

To exercise ark features end-to-end, drive it from the throwaway sandbox repo
`~/Code/arktesting` (a human runs it), not from inside this repo — dogfooding ark
from within the ark repo is awkward because of the recursion guard and worktree
machinery. Make code changes here; test the running behavior there.
