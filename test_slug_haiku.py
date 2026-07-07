"""Tests for Haiku-generated slugs (SPEC: FR-1..FR-10, AC-1..AC-19, EC-1..EC-13).

Two flavors:
  * Pure/seam tests — sanitizer + fallback + the injectable model seam
    (`ark._haiku_title`), monkeypatched to return a canned title or to
    fail/hang. No git, no tmux, no real model call.
  * Executed round-trips — a REAL git repo + REAL `ark.setup_worktree` /
    `ark.find_ark_worktrees`, so the resume path (`derive_slug` ->
    `_persisted_slug_for_feature` -> `git worktree list`) is exercised for
    real rather than via grep-for-symbol proxies (SPEC §10).

The round-trips deliberately DO NOT call `run_pipeline` (which would launch
agents/tmux). Instead they replicate exactly what run_pipeline persists — the
worktree, `.ark/FEATURE.md`, and `.ark/SLUG` — then re-derive, which is the
precise behavior AC-10/AC-11/AC-19 pin down.

Run:  pytest test_slug_haiku.py
"""

import re
import subprocess

import pytest

import ark


SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


# The long paragraph-style feature from SPEC §1.
TCORP_FEATURE = (
    "make this an api that is easy for agents to use it should be easy to list "
    "and create tickets ideally we built a tcorp bash tool where agents can run "
    "tcorp new title"
)


def base_of(slug):
    """Split a final slug into (base, suffix); suffix is the trailing hex hash."""
    assert "-" in slug
    base, suffix = slug.rsplit("-", 1)
    return base, suffix


# ---------------------------------------------------------------------------
# §4 format constraints — sanitizer + fallback (AC-1/2/3, EC-2/3/4/5/9/12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,expected", [
    ("Add Auth Login Flow", "add-auth-login-flow"),
    ("  spaced   out  ", "spaced-out"),
    ("Trailing hyphen-", "trailing-hyphen"),
    ("-leading and repeated--hyphens", "leading-and-repeated-hyphens"),
    ("Slash/And:Punct!uation?", "slash-and-punct-uation"),
    ("CamelCase123 mixed", "camelcase123-mixed"),
])
def test_sanitize_slug_base_basic(title, expected):
    assert ark.sanitize_slug_base(title) == expected


def test_ac3_sanitize_caps_words_and_chars():
    # More than 4 words -> only first 4 (EC-5).
    assert ark.sanitize_slug_base("one two three four five six") == "one-two-three-four"


def test_ac3_sanitize_caps_chars_no_trailing_hyphen():
    # A single very long token gets truncated to <= 48 chars, no trailing hyphen.
    base = ark.sanitize_slug_base("a" * 60)
    assert len(base) <= ark.SLUG_MAX_CHARS
    assert not base.endswith("-")
    # Four long words: truncation must not leave a dangling hyphen (EC-5/EC-9).
    base = ark.sanitize_slug_base("aaaaaaaaaaaa bbbbbbbbbbbb cccccccccccc dddddddddddd")
    assert len(base) <= ark.SLUG_MAX_CHARS
    assert not base.endswith("-")
    assert SLUG_RE.match(base) or base == ""


@pytest.mark.parametrize("title", ["", "   ", "!!!", "😀🎉", "。、・"])
def test_ec4_ec12_unusable_titles_sanitize_to_empty(title):
    # Empty / only-illegal / only-unicode -> empty base so caller falls back.
    assert ark.sanitize_slug_base(title) == ""


def test_ec5_unicode_never_splits_into_invalid_form():
    base = ark.sanitize_slug_base("café über señor niño")
    assert SLUG_RE.match(base) or base == ""


# ---------------------------------------------------------------------------
# Fallback base is byte-for-byte the historic slugify base (FR-7, AC-13, EC-2)
# ---------------------------------------------------------------------------


def test_fallback_base_matches_first_four_words():
    assert ark._fallback_base("Add auth login flow extra words here") == "add-auth-login-flow"


def test_ec2_no_alnum_words_falls_back_to_literal_feature():
    assert ark._fallback_base("!!! ??? ...") == "feature"


def test_slugify_is_pure_fallback_and_deterministic():
    # slugify() must NEVER call the model; it is the deterministic fallback.
    s1 = ark.slugify(TCORP_FEATURE)
    s2 = ark.slugify(TCORP_FEATURE)
    assert s1 == s2
    base, suffix = base_of(s1)
    assert base == ark._fallback_base(TCORP_FEATURE)
    assert suffix == ark.slug_suffix(TCORP_FEATURE)


# ---------------------------------------------------------------------------
# Final slug always satisfies §4 (AC-1/2/7/8) regardless of path
# ---------------------------------------------------------------------------


def test_ac1_ac2_final_slug_format(monkeypatch, tmp_path):
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: "A Nice Title!!!")
    slug = ark.derive_slug(TCORP_FEATURE, str(tmp_path))
    assert slug
    assert SLUG_RE.match(slug), slug


def test_ac7_final_slug_is_valid_git_ref(monkeypatch, tmp_path):
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: "make api for agents")
    slug = ark.derive_slug(TCORP_FEATURE, str(tmp_path))
    rc = subprocess.run(
        ["git", "check-ref-format", "--branch", f"ark/{slug}"],
        capture_output=True, text=True,
    ).returncode
    assert rc == 0, slug


def test_ac8_final_slug_is_valid_dir_name(monkeypatch, tmp_path):
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: "make api for agents")
    slug = ark.derive_slug(TCORP_FEATURE, str(tmp_path))
    assert "/" not in slug and "\0" not in slug
    assert slug not in (".", "..")
    assert len(f"ark/{slug}") < 255


# ---------------------------------------------------------------------------
# AC-9 — long paragraph summarizes short, not first-4-words-of-paragraph
# ---------------------------------------------------------------------------


def test_ac9_paragraph_base_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: "tcorp ticket api")
    slug = ark.derive_slug(TCORP_FEATURE, str(tmp_path))
    base, _ = base_of(slug)
    assert base == "tcorp-ticket-api"
    assert len(base.split("-")) <= ark.SLUG_MAX_WORDS
    assert len(base) <= ark.SLUG_MAX_CHARS


# ---------------------------------------------------------------------------
# AC-12 — canned title path (seam returns a sensible title)
# ---------------------------------------------------------------------------


def test_ac12_canned_title_base_differs_from_fallback(monkeypatch, tmp_path):
    feature = "please implement a comprehensive rate limiter for the gateway"
    title = "gateway rate limiter"
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: title)
    slug = ark.derive_slug(feature, str(tmp_path))
    base, _ = base_of(slug)
    assert base == ark.sanitize_slug_base(title) == "gateway-rate-limiter"
    # And it is NOT the first-four-words fallback for the same input.
    assert base != ark._fallback_base(feature)


# ---------------------------------------------------------------------------
# AC-13 — seam forced to fail -> byte-for-byte fallback base + unchanged suffix
# ---------------------------------------------------------------------------


def test_ac13_seam_failure_falls_back(monkeypatch, tmp_path):
    # The seam owns failure handling and returns None on failure (its documented
    # contract); derive_slug must then produce the exact historic fallback.
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: None)
    slug = ark.derive_slug(TCORP_FEATURE, str(tmp_path))
    assert slug == ark.slugify(TCORP_FEATURE)  # exact fallback, base + suffix
    base, suffix = base_of(slug)
    assert base == ark._fallback_base(TCORP_FEATURE)
    assert suffix == ark.slug_suffix(TCORP_FEATURE)


def test_ac13_empty_and_garbage_output_falls_back(monkeypatch, tmp_path):
    for title in ["", "   ", "😀😀😀"]:
        monkeypatch.setattr(ark, "_haiku_title", lambda f, t, _tt=title: _tt)
        slug = ark.derive_slug(TCORP_FEATURE, str(tmp_path))
        assert slug == ark.slugify(TCORP_FEATURE)


# ---------------------------------------------------------------------------
# AC-14 — a hanging seam is bounded by ARK_SLUG_TIMEOUT (executed)
# ---------------------------------------------------------------------------


def test_ac14_hanging_seam_bounded_and_falls_back(monkeypatch, tmp_path):
    # The REAL seam is `subprocess.run([...], timeout=...)`. Force the underlying
    # subprocess.run to raise TimeoutExpired so we exercise ark's real timeout
    # handling (not a stub of _haiku_title), and assert we reach the fallback.
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(ark.subprocess, "run", fake_run)
    monkeypatch.setenv("ARK_SLUG_TIMEOUT", "3")
    slug = ark.derive_slug(TCORP_FEATURE, str(tmp_path))
    assert calls["timeout"] == 3  # the bound was actually passed to subprocess
    assert slug == ark.slugify(TCORP_FEATURE)  # hung -> fallback form


def test_seam_returns_none_on_missing_cli(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("claude: not found")
    monkeypatch.setattr(ark.subprocess, "run", fake_run)
    assert ark._haiku_title("anything", 5) is None


def test_seam_returns_none_on_nonzero_exit(monkeypatch):
    class R:
        returncode = 1
        stdout = "whatever"
    monkeypatch.setattr(ark.subprocess, "run", lambda cmd, **k: R())
    assert ark._haiku_title("anything", 5) is None


# ---------------------------------------------------------------------------
# AC-5 / AC-6 — determinism and distinctness of the suffix
# ---------------------------------------------------------------------------


def test_ac5_identical_text_identical_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: "same title")
    a = ark.derive_slug("identical feature text", str(tmp_path))
    b = ark.derive_slug("identical feature text", str(tmp_path))
    assert a == b


def test_ac6_same_base_different_text_still_distinct(monkeypatch, tmp_path):
    # Two DIFFERENT features whose model titles sanitize to the same base must
    # still produce distinct slugs, because the suffix is feature-text-derived.
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: "shared base title")
    a = ark.derive_slug("feature one text", str(tmp_path))
    b = ark.derive_slug("feature two text — different", str(tmp_path))
    abase, _ = base_of(a)
    bbase, _ = base_of(b)
    assert abase == bbase          # same model-derived base
    assert a != b                  # ...but distinct overall (suffix differs)


def test_ac4_suffix_not_derived_from_model_output(monkeypatch, tmp_path):
    # Changing the model title must NOT change the suffix (suffix = f(text)).
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: "title one")
    a = ark.derive_slug("stable feature text", str(tmp_path))
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: "completely other title")
    # Fresh tmp dir has no persisted run, so this re-derives; only the suffix is
    # asserted, which must be identical because it is text-derived.
    _, sa = base_of(a)
    assert sa == ark.slug_suffix("stable feature text")


# ---------------------------------------------------------------------------
# AC-15 — no feature text / secrets leaked to stdout/stderr by generation
# ---------------------------------------------------------------------------


def test_ac15_no_side_effect_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ark, "_haiku_title", lambda f, t: "quiet title")
    ark.derive_slug("SECRET-feature-text-xyz", str(tmp_path))
    out = capsys.readouterr()
    assert "SECRET-feature-text-xyz" not in out.out
    assert "SECRET-feature-text-xyz" not in out.err
    assert out.out == "" and out.err == ""


# ---------------------------------------------------------------------------
# Executed round-trips against a REAL git repo (AC-10/11/18/19, EC-10/11)
# ---------------------------------------------------------------------------


def _init_repo(path):
    def g(*args):
        r = subprocess.run(["git", *args], cwd=str(path), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r
    g("init", "-q")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    g("commit", "--allow-empty", "-q", "-m", "root")
    return g


def _create_run(root, feature, title):
    """Replicate what run_pipeline persists for a NEW run, using real git.

    Derives the slug (Haiku path via the given title), makes the real worktree,
    and writes .ark/FEATURE.md + .ark/SLUG exactly as run_pipeline does.
    Returns (slug, worktree_path).
    """
    import os
    orig = ark._haiku_title
    ark._haiku_title = lambda f, t, _tt=title: _tt
    try:
        slug = ark.derive_slug(feature, str(root))
    finally:
        ark._haiku_title = orig
    wt = ark.setup_worktree(slug, str(root))
    ark_dir = ark.ensure_dir(wt)
    (ark_dir / "FEATURE.md").write_text(feature)
    (ark_dir / "SLUG").write_text(slug)
    return slug, wt


@pytest.fixture
def repo(tmp_path):
    _init_repo(tmp_path)
    return tmp_path


def test_ac10_ac11_second_derivation_reuses_run(repo):
    feature = "build a durable job queue with retries and backoff"
    slug1, wt1 = _create_run(repo, feature, "durable job queue")

    # Re-derive with a DIFFERENT title (or a failing seam): must yield the SAME
    # slug and must NOT create a second worktree/branch.
    import os
    orig = ark._haiku_title
    ark._haiku_title = lambda f, t: "totally different title now"
    try:
        slug2 = ark.derive_slug(feature, str(repo))
    finally:
        ark._haiku_title = orig
    assert slug2 == slug1  # byte-identical (AC-5/AC-19)

    runs = ark.find_ark_worktrees(str(repo))
    assert [s for s, _ in runs].count(slug1) == 1
    assert len(runs) == 1

    # Exactly one branch ark/<slug>.
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", f"ark/{slug1}"],
        capture_output=True, text=True,
    ).stdout
    assert branches.count(f"ark/{slug1}") == 1


def test_ac19_nondeterministic_seam_survives_continue(repo):
    """Model returns DIFFERENT titles on successive calls -> slug is stable."""
    feature = "add a metrics dashboard for the pipeline"
    slug1, wt1 = _create_run(repo, feature, "metrics dashboard")

    # `ark continue` re-reads FEATURE.md and re-derives. Even if the seam would
    # now return a different title, the persisted slug wins (model off resume).
    import os
    orig = ark._haiku_title
    ark._haiku_title = lambda f, t: "an entirely new name"
    try:
        slug2 = ark.derive_slug(feature, str(repo))
    finally:
        ark._haiku_title = orig

    assert slug2 == slug1
    wt2 = ark.setup_worktree(slug2, str(repo))
    assert str(ark.Path(wt2).resolve()) == str(ark.Path(wt1).resolve())
    assert len(ark.find_ark_worktrees(str(repo))) == 1


def test_ac18_old_style_run_without_slug_file_resumes(repo):
    """A run created BEFORE this change has no .ark/SLUG; its dir name is the slug.

    Re-derivation must return that old fallback slug, unchanged, and not orphan
    the run — because the fallback base is byte-identical to the historic form.
    """
    feature = "legacy feature created the old way"
    old_slug = ark.slugify(feature)  # historic first-4-words + suffix
    wt = ark.setup_worktree(old_slug, str(repo))
    ark_dir = ark.ensure_dir(wt)
    (ark_dir / "FEATURE.md").write_text(feature)
    # NOTE: deliberately NO .ark/SLUG file.

    # Even with a live seam that would summarize differently, resume must find
    # the old run by dir name.
    import os
    orig = ark._haiku_title
    ark._haiku_title = lambda f, t: "shiny new summary"
    try:
        rederived = ark.derive_slug(feature, str(repo))
    finally:
        ark._haiku_title = orig

    assert rederived == old_slug
    assert len(ark.find_ark_worktrees(str(repo))) == 1


def test_ec10_identical_feature_not_duplicated(repo):
    feature = "identical feature that must reuse its run"
    slug1, _ = _create_run(repo, feature, "reuse this run")
    # A second "ark new" for the same text: reuse, not duplicate.
    slug2 = ark.derive_slug(feature, str(repo))
    assert slug2 == slug1
    assert len(ark.find_ark_worktrees(str(repo))) == 1
