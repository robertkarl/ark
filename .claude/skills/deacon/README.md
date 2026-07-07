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

The deacon runs one patrol immediately, files any tickets, then arms a recurring
`CronCreate` job (every ~17 min) that re-invokes `/deacon`. The loop lives inside
your Claude Code session — it's session-only (dies when you close the session)
and cron auto-expires after 7 days. Say "stop the deacon" to delete the job.

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
   so a ~15-min patrol stays affordable). Each inspector `capture-pane`s its
   session, reads the run's `.ark/` artifacts (`SPEC.md`, `verify-*.mk`,
   `REVIEW.md`) and git history, and hunts two categories: **(A) false-green
   verify checks** — grep-for-symbol proxies, always-true one-liners,
   vacuous/stubbed build gates, assertions on the harness's own output,
   hard-coded expected values, error-swallowing checks, self-authored evidence;
   and **(B) prompt-rule violations** the agents are told not to commit —
   mutating `.ark/SPEC.md`/`verify-*.mk`, `git merge`, pushing, or writing
   product code into the verify Makefile. The known-cheat catalog is
   `~/.ark/LESSONS.md` (read fresh each patrol; it evolves). Each inspector
   applies an 80% confidence bar and returns `CLEAN` or structured findings.
3. For each confirmed finding, the deacon **dedups** against the repo's existing
   open tickets (the patrol runs every ~17 min — it won't page twice for the same
   cheat), then `file-ticket.sh` allocates the next `<PREFIX>-<n>` ID and writes
   a SEV-2 ticket in tcorp's markdown format.
4. It prints a one-line summary; the recurring cron fires the next patrol.

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
