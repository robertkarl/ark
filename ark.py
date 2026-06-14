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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MODEL_RAW = os.environ.get("ARK_MODEL", "opus")
_MODEL_VALIDATED = False
MODEL = _MODEL_RAW
MAX_LOOPS = 3
MAX_REVIEW_LOOPS = 3
ARK_DIR = ".ark"

# Default values for the legacy / fallback wall-clock cap. Preserved at 1800s so
# a no-probe caller of wait_for_sentinel behaves exactly as it did before the
# progress-aware fix (FR-5, AC-16).
DEFAULT_WALLCLOCK_TIMEOUT = 1800

# Documented defaults for the new progress-aware tunables (FR-10).
DEFAULT_IDLE_TIMEOUT = 900       # 15 minutes
DEFAULT_STEP_TIMEOUT = 86400     # 24 hours

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


def _read_positive_int_env(name, default):
    """Read a positive-integer env var, failing fast on a bad value (EC-6).

    Mirrors the fail-fast posture of _validate_model: a misconfigured timeout
    must NOT silently degrade into an instant (zero) timeout that kills a live
    job, and must NOT silently fall back to a default (silent fallback hides
    operator typos). A non-numeric or non-positive value aborts with a clear
    message naming the offending variable and its value.

    Returns the parsed int when the variable is unset (the documented default)
    or set to a valid positive integer.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError):
        raise ArkError(
            f"{name} must be a positive integer (seconds), got {raw!r}.\n"
            f"  Unset it to use the default ({default}) or set a positive integer."
        )
    if value <= 0:
        raise ArkError(
            f"{name} must be a positive integer (seconds), got {raw!r}.\n"
            f"  A zero/negative timeout would instantly kill live jobs. "
            f"Unset it to use the default ({default})."
        )
    return value


def idle_timeout():
    """Resolve ARK_IDLE_TIMEOUT (FR-10, EC-6)."""
    return _read_positive_int_env("ARK_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT)


def step_timeout():
    """Resolve ARK_STEP_TIMEOUT (FR-10, EC-6)."""
    return _read_positive_int_env("ARK_STEP_TIMEOUT", DEFAULT_STEP_TIMEOUT)


# ---------------------------------------------------------------------------
# Step outcomes — the waiting layer reports a distinct outcome (FR-6, FR-6a)
# ---------------------------------------------------------------------------


class _Outcome:
    """A single waiting outcome (FR-6a).

    Each outcome is a distinct singleton, so callers can compare identity/equality
    (`outcome == StepOutcome.IDLE_TIMEOUT`) without parsing stderr. `__bool__` is
    True ONLY for the success outcome (FINISHED), so legacy `if ok:` callsites
    keep working: a finished step is truthy, every timeout/death is falsy, and
    the four cases remain individually distinguishable (FR-6a, AC-6).
    """

    __slots__ = ("name", "_success")

    def __init__(self, name, success):
        self.name = name
        self._success = success

    def __bool__(self):
        return self._success

    def __repr__(self):
        return self.name

    def __str__(self):
        return self.name


class StepOutcome:
    """The four distinct outcomes the waiting layer can report (FR-6a).

    FINISHED is the ONLY success outcome (sentinel observed) and the only truthy
    one. IDLE_TIMEOUT / HARD_TIMEOUT / SESSION_DIED are all falsy yet remain
    individually distinguishable by an explicit `==` check.
    """

    FINISHED = _Outcome("FINISHED", True)
    IDLE_TIMEOUT = _Outcome("IDLE_TIMEOUT", False)
    HARD_TIMEOUT = _Outcome("HARD_TIMEOUT", False)
    SESSION_DIED = _Outcome("SESSION_DIED", False)

    #: Every non-success outcome. Reaching any of these means the step did NOT
    #: finish (no sentinel) and MUST NOT advance to verify (FR-7).
    FAILURES = (IDLE_TIMEOUT, HARD_TIMEOUT, SESSION_DIED)

    #: Timeout-flavored outcomes (idle or hard ceiling) — reported as an
    #: explicit timeout, distinct from a completed-but-failed verification.
    TIMEOUTS = (IDLE_TIMEOUT, HARD_TIMEOUT)


def _finished(outcome):
    """True iff the waiting layer reported the sole success outcome (FINISHED)."""
    return outcome is StepOutcome.FINISHED


def probe_paths(paths):
    """Snapshot (size, mtime) of every watched path for progress detection.

    `paths` is a list of files/directories. A directory contributes the
    (size, mtime) of every file beneath it (recursively), so a directory is
    "advancing" when it, or any file within it, changes (AC-4). A path that does
    not exist contributes nothing; its later creation appears as a brand-new key
    in the snapshot and therefore registers as progress (EC-3).

    Returns a dict {path_string: (size, mtime)}. Comparing two snapshots for
    inequality answers "did anything advance?" — a changed tuple, a new key, or
    a removed key all count as progress (a larger OR smaller size, any mtime
    inequality). The probe walks the tree once per poll and never blocks
    indefinitely; unreadable entries are skipped rather than raised (EC-5).
    """
    snap = {}
    for p in paths:
        path = Path(p)
        try:
            if path.is_dir():
                # os.walk is iterative and returns promptly even for deep trees.
                for dirpath, _dirnames, filenames in os.walk(path):
                    try:
                        dst = os.stat(dirpath)
                        snap[dirpath] = (dst.st_size, dst.st_mtime)
                    except OSError:
                        pass
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        try:
                            st = os.stat(fp)
                            snap[fp] = (st.st_size, st.st_mtime)
                        except OSError:
                            # File vanished mid-walk (e.g. a temp file). Skip.
                            pass
            elif path.exists():
                st = path.stat()
                snap[str(path)] = (st.st_size, st.st_mtime)
            # Non-existent path: contributes nothing now; creation later shows
            # up as a new key and registers as progress (EC-3).
        except OSError:
            # Permission or transient error on this path — treat as no data.
            pass
    return snap


def make_progress_probe(probe):
    """Normalize a progress probe argument into a zero-arg callable.

    `probe` may be:
      - None  -> returns None (no probe; fallback wall-clock semantics, FR-5).
      - a list/tuple of filesystem paths -> snapshot those paths each call.
      - a callable -> used as-is (must return a comparable progress value).

    The returned callable is invoked once per poll; its return value is compared
    (with !=) against the previous poll's value to decide whether progress
    occurred.
    """
    if probe is None:
        return None
    if callable(probe):
        return probe
    if isinstance(probe, (list, tuple)):
        watched = list(probe)
        return lambda: probe_paths(watched)
    raise TypeError(
        f"progress probe must be None, a list of paths, or a callable, "
        f"got {type(probe).__name__}"
    )


# ---------------------------------------------------------------------------
# Tmux helpers — ark runs in your terminal, tmux is the worker
# ---------------------------------------------------------------------------


class Tmux:
    """Manage a single tmux session with one window for the worker agent."""

    def __init__(self, session_name):
        self.session = session_name

    def create_session(self, working_dir):
        """Create a detached tmux session."""
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.session, "-c", working_dir],
            check=True,
        )

    def send_command(self, cmd):
        """Send a command string to the worker window via a temp script.

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
            ["tmux", "send-keys", "-t", self.session, f"bash {script_path}", "Enter"],
            check=True,
        )

    def pane_pid(self):
        """Get the shell PID of the worker pane."""
        result = subprocess.run(
            ["tmux", "list-panes", "-t", self.session, "-F", "#{pane_pid}"],
            capture_output=True, text=True,
        )
        return result.stdout.strip().split("\n")[0] if result.stdout.strip() else None

    def wait_for_sentinel(
            self, sentinel_path, poll_interval=5,
            timeout=DEFAULT_WALLCLOCK_TIMEOUT, progress_probe=None,
            idle_timeout=None, hard_timeout=None,
            clock=None, sleep=None):
        """Wait for a sentinel file, returning a StepOutcome (FR-6a).

        Two modes:

        * **No progress probe (legacy/fallback, FR-5):** behaves exactly as the
          pre-fix code — a single wall-clock cap at `timeout` (default 1800s).
          Returns FINISHED on sentinel, SESSION_DIED if the session dies, or
          IDLE_TIMEOUT once `timeout` elapses. The big ARK_STEP_TIMEOUT ceiling
          does NOT apply here, so a hung no-probe caller stays as responsive as
          before (AC-16).

        * **Progress probe supplied (FR-1, FR-2, FR-3):** the step is alive as
          long as a watched path keeps advancing. It times out only when no
          progress has been seen for >= `idle_timeout` seconds (IDLE_TIMEOUT),
          or when total elapsed reaches the hard `hard_timeout` ceiling
          (HARD_TIMEOUT) regardless of progress. The plain `timeout` argument is
          ignored in this mode.

        The sentinel always wins: if it is present on a poll it returns FINISHED
        and removes the file, even in the same poll that progress is observed
        (EC-2, AC-3). `clock`/`sleep` are injectable for deterministic tests
        (EC-4); they default to a monotonic clock and time.sleep.

        All timeout comparisons use `>=` and are evaluated at each poll, so
        detection latency is bounded by timeout + poll_interval (AC-5, AC-19).
        """
        clock = clock or time.monotonic
        sleep = sleep or time.sleep
        probe = make_progress_probe(progress_probe)

        start = clock()
        # The reference instant for the idle span: the later of (wait start, last
        # observed progress). Starts at `start`, so a job that never writes
        # accrues idle time from launch (AC-22).
        last_progress = start
        prev = probe() if probe is not None else None

        if idle_timeout is None:
            idle_timeout = timeout
        if hard_timeout is None:
            hard_timeout = timeout

        while True:
            # Sentinel always wins (EC-2, AC-3) — checked first, every poll.
            if os.path.exists(sentinel_path):
                os.unlink(sentinel_path)
                return StepOutcome.FINISHED

            if not self.is_alive():
                print("  [!] tmux session died", file=sys.stderr)
                return StepOutcome.SESSION_DIED

            now = clock()

            if probe is None:
                # Fallback wall-clock semantics (FR-5, AC-16). Guard against a
                # backward clock with max(0, ...) (EC-4).
                elapsed = max(0, now - start)
                if elapsed >= timeout:
                    print(
                        f"  [!] Timeout after {timeout}s waiting for agent",
                        file=sys.stderr,
                    )
                    return StepOutcome.IDLE_TIMEOUT
            else:
                # Progress-aware semantics. Re-snapshot and compare.
                cur = probe()
                if cur != prev:
                    # Progress observed: reset the idle timer to zero (AC-8).
                    last_progress = now
                    prev = cur

                # max(0, ...) tolerates a non-advancing / backward clock (EC-4).
                idle_elapsed = max(0, now - last_progress)
                total_elapsed = max(0, now - start)

                # Hard ceiling governs absolutely, even past idle config and
                # even while progress continues (FR-3, EC-7, AC-7).
                if total_elapsed >= hard_timeout:
                    print(
                        f"  [!] Hard ceiling reached after {hard_timeout}s "
                        f"(ARK_STEP_TIMEOUT) waiting for agent",
                        file=sys.stderr,
                    )
                    return StepOutcome.HARD_TIMEOUT

                if idle_elapsed >= idle_timeout:
                    print(
                        f"  [!] Idle for {idle_timeout}s with no progress "
                        f"(ARK_IDLE_TIMEOUT) — agent appears hung",
                        file=sys.stderr,
                    )
                    return StepOutcome.IDLE_TIMEOUT

            sleep(poll_interval)

    def is_alive(self):
        """Check if the tmux session still exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session],
            capture_output=True,
        )
        return result.returncode == 0

    def kill_session(self):
        subprocess.run(
            ["tmux", "kill-session", "-t", self.session],
            capture_output=True,
        )


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


def default_watch_set(project_dir):
    """Default progress-watch set for a real pipeline step (FR-4, AC-17).

    Watches the run's worktree output area so any agent writing output counts as
    alive: the worktree's `results/` directory specifically (the incident
    scenario appended to results/runs.jsonl) and the worktree directory
    generally. Both are returned even if `results/` does not yet exist — its
    later creation registers as progress (EC-3).

    The worktree-general path lets ANY write under the worktree (a commit, a new
    source file, a log) count as progress, so each step_* caller does not have
    to opt in individually (AC-17).
    """
    return [
        os.path.join(project_dir, "results"),
        project_dir,
    ]


def run_in_tmux(
        tmux, cmd, sentinel, timeout=DEFAULT_WALLCLOCK_TIMEOUT,
        progress_probe=None):
    """Send a command to the tmux worker and wait for the agent to signal done.

    The agent is instructed (via PROMPT_SENTINEL in its prompt) to create the
    sentinel file when finished. If the tmux session dies, recreates and retries.

    Returns a StepOutcome (FR-6a, FR-7d) — FINISHED only when the sentinel was
    observed; otherwise IDLE_TIMEOUT / HARD_TIMEOUT / SESSION_DIED. The outcome
    is threaded back so callers can gate verify on FINISHED (FR-7d). A timeout is
    NOT a finished step and NOT a successful one.

    When `progress_probe` is supplied (a list of watched paths or a callable),
    the wait is progress-aware: it survives indefinitely while output advances,
    bounded only by the idle timeout and the hard ceiling (FR-1..FR-4). With no
    probe, the legacy single wall-clock cap at `timeout` applies (FR-5).

    The session's working directory is the run's worktree: the sentinel lives in
    <worktree>/.ark/, so the worktree is sentinel.parent.parent (AC-5).
    """
    if progress_probe is not None:
        wait_kwargs = dict(
            progress_probe=progress_probe,
            idle_timeout=idle_timeout(),
            hard_timeout=step_timeout(),
        )
    else:
        wait_kwargs = dict(timeout=timeout)

    work_dir = str(Path(sentinel).parent.parent)
    if not tmux.is_alive():
        tmux.create_session(work_dir)
    tmux.send_command(cmd)
    outcome = tmux.wait_for_sentinel(sentinel, **wait_kwargs)
    if _finished(outcome):
        # Kill the interactive claude session so the pane is ready for the next step
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux.session, "/exit", "Enter"],
            capture_output=True,
        )
        time.sleep(2)
    # EC-1: preserve the existing session-death retry/recreate path. Only a
    # genuine session death (not an idle/hard timeout, which leave artifacts
    # intact — FR-9/EC-8) triggers a recreate-and-retry.
    if not _finished(outcome) and not tmux.is_alive():
        print("  [!] Retrying after tmux session loss", file=sys.stderr)
        tmux.create_session(work_dir)
        tmux.send_command(cmd)
        outcome = tmux.wait_for_sentinel(sentinel, **wait_kwargs)
        if _finished(outcome):
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux.session, "/exit", "Enter"],
                capture_output=True,
            )
            time.sleep(2)
    elif not _finished(outcome) and tmux.is_alive():
        # Idle/hard timeout: the session is still alive with the original (hung)
        # interactive agent at its prompt. Kill the stale pane so that any
        # subsequent retry attempt (run_pipeline loops back into run_in_tmux)
        # creates a fresh session via the `not is_alive()` branch above, instead
        # of stacking a second agent command onto the still-live hung pane.
        # Tearing down the tmux pane does NOT truncate or delete the in-flight
        # job's output artifacts, which live on the worktree filesystem
        # (FR-9/EC-8) — it only frees the interactive session for a clean retry.
        print(
            "  [!] Killing stale tmux pane after non-finishing outcome "
            f"({outcome}) so the next attempt starts clean",
            file=sys.stderr,
        )
        tmux.kill_session()
    return outcome


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


def write_prompt(project_dir, step_name, content):
    """Write a prompt to a temp file, return its path."""
    p = Path(project_dir) / ARK_DIR / f"_prompt_{step_name}.md"
    p.write_text(content)
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
    """Have an agent implement the spec.

    Returns a StepOutcome (FR-7d). The wait is progress-aware over the worktree
    output area (FR-4/AC-17): an agent that keeps writing output is never killed,
    and a timeout outcome is threaded back so run_pipeline can refuse to verify
    partial state.
    """
    print(f"[step:implement] Implementing (driver={driver})...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_IMPLEMENT.format(
        spec_path=kp(project_dir, "SPEC.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "implement", prompt)
    outcome = run_in_tmux(
        tmux, driver_cmd(pf, project_dir, driver), sentinel,
        progress_probe=default_watch_set(project_dir),
    )
    if _finished(outcome):
        print("  -> implementation complete")
    else:
        print(f"  -> implementation did not finish ({outcome})", file=sys.stderr)
    return outcome


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
    """Reimplement with spec + review context.

    Returns a StepOutcome (FR-7d) — see step_implement. The wait is
    progress-aware over the worktree output area.
    """
    print(f"[step:reimplement] Re-implementing (driver={driver})...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_REIMPLEMENT.format(
        spec_path=kp(project_dir, "SPEC.md"),
        review_path=kp(project_dir, "REVIEW.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "reimplement", prompt)
    outcome = run_in_tmux(
        tmux, driver_cmd(pf, project_dir, driver), sentinel,
        progress_probe=default_watch_set(project_dir),
    )
    if _finished(outcome):
        print("  -> re-implementation complete")
    else:
        print(f"  -> re-implementation did not finish ({outcome})", file=sys.stderr)
    return outcome


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
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(feature, invocation_dir, driver="claude"):
    """Execute the full ark pipeline.

    invocation_dir is where the user ran ark (inside the repo-root working tree).
    All file-modifying work happens in a per-run worktree, so artifacts, agent
    commands, and verification all use the worktree as their working directory —
    the repository-root working tree is never disturbed.
    """
    if os.environ.get("ARK_RUNNING"):
        print("Error: recursive ark invocation blocked", file=sys.stderr)
        sys.exit(1)
    os.environ["ARK_RUNNING"] = "1"

    invocation_dir = os.path.abspath(invocation_dir)

    # EC-6: fail fast on a misconfigured timeout env var BEFORE launching any
    # agent, naming the offending variable and value. A bad value must never
    # silently degrade into an instant (zero) timeout that kills a live job.
    try:
        _idle = idle_timeout()
        _step = step_timeout()
    except ArkError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # EC-1: must be inside a git repository before we touch anything.
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
    # FR-11/AC-15: document the progress-aware tunables alongside the existing
    # ARK_MODEL / ARK_SKIP_PERMISSIONS configuration.
    print(f"  idle cap:  {_idle}s (ARK_IDLE_TIMEOUT)")
    print(f"  hard cap:  {_step}s (ARK_STEP_TIMEOUT)")
    if SKIP_PERMISSIONS:
        print("  WARNING: agents run with --dangerously-skip-permissions")
        print("           set ARK_SKIP_PERMISSIONS=0 to disable")
    print()

    # Save driver choice for resume
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

    # Session name is a deterministic function of the slug (AC-10), so
    # concurrent runs with different slugs neither share nor kill each other's
    # session. If a session for THIS slug is already alive, a run for this slug
    # is in flight: don't destroy its agent state — refuse rather than clobber.
    tmux = Tmux(session_name)
    if tmux.is_alive():
        print(
            f"Error: a run for this feature is already in flight "
            f"(tmux session '{session_name}' is alive).\n"
            f"  Attach with: tmux attach -t {session_name}\n"
            f"  Or kill it first: tmux kill-session -t {session_name}",
            file=sys.stderr,
        )
        return 1
    # Reap any dead/leftover registration for this name, then create fresh.
    tmux.kill_session()
    tmux.create_session(project_dir)

    print(f"  Attach with: tmux attach -t {session_name}\n")

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

    # Phase 3: Implement + verify loop.
    #
    # A timed-out (hung) implement/reimplement step is NOT a finished step: it
    # never advances to verify and never renders a PASS/FAIL verdict (FR-7,
    # Defect 2). Under remedy (a), a timeout does NOT consume a MAX_LOOPS attempt
    # (FR-8/AC-11) — only a genuinely finished-but-failing verification does.
    #
    # `attempt` counts only finished implementation attempts; a separate, small
    # backstop bounds repeated non-finishing outcomes so a persistently hung or
    # session-dying agent cannot loop forever. The hard ceiling (HARD_TIMEOUT) is
    # the absolute backstop and is terminal — it is never retried (FR-7).
    passed = False
    timed_out = False
    attempt = 0
    timeout_retries = 0
    MAX_TIMEOUT_RETRIES = MAX_LOOPS
    while attempt < MAX_LOOPS:
        print(f"\n--- Attempt {attempt + 1}/{MAX_LOOPS} ---\n")

        if attempt == 0:
            outcome = step_implement(tmux, project_dir, driver)
        else:
            outcome = step_reimplement(tmux, project_dir, driver)

        if not _finished(outcome):
            # FR-7.1–FR-7.3: do NOT verify, do NOT render a verdict; report an
            # explicit timeout, distinct from a completed-but-failed step.
            print(
                f"\n  [!] TIMEOUT: implement step did not finish (outcome="
                f"{outcome}). Not running verify; no PASS/FAIL verdict rendered.",
                file=sys.stderr,
            )
            if outcome == StepOutcome.HARD_TIMEOUT:
                # Absolute backstop reached — terminal, never retried (FR-7).
                print(
                    "  [!] Hard ceiling (ARK_STEP_TIMEOUT) reached — stopping. "
                    "Partial output left intact.",
                    file=sys.stderr,
                )
                timed_out = True
                break
            # Idle timeout / session death: do not consume a MAX_LOOPS attempt
            # (FR-8/AC-11). Retry within the timeout backstop.
            timeout_retries += 1
            if timeout_retries >= MAX_TIMEOUT_RETRIES:
                print(
                    f"  [!] Gave up after {timeout_retries} non-finishing "
                    "attempts. Partial output left intact.",
                    file=sys.stderr,
                )
                timed_out = True
                break
            continue

        # Finished implementation — this attempt counts (FR-8).
        attempt += 1

        passed = step_verify(feature_slug, project_dir)
        if passed:
            print("\n  All checks passed!")
            break

        step_fix_make(tmux, feature_slug, project_dir)

    if timed_out:
        # FR-7.2/AC-10: a timeout is NOT a FAIL verdict. Report it as a timeout,
        # visibly distinct from a completed-but-failed verification. Do NOT
        # truncate or re-run over the in-flight job's output (FR-9/EC-8).
        print(
            f"\n  TIMED OUT — the implementation step never finished. "
            f"No verification verdict was rendered. Artifacts left intact.",
            file=sys.stderr,
        )
        archive_run(project_dir, feature_slug)
        return 2

    if not passed:
        print(f"\n  FAILED after {MAX_LOOPS} attempts.")
        archive_run(project_dir, feature_slug)
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
        archive_run(project_dir, feature_slug)
        return 1

    # Archive results
    archive_run(project_dir, feature_slug)

    # FR-6: results are discoverable from the repository root via the branch.
    print(f"\n  Done. Branch ark/{feature_slug} is ready to merge.")
    print(f"  Worktree: {project_dir}")
    print(f"  From the repo root ({root}):")
    print(f"    git merge ark/{feature_slug}")
    print(f"  Inspect the run: tmux attach -t {session_name}")
    return 0


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
  ark help

Environment:
  ARK_MODEL            model for all claude invocations (default: opus)
  ARK_SKIP_PERMISSIONS 0 to drop --dangerously-skip-permissions (default: 1)
  ARK_IDLE_TIMEOUT     seconds a step may make no progress before it is
                       declared hung (default: 900)
  ARK_STEP_TIMEOUT     absolute wall-clock ceiling per step, regardless of
                       progress (default: 86400)"""
    print(msg, file=file)


def main():
    if len(sys.argv) < 2:
        print_help(file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd in ("help", "-h", "--help"):
        print_help(file=sys.stdout)
        sys.exit(0)

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
