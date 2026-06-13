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
Run: git diff main...HEAD
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

PROMPT_LAND = """\
You are a landing agent. Two code reviewers examined this codebase. \
Their findings are at {claude_review_path} and {codex_review_path}.
Address every BLOCK finding. Address WARN findings if straightforward. Ignore NOTEs.

Rules:
- Do not modify anything under .ark/
- Do not merge branches. Do not run git merge. Commit on the current branch only.
- Do not push to any remote.

Read the spec at {spec_path} for context.
"""

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MODEL_RAW = os.environ.get("ARK_MODEL", "opus")
_MODEL_VALIDATED = False
MODEL = _MODEL_RAW
MAX_LOOPS = 3
ARK_DIR = ".ark"


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


def create_branch(slug):
    """Create and checkout an ark/ branch. Stashes if dirty."""
    # Check for dirty working tree
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True,
    )
    stashed = False
    if status.stdout.strip():
        print("  Stashing uncommitted changes...")
        subprocess.run(["git", "stash", "push", "-m", f"ark-{slug}-autostash"], check=True)
        stashed = True

    branch = f"ark/{slug}"
    result = subprocess.run(
        ["git", "rev-parse", "--verify", branch], capture_output=True,
    )
    if result.returncode == 0:
        subprocess.run(["git", "checkout", branch], check=True)
    else:
        subprocess.run(["git", "checkout", "-b", branch], check=True)

    if stashed:
        pop = subprocess.run(["git", "stash", "pop"], capture_output=True, text=True)
        if pop.returncode != 0:
            print("  [!] git stash pop failed — your changes are still in the stash.",
                  file=sys.stderr)
            print(f"      {pop.stderr.strip()}", file=sys.stderr)
            print("      Run 'git stash list' and 'git stash pop' manually to recover.",
                  file=sys.stderr)


def make_sentinel(project_dir):
    """Create a unique sentinel path for this invocation."""
    return os.path.join(project_dir, ARK_DIR, f"_sentinel_{uuid.uuid4().hex[:8]}")


def run_in_tmux(
        tmux, cmd, sentinel, timeout=1800):
    """Send a command to the tmux worker and wait for the agent to signal done.

    The agent is instructed (via PROMPT_SENTINEL in its prompt) to create the
    sentinel file when finished. If the tmux session dies, recreates and retries.
    """
    # sentinel is inside project_dir/.ark/ — go up two levels
    project_dir = str(Path(sentinel).parent.parent)
    if not tmux.is_alive():
        tmux.create_session(project_dir)
    tmux.send_command(cmd)
    ok = tmux.wait_for_sentinel(sentinel, timeout=timeout)
    if ok:
        # Kill the interactive claude session so the pane is ready for the next step
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux.session, "/exit", "Enter"],
            capture_output=True,
        )
        time.sleep(2)
    if not ok and not tmux.is_alive():
        print("  [!] Retrying after tmux session loss", file=sys.stderr)
        tmux.create_session(project_dir)
        tmux.send_command(cmd)
        ok = tmux.wait_for_sentinel(sentinel, timeout=timeout)
        if ok:
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux.session, "/exit", "Enter"],
                capture_output=True,
            )
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
        tmux, project_dir):
    """Spawn two adversarial reviewers sequentially."""
    print("[step:adversarial] Adversarial review...")

    # Claude review
    sentinel = make_sentinel(project_dir)
    claude_prompt = PROMPT_ADVERSARIAL.format(
        spec_path=kp(project_dir, "SPEC.md"),
        output_path=kp(project_dir, "adversarial-claude.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "adversarial-claude", claude_prompt)
    run_in_tmux(tmux, claude_cmd(pf), sentinel)
    print("  -> adversarial-claude.md")

    # Codex review (codex exits on its own, so use bash touch for sentinel)
    sentinel = make_sentinel(project_dir)
    codex_prompt = PROMPT_ADVERSARIAL.format(
        spec_path=kp(project_dir, "SPEC.md"),
        output_path=kp(project_dir, "adversarial-codex.md"),
    )
    pf = write_prompt(project_dir, "adversarial-codex", codex_prompt)
    codex_cmd_str = (
        f"prompt=$(<{shlex.quote(pf)}) && "
        f"codex exec --full-auto -C {shlex.quote(project_dir)} \"$prompt\" ; "
        f"touch {shlex.quote(sentinel)}"
    )
    run_in_tmux(tmux, codex_cmd_str, sentinel)
    print("  -> adversarial-codex.md")

    # Check for BLOCK/WARN findings or missing review files (agent failure)
    for name in ["adversarial-claude.md", "adversarial-codex.md"]:
        p = Path(project_dir) / ARK_DIR / name
        if not p.exists():
            print(f"  [!] {name} missing — treating as findings present", file=sys.stderr)
            return True
        content = p.read_text()
        if "BLOCK:" in content or "WARN:" in content:
            return True
    return False


def step_land(
        tmux, project_dir):
    """Fresh agent addresses adversarial findings. Interactive."""
    print("[step:land] Addressing adversarial findings...")
    sentinel = make_sentinel(project_dir)
    prompt = PROMPT_LAND.format(
        spec_path=kp(project_dir, "SPEC.md"),
        claude_review_path=kp(project_dir, "adversarial-claude.md"),
        codex_review_path=kp(project_dir, "adversarial-codex.md"),
    ) + PROMPT_SENTINEL.format(sentinel_path=sentinel)
    pf = write_prompt(project_dir, "land", prompt)
    run_in_tmux(tmux, claude_cmd(pf), sentinel)
    print("  -> landing complete")


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
    """Move .ark/ artifacts to ~/.ark/archive/<timestamp>/."""
    korc_dir = Path(project_dir) / ARK_DIR
    if not korc_dir.exists():
        print("Nothing to archive — no .ark/ directory", file=sys.stderr)
        return

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if label:
        archive_name = f"{ts}-{label}"
    else:
        archive_name = ts

    archive_dir = Path.home() / ".ark" / "archive" / archive_name
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Move all artifacts out of .ark/
    for f in korc_dir.iterdir():
        if f.is_file():
            shutil.move(str(f), str(archive_dir / f.name))

    print(f"  Archived to {archive_dir}")
    return archive_dir


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(feature, project_dir, driver="claude"):
    """Execute the full ark pipeline."""
    if os.environ.get("ARK_RUNNING"):
        print("Error: recursive ark invocation blocked", file=sys.stderr)
        sys.exit(1)
    os.environ["ARK_RUNNING"] = "1"

    project_dir = os.path.abspath(project_dir)
    korc_dir = ensure_dir(project_dir)
    feature_slug = slugify(feature)
    session_name = f"ark-{feature_slug}"

    print(f"ark: {feature_slug}")
    print(f"  project: {project_dir}")
    print(f"  artifacts: {korc_dir}")
    print(f"  tmux: {session_name}")
    print(f"  driver: {driver}")
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

    # Create branch
    create_branch(feature_slug)

    # Check resume point
    resume_after = detect_resume_point(feature_slug, project_dir)

    # Create tmux session (kill old one if exists)
    tmux = Tmux(session_name)
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
        archive_run(project_dir, feature_slug)
        return 1

    # Phase 4: Adversarial review
    has_findings = step_adversarial(tmux, project_dir)

    # Phase 5: Land — address BLOCK/WARN findings
    if has_findings:
        step_land(tmux, project_dir)

    # Archive results
    archive_run(project_dir, feature_slug)

    print(f"\n  Done. Branch ark/{feature_slug} is ready to merge.")
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
  ark continue
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

    if cmd == "archive":
        label = sys.argv[2] if len(sys.argv) > 2 else None
        archive_run(os.getcwd(), label)
        return

    if cmd == "continue":
        project_dir = os.getcwd()
        feature_file = Path(project_dir) / ARK_DIR / "FEATURE.md"
        if not feature_file.exists():
            print("Error: no .ark/FEATURE.md — nothing to continue", file=sys.stderr)
            sys.exit(1)
        feature = feature_file.read_text().strip()
        driver_file = Path(project_dir) / ARK_DIR / "DRIVER"
        driver = driver_file.read_text().strip() if driver_file.exists() else "claude"
        sys.exit(run_pipeline(feature, project_dir, driver=driver))

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
