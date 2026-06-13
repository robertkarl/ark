#!/usr/bin/env python3
"""kiss_korc - A dumb Python orchestrator for LLM agents.

No LLM in the loop. Just a script that runs a fixed pipeline,
spawning fresh-context agents at each step.

Usage:
    echo 'Add a feature' | kiss-korc new
    kiss-korc archive
"""

# TODO: add the option for korc to spin up agents when it wants/needs
# some judgement (like, "is this work good enough?")

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Prompts — each agent gets only what it needs, nothing more.
# Agents read/write files by path. No prompt contains file contents inline.
# ---------------------------------------------------------------------------

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
- Do not modify anything under .kisskorc/
- Do not merge branches. Do not run git merge. Commit on the current branch only.
- Do not push to any remote.
"""

PROMPT_REIMPLEMENT = """\
You are an implementer. A previous implementation attempt failed verification.
Read the spec at {spec_path} and the verification results at {review_path}, \
then fix the implementation.

Rules:
- Do not modify anything under .kisskorc/
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
You are an adversarial code reviewer. Your job is to find bugs, security \
issues, missing edge cases, and spec violations.

Be harsh. Be thorough. Assume nothing works until proven otherwise.
Output a numbered list of findings. For each finding, state:
- What is wrong
- Where in the code (file + function/line)
- Severity (critical / major / minor)

Read the spec at {spec_path}, then review the code in the current working \
directory against it. Write your findings to {output_path}.
"""

PROMPT_LAND = """\
You are a landing agent. Two adversarial reviewers examined this codebase. \
Their findings are at {claude_review_path} and {codex_review_path}.
Address every critical and major finding. Ignore minor findings.

Rules:
- Do not modify anything under .kisskorc/
- Do not merge branches. Do not run git merge. Commit on the current branch only.
- Do not push to any remote.

Read the spec at {spec_path} for context.
"""

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL = os.environ.get("KISSKORC_MODEL", "opus")
MAX_LOOPS = 3
KISSKORC_DIR = ".kisskorc"

# ---------------------------------------------------------------------------
# Tmux helpers — kiss_korc runs in your terminal, tmux is the worker
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

    def send_command(self, cmd, sentinel_path=None):
        """Send a command string to the worker window via a temp script.

        If sentinel_path is provided, the script touches it when done.
        This avoids tmux send-keys buffer limits on long commands.
        Uses mkstemp to avoid symlink attacks on predictable paths.
        """
        fd, script_path = tempfile.mkstemp(prefix="korc-", suffix=".sh")
        script_body = f"#!/bin/bash\n{cmd}\n"
        if sentinel_path:
            script_body += f"touch {sentinel_path}\n"
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
    """Return absolute path inside .kisskorc/."""
    return str(Path(project_dir) / KISSKORC_DIR / filename)


def kf_exists(project_dir, filename):
    """Check if a .kisskorc/ artifact exists and is non-empty."""
    p = Path(project_dir) / KISSKORC_DIR / filename
    return p.exists() and p.stat().st_size > 0


def slugify(text):
    """Turn feature description into a short slug."""
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return "-".join(words[:4]) or "feature"


def ensure_dir(project_dir):
    """Create .kisskorc/ directory."""
    d = Path(project_dir) / KISSKORC_DIR
    d.mkdir(exist_ok=True)
    return d


def create_branch(slug):
    """Create and checkout a korc/ branch. Stashes if dirty."""
    # Check for dirty working tree
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True,
    )
    stashed = False
    if status.stdout.strip():
        print("  Stashing uncommitted changes...")
        subprocess.run(["git", "stash", "push", "-m", f"korc-{slug}-autostash"], check=True)
        stashed = True

    branch = f"korc/{slug}"
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


def run_in_tmux(tmux, cmd, project_dir, timeout=1800):
    """Send a command to the tmux worker and wait for it to finish."""
    sentinel = os.path.join(project_dir, KISSKORC_DIR, "_sentinel")
    # Remove stale sentinel
    if os.path.exists(sentinel):
        os.unlink(sentinel)
    tmux.send_command(cmd, sentinel_path=sentinel)
    return tmux.wait_for_sentinel(sentinel, timeout=timeout)


def claude_cmd(prompt_file):
    """Build a claude -p command that reads its prompt from a file."""
    return f'claude -p --model {MODEL} --dangerously-skip-permissions "$(cat {prompt_file})"'


def write_prompt(project_dir, step_name, content):
    """Write a prompt to a temp file, return its path."""
    p = Path(project_dir) / KISSKORC_DIR / f"_prompt_{step_name}.md"
    p.write_text(content)
    return str(p)


# ---------------------------------------------------------------------------
# Pipeline steps — each is a small function
# ---------------------------------------------------------------------------


def step_spec(tmux, feature, project_dir):
    """Turn feature description into a spec."""
    print("[step:spec] Writing specification...")
    prompt = PROMPT_SPEC.format(
        feature_path=kp(project_dir, "FEATURE.md"),
        spec_path=kp(project_dir, "SPEC.md"),
    )
    pf = write_prompt(project_dir, "spec", prompt)
    run_in_tmux(tmux, claude_cmd(pf), project_dir)
    ok = kf_exists(project_dir, "SPEC.md")
    print(f"  -> SPEC.md {'(written)' if ok else '(MISSING!)'}")
    return ok


def step_review_spec(tmux, project_dir):
    """Review the spec with fresh context."""
    print("[step:review-spec] Reviewing specification...")
    prompt = PROMPT_REVIEW_SPEC.format(
        spec_path=kp(project_dir, "SPEC.md"),
        review_path=kp(project_dir, "review-spec.md"),
    )
    pf = write_prompt(project_dir, "review-spec", prompt)
    run_in_tmux(tmux, claude_cmd(pf), project_dir)
    print("  -> review-spec.md")


def step_encode(tmux, feature_slug, project_dir):
    """Turn spec into a verification Makefile."""
    print("[step:encode] Writing verification Makefile...")
    mk_name = f"verify-{feature_slug}.mk"
    prompt = PROMPT_ENCODE.format(
        spec_path=kp(project_dir, "SPEC.md"),
        makefile_path=kp(project_dir, mk_name),
    )
    pf = write_prompt(project_dir, "encode", prompt)
    run_in_tmux(tmux, claude_cmd(pf), project_dir)
    ok = kf_exists(project_dir, mk_name)
    print(f"  -> {mk_name} {'(written)' if ok else '(MISSING!)'}")
    return ok


def step_review_make(tmux, feature_slug, project_dir):
    """Review the Makefile with fresh context."""
    print("[step:review-make] Reviewing Makefile...")
    prompt = PROMPT_REVIEW_MAKE.format(
        spec_path=kp(project_dir, "SPEC.md"),
        makefile_path=kp(project_dir, f"verify-{feature_slug}.mk"),
        review_path=kp(project_dir, "review-make.md"),
    )
    pf = write_prompt(project_dir, "review-make", prompt)
    run_in_tmux(tmux, claude_cmd(pf), project_dir)
    print("  -> review-make.md")


def step_implement(tmux, project_dir):
    """Have an agent implement the spec. Interactive — user can intervene."""
    print("[step:implement] Implementing (interactive)...")
    prompt = PROMPT_IMPLEMENT.format(
        spec_path=kp(project_dir, "SPEC.md"),
    )
    pf = write_prompt(project_dir, "implement", prompt)
    run_in_tmux(tmux, claude_cmd(pf), project_dir)
    print("  -> implementation complete")


def step_verify(feature_slug, project_dir):
    """Run the verification Makefile. Returns True if all checks pass."""
    print("[step:verify] Running verification...")
    mk_path = Path(project_dir) / KISSKORC_DIR / f"verify-{feature_slug}.mk"
    if not mk_path.exists():
        print("  [!] Makefile not found, skipping verify")
        return False
    result = subprocess.run(
        ["make", "-k", "-f", str(mk_path)],
        capture_output=True, text=True,
        cwd=project_dir,
    )
    output = f"exit code: {result.returncode}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    review_path = Path(project_dir) / KISSKORC_DIR / "REVIEW.md"
    review_path.write_text(output)
    passed = result.returncode == 0
    print(f"  -> {'PASS' if passed else 'FAIL'} (exit {result.returncode})")
    if not passed and result.stderr:
        # Show first few lines of errors
        for line in result.stderr.strip().split("\n")[:5]:
            print(f"     {line}")
    return passed


def step_fix_make(tmux, feature_slug, project_dir):
    """Fix Makefile issues with fresh context."""
    print("[step:fix-make] Fixing Makefile...")
    prompt = PROMPT_FIX_MAKE.format(
        makefile_path=kp(project_dir, f"verify-{feature_slug}.mk"),
        review_path=kp(project_dir, "REVIEW.md"),
    )
    pf = write_prompt(project_dir, "fix-make", prompt)
    run_in_tmux(tmux, claude_cmd(pf), project_dir)
    print(f"  -> verify-{feature_slug}.mk updated")


def step_reimplement(tmux, project_dir):
    """Reimplement with spec + review context. Interactive."""
    print("[step:reimplement] Re-implementing (interactive)...")
    prompt = PROMPT_REIMPLEMENT.format(
        spec_path=kp(project_dir, "SPEC.md"),
        review_path=kp(project_dir, "REVIEW.md"),
    )
    pf = write_prompt(project_dir, "reimplement", prompt)
    run_in_tmux(tmux, claude_cmd(pf), project_dir)
    print("  -> re-implementation complete")


def step_adversarial(tmux, project_dir):
    """Spawn two adversarial reviewers sequentially."""
    print("[step:adversarial] Adversarial review...")

    # Claude review
    claude_prompt = PROMPT_ADVERSARIAL.format(
        spec_path=kp(project_dir, "SPEC.md"),
        output_path=kp(project_dir, "adversarial-claude.md"),
    )
    pf = write_prompt(project_dir, "adversarial-claude", claude_prompt)
    run_in_tmux(tmux, claude_cmd(pf), project_dir)
    print("  -> adversarial-claude.md")

    # Codex review
    codex_prompt = PROMPT_ADVERSARIAL.format(
        spec_path=kp(project_dir, "SPEC.md"),
        output_path=kp(project_dir, "adversarial-codex.md"),
    )
    pf = write_prompt(project_dir, "adversarial-codex", codex_prompt)
    codex_cmd_str = f'codex exec --full-auto -C {project_dir} "$(cat {pf})"'
    run_in_tmux(tmux, codex_cmd_str, project_dir)
    print("  -> adversarial-codex.md")

    # Check for critical/major findings
    for name in ["adversarial-claude.md", "adversarial-codex.md"]:
        p = Path(project_dir) / KISSKORC_DIR / name
        if p.exists() and any(w in p.read_text().lower() for w in ["critical", "major"]):
            return True
    return False


def step_land(tmux, project_dir):
    """Fresh agent addresses adversarial findings. Interactive."""
    print("[step:land] Addressing adversarial findings...")
    prompt = PROMPT_LAND.format(
        spec_path=kp(project_dir, "SPEC.md"),
        claude_review_path=kp(project_dir, "adversarial-claude.md"),
        codex_review_path=kp(project_dir, "adversarial-codex.md"),
    )
    pf = write_prompt(project_dir, "land", prompt)
    run_in_tmux(tmux, claude_cmd(pf), project_dir)
    print("  -> landing complete")


def step_ship(feature_slug, project_dir):
    """Final step: verify everything passes and declare ready."""
    print("[step:ship] Final verification...")
    mk_path = Path(project_dir) / KISSKORC_DIR / f"verify-{feature_slug}.mk"
    if not mk_path.exists():
        print("  [!] No Makefile found. Cannot ship.")
        return False
    result = subprocess.run(
        ["make", "-k", "-f", str(mk_path)],
        capture_output=True, text=True,
        cwd=project_dir,
    )
    if result.returncode == 0:
        print(f"  SHIP IT. Branch korc/{feature_slug} is ready to merge.")
        return True
    else:
        print("  [!] Final verification failed.")
        review_path = Path(project_dir) / KISSKORC_DIR / "REVIEW.md"
        output = f"exit code: {result.returncode}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        review_path.write_text(output)
        return False


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
    """Copy .kisskorc/ artifacts to .kisskorc/archive/<timestamp>/."""
    korc_dir = Path(project_dir) / KISSKORC_DIR
    if not korc_dir.exists():
        print("Nothing to archive — no .kisskorc/ directory", file=sys.stderr)
        return

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if label:
        archive_name = f"{ts}-{label}"
    else:
        archive_name = ts

    archive_dir = korc_dir / "archive" / archive_name
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Copy all non-archive, non-prompt artifacts
    for f in korc_dir.iterdir():
        if f.name == "archive" or f.name.startswith("_prompt_"):
            continue
        if f.is_file():
            shutil.copy2(f, archive_dir / f.name)

    print(f"  Archived to {archive_dir}")
    return archive_dir


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(feature, project_dir):
    """Execute the full kiss_korc pipeline."""
    project_dir = os.path.abspath(project_dir)
    korc_dir = ensure_dir(project_dir)
    feature_slug = slugify(feature)
    session_name = f"korc-{feature_slug}"

    print(f"kiss_korc: {feature_slug}")
    print(f"  project: {project_dir}")
    print(f"  artifacts: {korc_dir}")
    print(f"  tmux: {session_name}")
    print()

    # Save feature description
    feature_file = Path(project_dir) / KISSKORC_DIR / "FEATURE.md"
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
            step_implement(tmux, project_dir)
        else:
            step_reimplement(tmux, project_dir)

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

    # Phase 5: Land — address critical/major findings
    if has_findings:
        step_land(tmux, project_dir)

    # Phase 6: Ship
    result = 0 if step_ship(feature_slug, project_dir) else 1

    # Archive results
    archive_run(project_dir, feature_slug)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def print_help(file=sys.stdout):
    """Print the help message to the given file object."""
    msg = """\
kiss-korc — a dumb Python orchestrator that runs a fixed, multi-phase pipeline
for developing features using fresh-context AI agents in tmux.

Usage:
  kiss-korc new              Read a feature description from stdin and run the
                             full pipeline (spec → encode → implement → review
                             → land → ship).
  kiss-korc archive [label]  Archive the current .kisskorc/ artifacts. The label
                             argument is optional.
  kiss-korc help             Show this help message.

Examples:
  echo 'Add auth' | kiss-korc new
  kiss-korc archive my-label
  kiss-korc help"""
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

    if cmd == "new":
        if sys.stdin.isatty():
            print("Error: pipe a feature description to stdin", file=sys.stderr)
            print("  echo 'Add auth' | kiss-korc new", file=sys.stderr)
            sys.exit(1)

        feature = sys.stdin.read().strip()
        if not feature:
            print("Error: empty feature description", file=sys.stderr)
            sys.exit(1)

        project_dir = os.getcwd()
        sys.exit(run_pipeline(feature, project_dir))

    print(f"Unknown command: {cmd}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
