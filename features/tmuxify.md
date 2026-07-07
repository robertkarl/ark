Invert ark's foreground/background layout so the human watches the AGENT, not the orchestrator.

## Problem

Today `ark new` foregrounds the Python orchestrator process and backgrounds the
agent in a tmux session. This is backwards: the orchestrator's foreground output
is just a progress spinner — `[step:spec]`, `[step:review-spec]`,
`[step:encode]`, etc. — while ALL the interesting work (the agent exploring the
repo, reasoning, writing code/Makefiles) happens in the tmux session the user has
to `tmux attach` to separately. The boring pane is front-and-center; the
interesting pane is hidden one command away.

## Desired behavior

Flip it. When `ark new` (or `ark continue`) starts:

1. Create the git worktree and init the run as today.
2. Create the tmux session as today.
3. Set up a TWO-PANE layout in that session:
   - **pane 0 (focused/primary): agentic workloads** — this is where the
     claude/codex agent runs for each step (implement, reimplement, encode,
     review, fix-make). The user's eyes land here by default.
   - **pane 1 (secondary/status): the long-running Python orchestrator** — the
     step-by-step pipeline driver and its `[step:...]` progress output. Glanceable
     status, not the focus. A small pane (e.g. a bottom strip or right sidebar) is
     fine.
4. **Attach the user to the tmux session** (when interactive) so they're INSIDE
   the session with the agent pane primary — instead of staring at the
   orchestrator's stdout from outside the session.

So the end state: one `tmux attach`-style view, agent pane large and primary,
orchestrator status pane small. Detaching leaves the whole pipeline running
(nice side effect — the orchestrator is now in tmux too, so detach-survival is
total, not partial).

## Constraints / things to preserve

- The sentinel completion mechanism must keep working unchanged: each step runs
  the agent in pane 0, the agent drops the `_sentinel_<hash>` file when done, and
  the orchestrator (now in pane 1) polls for it via `wait_for_sentinel`. Don't
  change the agent contract or the sentinel protocol.
- The control-inversion is the tricky part: today the Python process is the
  parent and drives tmux from OUTSIDE (`run_in_tmux` send-keys + poll). In the new
  model the orchestrator lives INSIDE a pane it doesn't own. The orchestrator
  needs to: create the session, launch itself (or re-exec) into pane 1, and keep
  its sentinel-polling loop alive there, while sending agent commands to pane 0.
  Decide cleanly who is parent of whom. One option: a thin launcher creates the
  session + panes, then starts the orchestrator in pane 1 and the agent shell in
  pane 0; the orchestrator targets pane 0 for send-keys instead of the current
  single-pane target.
- `ark continue` should attach to the already-live pane layout rather than
  re-foregrounding a script.
- Keep `ARK_SKIP_PERMISSIONS`, the worktree setup, archiving, and all existing
  step logic intact. This is a presentation/process-topology change, not a change
  to what the pipeline does.
- Non-interactive / piped invocations (no tty) must still work headless: if
  there's no terminal to attach to, fall back to running detached (current
  behavior is acceptable there) — don't block on an attach that can't happen.

## Touch points (in ark.py)

- `run_pipeline` — the top-level driver loop.
- `run_in_tmux` / `TmuxSession` — session creation, send_command, the send-keys
  target pane, `wait_for_sentinel`.
- session setup (`create_session`) — add the split-pane layout + pane targeting.
- the `main()` / `ark new` / `ark continue` entry points — where the
  attach-vs-foreground decision is made.

## Acceptance

- `ark new < some-feature` opens a tmux session, attaches the user (when
  interactive), with the AGENT pane primary and the orchestrator `[step:...]`
  output in a secondary pane.
- The pipeline runs to completion exactly as before (same steps, same sentinel
  handshake, same artifacts).
- Detaching from the session leaves the pipeline running; re-attaching
  (`ark continue` or `tmux attach`) shows the live layout.
- Headless/piped runs with no tty still complete without trying to attach.
