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
