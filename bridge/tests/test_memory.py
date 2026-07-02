from hermes_bridge.memory import LearnedStore, parse_frontmatter, truncate_at_boundary


def test_parse_frontmatter():
    meta, body = parse_frontmatter("---\nname: x\nstatus: active\n---\nBODY TEXT")
    assert meta["name"] == "x"
    assert meta["status"] == "active"
    assert body.strip() == "BODY TEXT"


def test_parse_frontmatter_none():
    meta, body = parse_frontmatter("just a body")
    assert meta == {}
    assert body == "just a body"


def test_format_for_prompt(tmp_path):
    (tmp_path / "trader-profile.md").write_text("Risk-averse; max 2 contracts.", encoding="utf-8")
    (tmp_path / "agent-notes.md").write_text("MNQ is whippy near the open.", encoding="utf-8")
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    (lessons / "dont-fade-open.md").write_text(
        "---\nname: dont-fade-open\nstatus: active\n---\nAvoid counter-trend in first 30m.",
        encoding="utf-8")
    (lessons / "retired.md").write_text(
        "---\nname: old\nstatus: retired\n---\nObsolete.", encoding="utf-8")
    out = LearnedStore(str(tmp_path)).format_for_prompt()
    assert "Risk-averse" in out
    assert "whippy" in out
    assert "dont-fade-open" in out
    assert "Obsolete" not in out  # retired lessons excluded


def test_format_for_prompt_empty_dir(tmp_path):
    assert LearnedStore(str(tmp_path / "nope")).format_for_prompt() == ""


def test_truncate_within_limit_unchanged():
    assert truncate_at_boundary("short text", 100) == "short text"


def test_truncate_zero_or_negative_limit():
    assert truncate_at_boundary("anything", 0) == ""
    assert truncate_at_boundary("anything", -5) == ""


def test_truncate_prefers_bullet_boundary():
    text = "### RULES\n- first bullet stays\n- second bullet stays\n- " + "x" * 200
    out = truncate_at_boundary(text, 80)
    assert len(out) <= 80
    assert out.endswith("…")
    # the partial third bullet is dropped entirely; the second survives whole
    assert "- second bullet stays" in out
    assert "xxx" not in out


def test_truncate_sentence_fallback():
    text = "First sentence here. Second sentence here. " + "word " * 50
    out = truncate_at_boundary(text, 60)
    assert len(out) <= 60
    body = out.removesuffix("\n…")
    assert body.endswith(".")


def test_truncate_whitespace_fallback_never_cuts_mid_word():
    text = "alpha beta gamma delta epsilon zeta " * 10  # no sentences, no bullets
    out = truncate_at_boundary(text, 50)
    assert len(out) <= 50
    body = out.removesuffix("\n…")
    # ends exactly at a word from the vocabulary — no partial word
    assert body.split()[-1] in {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}


def test_truncate_hard_fallback_single_token():
    out = truncate_at_boundary("A" * 500, 40)
    assert len(out) <= 40
    assert out.endswith("…")


def test_truncate_tiny_limits_respect_length_contract():
    for limit in (1, 2, 3):
        for text in ("ab", "abc", "a b c", "word " * 10):
            out = truncate_at_boundary(text, limit)
            assert len(out) <= limit, (text, limit, out)


def _store_with_reviews(tmp_path, bodies):
    """LearnedStore over a tmp learned dir seeded with day-reviews (bodies newest first,
    written in the same newest-first file order append_day_review produces)."""
    from hermes_bridge.memory import LearnedStore
    d = tmp_path / "learned"
    d.mkdir()
    sections = [f"## 2026-07-{len(bodies) - i:02d}\n{b}" for i, b in enumerate(bodies)]
    (d / "day-reviews.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    return LearnedStore(str(d))


def test_day_reviews_small_all_included(tmp_path):
    ls = _store_with_reviews(tmp_path, ["tiny review one", "tiny review two"])
    out = ls.format_for_prompt(1400, 2200, 2500, day_reviews_n=10, day_reviews_chars=4000)
    assert "=== RECENT DAY-REVIEWS ===" in out
    assert "tiny review one" in out and "tiny review two" in out


def test_day_reviews_oversized_newest_truncates_to_fit(tmp_path):
    big = "word " * 700  # ~3500 chars, larger than the 1800 budget below
    ls = _store_with_reviews(tmp_path, [big])
    out = ls.format_for_prompt(1400, 2200, 2500, day_reviews_n=10, day_reviews_chars=1800)
    assert "=== RECENT DAY-REVIEWS ===" in out  # never an empty section
    section = out.split("=== RECENT DAY-REVIEWS ===\n", 1)[1]
    assert len(section) <= 1800
    assert section.endswith("…")


def test_day_reviews_older_dropped_newest_kept_whole(tmp_path):
    ls = _store_with_reviews(tmp_path, ["newest fits fine", "y" * 3000])
    out = ls.format_for_prompt(1400, 2200, 2500, day_reviews_n=10, day_reviews_chars=1800)
    assert "newest fits fine" in out
    assert "yyy" not in out


def test_day_reviews_default_budget_fits_real_sized_review(tmp_path):
    from hermes_bridge.config import LearningConfig
    lc = LearningConfig()
    big = "z" * 3103  # today's largest real review body
    ls = _store_with_reviews(tmp_path, [big])
    out = ls.format_for_prompt(1400, 2200, 2500, day_reviews_n=lc.day_review_keep,
                               day_reviews_chars=lc.day_review_char_limit)
    assert "z" * 3103 in out  # fits whole under the new default


def test_corpus_mtime_includes_day_reviews(tmp_path):
    from hermes_bridge.memory import LearnedStore
    d = tmp_path / "learned"
    d.mkdir()
    ls = LearnedStore(str(d))
    assert ls.corpus_mtime() == 0.0
    ls.append_day_review("2026-07-01", "a review body\n\n_theme: some_theme_", keep=10)
    assert ls.corpus_mtime() > 0.0
    assert ls.corpus_mtime() >= ls.day_reviews_mtime()
