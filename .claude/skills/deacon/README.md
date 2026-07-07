# deacon

> The daemon beacon. A patrol agent that watches ARK runs for test-cheating.

The deacon is a Claude Code skill (`/deacon`, shown as `ark:deacon`) that runs a
**patrol loop**: every 15 minutes it inspects the live `ark-*` tmux sessions and
looks for the one failure mode ARK's own harness struggles to catch from the
inside — **agents cheating the tests**: making the verify Makefile go green
without making the behavior correct (grep-for-the-symbol proxies, always-true
one-liners, vacuous/stubbed gates, deleted or weakened checks).

When it finds degenerate cheating it files a **SEV-2 [tcorp](../../../../tcorp)
ticket** in the *offending run's own repo* (`<repo>/.tcorp/tickets/`) and moves
on. It never touches the run — it's a watchdog, not a gate.

## Use

From inside the `ark` repo:

```
/deacon
```

The deacon runs one patrol immediately, files any tickets, then reschedules
itself for +15 min via the harness wakeup. The loop lives inside your Claude
Code session — closing the session stops the deacon. Say "stop the deacon" to
cancel the schedule.

## How it works

1. `list-sessions.sh` enumerates the **in-progress** `ark-*` tmux sessions and
   maps each to its source repo. ARK runs each pipeline in a worktree at
   `<repo>/.git/ark-worktrees/<slug>`, so the repo root is the path before
   `/.git/ark-worktrees/` — that repo's `.tcorp/` is where a finding is filed.

   **Liveness filter.** ARK never kills the tmux session when a run finishes (the
   session lingers so the branch stays mergeable), so a live session name is not
   evidence of an active run. The definitive completion signal is that ARK's
   terminal `archive_run()` **sweeps `.ark/` clean** — every terminal outcome
   (success, loop-exhausted, or timeout) moves `SPEC.md`/`verify-*.mk`/`REVIEW.md`
   into `.ark/archive/`. So the script lists a session only if its worktree still
   has a top-level `.ark/SPEC.md` (in progress); completed runs — whose panes
   often show stale/reused scrollback — are skipped. Without this, the deacon
   wastes inspectors (tens of thousands of tokens each) auditing dead runs.
2. The deacon spawns **one inspector subagent per session, in parallel, on
   Sonnet** (the cheap model — inspection is bounded, mechanical pattern-matching,
   so a 15-min patrol stays affordable). Each inspector `capture-pane`s its
   session, reads the run's `.ark/` artifacts
   (`SPEC.md`, `verify-*.mk`, `REVIEW.md`, commits), and judges each acceptance
   criterion against the known cheat shapes (cross-referencing
   `~/.ark/LESSONS.md`). It applies an 80% confidence bar and returns `CLEAN` or
   structured findings.
3. For each confirmed finding, the deacon **dedups** against the repo's existing
   open tickets (the patrol runs every 15 min — it won't page twice for the same
   cheat), then `file-ticket.sh` allocates the next `<PREFIX>-<n>` ID and writes
   a SEV-2 ticket in tcorp's markdown format.
4. It prints a one-line summary and reschedules the next patrol.

## Files

| file                | role                                                          |
|---------------------|--------------------------------------------------------------|
| `SKILL.md`          | the patrol instructions (`/deacon`)                          |
| `list-sessions.sh`  | enumerate live `ark-*` sessions → `session repo worktree tcorp` TSV |
| `file-ticket.sh`    | allocate ID + write a SEV-2 tcorp ticket in a repo's `.tcorp/` |

## Design notes

- **Read-only over runs.** Inspectors observe; the deacon files tickets. Nothing
  edits a run's files, Makefile, SPEC, or branch.
- **Precision over recall.** A false SEV-2 pages a human for nothing. When
  unsure, the deacon notes it in the patrol summary rather than filing.
- **Out-of-band.** Filing a ticket never blocks or slows the run being watched.
