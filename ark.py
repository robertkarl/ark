#!/usr/bin/env python3
"""ark - A dumb Python orchestrator for LLM agents.

No LLM in the loop. Just a script that runs a fixed pipeline,
spawning fresh-context agents at each step.

Usage:
    echo 'Add a feature' | ark new
    ark archive
"""

# TODO: add the option for ark to spin up agents when it wants/needs
# some judgement (like, "is this work good enough?")

import functools
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

# Force unbuffered output so pipeline progress shows in real time
print = functools.partial(print, flush=True)

# ---------------------------------------------------------------------------
# Prompts — each agent gets only what it needs, nothing more.
# Agents read/write files by path. No prompt contains file contents inline.
# ---------------------------------------------------------------------------

PROMPT_SENTINEL = """
IMPORTANT: When you are completely finished with your task, create this \
file to signal completion: {sentinel_path}
"""

PROMPT_SPEC = """\
You are a specification writer. Explore the current project to understand \
its structure, then turn the feature description in {feature_path} into a \
clear, testable specification document.

Requirements:
- Each requirement must be independently testable
- Use numbered acceptance criteria (AC-1, AC-2, ...)
- Include edge cases and error conditions
- Do NOT include implementation details or code
- Write the final spec to {spec_path}
"""

PROMPT_REVIEW_SPEC = """\
You are a specification reviewer. Explore the current project to understand \
its structure, then review the spec at {spec_path} for:
- Ambiguity or vagueness
- Missing edge cases
- Untestable criteria
- Contradictions

If issues are found, rewrite the spec with fixes applied and overwrite {spec_path}.
If the spec is solid, leave it unchanged.
Also write your review notes to {review_path}.
"""

PROMPT_ENCODE = """\
You are a test author. Explore the current project to understand its \
structure, then read the spec at {spec_path} and turn it into a Makefile \
that verifies each acceptance criterion.

Rules:
- One make target per acceptance criterion (ac-1, ac-2, ...)
- Each target should run a concrete check (curl, grep, test -f, etc.)
- Use .PHONY for all targets
- Include an "all" target that depends on every ac-* target
- The Makefile must work with `make -k` (keep going on failure)
- Use standard unix tools only
- Do NOT implement the feature — only write verification checks
- CRITICAL: Test targets must complete quickly (under 10 seconds each). \
Do NOT invoke commands that start long-running processes, spawn LLM agents, \
or trigger full pipeline runs. For CLI flag tests, only check argument \
parsing behavior (help text, error messages, exit codes) — never pipe \
real input that would start an actual pipeline.
- Write the Makefile to {makefile_path}
"""

PROMPT_REVIEW_MAKE = """\
You are a Makefile reviewer. Read the verification Makefile at {makefile_path} \
and the spec at {spec_path}. Review the Makefile for:
- Syntax errors
- Targets that would never pass (impossible checks)
- Targets that would always pass (vacuous checks)
- Missing .PHONY declarations
- Checks that test implementation details instead of behavior

If issues are found, overwrite {makefile_path} with a corrected version.
If it's solid, leave it unchanged.
Also write your review notes to {review_path}.
"""

PROMPT_IMPLEMENT = """\
You are an implementer. Read the spec at {spec_path} and implement it in \
the current project.

Rules:
- Do not modify anything under .ark/
- Do not merge branches. Do not run git merge. Commit on the current branch only.
- Do not push to any remote.
"""

PROMPT_REIMPLEMENT = """\
You are an implementer. A previous implementation attempt failed verification.
Read the spec at {spec_path} and the verification results at {review_path}, \
then fix the implementation.

Rules:
- Do not modify anything under .ark/
- Do not merge branches. Do not run git merge. Commit on the current branch only.
- Do not push to any remote.
"""

PROMPT_FIX_MAKE = """\
You are a Makefile fixer. The verification Makefile at {makefile_path} \
produced errors when run. The output is at {review_path}.

Fix ONLY syntax errors or structural problems with the Makefile itself. \
Do NOT change what the targets are testing — only fix how they test it.

Overwrite {makefile_path} with the corrected version.
"""

PROMPT_ADVERSARIAL = """\
You are a fresh-context code reviewer. You have ZERO knowledge of the build \
process. You review only the diff and the codebase as it exists right now.

Step 1: Gather the diff.
First, determine the default branch: check if `main` exists with \
`git rev-parse --verify main`, and if not, fall back to `master`.
Then run: git diff <default-branch>...HEAD
If the diff is empty, write "Nothing to review" to {output_path} and stop.

Step 2: Read context.
For each file touched in the diff, read enough surrounding code to understand \
the change in context. Do not read the entire codebase. Focus on what the diff touches.

Step 3: Review with precision over recall.
For each potential finding, ask yourself:
1. Am I at least 80% confident this is a real issue? If not, skip it.
2. Can I point to a specific file and line? If not, skip it.
3. Is this a real defect, or just a style preference? Skip style preferences.
An empty report is a valid outcome. Do not manufacture findings to look thorough.

Read the spec at {spec_path} for context on what was intended.

Step 4: Write your report to {output_path} using this format:

## Code Review
**Verdict: PASS / FAIL** (PASS = 0 BLOCKs, FAIL = 1+ BLOCKs)

### Findings

#### BLOCK: title
- **File:** path:line
- **Evidence:** what you see in the code
- **Impact:** what goes wrong

#### WARN: title
- **File:** path:line
- **Evidence:** what you see in the code
- **Risk:** what could go wrong

#### NOTE: title (low urgency, may be intentional)

Severity tiers:
- BLOCK: Defect causing incorrect behavior, data loss, security issue, or test failure.
- WARN: Likely problem, 80%+ confident it matters, but not immediate failure.
- NOTE: Observation worth mentioning. Low urgency.

Every finding must cite file:line. Confidence threshold: 80%.
"""

PROMPT_FIX_REVIEW = """\
You are an implementer addressing code review feedback. Two code reviewers \
examined this codebase. Their findings are at {claude_review_path} and \
{codex_review_path}.

Address every BLOCK finding. Address WARN findings if straightforward. Ignore NOTEs.

Do not reimplement the whole feature. Make targeted fixes to address the \
specific findings only.

Read the spec at {spec_path} for context on what was intended.

Rules:
- Do not modify anything under .ark/
- Do not merge branches. Do not run git merge. Commit on the current branch only.
- Do not push to any remote.
"""

PROMPT_INTROSPECT = """\
You are a post-hoc introspection agent. An ark run just finished. ark is the \
orchestrator/harness that ran a fixed pipeline of fresh-context LLM agents \
(spec, review, encode, implement, verify, adversarial review, fix). Your job \
is to look at the run that just finished and record LESSONS about *ark itself* \
— the harness — that hurt this run, so a human operator can improve ark.

This run's terminal outcome was: {outcome}

The run's artifacts have been archived. Read them from the archive directory:
    {archive_dir}
That directory contains this run's SPEC.md, review notes, verification output \
(REVIEW.md), adversarial review reports, and other artifacts. Read whatever \
you need from there to understand how the run went. Examine ONLY this run's \
own artifacts — do not read other runs' archives.

What qualifies as a LESSON (be strict — precision over recall):
- It is about ARK THE HARNESS: its orchestration, timeouts, prompts, pipeline \
structure, loop bounds, archive behavior, or similar. NOT about the target \
codebase or the specific feature that was being built.
- It is CAUSAL: it identifies something that actually degraded or broke this \
run (a timeout/race that derailed it, a loop that degenerated, a prompt that \
misled an agent). Not cosmetic, not incidental, not a superstitious \
correlation (e.g. "runs fail when a filename contains a certain word").
- It can be PAIRED WITH A PLAUSIBLE FIX expressible as roughly a one-line ark \
change or a single spec item that could be fed back into ark.
- If you cannot pair an observation with a plausible ark fix, DISCARD it. Do \
not record a fix-less or one-clause note.

Authoring format for each lesson — exactly TWO prose sentences:
1. The observation: what about ark hurt this run.
2. The proposed fix: a ~one-line ark change that would address it.
Write each lesson as prose (NOT a bulleted list). Separate multiple lessons \
with a blank line.

Producing ZERO lessons is a valid and expected outcome, especially for a \
clean successful run. Do NOT fabricate a lesson to appear productive.

Output:
- If and only if you have one or more real lessons, write them to {output_path}.
- Each lesson you write must be on its own (it may span the two sentences).
- If you have NO lessons, do NOT create {output_path} at all (leave it absent). \
Write nothing rather than an empty or placeholder lesson.

Do NOT modify the source tree, do not commit, do not merge, do not push, and \
do not start a new ark pipeline. Your only output is the lessons file above.
"""

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MODEL_RAW = os.environ.get("ARK_MODEL", "opus")
_MODEL_VALIDATED = False
MODEL = _MODEL_RAW
MAX_LOOPS = 3
MAX_REVIEW_LOOPS = 3
ARK_DIR = ".ark"
# Machine-global, append-only lessons file (one per machine, outside any repo).
# Introspection appends lessons here; every agent step injects its contents.
GLOBAL_LESSONS_FILE = Path("~/.ark/LESSONS.md").expanduser()
# Run-local copy of lessons, written into the archive directory alongside the
# archived SPEC.md and other artifacts (AC-21).
RUN_LESSONS_FILENAME = "LESSONS.md"
# Worktrees live under .git/<WORKTREE_SUBDIR>/<slug>. This keeps them out of
# the repository-root working tree (so they never show up as tracked or
# untracked entries) while remaining deterministic and tied to the repo.
WORKTREE_SUBDIR = "ark-worktrees"


def _validate_model():
    """Validate MODEL on first use. Deferred so 'help' and unknown commands work
    even when ARK_MODEL is invalid."""
    global _MODEL_VALIDATED
    if _MODEL_VALIDATED:
        return
    # Must start with alphanumeric to prevent flag injection (e.g. --help).
    # Only alphanumeric, hyphens, dots, and underscores allowed after that.
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", MODEL):
        print(
            f"Error: ARK_MODEL contains invalid characters: {MODEL!r}\n"
            f"  Must start with a letter/digit. Only alphanumeric, hyphens, "
            f"dots, and underscores are allowed.",
            file=sys.stderr,
        )
        sys.exit(1)
    _MODEL_VALIDATED = True

# ---------------------------------------------------------------------------
# Tmux helpers — the agent is the foreground; tmux carries the whole run
# ---------------------------------------------------------------------------

# Pane layout (FR-1): the AGENT pane is primary/large and active; the STATUS
# pane is the small secondary strip in which the orchestrator's driver loop and
# its [step:...] progress run. The status pane is a minority of the split
# dimension so the agent pane spans the majority (AC-2).
AGENT_PANE = 0
STATUS_PANE = 1
# Status pane gets a minority of the window along the split dimension; the agent
# pane gets the rest (>=60%, AC-2). 25% leaves the agent ~75%.
STATUS_PANE_SIZE = "25%"


class TmuxError(Exception):
    """tmux is unavailable or a tmux operation failed (EC-9 / AC-42)."""


class Tmux:
    """Manage a single tmux session laid out as two panes:

    pane 0 (AGENT_PANE)  — primary/large/active, where each step's agent runs.
    pane 1 (STATUS_PANE) — small secondary strip, where the orchestrator's
                           driver loop and its [step:...] progress run.

    The orchestrator drives the agent pane via send-keys to pane 0 and polls for
    the agent's sentinel exactly as before; only the on-screen topology changed.
    """

    def __init__(self, session_name):
        self.session = session_name

    @property
    def agent_target(self):
        """send-keys/-t target for the agent pane (pane 0)."""
        return f"{self.session}.{AGENT_PANE}"

    @property
    def status_target(self):
        """send-keys/-t target for the status pane (pane 1)."""
        return f"{self.session}.{STATUS_PANE}"

    def create_session(self, working_dir):
        """Create a detached two-pane session with the agent pane primary.

        Raises TmuxError if tmux is unavailable or session/pane creation fails,
        so the caller can fail loudly instead of later hanging on a sentinel in
        a session that was never built (EC-9 / AC-42).
        """
        try:
            new = subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.session, "-c",
                 working_dir],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            # tmux binary not installed/usable (EC-9 / AC-42).
            raise TmuxError(
                "tmux is not installed or not on PATH — ark needs tmux to run "
                "the agent and orchestrator panes."
            )
        if new.returncode != 0:
            raise TmuxError(
                f"failed to create tmux session '{self.session}': "
                f"{new.stderr.strip() or 'tmux unavailable'}"
            )
        # Split off a small secondary pane (pane 1). The new pane becomes active
        # and would normally take ~50%; size it to a minority and hand focus back
        # to the agent pane (pane 0) so the agent is dominant and active (AC-2,
        # AC-3).
        split = subprocess.run(
            ["tmux", "split-window", "-v", "-l", STATUS_PANE_SIZE,
             "-t", self.session, "-c", working_dir],
            capture_output=True, text=True,
        )
        if split.returncode != 0:
            # Don't leave a half-built single-pane layout that a later
            # `ark continue` would mistake for a healthy run (AC-42).
            self.kill_session()
            raise TmuxError(
                f"failed to split tmux session '{self.session}' into two panes: "
                f"{split.stderr.strip() or 'tmux split-window failed'}"
            )
        subprocess.run(
            ["tmux", "select-pane", "-t", self.agent_target],
            capture_output=True,
        )

    def has_two_panes(self):
        """True if the session is alive and already has both panes (AC-15)."""
        result = subprocess.run(
            ["tmux", "list-panes", "-t", self.session, "-F", "#{pane_index}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False
        return len([l for l in result.stdout.splitlines() if l.strip()]) >= 2

    def send_command(self, cmd, pane=AGENT_PANE):
        """Send a command string to a pane (default: the agent pane) via a
        temp script.

        This avoids tmux send-keys buffer limits on long commands.
        Uses mkstemp to avoid symlink attacks on predictable paths.
        """
        fd, script_path = tempfile.mkstemp(prefix="ark-", suffix=".sh")
        script_body = f"#!/bin/bash\n{cmd}\n"
        try:
            os.write(fd, script_body.encode())
        finally:
            os.close(fd)
        os.chmod(script_path, 0o700)
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{self.session}.{pane}",
             f"bash {script_path}", "Enter"],
            check=True,
        )

    def exit_agent_repl(self):
        """Drop any interactive agent REPL in the agent pane back to its shell.

        Sent to the agent pane (pane 0), not the session default, so the
        orchestrator running in the status pane (pane 1) is never disturbed
        (AC-13).
        """
        subprocess.run(
            ["tmux", "send-keys", "-t", self.agent_target, "/exit", "Enter"],
            capture_output=True,
        )

    def pane_pid(self):
        """Get the shell PID of the agent pane."""
        result = subprocess.run(
            ["tmux", "list-panes", "-t", self.agent_target, "-F", "#{pane_pid}"],
            capture_output=True, text=True,
        )
        return result.stdout.strip().split("\n")[0] if result.stdout.strip() else None

    def attach(self):
        """Attach the user's terminal to the session (interactive runs).

        Blocks until the user detaches or the session ends. Detaching leaves the
        session — and the orchestrator running inside it — alive (FR-6).
        """
        subprocess.run(["tmux", "attach-session", "-t", self.session])

    def wait_for_sentinel(self, sentinel_path, poll_interval=5, timeout=1800):
        """Wait for a sentinel file to appear, meaning the command finished."""
        start = time.time()
        while time.time() - start < timeout:
            if os.path.exists(sentinel_path):
                os.unlink(sentinel_path)
                return True
            if not self.is_alive():
                print("  [!] tmux session died", file=sys.stderr)
                return False
            time.sleep(poll_interval)
        print(f"  [!] Timeout after {timeout}s waiting for agent", file=sys.stderr)
        return False

    def is_alive(self):
        """Check if the tmux session still exists.

        Returns False if tmux is not installed, so the caller falls through to
        create_session, which raises a clear TmuxError (EC-9 / AC-42) rather than
        letting a FileNotFoundError traceback escape here.
        """
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", self.session],
                capture_output=True,
            )
        except FileNotFoundError:
            return False
        return result.returncode == 0

    def status_pane_busy(self):
        """True if an orchestrator driver loop is running in the status pane.

        Used by interactive reuse to tell "alive and actively being driven by an
        in-tmux orchestrator the user detached from" (re-attach only, AC-18)
        apart from "alive but idle at a shell prompt" (safe to drive a new step).
        Detected via the status pane's foreground command: a python/ark process
        there is the inner orchestrator; a bare shell is idle.
        """
        result = subprocess.run(
            ["tmux", "list-panes", "-t", self.status_target,
             "-F", "#{pane_current_command}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False
        cmd = result.stdout.strip().lower()
        return any(tok in cmd for tok in ("python", "ark"))

    def kill_session(self):
        # Tolerate tmux being absent: reaping a session is best-effort, and the
        # caller's subsequent create_session() raises the clear TmuxError for the
        # tmux-unavailable case (EC-9 / AC-42).
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", self.session],
                capture_output=True,
            )
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def kp(project_dir, filename):
    """Return absolute path inside .ark/."""
    return str(Path(project_dir) / ARK_DIR / filename)


def kf_exists(project_dir, filename):
    """Check if a .ark/ artifact exists and is non-empty."""
    p = Path(project_dir) / ARK_DIR / filename
    return p.exists() and p.stat().st_size > 0


def slugify(text):
    """Turn feature description into a short slug.

    Appends a short hash to avoid collisions when two features share the
    same first 4 words (e.g., 'add auth login flow extra' vs
    'add auth login flow different').
    """
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    base = "-".join(words[:4]) or "feature"
    suffix = hashlib.sha1(text.lower().encode()).hexdigest()[:6]
    return f"{base}-{suffix}"


def ensure_dir(project_dir):
    """Create .ark/ directory."""
    d = Path(project_dir) / ARK_DIR
    d.mkdir(exist_ok=True)
    return d


class ArkError(Exception):
    """A user-facing error that should abort the run with a clear message."""


def _git(args, cwd=None, check=False):
    """Run a git command, returning the CompletedProcess."""
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check,
    )


def repo_root(cwd=None):
    """Return the absolute path of the repository-root working tree.

    Raises ArkError if cwd is not inside a git repository (EC-1).
    """
    result = _git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if result.returncode != 0:
        raise ArkError(
            "not inside a git repository — ark needs a git repo to create "
            "worktrees.\n  Run ark from inside a git working tree."
        )
    return result.stdout.strip()


def git_common_dir(cwd=None):
    """Return the absolute path of the shared .git directory for this repo.

    Using the *common* dir (not the per-worktree git dir) means every worktree
    resolves to the same base, so worktree paths are stable regardless of which
    working tree ark is invoked from.
    """
    result = _git(["rev-parse", "--git-common-dir"], cwd=cwd)
    if result.returncode != 0:
        raise ArkError("not inside a git repository")
    common = result.stdout.strip()
    # --git-common-dir may be relative (e.g. ".git"); resolve against cwd.
    base = Path(cwd) if cwd else Path.cwd()
    return str((base / common).resolve())


def worktree_path(slug, cwd=None):
    """Deterministic worktree path for a slug (AC-40, EC-10).

    Same slug -> same path; different slugs -> different paths. Lives under the
    shared .git directory so it never appears in the repo-root working tree.
    """
    return str(Path(git_common_dir(cwd)) / WORKTREE_SUBDIR / slug)


def _registered_worktrees(cwd=None):
    """Parse `git worktree list --porcelain` into {abs_path: branch_or_None}."""
    result = _git(["worktree", "list", "--porcelain"], cwd=cwd)
    trees = {}
    cur_path = None
    cur_branch = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            cur_path = line[len("worktree "):]
            cur_branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            # refs/heads/ark/<slug> -> ark/<slug>
            cur_branch = ref.replace("refs/heads/", "", 1)
        elif line == "" and cur_path is not None:
            trees[str(Path(cur_path).resolve())] = cur_branch
            cur_path = None
    if cur_path is not None:
        trees[str(Path(cur_path).resolve())] = cur_branch
    return trees


def _branch_exists(branch, cwd=None):
    return _git(["rev-parse", "--verify", "--quiet", branch], cwd=cwd).returncode == 0


def _is_ark_worktree_path(path, cwd=None):
    """True if `path` lives under the ark-managed worktree base.

    Used to distinguish ark's own run worktrees from the repository-root
    working tree (and any user-created worktrees), which must never be treated
    as a reusable run worktree.
    """
    base = str(Path(git_common_dir(cwd)) / WORKTREE_SUBDIR)
    resolved = str(Path(path).resolve())
    return resolved == base or resolved.startswith(base + os.sep)


def _branch_checked_out_at(branch, cwd=None):
    """Return the ark-managed worktree path where `branch` is checked out.

    Only ark's own run worktrees are considered: the repository-root working
    tree (and any user worktree) is excluded, so a run branch that happens to
    be checked out in the repo root never masquerades as a reusable worktree.
    """
    for path, br in _registered_worktrees(cwd).items():
        if br == branch and _is_ark_worktree_path(path, cwd=cwd):
            return path
    return None


def setup_worktree(slug, root):
    """Create or reuse a dedicated worktree for this run's slug.

    Returns the absolute path to the worktree. Handles the worktree edge cases:
      EC-3  path exists       -> reuse if valid for this slug, else error
      EC-4  branch exists     -> check it out (preserve history)
      EC-5  branch elsewhere  -> reuse that worktree, or error
      EC-6  stale registration-> prune and recreate

    Raises ArkError on unrecoverable problems (EC-2, EC-3 conflict).
    """
    branch = f"ark/{slug}"
    wt = worktree_path(slug, cwd=root)
    wt_resolved = str(Path(wt).resolve())

    registered = _registered_worktrees(cwd=root)
    reg_branch = registered.get(wt_resolved)
    on_disk = Path(wt).is_dir()

    # EC-6: stale registration — registered but directory is gone. Prune it.
    if wt_resolved in registered and not on_disk:
        print("  Pruning stale worktree registration...")
        _git(["worktree", "prune"], cwd=root)
        registered = _registered_worktrees(cwd=root)
        reg_branch = registered.get(wt_resolved)

    # EC-3 / AC-32: path already a valid worktree for THIS run -> reuse.
    if wt_resolved in registered and Path(wt).is_dir() and reg_branch == branch:
        print(f"  Reusing existing worktree: {wt}")
        return wt

    # EC-3 / AC-33: registered at our path but with the wrong branch -> error
    # rather than silently corrupting it.
    if wt_resolved in registered and reg_branch != branch:
        raise ArkError(
            f"worktree path already exists but has '{reg_branch}' checked out, "
            f"not '{branch}':\n  {wt}\n  Resolve manually "
            f"(git worktree remove) before retrying."
        )

    # EC-5: the branch is checked out in some OTHER worktree.
    other = _branch_checked_out_at(branch, cwd=root)
    if other is not None and str(Path(other).resolve()) != wt_resolved:
        if Path(other).is_dir():
            print(f"  Reusing existing worktree for {branch}: {other}")
            return other
        # Registered elsewhere but missing on disk -> prune and continue.
        print("  Pruning stale worktree for this branch...")
        _git(["worktree", "prune"], cwd=root)

    # EC-5 (AC-35): the branch is checked out somewhere git won't let us reuse
    # as a run worktree — e.g. the repository root. Report a clear, actionable
    # error instead of letting `worktree add` fail with a cryptic message or,
    # worse, mutating the repo root.
    non_ark = None
    for path, br in _registered_worktrees(cwd=root).items():
        if br == branch and not _is_ark_worktree_path(path, cwd=root):
            non_ark = path
            break
    if non_ark is not None:
        raise ArkError(
            f"branch '{branch}' is already checked out outside an ark "
            f"worktree:\n  {non_ark}\n  Check out a different branch there "
            f"(e.g. `git -C {non_ark} switch -`) before starting this run."
        )

    # EC-3 (AC-33): a non-worktree directory squats our path -> error.
    if Path(wt).exists():
        raise ArkError(
            f"intended worktree path already exists and is not a valid ark "
            f"worktree:\n  {wt}\n  Remove it or choose a different slug."
        )

    # Create the worktree, checking out the branch (EC-4: existing branch is
    # reused with its history; otherwise a new branch is created).
    Path(wt).parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(branch, cwd=root):
        add = _git(["worktree", "add", wt, branch], cwd=root)
    else:
        add = _git(["worktree", "add", "-b", branch, wt], cwd=root)

    if add.returncode != 0:
        # EC-2: worktree creation failed (git error, permission denied, etc.)
        raise ArkError(
            f"failed to create worktree at {wt}:\n  {add.stderr.strip()}"
        )

    print(f"  Created worktree: {wt}")
    return wt


def remove_worktree(slug, root):
    """Remove a run's worktree and prune stale registrations (AC-21)."""
    wt = worktree_path(slug, cwd=root)
    if Path(wt).exists():
        _git(["worktree", "remove", "--force", wt], cwd=root)
    _git(["worktree", "prune"], cwd=root)


def find_ark_worktrees(root):
    """Return ark run worktrees that have a .ark/FEATURE.md (in-progress runs).

    Returns a list of (slug, path) for worktrees living under the deterministic
    ark worktree base. Used by `ark continue`/`ark archive` to locate a run's
    artifacts from the repository root without knowing the worktree path.
    """
    base = str(Path(git_common_dir(cwd=root)) / WORKTREE_SUBDIR)
    found = []
    for path in _registered_worktrees(cwd=root):
        if not (path == base or path.startswith(base + os.sep)):
            continue
        if (Path(path) / ARK_DIR / "FEATURE.md").exists():
            found.append((Path(path).name, path))
    return sorted(found)


def make_sentinel(project_dir):
    """Create a unique sentinel path for this invocation."""
    return os.path.join(project_dir, ARK_DIR, f"_sentinel_{uuid.uuid4().hex[:8]}")


def run_in_tmux(
        tmux, cmd, sentinel, timeout=1800):
    """Send a command to the tmux worker and wait for the agent to signal done.

    The agent is instructed (via PROMPT_SENTINEL in its prompt) to create the
    sentinel file when finished. If the tmux session dies, recreates and retries.

    The session's working directory is the run's worktree: the sentinel lives in
    <worktree>/.ark/, so the worktree is sentinel.parent.parent (AC-5).
    """
    work_dir = str(Path(sentinel).parent.parent)
    if not tmux.is_alive():
        tmux.create_session(work_dir)
    # Send the agent command to the AGENT pane (pane 0), where it runs as that
    # pane's foreground process and renders its reasoning stream (AC-4, AC-5).
    tmux.send_command(cmd, pane=AGENT_PANE)
    ok = tmux.wait_for_sentinel(sentinel, timeout=timeout)
    if ok:
        # End the interactive agent REPL so the agent pane is ready for the next
        # step's agent (AC-13). Targets pane 0 only — the orchestrator in pane 1
        # is untouched.
        tmux.exit_agent_repl()
        time.sleep(2)
    if not ok and not tmux.is_alive():
        print("  [!] Retrying after tmux session loss", file=sys.stderr)
        tmux.create_session(work_dir)
        tmux.send_command(cmd, pane=AGENT_PANE)
        ok = tmux.wait_for_sentinel(sentinel, timeout=timeout)
        if ok:
            tmux.exit_agent_repl()
            time.sleep(2)
    return ok


SKIP_PERMISSIONS = os.environ.get("ARK_SKIP_PERMISSIONS", "1") == "1"


def claude_cmd(
        prompt_file):
    """Build an interactive claude command that reads its prompt from a file.

    Uses $(cat file) command substitution inside double quotes as a positional
    argument, so the prompt streams visibly as the agent works.
    """
    _validate_model()
    skip = " --dangerously-skip-permissions" if SKIP_PERMISSIONS else ""
    return f'claude --model {shlex.quote(MODEL)}{skip} "$(cat {shlex.quote(prompt_file)})"'


def codex_cmd(
        prompt_file, project_dir):
    """Build a codex exec command that reads its prompt from a file."""
    return f'prompt=$(<{shlex.quote(prompt_file)}) && codex exec --full-auto -C {shlex.quote(project_dir)} "$prompt"'


def driver_cmd(
        prompt_file, project_dir, driver="claude"):
    """Build the right agent command based on the driver."""
    if driver == "codex":
        return codex_cmd(prompt_file, project_dir)
    return claude_cmd(prompt_file)


def lessons_injection():
    """Return a prompt block injecting ~/.ark/LESSONS.md, re-read fresh.

    Called at the moment each agent step builds its prompt (AC-24, AC-25), so an
    operator editing the file mid-run has the edit picked up at the next step.
    If the file is absent, or empty/whitespace-only, injection is a clean no-op:
    returns "" so the agent step proceeds normally (AC-26, EC-7).
    """
    try:
        content = GLOBAL_LESSONS_FILE.read_text()
    except (FileNotFoundError, OSError):
        return ""
    if not content.strip():
        return ""
    return (
        "\n\n---\n"
        "ARK LESSONS (observations about the ark harness itself from past "
        "runs, with proposed fixes). Keep these in mind; they describe how ark "
        "has hurt prior runs:\n\n"
        f"{content.strip()}\n"
        "--- end ark lessons ---\n"
    )


def write_prompt(project_dir, step_name, content):
    """Write a prompt to a temp file, return its path.

    Every agent step routes its prompt through here, so injecting the global
    lessons file at this single point applies uniformly to all agent steps
    (AC-24, AC-27) — including introspection, which builds its prompt before it
    appends its own lesson (AC-27a). The non-agent verify step never calls this,
    so it is unaffected (AC-28).
    """
    p = Path(project_dir) / ARK_DIR / f"_prompt_{step_name}.md"
    p.write_text(content + lessons_injection())
    return str(p)


# ---------------------------------------------------------------------------
# Pipeline steps — each is a small function
# ---------------------------------------------------------------------------


def step_spec(
        tmux, feature, project_dir):
    """Turn feature description into a spec."""
    print("[step:spec] Writing specification...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_SPEC.format(
        feature_path=kp(project_dir, "FEATURE.md"),
        spec_path=kp(project_dir, "SPEC.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "spec", prompt)
    run_in_tmux(tmux, claude_cmd(pf), sentinel)
    ok = kf_exists(project_dir, "SPEC.md")
    print(f"  -> SPEC.md {'(written)' if ok else '(MISSING!)'}")
    return ok


def step_review_spec(
        tmux, project_dir):
    """Review the spec with fresh context."""
    print("[step:review-spec] Reviewing specification...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_REVIEW_SPEC.format(
        spec_path=kp(project_dir, "SPEC.md"),
        review_path=kp(project_dir, "review-spec.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "review-spec", prompt)
    run_in_tmux(tmux, claude_cmd(pf), sentinel)
    print("  -> review-spec.md")


def step_encode(
        tmux, feature_slug, project_dir):
    """Turn spec into a verification Makefile."""
    print("[step:encode] Writing verification Makefile...")
    mk_name = f"verify-{feature_slug}.mk"
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_ENCODE.format(
        spec_path=kp(project_dir, "SPEC.md"),
        makefile_path=kp(project_dir, mk_name),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "encode", prompt)
    run_in_tmux(tmux, claude_cmd(pf), sentinel)
    ok = kf_exists(project_dir, mk_name)
    print(f"  -> {mk_name} {'(written)' if ok else '(MISSING!)'}")
    return ok


def step_review_make(
        tmux, feature_slug, project_dir):
    """Review the Makefile with fresh context."""
    print("[step:review-make] Reviewing Makefile...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_REVIEW_MAKE.format(
        spec_path=kp(project_dir, "SPEC.md"),
        makefile_path=kp(project_dir, f"verify-{feature_slug}.mk"),
        review_path=kp(project_dir, "review-make.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "review-make", prompt)
    run_in_tmux(tmux, claude_cmd(pf), sentinel)
    print("  -> review-make.md")


def step_implement(
        tmux, project_dir, driver="claude"):
    """Have an agent implement the spec."""
    print(f"[step:implement] Implementing (driver={driver})...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_IMPLEMENT.format(
        spec_path=kp(project_dir, "SPEC.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "implement", prompt)
    run_in_tmux(tmux, driver_cmd(pf, project_dir, driver), sentinel)
    print("  -> implementation complete")


def step_verify(feature_slug, project_dir):
    """Run the verification Makefile. Returns True if all checks pass.

    Security model: the Makefile is LLM-generated and has unfettered shell
    access. This is intentional — the agents already run with full permissions.
    The Makefile is no more dangerous than the implement step itself.
    """
    print("[step:verify] Running verification...")
    mk_path = Path(project_dir) / ARK_DIR / f"verify-{feature_slug}.mk"
    if not mk_path.exists():
        print("  [!] Makefile not found, skipping verify")
        return False
    result = subprocess.run(
        ["make", "-k", "-f", str(mk_path)],
        capture_output=True, text=True,
        cwd=project_dir,
    )
    output = f"exit code: {result.returncode}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    review_path = Path(project_dir) / ARK_DIR / "REVIEW.md"
    review_path.write_text(output)
    passed = result.returncode == 0
    print(f"  -> {'PASS' if passed else 'FAIL'} (exit {result.returncode})")
    if not passed and result.stderr:
        # Show first few lines of errors
        for line in result.stderr.strip().split("\n")[:5]:
            print(f"     {line}")
    return passed


def step_fix_make(
        tmux, feature_slug, project_dir):
    """Fix Makefile issues with fresh context."""
    print("[step:fix-make] Fixing Makefile...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_FIX_MAKE.format(
        makefile_path=kp(project_dir, f"verify-{feature_slug}.mk"),
        review_path=kp(project_dir, "REVIEW.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "fix-make", prompt)
    run_in_tmux(tmux, claude_cmd(pf), sentinel)
    print(f"  -> verify-{feature_slug}.mk updated")


def step_reimplement(
        tmux, project_dir, driver="claude"):
    """Reimplement with spec + review context."""
    print(f"[step:reimplement] Re-implementing (driver={driver})...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_REIMPLEMENT.format(
        spec_path=kp(project_dir, "SPEC.md"),
        review_path=kp(project_dir, "REVIEW.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "reimplement", prompt)
    run_in_tmux(tmux, driver_cmd(pf, project_dir, driver), sentinel)
    print("  -> re-implementation complete")


def step_adversarial(
        tmux, project_dir, iteration=1):
    """Spawn two adversarial reviewers sequentially.

    Returns True if BLOCK or WARN findings were detected, False otherwise.
    Output files are named adversarial-claude-{iteration}.md and
    adversarial-codex-{iteration}.md.
    """
    print(f"[step:adversarial] Adversarial review (iteration {iteration})...")

    claude_name = f"adversarial-claude-{iteration}.md"
    codex_name = f"adversarial-codex-{iteration}.md"

    # Claude review
    sentinel = make_sentinel(project_dir)
    claude_prompt = PROMPT_ADVERSARIAL.format(
        spec_path=kp(project_dir, "SPEC.md"),
        output_path=kp(project_dir, claude_name),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "adversarial-claude", claude_prompt)
    run_in_tmux(tmux, claude_cmd(pf), sentinel)
    print(f"  -> {claude_name}")

    # Codex review (codex exits on its own, so use bash touch for sentinel)
    sentinel = make_sentinel(project_dir)
    codex_prompt = PROMPT_ADVERSARIAL.format(
        spec_path=kp(project_dir, "SPEC.md"),
        output_path=kp(project_dir, codex_name),
    )
    pf = write_prompt(project_dir, "adversarial-codex", codex_prompt)
    codex_cmd_str = (
        f"prompt=$(<{shlex.quote(pf)}) && "
        f"codex exec --full-auto -C {shlex.quote(project_dir)} \"$prompt\" ; "
        f"touch {shlex.quote(sentinel)}"
    )
    run_in_tmux(tmux, codex_cmd_str, sentinel)
    print(f"  -> {codex_name}")

    # Check for BLOCK/WARN findings or missing review files (agent failure)
    for name in [claude_name, codex_name]:
        p = Path(project_dir) / ARK_DIR / name
        if not p.exists():
            print(f"  [!] {name} missing — treating as findings present", file=sys.stderr)
            return True
        content = p.read_text()
        if "BLOCK:" in content or "WARN:" in content:
            return True
    return False


def step_fix_review(
        tmux, project_dir, iteration=1, driver="claude"):
    """Fresh agent addresses adversarial findings from a specific iteration.

    Returns True if the fix agent completed successfully, False on failure.
    """
    print(f"[step:fix-review] Addressing findings from iteration {iteration} (driver={driver})...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_FIX_REVIEW.format(
        spec_path=kp(project_dir, "SPEC.md"),
        claude_review_path=kp(project_dir, f"adversarial-claude-{iteration}.md"),
        codex_review_path=kp(project_dir, f"adversarial-codex-{iteration}.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "fix-review", prompt)
    ok = run_in_tmux(tmux, driver_cmd(pf, project_dir, driver), sentinel)
    if ok:
        print("  -> fix complete")
    else:
        print("  [!] fix agent failed", file=sys.stderr)
    return ok


# ---------------------------------------------------------------------------
# Resume logic
# ---------------------------------------------------------------------------


def detect_resume_point(feature_slug, project_dir):
    """Figure out which step to resume from based on existing artifacts."""
    checks = [
        ("spec", "SPEC.md"),
        ("review-spec", "review-spec.md"),
        ("encode", f"verify-{feature_slug}.mk"),
        ("review-make", "review-make.md"),
    ]
    last_completed = None
    for step_name, filename in checks:
        if kf_exists(project_dir, filename):
            last_completed = step_name
        else:
            break

    if last_completed:
        print(f"  Resuming after: {last_completed}")
    return last_completed


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def archive_run(project_dir, label=None):
    """Move every .ark/ artifact into .ark/archive/<timestamp>/.

    Archiving is terminal: it sweeps the whole run out of .ark/ so the run is no
    longer discoverable or resumable. An archived run is gone — start a fresh run
    if you want to revisit the feature.
    """
    ark_dir = Path(project_dir) / ARK_DIR
    if not ark_dir.exists():
        print("Nothing to archive — no .ark/ directory", file=sys.stderr)
        return

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if label:
        archive_name = f"{ts}-{label}"
    else:
        archive_name = ts

    archive_dir = ark_dir / "archive" / archive_name
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Move all artifacts out of .ark/ (skip the archive directory itself).
    for f in ark_dir.iterdir():
        if f.name == "archive":
            continue
        if not f.is_file():
            continue
        shutil.move(str(f), str(archive_dir / f.name))

    print(f"  Archived to {archive_dir}")
    return archive_dir


# ---------------------------------------------------------------------------
# Introspection — post-hoc reflection on the run that just finished
# ---------------------------------------------------------------------------


def _append_global_lessons(slug, lessons_text):
    """Append a run's lessons to ~/.ark/LESSONS.md (AC-18, AC-19, AC-20, AC-22).

    Creates ~/.ark/ and the file if absent (AC-20). Append-only: existing
    content is preserved (AC-19). The entry is introduced by a header carrying
    the run's slug so it is traceable by a literal search for the slug (AC-22).
    """
    GLOBAL_LESSONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n## {slug} ({ts})\n\n{lessons_text.strip()}\n"
    with open(GLOBAL_LESSONS_FILE, "a") as f:
        f.write(entry)


def step_introspect(tmux, project_dir, archive_dir, slug, outcome):
    """Post-hoc introspection: reflect on the finished run, record lessons.

    Best-effort and never a gate (AC-29): any failure here is swallowed so the
    run's exit status is unchanged. Reads the run's artifacts from the archive
    directory (AC-8) and is told the terminal outcome (AC-9). If the agent
    produces lessons, they are dual-written: appended to ~/.ark/LESSONS.md
    (AC-18) and copied into the archive directory (AC-21). Zero lessons makes no
    modification to either location (AC-17, AC-23).
    """
    print("[step:introspect] Reflecting on the run...")
    # Track the live .ark/ scratch files we create so we can sweep them out
    # afterwards. The archive step already swept the run's artifacts into the
    # archive directory, so introspection must not leave new files behind in the
    # live .ark/ — its only durable writes are the global lessons file and the
    # run-local copy inside the archive directory (AC-30).
    sentinel = make_sentinel(project_dir)
    scratch_path = Path(project_dir) / ARK_DIR / "_introspect_lessons.md"
    prompt_path = Path(project_dir) / ARK_DIR / "_prompt_introspect.md"
    try:
        # The introspection agent writes lessons (if any) to a scratch file in
        # the live .ark/ — NOT directly into the archive — so we control the
        # dual write and the "zero lessons => no file" semantics ourselves.
        if scratch_path.exists():
            scratch_path.unlink()

        prompt = PROMPT_INTROSPECT.format(
            outcome=outcome,
            archive_dir=str(archive_dir),
            output_path=str(scratch_path),
        ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
        # write_prompt injects ~/.ark/LESSONS.md as of now — before we append
        # this run's lesson — so introspection never injects the lesson it is
        # about to write (AC-27, AC-27a).
        pf = write_prompt(project_dir, "introspect", prompt)
        run_in_tmux(tmux, claude_cmd(pf), sentinel)

        # Zero lessons is valid: no file (or empty file) => no writes anywhere.
        if not scratch_path.exists():
            print("  -> no lessons recorded")
            return
        lessons_text = scratch_path.read_text()
        if not lessons_text.strip():
            print("  -> no lessons recorded")
            return

        # Dual write: global append (AC-18) + run-local copy in the archive
        # directory (AC-21).
        _append_global_lessons(slug, lessons_text)
        run_local = Path(archive_dir) / RUN_LESSONS_FILENAME
        run_local.write_text(lessons_text.strip() + "\n")
        print(f"  -> lessons appended to {GLOBAL_LESSONS_FILE}")
        print(f"  -> run-local copy at {run_local}")
    except Exception as e:  # best-effort: never let introspection break the run
        print(f"  [!] introspection failed (ignored): {e}", file=sys.stderr)
    finally:
        # Sweep introspection's own scratch/prompt/sentinel files out of the
        # live .ark/ so a terminal run leaves no leftover artifacts there (AC-30).
        for leftover in (scratch_path, prompt_path, Path(sentinel)):
            try:
                leftover.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def _attach_target_is_tty():
    """Interactivity is decided by the attach-target tty, NOT by stdin.

    The attach target ark would hand to `tmux attach` is the controlling
    terminal on stdout/stderr. A run is interactive iff one of those is a tty;
    a piped/redirected stdin (the normal way the feature text is supplied) does
    NOT make a run headless (AC-20b, FR-7).
    """
    return sys.stdout.isatty() or sys.stderr.isatty()


def _launch_inner_orchestrator(tmux, project_dir, feature_slug, driver):
    """Launch the orchestrator's driver loop INSIDE the status pane (pane 1).

    Re-execs this same ark.py with ARK_INNER set so the driver loop and its
    [step:...] progress run in pane 1 (FR-3), driving the agent in pane 0. The
    feature, root, slug, and session are reconstructed by the inner process from
    its environment and the worktree's .ark/ artifacts. ARK_RUNNING is NOT
    exported here: the inner orchestrator is legitimate and sets the guard
    itself, so only the agents it spawns are blocked from recursing.
    """
    ark_py = os.path.abspath(__file__)
    env_prefix = (
        f"ARK_INNER=1 "
        f"ARK_SESSION={shlex.quote(tmux.session)} "
        f"ARK_WORKDIR={shlex.quote(project_dir)} "
        f"ARK_SLUG={shlex.quote(feature_slug)} "
        f"ARK_DRIVER={shlex.quote(driver)}"
    )
    cmd = f"{env_prefix} {shlex.quote(sys.executable)} {shlex.quote(ark_py)} _inner"
    tmux.send_command(cmd, pane=STATUS_PANE)


def run_pipeline(feature, invocation_dir, driver="claude"):
    """Set up the run's topology, then drive (or hand off) the pipeline.

    This is the OUTER entry invoked by `ark new`/`ark continue`. It performs the
    one-time setup (repo check, worktree, session), then:

      - interactive (an attach-target tty exists): launches the orchestrator's
        driver loop inside the status pane (pane 1) and attaches the user's
        terminal to the two-pane session, agent pane primary (FR-5). Detaching
        leaves that in-tmux orchestrator running (FR-6).
      - headless (no attach-target tty): builds the same two-pane session but
        drives the pipeline in THIS process without attaching (FR-7, AC-20a).

    Exactly one orchestrator driver loop runs per run either way (AC-9).

    invocation_dir is where the user ran ark (inside the repo-root working tree).
    All file-modifying work happens in a per-run worktree, so artifacts, agent
    commands, and verification all use the worktree as their working directory —
    the repository-root working tree is never disturbed.
    """
    if os.environ.get("ARK_RUNNING"):
        print("Error: recursive ark invocation blocked", file=sys.stderr)
        sys.exit(1)

    invocation_dir = os.path.abspath(invocation_dir)

    # EC-1: must be inside a git repository before we touch anything (AC-30).
    try:
        root = repo_root(invocation_dir)
    except ArkError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    feature_slug = slugify(feature)
    session_name = f"ark-{feature_slug}"

    # EC-2..EC-6: create or reuse the run's dedicated worktree.
    try:
        work_dir = setup_worktree(feature_slug, root)
    except ArkError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # From here on, project_dir == the worktree. Artifacts, agents, and make all
    # operate against the worktree (AC-5, AC-5b, AC-9).
    project_dir = work_dir
    korc_dir = ensure_dir(project_dir)

    print(f"ark: {feature_slug}")
    print(f"  repo root: {root}")
    print(f"  worktree:  {project_dir}")
    print(f"  artifacts: {korc_dir}")
    print(f"  branch:    ark/{feature_slug}")
    print(f"  tmux:      {session_name}")
    print(f"  driver:    {driver}")
    if SKIP_PERMISSIONS:
        print("  WARNING: agents run with --dangerously-skip-permissions")
        print("           set ARK_SKIP_PERMISSIONS=0 to disable")
    print()

    # Save driver choice for resume (AC-25)
    driver_file = Path(project_dir) / ARK_DIR / "DRIVER"
    if not driver_file.exists():
        driver_file.write_text(driver)
    else:
        driver = driver_file.read_text().strip()

    # Save feature description
    feature_file = Path(project_dir) / ARK_DIR / "FEATURE.md"
    if not feature_file.exists():
        feature_file.write_text(feature)

    # Check resume point (based on artifacts in this worktree's .ark/) — AC-18.
    resume_after = detect_resume_point(feature_slug, project_dir)

    # Session name is a deterministic function of the slug (AC-27), so
    # concurrent runs with different slugs neither share nor kill each other's
    # session. If a session for THIS slug is already alive, a run for this slug
    # is in flight: don't destroy its agent state — refuse rather than clobber.
    tmux = Tmux(session_name)
    already_driving = False
    if tmux.is_alive():
        # A live session is the NORMAL state once a run has started: each step
        # launches the agent in pane 0's shell and ends it with "/exit", which
        # quits the agent REPL but leaves the pane shell running. So the session
        # stays alive from creation until something kills it.
        #
        # Distinguish the two reasons it can be alive here using the resume
        # point (derived from on-disk artifacts):
        #   1. No resume point -> no step has produced an artifact yet, so a
        #      run is genuinely in flight from the start (e.g. a second `ark
        #      new` racing the first). Refuse, to avoid clobbering its agent
        #      (AC-28).
        #   2. A resume point exists -> the previous step completed (artifact on
        #      disk) and the session is just sitting at the pane shell (or an
        #      interactive agent someone left open). Reuse its two-pane layout
        #      (AC-29): drop any lingering REPL in the agent pane back to its
        #      shell, then proceed with a fresh agent for the next step.
        if resume_after is None:
            print(
                f"Error: a run for this feature is already in flight "
                f"(tmux session '{session_name}' is alive).\n"
                f"  Attach with: tmux attach -t {session_name}\n"
                f"  Or kill it first: tmux kill-session -t {session_name}",
                file=sys.stderr,
            )
            return 1
        # If an orchestrator is already driving pane 1 (a detached-from
        # interactive run), this is a pure re-attach: don't disturb the agent
        # pane and don't launch a second driver (AC-9, AC-18).
        already_driving = tmux.has_two_panes() and tmux.status_pane_busy()
        if already_driving:
            print(
                f"  Re-attaching to live session '{session_name}' — an "
                f"orchestrator is already driving in the status pane."
            )
        else:
            print(
                f"  Reusing live session '{session_name}' (idle after "
                f"'{resume_after}'); clearing any open agent."
            )
            # No-op if the agent pane is already at a shell prompt ("/exit" is
            # just an unknown command); ends the REPL if an interactive agent was
            # left open. Targets pane 0 so anything in pane 1 stays untouched.
            tmux.exit_agent_repl()
            time.sleep(2)
            # Defensive: a session reused from an older single-pane ark, or one
            # whose split was lost, may lack the status pane. Rebuild it cleanly.
            if not tmux.is_alive():
                try:
                    tmux.create_session(project_dir)
                except TmuxError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    return 1
            elif not tmux.has_two_panes():
                tmux.kill_session()
                try:
                    tmux.create_session(project_dir)
                except TmuxError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    return 1
    else:
        # Reap any dead/leftover registration for this name, then create fresh.
        tmux.kill_session()
        try:
            tmux.create_session(project_dir)
        except TmuxError as e:
            # EC-9 / AC-42: tmux unavailable or session creation failed — fail
            # loudly rather than driving a pipeline against a session that does
            # not exist.
            print(f"Error: {e}", file=sys.stderr)
            return 1

    print(f"  Attach with: tmux attach -t {session_name}\n")

    interactive = _attach_target_is_tty()

    if interactive:
        # FR-5/FR-6: the single orchestrator driver loop runs INSIDE the status
        # pane (pane 1). Launch it there (unless one is already driving this
        # reused session), then attach the user to the two-pane layout with the
        # agent pane primary (AC-14). Detaching leaves the in-tmux orchestrator
        # running (FR-6); this outer process just exits.
        if not already_driving:
            _launch_inner_orchestrator(tmux, project_dir, feature_slug, driver)
        tmux.attach()
        print(f"\n  Detached. The run continues in tmux session "
              f"'{session_name}'.")
        print(f"  Re-attach: ark continue {feature_slug}  "
              f"(or tmux attach -t {session_name})")
        print(f"  Branch ark/{feature_slug} will be ready to merge when the run "
              f"finishes.")
        return 0

    # Headless (AC-20/AC-20a/AC-21): no attach-target tty.
    if already_driving:
        # An interactive orchestrator the user detached from is already driving
        # this run inside pane 1. A headless `ark continue` must not start a
        # second driver (AC-9) — report the live run and let it finish (AC-22).
        print(f"  A run for '{feature_slug}' is already being driven in tmux "
              f"session '{session_name}'; leaving it to finish.")
        print(f"  Inspect: tmux attach -t {session_name}")
        print(f"  Branch ark/{feature_slug} will be ready to merge when it "
              f"finishes.")
        return 0

    # The two-pane session is already built; drive the pipeline in THIS process
    # without ever attaching. This is the single orchestrator for a headless run.
    return _drive_pipeline(
        tmux, feature, root, project_dir, feature_slug, session_name,
        driver, resume_after,
    )


def _drive_pipeline(tmux, feature, root, project_dir, feature_slug,
                    session_name, driver, resume_after):
    """The orchestrator driver loop: sequence steps, drive pane 0, poll sentinels.

    Runs either inside the status pane (interactive, launched by
    `_launch_inner_orchestrator`) or in the outer process (headless). Either way
    this is the ONE driver loop for the run (AC-9). The two-pane session already
    exists; this function only drives it.
    """
    # Block agents this orchestrator spawns from recursively invoking ark. Set
    # here (not in the outer setup) so it covers both the in-tmux inner process
    # and the headless in-process driver.
    os.environ["ARK_RUNNING"] = "1"

    # Phase 1: Spec
    if resume_after is None:
        if not step_spec(tmux, feature, project_dir):
            print("  [!] Spec step failed to produce SPEC.md")
            return 1
        resume_after = "spec"

    if resume_after == "spec":
        step_review_spec(tmux, project_dir)
        resume_after = "review-spec"

    # Phase 2: Encode verification
    if resume_after == "review-spec":
        if not step_encode(tmux, feature_slug, project_dir):
            print("  [!] Encode step failed to produce Makefile")
            return 1
        resume_after = "encode"

    if resume_after == "encode":
        step_review_make(tmux, feature_slug, project_dir)
        resume_after = "review-make"

    # Phase 3: Implement + verify loop
    passed = False
    for attempt in range(1, MAX_LOOPS + 1):
        print(f"\n--- Attempt {attempt}/{MAX_LOOPS} ---\n")

        if attempt == 1:
            step_implement(tmux, project_dir, driver)
        else:
            step_reimplement(tmux, project_dir, driver)

        passed = step_verify(feature_slug, project_dir)
        if passed:
            print("\n  All checks passed!")
            break

        step_fix_make(tmux, feature_slug, project_dir)

    if not passed:
        print(f"\n  FAILED after {MAX_LOOPS} attempts.")
        archive_dir = archive_run(project_dir, feature_slug)
        if archive_dir is not None:
            step_introspect(
                tmux, project_dir, archive_dir, feature_slug,
                outcome="exhausted the implement/verify loop "
                        "(implementation never passed verification)",
            )
        return 1

    # Phase 4: Review-fix loop
    review_pass = False

    if MAX_REVIEW_LOOPS == 0:
        # EC-4: Run review once, report findings, but never spawn a fix agent.
        has_findings = step_adversarial(tmux, project_dir, iteration=1)
        review_pass = not has_findings
    else:
        for iteration in range(1, MAX_REVIEW_LOOPS + 1):
            print(f"\n--- Review-fix iteration {iteration}/{MAX_REVIEW_LOOPS} ---\n")

            has_findings = step_adversarial(tmux, project_dir, iteration=iteration)
            if not has_findings:
                review_pass = True
                print("\n  No findings — review passed!")
                break

            if iteration == MAX_REVIEW_LOOPS:
                break

            ok = step_fix_review(tmux, project_dir, iteration=iteration, driver=driver)
            if not ok:
                print("\n  Fix agent failed — aborting review loop.", file=sys.stderr)
                break

    if not review_pass:
        print(f"\n  Review-fix loop exhausted after {MAX_REVIEW_LOOPS} iterations")
        archive_dir = archive_run(project_dir, feature_slug)
        if archive_dir is not None:
            step_introspect(
                tmux, project_dir, archive_dir, feature_slug,
                outcome="exhausted the review-fix loop "
                        "(adversarial findings remained after the allowed "
                        "iterations)",
            )
        return 1

    # Archive results
    archive_dir = archive_run(project_dir, feature_slug)
    if archive_dir is not None:
        step_introspect(
            tmux, project_dir, archive_dir, feature_slug,
            outcome="succeeded (the implementation passed verification and "
                    "adversarial review)",
        )

    # FR-6: results are discoverable from the repository root via the branch.
    print(f"\n  Done. Branch ark/{feature_slug} is ready to merge.")
    print(f"  Worktree: {project_dir}")
    print(f"  From the repo root ({root}):")
    print(f"    git merge ark/{feature_slug}")
    print(f"  Inspect the run: tmux attach -t {session_name}")
    return 0


def _run_inner_from_env():
    """Reconstruct run state from ARK_* env vars and drive the pipeline.

    Invoked as `ark _inner` inside the status pane (pane 1). The outer process
    already created the two-pane session and the worktree; here we only rebuild
    the small amount of state the driver loop needs and hand off to
    `_drive_pipeline`. The feature text and resume point come from the worktree's
    own .ark/ artifacts, so nothing fragile is passed on the command line.
    """
    session_name = os.environ.get("ARK_SESSION")
    project_dir = os.environ.get("ARK_WORKDIR")
    feature_slug = os.environ.get("ARK_SLUG")
    driver = os.environ.get("ARK_DRIVER", "claude")
    if not (session_name and project_dir and feature_slug):
        print("Error: ark _inner is internal and requires ARK_SESSION, "
              "ARK_WORKDIR, and ARK_SLUG", file=sys.stderr)
        return 1

    feature_file = Path(project_dir) / ARK_DIR / "FEATURE.md"
    if not feature_file.exists():
        print(f"Error: ark _inner: no FEATURE.md in {project_dir}",
              file=sys.stderr)
        return 1
    feature = feature_file.read_text().strip()

    try:
        root = repo_root(project_dir)
    except ArkError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Re-derive the resume point from on-disk artifacts so a launch into a
    # partially complete run picks up where it left off (AC-18).
    resume_after = detect_resume_point(feature_slug, project_dir)

    tmux = Tmux(session_name)
    return _drive_pipeline(
        tmux, feature, root, project_dir, feature_slug, session_name,
        driver, resume_after,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def print_help(file=sys.stdout):
    """Print the help message to the given file object."""
    msg = """\
ark — a dumb Python orchestrator for LLM agents.

Usage:
  ark new 'Add auth' [--driver claude|codex]
  echo 'Add auth' | ark new [--driver claude|codex]
  ark continue [slug]
  ark archive [label]
  ark help"""
    print(msg, file=file)


def main():
    if len(sys.argv) < 2:
        print_help(file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd in ("help", "-h", "--help"):
        print_help(file=sys.stdout)
        sys.exit(0)

    if cmd == "_inner":
        # Internal entry: the orchestrator driver loop, launched by the outer
        # process inside the status pane (pane 1) for interactive runs (FR-3).
        # Not a user-facing command. All state is passed via the ARK_* env vars
        # set by _launch_inner_orchestrator; the two-pane session already exists.
        sys.exit(_run_inner_from_env())

    if cmd == "archive":
        label = sys.argv[2] if len(sys.argv) > 2 else None
        invocation_dir = os.getcwd()

        # Locate the run worktree so archiving operates on the correct .ark/
        # (AC-22). If ark is run from inside a worktree itself, that worktree's
        # local .ark/ is used directly.
        if (Path(invocation_dir) / ARK_DIR).exists():
            archive_run(invocation_dir, label)
            return
        try:
            root = repo_root(invocation_dir)
            runs = find_ark_worktrees(root)
        except ArkError:
            runs = []
        if not runs:
            print("Nothing to archive — no .ark/ directory", file=sys.stderr)
            return
        if len(runs) > 1:
            print("Multiple ark runs found; cd into the worktree you want to "
                  "archive:", file=sys.stderr)
            for slug, path in runs:
                print(f"  {slug}  ({path})", file=sys.stderr)
            return
        archive_run(runs[0][1], label)
        return

    if cmd == "continue":
        invocation_dir = os.getcwd()
        try:
            root = repo_root(invocation_dir)
        except ArkError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        # Optional slug selects a specific run when several are in flight — the
        # normal case under the parallel-run model, where bare `continue` can't
        # know which worktree the user means.
        want_slug = sys.argv[2] if len(sys.argv) > 2 else None

        # Discover the run worktree(s) created by ark.
        runs = find_ark_worktrees(root)
        if not runs:
            print("Error: no in-progress ark run found — nothing to continue",
                  file=sys.stderr)
            sys.exit(1)
        if want_slug is not None:
            match = [(s, p) for s, p in runs if s == want_slug]
            if not match:
                print(f"Error: no in-progress ark run for slug '{want_slug}'.",
                      file=sys.stderr)
                for slug, path in runs:
                    print(f"  {slug}  ({path})", file=sys.stderr)
                sys.exit(1)
            run_dir = match[0][1]
        elif len(runs) > 1:
            print("Error: multiple in-progress ark runs found; "
                  "pick one with `ark continue <slug>`:",
                  file=sys.stderr)
            for slug, path in runs:
                print(f"  {slug}  ({path})", file=sys.stderr)
            sys.exit(1)
        else:
            run_dir = runs[0][1]

        feature = (Path(run_dir) / ARK_DIR / "FEATURE.md").read_text().strip()
        driver_file = Path(run_dir) / ARK_DIR / "DRIVER"
        driver = driver_file.read_text().strip() if driver_file.exists() else "claude"
        # run_pipeline re-derives the slug from the feature and reuses the
        # existing worktree/branch (AC-16, AC-17).
        sys.exit(run_pipeline(feature, invocation_dir, driver=driver))

    if cmd == "new":
        # Parse --driver flag and optional positional argument
        driver = "claude"
        positional = []
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--driver":
                if i + 1 >= len(args):
                    print("Error: --driver requires a value (claude or codex)", file=sys.stderr)
                    sys.exit(1)
                driver = args[i + 1]
                if driver not in ("claude", "codex"):
                    print(f"Error: unsupported driver: {driver!r} (must be claude or codex)", file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif args[i].startswith("-"):
                print(f"Error: unknown flag: {args[i]}", file=sys.stderr)
                sys.exit(1)
            else:
                positional.append(args[i])
                i += 1

        if len(positional) > 1:
            print("Error: too many arguments — expected at most one feature description", file=sys.stderr)
            sys.exit(1)

        if positional:
            feature = positional[0]
        elif sys.stdin.isatty():
            print("Error: pipe a feature description to stdin or pass it as an argument", file=sys.stderr)
            print("  ark new 'Add auth'", file=sys.stderr)
            print("  echo 'Add auth' | ark new", file=sys.stderr)
            sys.exit(1)
        else:
            feature = sys.stdin.read().strip()

        if not feature or not feature.strip():
            print("Error: empty feature description", file=sys.stderr)
            sys.exit(1)

        project_dir = os.getcwd()
        sys.exit(run_pipeline(feature, project_dir, driver=driver))

    print(f"Unknown command: {cmd}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
