#!/usr/bin/env python3
"""kiss_korc - A dumb Python orchestrator for LLM agents.

No LLM in the loop. Just a script that runs a fixed pipeline,
spawning fresh-context agents at each step.

Usage:
    cat feature.txt | kiss-korc new
"""

# TODO: add the option for korc to spin up agents when it wants/needs
# some judgement (like, "is this work good enough?")

import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Prompts — each agent gets only what it needs, nothing more
# ---------------------------------------------------------------------------

PROMPT_SPEC = """\
You are a specification writer. Explore the current project to understand \
its structure, then turn the feature description below into a clear, \
testable specification document.

Requirements:
- Each requirement must be independently testable
- Use numbered acceptance criteria (AC-1, AC-2, ...)
- Include edge cases and error conditions
- Do NOT include implementation details or code
- Write the final spec to {spec_path}

Feature description:
{feature}
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
# Tmux helpers
# ---------------------------------------------------------------------------


class Tmux:
    """Thin wrapper around tmux commands. No magic."""

    def __init__(self, session_name):
        self.session = session_name

    def create_session(self):
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.session, "-x", "200", "-y", "50"],
            check=True,
        )

    def split_pane(self):
        """Split horizontally — pane 0 is korc, pane 1 is the worker."""
        subprocess.run(
            ["tmux", "split-window", "-h", "-t", f"{self.session}:0"],
            check=True,
        )

    def send_keys(self, cmd):
        """Send a command to pane 1 (the worker pane)."""
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{self.session}:0.1", cmd, "Enter"],
            check=True,
        )

    def worker_pane_pid(self):
        """Get the shell PID of pane 1."""
        result = subprocess.run(
            ["tmux", "list-panes", "-t", f"{self.session}:0", "-F", "#{pane_pid}"],
            capture_output=True, text=True,
        )
        pids = result.stdout.strip().split("\n")
        return pids[1] if len(pids) > 1 else None

    def wait_for_idle(self, poll_interval=5):
        """Wait until pane 1's shell has no child processes."""
        while True:
            pid = self.worker_pane_pid()
            if not pid:
                break
            child_check = subprocess.run(
                ["pgrep", "-P", pid],
                capture_output=True,
            )
            if child_check.returncode != 0:
                break
            time.sleep(poll_interval)

    def kill_session(self):
        subprocess.run(
            ["tmux", "kill-session", "-t", self.session],
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def korc_path(project_dir, filename):
    """Return a path inside .kisskorc/."""
    return str(Path(project_dir) / KISSKORC_DIR / filename)


def korc_file_exists(project_dir, filename):
    """Check if a .kisskorc/ artifact exists."""
    return (Path(project_dir) / KISSKORC_DIR / filename).exists()


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
    """Create and checkout a korc/ branch."""
    branch = f"korc/{slug}"
    result = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        capture_output=True,
    )
    if result.returncode == 0:
        subprocess.run(["git", "checkout", branch], check=True)
    else:
        subprocess.run(["git", "checkout", "-b", branch], check=True)


def run_in_pane(tmux, cmd):
    """Send a command to the worker pane and wait for it to finish."""
    tmux.send_keys(cmd)
    time.sleep(1)  # let the process start
    tmux.wait_for_idle()


def build_claude_batch_cmd(prompt):
    """Build a claude -p command as a string for send-keys."""
    return shlex.join([
        "claude", "-p",
        "--model", MODEL,
        "--dangerously-skip-permissions",
        prompt,
    ])


def build_claude_interactive_cmd(prompt):
    """Build an interactive claude command as a string for send-keys."""
    return shlex.join([
        "claude",
        "--model", MODEL,
        "--dangerously-skip-permissions",
        "-p",
        prompt,
    ])


# ---------------------------------------------------------------------------
# Pipeline steps — each is a small function
# ---------------------------------------------------------------------------


def step_spec(tmux, feature, project_dir):
    """Turn feature description into a spec."""
    print("[step:spec] Writing specification...")
    prompt = PROMPT_SPEC.format(
        feature=feature,
        spec_path=korc_path(project_dir, "SPEC.md"),
    )
    run_in_pane(tmux, build_claude_batch_cmd(prompt))
    print("  -> SPEC.md")


def step_review_spec(tmux, project_dir):
    """Review the spec with fresh context."""
    print("[step:review-spec] Reviewing specification...")
    prompt = PROMPT_REVIEW_SPEC.format(
        spec_path=korc_path(project_dir, "SPEC.md"),
        review_path=korc_path(project_dir, "review-spec.md"),
    )
    run_in_pane(tmux, build_claude_batch_cmd(prompt))
    print("  -> review-spec.md")


def step_encode(tmux, feature_slug, project_dir):
    """Turn spec into a verification Makefile."""
    print("[step:encode] Writing verification Makefile...")
    prompt = PROMPT_ENCODE.format(
        spec_path=korc_path(project_dir, "SPEC.md"),
        makefile_path=korc_path(project_dir, f"verify-{feature_slug}.mk"),
    )
    run_in_pane(tmux, build_claude_batch_cmd(prompt))
    print(f"  -> verify-{feature_slug}.mk")


def step_review_make(tmux, feature_slug, project_dir):
    """Review the Makefile with fresh context."""
    print("[step:review-make] Reviewing Makefile...")
    prompt = PROMPT_REVIEW_MAKE.format(
        spec_path=korc_path(project_dir, "SPEC.md"),
        makefile_path=korc_path(project_dir, f"verify-{feature_slug}.mk"),
        review_path=korc_path(project_dir, "review-make.md"),
    )
    run_in_pane(tmux, build_claude_batch_cmd(prompt))
    print("  -> review-make.md")


def step_implement(tmux, project_dir):
    """Have an agent implement the spec. Interactive — user can watch/intervene."""
    print("[step:implement] Implementing...")
    prompt = PROMPT_IMPLEMENT.format(
        spec_path=korc_path(project_dir, "SPEC.md"),
    )
    run_in_pane(tmux, build_claude_interactive_cmd(prompt))
    print("  -> implementation complete")


def step_verify(feature_slug, project_dir):
    """Run the verification Makefile. Returns True if all checks pass."""
    print("[step:verify] Running verification...")
    mk_path = Path(project_dir) / KISSKORC_DIR / f"verify-{feature_slug}.mk"
    result = subprocess.run(
        ["make", "-k", "-f", str(mk_path)],
        capture_output=True, text=True,
        cwd=project_dir,
    )
    output = f"exit code: {result.returncode}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    review_path = Path(project_dir) / KISSKORC_DIR / "REVIEW.md"
    review_path.write_text(output)
    print(f"  -> exit code {result.returncode}")
    return result.returncode == 0


def step_fix_make(tmux, feature_slug, project_dir):
    """Fix Makefile issues with fresh context."""
    print("[step:fix-make] Fixing Makefile...")
    prompt = PROMPT_FIX_MAKE.format(
        makefile_path=korc_path(project_dir, f"verify-{feature_slug}.mk"),
        review_path=korc_path(project_dir, "REVIEW.md"),
    )
    run_in_pane(tmux, build_claude_batch_cmd(prompt))
    print(f"  -> verify-{feature_slug}.mk updated")


def step_reimplement(tmux, project_dir):
    """Reimplement with spec + review context. Interactive."""
    print("[step:reimplement] Re-implementing...")
    prompt = PROMPT_REIMPLEMENT.format(
        spec_path=korc_path(project_dir, "SPEC.md"),
        review_path=korc_path(project_dir, "REVIEW.md"),
    )
    run_in_pane(tmux, build_claude_interactive_cmd(prompt))
    print("  -> re-implementation complete")


def step_adversarial(tmux, project_dir):
    """Spawn two adversarial reviewers (claude + codex) sequentially in pane."""
    print("[step:adversarial] Adversarial review...")

    # Claude review
    claude_prompt = PROMPT_ADVERSARIAL.format(
        spec_path=korc_path(project_dir, "SPEC.md"),
        output_path=korc_path(project_dir, "adversarial-claude.md"),
    )
    run_in_pane(tmux, build_claude_batch_cmd(claude_prompt))
    print("  -> adversarial-claude.md")

    # Codex review
    codex_prompt = PROMPT_ADVERSARIAL.format(
        spec_path=korc_path(project_dir, "SPEC.md"),
        output_path=korc_path(project_dir, "adversarial-codex.md"),
    )
    codex_cmd = shlex.join([
        "codex", "exec", "--full-auto",
        "-C", project_dir,
        codex_prompt,
    ])
    run_in_pane(tmux, codex_cmd)
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
        spec_path=korc_path(project_dir, "SPEC.md"),
        claude_review_path=korc_path(project_dir, "adversarial-claude.md"),
        codex_review_path=korc_path(project_dir, "adversarial-codex.md"),
    )
    run_in_pane(tmux, build_claude_interactive_cmd(prompt))
    print("  -> landing complete")


def step_ship(feature_slug, project_dir):
    """Final step: verify everything passes and declare ready."""
    print("[step:ship] Final verification...")
    mk_path = Path(project_dir) / KISSKORC_DIR / f"verify-{feature_slug}.mk"
    result = subprocess.run(
        ["make", "-k", "-f", str(mk_path)],
        capture_output=True, text=True,
        cwd=project_dir,
    )
    if result.returncode == 0:
        print(f"  SHIP IT. Branch korc/{feature_slug} is ready to merge.")
        return True
    else:
        print(f"  [!] Final verification failed. Check .kisskorc/REVIEW.md")
        review_path = Path(project_dir) / KISSKORC_DIR / "REVIEW.md"
        output = f"exit code: {result.returncode}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        review_path.write_text(output)
        return False


# ---------------------------------------------------------------------------
# Resume logic
# ---------------------------------------------------------------------------

STEPS = [
    "spec",
    "review-spec",
    "encode",
    "review-make",
    "implement",
    "verify",
    # steps 5-8 loop, handled separately
]


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
        if korc_file_exists(project_dir, filename):
            last_completed = step_name
        else:
            break

    if last_completed:
        print(f"  Resuming after: {last_completed}")
    return last_completed


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
    print()

    # Save feature description
    feature_file = Path(project_dir) / KISSKORC_DIR / "FEATURE.md"
    if not feature_file.exists():
        feature_file.write_text(feature)

    # Create branch
    create_branch(feature_slug)

    # Check resume point
    resume_after = detect_resume_point(feature_slug, project_dir)

    # Create tmux session
    tmux = Tmux(session_name)
    tmux.kill_session()
    tmux.create_session()
    tmux.split_pane()

    # Phase 1: Spec
    if resume_after is None:
        step_spec(tmux, feature, project_dir)
        resume_after = "spec"

    if resume_after == "spec":
        step_review_spec(tmux, project_dir)
        resume_after = "review-spec"

    # Phase 2: Encode verification
    if resume_after == "review-spec":
        step_encode(tmux, feature_slug, project_dir)
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
        return 1

    # Phase 4: Adversarial review
    has_findings = step_adversarial(tmux, project_dir)

    # Phase 5: Land — address critical/major findings
    if has_findings:
        step_land(tmux, project_dir)

    # Phase 6: Ship
    if step_ship(feature_slug, project_dir):
        return 0
    else:
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "new":
        print("Usage: cat feature.txt | kiss-korc new", file=sys.stderr)
        sys.exit(1)

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


if __name__ == "__main__":
    main()
