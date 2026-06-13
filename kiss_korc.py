#!/usr/bin/env python3
"""kiss_korc - A dumb Python orchestrator for LLM agents.

No LLM in the loop. Just a script that runs a fixed pipeline,
spawning fresh-context agents at each step.

Usage:
    cat feature.txt | kiss-korc new
"""

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
You are a specification writer. Turn the following feature description into \
a clear, testable specification document.

Requirements:
- Each requirement must be independently testable
- Use numbered acceptance criteria (AC-1, AC-2, ...)
- Include edge cases and error conditions
- Do NOT include implementation details or code
- Output only the spec in markdown

Feature description:
{feature}
"""

PROMPT_REVIEW_SPEC = """\
You are a specification reviewer. Review the following spec for:
- Ambiguity or vagueness
- Missing edge cases
- Untestable criteria
- Contradictions

If issues are found, rewrite the spec with fixes applied.
If the spec is solid, output it unchanged.

Output only the final spec in markdown.

Spec:
{spec}
"""

PROMPT_ENCODE = """\
You are a test author. Turn this specification into a Makefile that verifies \
each acceptance criterion.

Rules:
- One make target per acceptance criterion (ac-1, ac-2, ...)
- Each target should run a concrete check (curl, grep, test -f, etc.)
- Use .PHONY for all targets
- Include an "all" target that depends on every ac-* target
- The Makefile must work with `make -k` (keep going on failure)
- Use standard unix tools only
- Do NOT implement the feature — only write verification checks
- Output ONLY the Makefile contents, no markdown fences, no commentary

Spec:
{spec}
"""

PROMPT_REVIEW_MAKE = """\
You are a Makefile reviewer. Review this verification Makefile for:
- Syntax errors
- Targets that would never pass (impossible checks)
- Targets that would always pass (vacuous checks)
- Missing .PHONY declarations
- Checks that test implementation details instead of behavior

If issues are found, output a corrected Makefile.
If it's solid, output it unchanged.

Output ONLY the Makefile contents, no markdown fences, no commentary.

Makefile:
{makefile}
"""

PROMPT_IMPLEMENT = """\
You are an implementer. Read the specification below and implement it in \
the current project. Do not modify anything under .kisskorc/.

Spec:
{spec}
"""

PROMPT_REIMPLEMENT = """\
You are an implementer. A previous implementation attempt failed verification. \
Read the spec and the review below, then fix the implementation. \
Do not modify anything under .kisskorc/.

Spec:
{spec}

Verification results:
{review}
"""

PROMPT_FIX_MAKE = """\
You are a Makefile fixer. The verification Makefile below produced errors \
when run. Fix ONLY syntax errors or structural problems with the Makefile \
itself. Do NOT change what the targets are testing — only fix how they test it.

Output ONLY the corrected Makefile contents, no markdown fences, no commentary.

Makefile:
{makefile}

Make output:
{output}
"""

PROMPT_ADVERSARIAL = """\
You are an adversarial code reviewer. Your job is to find bugs, security \
issues, missing edge cases, and spec violations.

Be harsh. Be thorough. Assume nothing works until proven otherwise.

Spec:
{spec}

Review the code in the current working directory against this spec.
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
            ["tmux", "new-session", "-d", "-s", self.session],
            check=True,
        )

    def new_window(self, name):
        subprocess.run(
            ["tmux", "new-window", "-t", self.session, "-n", name],
            check=True,
        )

    def send_keys(self, target, cmd):
        full_target = f"{self.session}:{target}"
        subprocess.run(
            ["tmux", "send-keys", "-t", full_target, cmd, "Enter"],
            check=True,
        )

    def wait_for_idle(self, target, poll_interval=5):
        """Wait until no child processes are running in the pane."""
        full_target = f"{self.session}:{target}"
        while True:
            result = subprocess.run(
                ["tmux", "list-panes", "-t", full_target, "-F", "#{pane_pid}"],
                capture_output=True, text=True,
            )
            pane_pid = result.stdout.strip()
            if not pane_pid:
                break
            # Check if the pane's shell has children (the agent process)
            child_check = subprocess.run(
                ["pgrep", "-P", pane_pid],
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
# Agent runners
# ---------------------------------------------------------------------------


def run_claude_batch(prompt, model=MODEL):
    """Run claude in --print mode, return stdout."""
    cmd = ["claude", "-p", "--model", model, prompt]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [!] claude exited {result.returncode}", file=sys.stderr)
        if result.stderr:
            print(f"  {result.stderr[:500]}", file=sys.stderr)
    return result.stdout


def run_claude_interactive(tmux, window, prompt, project_dir):
    """Launch claude in a tmux window with full permissions. Blocks until done."""
    prompt_file = Path(project_dir) / KISSKORC_DIR / "_prompt.tmp"
    prompt_file.write_text(prompt)
    cmd = shlex.join([
        "claude",
        "--model", MODEL,
        "--dangerously-skip-permissions",
        "-p",
        prompt,
    ])
    tmux.send_keys(window, cmd)
    tmux.wait_for_idle(window)
    prompt_file.unlink(missing_ok=True)


def run_codex_batch(prompt, project_dir):
    """Run codex exec, return stdout."""
    cmd = ["codex", "exec", "--full-auto", "-C", project_dir, prompt]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


# ---------------------------------------------------------------------------
# Pipeline steps — each is a small function
# ---------------------------------------------------------------------------


def ensure_dir(project_dir):
    """Create .kisskorc/ directory."""
    d = Path(project_dir) / KISSKORC_DIR
    d.mkdir(exist_ok=True)
    return d


def slugify(text):
    """Turn feature description into a short slug."""
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return "-".join(words[:4]) or "feature"


def step_spec(feature, project_dir):
    """Step 1: Turn feature description into a spec."""
    print("[1/10] Writing spec...")
    prompt = PROMPT_SPEC.format(feature=feature)
    spec = run_claude_batch(prompt)
    out = Path(project_dir) / KISSKORC_DIR / "SPEC.md"
    out.write_text(spec)
    print(f"  -> {out}")
    return spec


def step_review_spec(project_dir):
    """Step 2: Review the spec with fresh context."""
    print("[2/10] Reviewing spec...")
    spec = (Path(project_dir) / KISSKORC_DIR / "SPEC.md").read_text()
    prompt = PROMPT_REVIEW_SPEC.format(spec=spec)
    reviewed = run_claude_batch(prompt)
    out = Path(project_dir) / KISSKORC_DIR / "SPEC.md"
    out.write_text(reviewed)
    review_log = Path(project_dir) / KISSKORC_DIR / "review-spec.md"
    review_log.write_text(reviewed)
    print(f"  -> {out}")
    return reviewed


def step_encode(feature_slug, project_dir):
    """Step 3: Turn spec into a verification Makefile."""
    print("[3/10] Encoding spec as Makefile...")
    spec = (Path(project_dir) / KISSKORC_DIR / "SPEC.md").read_text()
    prompt = PROMPT_ENCODE.format(spec=spec)
    makefile = run_claude_batch(prompt)
    out = Path(project_dir) / KISSKORC_DIR / f"verify-{feature_slug}.mk"
    out.write_text(makefile)
    print(f"  -> {out}")
    return makefile


def step_review_make(feature_slug, project_dir):
    """Step 4: Review the Makefile with fresh context."""
    print("[4/10] Reviewing Makefile...")
    mk_path = Path(project_dir) / KISSKORC_DIR / f"verify-{feature_slug}.mk"
    makefile = mk_path.read_text()
    prompt = PROMPT_REVIEW_MAKE.format(makefile=makefile)
    reviewed = run_claude_batch(prompt)
    mk_path.write_text(reviewed)
    review_log = Path(project_dir) / KISSKORC_DIR / "review-make.md"
    review_log.write_text(reviewed)
    print(f"  -> {mk_path}")
    return reviewed


def step_implement(tmux_session, project_dir):
    """Step 5: Have an agent implement the spec."""
    print("[5/10] Implementing...")
    spec = (Path(project_dir) / KISSKORC_DIR / "SPEC.md").read_text()
    prompt = PROMPT_IMPLEMENT.format(spec=spec)
    run_claude_interactive(tmux_session, "implement", prompt, project_dir)
    print("  -> Implementation complete")


def step_verify(feature_slug, project_dir):
    """Step 6: Run the verification Makefile."""
    print("[6/10] Verifying...")
    mk_path = Path(project_dir) / KISSKORC_DIR / f"verify-{feature_slug}.mk"
    result = subprocess.run(
        ["make", "-k", "-f", str(mk_path)],
        capture_output=True, text=True,
        cwd=project_dir,
    )
    output = f"exit code: {result.returncode}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    review = Path(project_dir) / KISSKORC_DIR / "REVIEW.md"
    review.write_text(output)
    print(f"  -> exit code {result.returncode}")
    print(f"  -> {review}")
    return result.returncode == 0


def step_fix_make(feature_slug, project_dir):
    """Step 7: Fix Makefile issues with fresh context."""
    print("[7/10] Fixing Makefile...")
    mk_path = Path(project_dir) / KISSKORC_DIR / f"verify-{feature_slug}.mk"
    makefile = mk_path.read_text()
    review = (Path(project_dir) / KISSKORC_DIR / "REVIEW.md").read_text()
    prompt = PROMPT_FIX_MAKE.format(makefile=makefile, output=review)
    fixed = run_claude_batch(prompt)
    mk_path.write_text(fixed)
    print(f"  -> {mk_path}")
    return fixed


def step_reimplement(tmux_session, window_name, project_dir):
    """Step 8: Reimplement with spec + review context."""
    print("[8/10] Re-implementing...")
    spec = (Path(project_dir) / KISSKORC_DIR / "SPEC.md").read_text()
    review = (Path(project_dir) / KISSKORC_DIR / "REVIEW.md").read_text()
    prompt = PROMPT_REIMPLEMENT.format(spec=spec, review=review)
    tmux_session.new_window(window_name)
    run_claude_interactive(tmux_session, window_name, prompt, project_dir)
    print("  -> Re-implementation complete")


def step_adversarial(project_dir):
    """Step 10: Spawn two adversarial reviewers in parallel."""
    print("[10/10] Adversarial review (claude + codex)...")
    spec = (Path(project_dir) / KISSKORC_DIR / "SPEC.md").read_text()
    prompt = PROMPT_ADVERSARIAL.format(spec=spec)

    # Run both in parallel
    claude_proc = subprocess.Popen(
        ["claude", "-p", "--model", MODEL, prompt],
        capture_output=True, text=True,
    )
    codex_proc = subprocess.Popen(
        ["codex", "exec", "--full-auto", "-C", project_dir, prompt],
        capture_output=True, text=True,
    )

    claude_out, _ = claude_proc.communicate()
    codex_out, _ = codex_proc.communicate()

    (Path(project_dir) / KISSKORC_DIR / "adversarial-claude.md").write_text(claude_out)
    (Path(project_dir) / KISSKORC_DIR / "adversarial-codex.md").write_text(codex_out)
    print("  -> adversarial-claude.md")
    print("  -> adversarial-codex.md")


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

    # Create tmux session
    tmux = Tmux(session_name)
    tmux.kill_session()  # clean slate
    tmux.create_session()

    # Phase 1: Spec
    step_spec(feature, project_dir)
    step_review_spec(project_dir)

    # Phase 2: Encode verification
    step_encode(feature_slug, project_dir)
    step_review_make(feature_slug, project_dir)

    # Phase 3: Implement + verify loop
    passed = False
    for attempt in range(1, MAX_LOOPS + 1):
        print(f"\n--- Attempt {attempt}/{MAX_LOOPS} ---\n")

        if attempt == 1:
            step_implement(tmux, project_dir)
        else:
            step_reimplement(tmux, f"impl-{attempt}", project_dir)

        passed = step_verify(feature_slug, project_dir)
        if passed:
            print("\n  All checks passed!")
            break

        # Fix makefile + loop
        step_fix_make(feature_slug, project_dir)

    if not passed:
        print(f"\n  FAILED after {MAX_LOOPS} attempts.")
        tmux.kill_session()
        return 1

    # Phase 4: Adversarial review
    step_adversarial(project_dir)

    print(f"\n  Done. Artifacts in {korc_dir}")
    print(f"  tmux session: {session_name}")
    return 0


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
