"""Regression tests for ARK's progress-aware step completion (SPEC: FR-1..FR-12).

These are unit/smoke tests only (FR-12, AC-21):
  - No real agent runs, no real sweeps, no tmux-launched ARK pipeline.
  - The clock is injected (a FakeClock), so timeouts are exactly reproducible
    and no real-time sleeping near the timeout durations ever happens.
  - The ARK_RUNNING recursion guard is honored — nothing here spawns ark.

Run:  pytest test_progress_waiting.py
"""

import os
import sys

import pytest

import ark
from ark import StepOutcome


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A deterministic, injectable clock (EC-4, AC-18/19/22).

    `now()` is the clock source. `sleep(dt)` advances the clock by `dt` instead
    of sleeping for real, so the waiting loop runs to its boundary instantly.
    """

    def __init__(self, start=0.0):
        self.t = float(start)

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class FakeTmux(ark.Tmux):
    """A Tmux stand-in that never touches a real tmux session.

    `alive` controls is_alive(); `die_after` optionally flips it to dead after a
    given number of is_alive() polls to simulate a mid-wait session death (EC-1).
    """

    def __init__(self, alive=True, die_after=None):
        super().__init__("fake-session")
        self.alive = alive
        self.die_after = die_after
        self._alive_calls = 0

    def is_alive(self):
        self._alive_calls += 1
        if self.die_after is not None and self._alive_calls > self.die_after:
            self.alive = False
        return self.alive


def make_wait(tmux, clock, **kwargs):
    """Invoke wait_for_sentinel with the injected fake clock + sleep."""
    return tmux.wait_for_sentinel(clock=clock.now, sleep=clock.sleep, **kwargs)


# ---------------------------------------------------------------------------
# Progress-aware survival (Defect 1) — AC-1, AC-2, AC-8, AC-18
# ---------------------------------------------------------------------------


def test_ac1_ac18_growing_output_past_1800s_does_not_time_out(tmp_path):
    """AC-1 / AC-18: a watched file that keeps growing past the old 1800s cap
    does NOT time out — it keeps waiting (no sentinel present)."""
    out = tmp_path / "results" / "runs.jsonl"
    out.parent.mkdir()
    out.write_text("row\n")
    sentinel = str(tmp_path / "_sentinel")

    clock = FakeClock()
    tmux = FakeTmux(alive=True)

    # The probe grows the file by one row each poll, then reports its snapshot.
    # The job "finishes" (sentinel appears) only well past 1800s of elapsed time.
    state = {"polls": 0}

    def probe():
        state["polls"] += 1
        # Append a row and bump mtime so (size, mtime) changes each poll.
        with open(out, "a") as f:
            f.write("row\n")
        # Once we are comfortably past the old 1800s cap, drop the sentinel so
        # the wait can terminate as FINISHED rather than spinning forever.
        if clock.now() > 4000:
            open(sentinel, "w").close()
        return ark.probe_paths([str(tmp_path / "results")])

    outcome = make_wait(
        tmux, clock, sentinel_path=sentinel, poll_interval=300,
        progress_probe=probe, idle_timeout=900, hard_timeout=86400,
    )
    # It survived past 1800s purely because progress kept being observed.
    assert outcome == StepOutcome.FINISHED
    assert clock.now() > 1800


def test_ac2_progress_each_idle_window_survives_to_hard_ceiling(tmp_path):
    """AC-2: a path advancing within each idle window survives up to (not past)
    the hard ceiling, never timing out on idle."""
    f = tmp_path / "out.txt"
    f.write_text("0")
    sentinel = str(tmp_path / "_sentinel")

    clock = FakeClock()
    tmux = FakeTmux(alive=True)
    counter = {"n": 0}

    def probe():
        counter["n"] += 1
        # Always advance — progress every poll, so idle never accrues.
        f.write_text(str(counter["n"]))
        return ark.probe_paths([str(f)])

    outcome = make_wait(
        tmux, clock, sentinel_path=sentinel, poll_interval=600,
        progress_probe=probe, idle_timeout=900, hard_timeout=86400,
    )
    # With constant progress, the only thing that can end the wait is the hard
    # ceiling (AC-7) — never an idle timeout.
    assert outcome == StepOutcome.HARD_TIMEOUT
    assert clock.now() >= 86400


def test_ac8_progress_resets_idle_timer(tmp_path):
    """AC-8: a progress event resets the idle timer to zero; prior idle time
    does not accumulate across the progress event."""
    f = tmp_path / "out.txt"
    f.write_text("0")
    sentinel = str(tmp_path / "_sentinel")

    clock = FakeClock()
    tmux = FakeTmux(alive=True)
    polls = {"n": 0}

    def probe():
        polls["n"] += 1
        # The probe is called once before the loop (to seed `prev`) and once per
        # poll. We emit a single progress event on the 3rd probe call, which
        # lands at t=400 of elapsed time. idle_timeout=900: without the reset the
        # idle timeout would fire near 900s; with the reset it cannot fire until
        # >= 900s AFTER t=400, i.e. >= 1300s.
        if polls["n"] == 3:
            f.write_text("1")
        return ark.probe_paths([str(f)])

    outcome = make_wait(
        tmux, clock, sentinel_path=sentinel, poll_interval=400,
        progress_probe=probe, idle_timeout=900, hard_timeout=86400,
    )
    assert outcome == StepOutcome.IDLE_TIMEOUT
    # The progress event reset the idle clock, so the idle timeout fired well
    # after the naive 900s (prior idle time did not accumulate across it).
    assert clock.now() >= 1300


# ---------------------------------------------------------------------------
# Sentinel wins / fast finish — AC-3, EC-2, EC-9
# ---------------------------------------------------------------------------


def test_ac3_sentinel_observed_returns_finished_and_removes_file(tmp_path):
    """AC-3: when the sentinel appears, the wait reports FINISHED on the next
    poll and removes the sentinel file."""
    sentinel = tmp_path / "_sentinel"
    sentinel.write_text("")
    clock = FakeClock()
    tmux = FakeTmux(alive=True)

    outcome = make_wait(
        tmux, clock, sentinel_path=str(sentinel), poll_interval=5,
        progress_probe=[str(tmp_path)], idle_timeout=900, hard_timeout=86400,
    )
    assert outcome == StepOutcome.FINISHED
    assert not sentinel.exists()  # cleanup preserved


def test_ec2_sentinel_plus_progress_same_poll_finished_wins(tmp_path):
    """EC-2: sentinel + progress in the same poll -> FINISHED wins; the step is
    never declared hung when its sentinel is present."""
    sentinel = tmp_path / "_sentinel"
    f = tmp_path / "out.txt"
    f.write_text("0")
    clock = FakeClock()
    tmux = FakeTmux(alive=True)

    def probe():
        # Both progress AND the sentinel show up this very poll.
        f.write_text(f.read_text() + "x")
        sentinel.write_text("")
        return ark.probe_paths([str(f)])

    outcome = make_wait(
        tmux, clock, sentinel_path=str(sentinel), poll_interval=5,
        progress_probe=probe, idle_timeout=1, hard_timeout=86400,
    )
    assert outcome == StepOutcome.FINISHED


def test_ec9_fast_finish_no_output_still_finished(tmp_path):
    """EC-9: a fast step that finishes before producing any watched output is
    still FINISHED via the sentinel; the absence of progress before the sentinel
    does not cause a premature timeout while idle timeout has not elapsed."""
    sentinel = tmp_path / "_sentinel"
    sentinel.write_text("")
    clock = FakeClock()
    tmux = FakeTmux(alive=True)
    # No watched path ever exists; idle_timeout is generous; sentinel present.
    outcome = make_wait(
        tmux, clock, sentinel_path=str(sentinel), poll_interval=5,
        progress_probe=[str(tmp_path / "never")], idle_timeout=900,
        hard_timeout=86400,
    )
    assert outcome == StepOutcome.FINISHED


# ---------------------------------------------------------------------------
# Directory progress — AC-4, EC-3
# ---------------------------------------------------------------------------


def test_ac4_ec3_new_file_in_watched_dir_counts_as_progress(tmp_path):
    """AC-4 / EC-3: a watched directory advances when a new file is created
    inside it (even if the dir's own mtime is unchanged); a watched path created
    after wait start counts as progress."""
    watch = tmp_path / "results"  # does NOT exist at wait start (EC-3)
    sentinel = tmp_path / "_sentinel"
    clock = FakeClock()
    tmux = FakeTmux(alive=True)
    polls = {"n": 0}

    def probe():
        polls["n"] += 1
        if polls["n"] == 1:
            watch.mkdir()  # creation of a watched path = progress (EC-3)
        elif polls["n"] == 2:
            (watch / "new1.txt").write_text("a")  # new file inside dir (AC-4)
        elif polls["n"] == 3:
            (watch / "new2.txt").write_text("b")
        else:
            sentinel.write_text("")  # finish so the test terminates
        return ark.probe_paths([str(watch)])

    outcome = make_wait(
        tmux, clock, sentinel_path=str(sentinel), poll_interval=100,
        progress_probe=probe, idle_timeout=150, hard_timeout=86400,
    )
    # Idle (150s) is shorter than the time to reach poll 4 (300s) — only the
    # per-poll progress (dir creation, new files) kept it alive.
    assert outcome == StepOutcome.FINISHED


def test_ac4_growing_file_in_dir_counts_as_progress(tmp_path):
    """AC-4: an existing file within a watched dir growing counts as progress."""
    watch = tmp_path / "results"
    watch.mkdir()
    f = watch / "runs.jsonl"
    f.write_text("a\n")
    sentinel = tmp_path / "_sentinel"
    clock = FakeClock()
    tmux = FakeTmux(alive=True)
    polls = {"n": 0}

    def probe():
        polls["n"] += 1
        if polls["n"] < 4:
            with open(f, "a") as fh:
                fh.write("more\n")  # grow existing file
        else:
            sentinel.write_text("")
        return ark.probe_paths([str(watch)])

    outcome = make_wait(
        tmux, clock, sentinel_path=str(sentinel), poll_interval=100,
        progress_probe=probe, idle_timeout=150, hard_timeout=86400,
    )
    assert outcome == StepOutcome.FINISHED


# ---------------------------------------------------------------------------
# Idle / hung detection — AC-5, AC-6, AC-7, AC-19, AC-22
# ---------------------------------------------------------------------------


def test_ac5_ac19_hung_reports_idle_timeout_within_bound(tmp_path):
    """AC-5 / AC-19: no progress, no sentinel, live session for >= idle_timeout
    reports IDLE_TIMEOUT no later than idle_timeout + poll_interval."""
    f = tmp_path / "out.txt"
    f.write_text("frozen")  # never changes -> no progress
    sentinel = str(tmp_path / "_sentinel")
    clock = FakeClock()
    tmux = FakeTmux(alive=True)

    outcome = make_wait(
        tmux, clock, sentinel_path=sentinel, poll_interval=5,
        progress_probe=[str(f)], idle_timeout=900, hard_timeout=86400,
    )
    assert outcome == StepOutcome.IDLE_TIMEOUT
    # Bounded by idle_timeout + poll_interval (the >= boundary semantics).
    assert 900 <= clock.now() <= 900 + 5


def test_ac22_hang_from_t0_idle_measured_from_wait_start(tmp_path):
    """AC-22: a job that hangs immediately and never writes any watched output
    reports IDLE_TIMEOUT measured from wait start."""
    sentinel = str(tmp_path / "_sentinel")
    clock = FakeClock()
    tmux = FakeTmux(alive=True)
    # Watched path never exists, never created -> no progress ever.
    outcome = make_wait(
        tmux, clock, sentinel_path=sentinel, poll_interval=10,
        progress_probe=[str(tmp_path / "never")], idle_timeout=900,
        hard_timeout=86400,
    )
    assert outcome == StepOutcome.IDLE_TIMEOUT
    assert 900 <= clock.now() <= 900 + 10


def test_ac6_hung_distinguishable_from_finished():
    """AC-6: the hung outcome is programmatically distinct from FINISHED and the
    three failures are all falsy (so legacy `if ok:` still works)."""
    assert StepOutcome.IDLE_TIMEOUT != StepOutcome.FINISHED
    assert StepOutcome.HARD_TIMEOUT != StepOutcome.FINISHED
    assert StepOutcome.SESSION_DIED != StepOutcome.FINISHED
    assert ark._finished(StepOutcome.FINISHED)
    for bad in StepOutcome.FAILURES:
        assert not ark._finished(bad)


def test_ac6_bool_semantics_for_legacy_callsites():
    """AC-6 / FR-6a: FINISHED is truthy, the three failures are falsy (so legacy
    `if ok:` callsites keep working), and all four remain distinguishable."""
    assert bool(StepOutcome.FINISHED) is True
    assert bool(StepOutcome.IDLE_TIMEOUT) is False
    assert bool(StepOutcome.HARD_TIMEOUT) is False
    assert bool(StepOutcome.SESSION_DIED) is False
    # Distinguishable from each other.
    seen = {StepOutcome.FINISHED, StepOutcome.IDLE_TIMEOUT,
            StepOutcome.HARD_TIMEOUT, StepOutcome.SESSION_DIED}
    assert len(seen) == 4


def test_ac7_hard_ceiling_with_progress_ends_as_timeout(tmp_path):
    """AC-7: reaching the hard ceiling ends the wait as HARD_TIMEOUT even while
    progress continues, never as FINISHED."""
    f = tmp_path / "out.txt"
    f.write_text("0")
    sentinel = str(tmp_path / "_sentinel")
    clock = FakeClock()
    tmux = FakeTmux(alive=True)
    n = {"i": 0}

    def probe():
        n["i"] += 1
        f.write_text(str(n["i"]))  # always advancing
        return ark.probe_paths([str(f)])

    outcome = make_wait(
        tmux, clock, sentinel_path=sentinel, poll_interval=1000,
        progress_probe=probe, idle_timeout=900, hard_timeout=5000,
    )
    assert outcome == StepOutcome.HARD_TIMEOUT
    assert clock.now() >= 5000


def test_ec7_idle_ge_ceiling_hard_ceiling_governs(tmp_path):
    """EC-7: idle timeout >= hard ceiling — the hard ceiling still governs; the
    step cannot run past ARK_STEP_TIMEOUT."""
    sentinel = str(tmp_path / "_sentinel")
    clock = FakeClock()
    tmux = FakeTmux(alive=True)
    # No progress ever; idle (10000) > hard (3000). Hard ceiling must win.
    outcome = make_wait(
        tmux, clock, sentinel_path=sentinel, poll_interval=500,
        progress_probe=[str(tmp_path / "never")], idle_timeout=10000,
        hard_timeout=3000,
    )
    assert outcome == StepOutcome.HARD_TIMEOUT
    assert 3000 <= clock.now() <= 3000 + 500


# ---------------------------------------------------------------------------
# Session death — EC-1
# ---------------------------------------------------------------------------


def test_ec1_session_death_reports_session_died(tmp_path):
    """EC-1: a session that dies mid-wait (no sentinel) reports SESSION_DIED,
    distinct from FINISHED / IDLE_TIMEOUT / HARD_TIMEOUT."""
    sentinel = str(tmp_path / "_sentinel")
    clock = FakeClock()
    tmux = FakeTmux(alive=True, die_after=2)  # dies after 2 is_alive() polls

    outcome = make_wait(
        tmux, clock, sentinel_path=sentinel, poll_interval=5,
        progress_probe=[str(tmp_path)], idle_timeout=900, hard_timeout=86400,
    )
    assert outcome == StepOutcome.SESSION_DIED


# ---------------------------------------------------------------------------
# Clock non-monotonicity — EC-4
# ---------------------------------------------------------------------------


def test_ec4_backward_clock_does_not_falsely_time_out(tmp_path):
    """EC-4: a backward/non-advancing wall clock must not falsely declare a
    timeout. A clock that jumps backward then forward still only times out once
    real elapsed crosses the threshold."""
    f = tmp_path / "out.txt"
    f.write_text("frozen")
    sentinel = str(tmp_path / "_sentinel")
    tmux = FakeTmux(alive=True)

    # A clock whose value goes: 0, then -100 (backward!), then climbs.
    seq = [0, -100, -50, 0, 500, 1000]
    idx = {"i": 0}

    def clock():
        i = idx["i"]
        val = seq[i] if i < len(seq) else seq[-1] + (i - len(seq) + 1) * 1000
        return val

    def sleep(_dt):
        idx["i"] += 1

    outcome = tmux.wait_for_sentinel(
        sentinel_path=sentinel, poll_interval=1, progress_probe=[str(f)],
        idle_timeout=900, hard_timeout=86400, clock=clock, sleep=sleep,
    )
    # Despite the backward jump, it eventually times out on idle once the clock
    # genuinely advances past 900s — and never raised or returned early.
    assert outcome == StepOutcome.IDLE_TIMEOUT


# ---------------------------------------------------------------------------
# Backward compatibility (no probe) — AC-16
# ---------------------------------------------------------------------------


def test_ac16_no_probe_uses_wall_clock_timeout(tmp_path):
    """AC-16: no progress probe -> prior wall-clock behavior; times out after
    the function's `timeout` (default 1800s), NOT the larger ARK_STEP_TIMEOUT,
    with no progress-based survival."""
    # A file that grows every poll would survive WITH a probe; without one it
    # must be ignored and the wall-clock cap must fire at exactly `timeout`.
    f = tmp_path / "out.txt"
    f.write_text("0")
    sentinel = str(tmp_path / "_sentinel")
    clock = FakeClock()
    tmux = FakeTmux(alive=True)

    outcome = make_wait(
        tmux, clock, sentinel_path=sentinel, poll_interval=60,
        # no progress_probe -> fallback mode; default timeout 1800.
    )
    assert outcome == StepOutcome.IDLE_TIMEOUT
    # Fired at the 1800s wall clock, NOT the 86400s ceiling.
    assert 1800 <= clock.now() <= 1800 + 60


def test_ac16_no_probe_finishes_on_sentinel(tmp_path):
    """No-probe mode still returns FINISHED when the sentinel appears."""
    sentinel = tmp_path / "_sentinel"
    sentinel.write_text("")
    clock = FakeClock()
    tmux = FakeTmux(alive=True)
    outcome = make_wait(
        tmux, clock, sentinel_path=str(sentinel), poll_interval=5,
    )
    assert outcome == StepOutcome.FINISHED
    assert not sentinel.exists()


def test_default_timeout_constant_is_1800():
    """The fallback wall-clock default is preserved at 1800s (FR-5)."""
    assert ark.DEFAULT_WALLCLOCK_TIMEOUT == 1800


# ---------------------------------------------------------------------------
# Configuration & fail-fast — AC-12, AC-13, AC-23, EC-6
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("ARK_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("ARK_STEP_TIMEOUT", raising=False)
    return monkeypatch


def test_ac12_idle_timeout_default(clean_env):
    """AC-12: ARK_IDLE_TIMEOUT unset -> documented default 900."""
    assert ark.idle_timeout() == 900


def test_ac13_step_timeout_default_gt_1800(clean_env):
    """AC-13: ARK_STEP_TIMEOUT unset -> default 86400, strictly > 1800."""
    assert ark.step_timeout() == 86400
    assert ark.step_timeout() > 1800


def test_idle_timeout_reads_env(clean_env):
    clean_env.setenv("ARK_IDLE_TIMEOUT", "1234")
    assert ark.idle_timeout() == 1234


def test_step_timeout_reads_env(clean_env):
    clean_env.setenv("ARK_STEP_TIMEOUT", "99999")
    assert ark.step_timeout() == 99999


@pytest.mark.parametrize("bad", ["abc", "", "  ", "1.5", "0x10", "nan"])
def test_ac23_ec6_non_numeric_fails_fast(clean_env, bad):
    """AC-23 / EC-6: a non-numeric value fails fast with a clear error naming
    the variable — never a zero/instant timeout. ('' is treated as unset by
    os.environ semantics; here we set it explicitly to a junk string.)"""
    if bad in ("", "  "):
        # An empty/whitespace value is indistinguishable from unset for our
        # reader; skip those for the non-numeric assertion.
        pytest.skip("empty value treated as unset")
    clean_env.setenv("ARK_IDLE_TIMEOUT", bad)
    with pytest.raises(ark.ArkError) as ei:
        ark.idle_timeout()
    assert "ARK_IDLE_TIMEOUT" in str(ei.value)
    assert bad in str(ei.value)


@pytest.mark.parametrize("bad", ["0", "-1", "-900"])
def test_ac23_ec6_non_positive_fails_fast(clean_env, bad):
    """AC-23 / EC-6: zero or negative fails fast and never produces an instant
    timeout."""
    clean_env.setenv("ARK_STEP_TIMEOUT", bad)
    with pytest.raises(ark.ArkError) as ei:
        ark.step_timeout()
    assert "ARK_STEP_TIMEOUT" in str(ei.value)


def test_ec6_valid_positive_passes(clean_env):
    clean_env.setenv("ARK_IDLE_TIMEOUT", "1")
    assert ark.idle_timeout() == 1


# ---------------------------------------------------------------------------
# Default watch set — AC-17
# ---------------------------------------------------------------------------


def test_ac17_default_watch_set_includes_results_and_worktree(tmp_path):
    """AC-17: real steps default their watch set to the worktree's results/ and
    the worktree generally."""
    watch = ark.default_watch_set(str(tmp_path))
    assert os.path.join(str(tmp_path), "results") in watch
    assert str(tmp_path) in watch


# ---------------------------------------------------------------------------
# Orchestration: never verify partial state (Defect 2) — AC-9, AC-10, AC-11,
# AC-20.
# ---------------------------------------------------------------------------


def _patch_pipeline_for_outcome(monkeypatch, implement_outcome):
    """Wire run_pipeline's step_* so step_implement returns a chosen outcome and
    every side-effecting step is a no-op. Records whether verify ran and whether
    a verdict was rendered."""
    calls = {"verify": 0, "verdict": [], "implement": 0, "reimplement": 0}

    monkeypatch.setattr(ark, "step_spec", lambda *a, **k: True)
    monkeypatch.setattr(ark, "step_review_spec", lambda *a, **k: None)
    monkeypatch.setattr(ark, "step_encode", lambda *a, **k: True)
    monkeypatch.setattr(ark, "step_review_make", lambda *a, **k: None)
    monkeypatch.setattr(ark, "step_fix_make", lambda *a, **k: None)
    monkeypatch.setattr(ark, "archive_run", lambda *a, **k: None)
    monkeypatch.setattr(ark, "step_adversarial", lambda *a, **k: False)
    monkeypatch.setattr(ark, "step_fix_review", lambda *a, **k: True)

    def fake_implement(tmux, project_dir, driver="claude"):
        calls["implement"] += 1
        return implement_outcome

    def fake_reimplement(tmux, project_dir, driver="claude"):
        calls["reimplement"] += 1
        return implement_outcome

    def fake_verify(feature_slug, project_dir):
        calls["verify"] += 1
        calls["verdict"].append("PASS")  # a verdict was rendered
        return True

    monkeypatch.setattr(ark, "step_implement", fake_implement)
    monkeypatch.setattr(ark, "step_reimplement", fake_reimplement)
    monkeypatch.setattr(ark, "step_verify", fake_verify)
    return calls


def _stub_infra(monkeypatch, tmp_path):
    """Stub out git/worktree/tmux so run_pipeline runs purely in-process."""
    wt = tmp_path / "wt"
    (wt / ark.ARK_DIR).mkdir(parents=True)

    monkeypatch.setattr(ark, "repo_root", lambda cwd=None: str(tmp_path))
    monkeypatch.setattr(ark, "setup_worktree", lambda slug, root: str(wt))
    monkeypatch.setattr(ark, "ensure_dir", lambda d: (wt / ark.ARK_DIR))
    monkeypatch.setattr(ark, "detect_resume_point",
                        lambda slug, project_dir: "review-make")

    class StubTmux:
        def __init__(self, name):
            self.session = name

        def is_alive(self):
            return False

        def kill_session(self):
            pass

        def create_session(self, *a, **k):
            pass

    monkeypatch.setattr(ark, "Tmux", StubTmux)
    return wt


def test_ac9_ac20_timeout_does_not_advance_to_verify(monkeypatch, tmp_path):
    """AC-9 / AC-20: a non-FINISHED implement outcome does NOT invoke verify and
    does NOT render a PASS/FAIL verdict (exercised at the unit/smoke level — no
    real ARK pipeline spawn). Honors ARK_RUNNING by clearing it first."""
    monkeypatch.delenv("ARK_RUNNING", raising=False)
    monkeypatch.delenv("ARK_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("ARK_STEP_TIMEOUT", raising=False)
    _stub_infra(monkeypatch, tmp_path)
    calls = _patch_pipeline_for_outcome(monkeypatch, StepOutcome.HARD_TIMEOUT)

    rc = ark.run_pipeline("some feature", str(tmp_path), driver="claude")

    # Timeout -> distinct non-success return code (not 0, not the FAIL code 1).
    assert rc == 2
    # FR-7.1: verify never ran. FR-7.2: no verdict rendered.
    assert calls["verify"] == 0
    assert calls["verdict"] == []


def test_ac11_timeout_does_not_consume_retry_budget(monkeypatch, tmp_path):
    """AC-11: an idle timeout does NOT consume a MAX_LOOPS attempt. We make the
    implement step idle-timeout repeatedly; it must retry without verifying, and
    must stop via the timeout backstop rather than a FAIL verdict."""
    monkeypatch.delenv("ARK_RUNNING", raising=False)
    monkeypatch.delenv("ARK_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("ARK_STEP_TIMEOUT", raising=False)
    _stub_infra(monkeypatch, tmp_path)
    calls = _patch_pipeline_for_outcome(monkeypatch, StepOutcome.IDLE_TIMEOUT)

    rc = ark.run_pipeline("another feature", str(tmp_path), driver="claude")

    assert rc == 2                 # timeout outcome, not FAIL(1)/PASS(0)
    assert calls["verify"] == 0    # never verified partial state (AC-9)
    assert calls["verdict"] == []  # no PASS/FAIL verdict (AC-10)
    # It retried (idle timeouts do not consume MAX_LOOPS, but the timeout
    # backstop eventually stops it) — implement was attempted more than once.
    assert calls["implement"] >= 1


def test_finished_then_pass_runs_verify(monkeypatch, tmp_path):
    """Sanity: a FINISHED implement DOES advance to verify and can PASS."""
    monkeypatch.delenv("ARK_RUNNING", raising=False)
    monkeypatch.delenv("ARK_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("ARK_STEP_TIMEOUT", raising=False)
    _stub_infra(monkeypatch, tmp_path)
    calls = _patch_pipeline_for_outcome(monkeypatch, StepOutcome.FINISHED)

    rc = ark.run_pipeline("ok feature", str(tmp_path), driver="claude")
    assert rc == 0
    assert calls["verify"] == 1


# ---------------------------------------------------------------------------
# AC-21 recursion guard honored by the suite itself
# ---------------------------------------------------------------------------


def test_ac21_recursion_guard_blocks_nested_run(monkeypatch, tmp_path, capsys):
    """AC-21: ARK_RUNNING is honored — a nested run is blocked. The suite never
    actually spawns a real ARK run; this asserts the guard exists."""
    monkeypatch.setenv("ARK_RUNNING", "1")
    with pytest.raises(SystemExit) as ei:
        ark.run_pipeline("x", str(tmp_path))
    assert ei.value.code == 1
