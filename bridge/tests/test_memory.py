from hermes_bridge.memory import LearnedStore, parse_frontmatter


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
