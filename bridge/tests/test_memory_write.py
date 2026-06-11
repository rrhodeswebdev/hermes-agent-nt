from hermes_bridge.memory import LearnedStore, parse_frontmatter


def test_set_profile_and_append_note(tmp_path):
    s = LearnedStore(str(tmp_path))
    s.set_profile("New profile body.")
    assert "New profile body." in s.profile()
    s.append_note("first obs")
    s.append_note("second obs")
    notes = s.notes()
    assert "first obs" in notes and "second obs" in notes


def test_apply_lesson_create_update_retire(tmp_path):
    s = LearnedStore(str(tmp_path))
    s.apply_lesson("create", "Dont Fade Open", body="Avoid counter-trend first 30m.",
                   regime_tags=["trend-up"])
    ls = s.lessons()
    assert len(ls) == 1 and ls[0].name == "Dont Fade Open"
    assert "Avoid counter-trend" in ls[0].body

    s.apply_lesson("update", "Dont Fade Open", body="Updated body.", regime_tags=["trend-up"])
    assert "Updated body." in s.lessons()[0].body

    s.apply_lesson("retire", "Dont Fade Open")
    assert s.lessons() == []  # retired excluded from active list
    f = tmp_path / "lessons" / "dont-fade-open.md"
    meta, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
    assert meta["status"] == "retired"


def test_apply_lesson_name_is_slugged_safely(tmp_path):
    s = LearnedStore(str(tmp_path))
    s.apply_lesson("create", "../../evil name!!", body="x")
    files = list((tmp_path / "lessons").glob("*.md"))
    assert len(files) == 1
    assert ".." not in files[0].name
