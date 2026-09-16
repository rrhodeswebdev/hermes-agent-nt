"""The DISTILLED tier must not truncate silently.

Every other tier announces its loss: notes render "(… N older notes omitted — full history
in the archive)" and the raw-lessons tier renders "(… N more lessons over the prompt
budget)", and both feed the [learned] truncation report. The distilled tier was a bare
`distilled[:lessons_chars]` — it would cut mid-sentence with no banner and no counter, so
the HARD RULES could start silently losing their tail once curation pushed the document
past the budget. Measured 2026-09-10: distilled.md was 2229 chars against a 2500 budget,
i.e. ~271 from failing invisibly.
"""

from hermes_bridge.memory import LearnedStore


def _mem(tmp_path, distilled: str) -> LearnedStore:
    (tmp_path / "distilled.md").write_text(distilled, encoding="utf-8")
    return LearnedStore(str(tmp_path))


def test_distilled_under_budget_is_untouched(tmp_path):
    body = "## HARD RULES\n- **Location clearance:** keep 1xATR.\n"
    out = _mem(tmp_path, body).format_for_prompt(lessons_chars=2500)
    assert "Location clearance" in out
    assert "over the prompt budget" not in out


def test_distilled_over_budget_announces_the_loss(tmp_path):
    body = "## HARD RULES\n" + ("- **Rule:** " + "x" * 120 + "\n") * 40  # ~5k chars
    out = _mem(tmp_path, body).format_for_prompt(lessons_chars=600)
    assert "=== DISTILLED LESSONS ===" in out
    assert "over the prompt budget" in out, "a silent cut is the defect"
    assert "curation" in out


def test_distilled_result_still_respects_the_budget(tmp_path):
    # The banner must be RESERVED, not appended past the cap.
    body = "## HARD RULES\n" + ("- **Rule:** " + "y" * 120 + "\n") * 40
    budget = 600
    out = _mem(tmp_path, body).format_for_prompt(lessons_chars=budget)
    block = out.split("=== DISTILLED LESSONS ===\n", 1)[1]
    assert len(block) <= budget, f"block {len(block)} exceeded budget {budget}"


def test_distilled_truncation_reaches_the_operator_report(tmp_path, capsys):
    body = "## HARD RULES\n" + ("- **Rule:** " + "z" * 120 + "\n") * 40
    _mem(tmp_path, body).format_for_prompt(lessons_chars=600)
    assert "distilled_dropped=" in capsys.readouterr().out


def test_no_distilled_file_keeps_the_raw_lessons_path(tmp_path):
    # Guard the existing behavior: with no distilled.md the raw lessons tier still runs.
    out = LearnedStore(str(tmp_path)).format_for_prompt(lessons_chars=2500)
    assert "DISTILLED LESSONS" not in out


# --- the TRADER PROFILE tier must not cut mid-sentence either ----------------------- #
def test_profile_over_budget_cuts_at_a_boundary_with_a_marker(tmp_path):
    # `p[:profile_chars]` was a bare slice: on 2026-09-15 it dropped the actionable half of
    # the operator's roll-week note mid-sentence ("... while ") with no banner and no counter.
    body = "# Trader Profile\n" + "".join(
        f"- Guidance {i}: hold the line and wait. " + "x" * 60 + "\n" for i in range(12))
    (tmp_path / "trader-profile.md").write_text(body, encoding="utf-8")
    out = LearnedStore(str(tmp_path)).format_for_prompt(profile_chars=400)
    sec = out.split("=== TRADER PROFILE ===\n", 1)[1].split("\n\n===", 1)[0]
    assert len(sec) <= 400
    assert sec.rstrip().endswith("…"), "a cut profile must carry the truncation marker"
    # cut lands on a line/sentence boundary, never inside a word
    kept = sec.rstrip("…\n")
    assert kept.endswith(("\n", ".", "x")), f"cut mid-word: {kept[-30:]!r}"


def test_profile_truncation_reaches_the_operator_report(tmp_path, capsys):
    body = "# Trader Profile\n" + "".join(f"- Guidance {i}. " + "y" * 80 + "\n" for i in range(12))
    (tmp_path / "trader-profile.md").write_text(body, encoding="utf-8")
    LearnedStore(str(tmp_path)).format_for_prompt(profile_chars=400)
    assert "profile_dropped=" in capsys.readouterr().out


def test_profile_within_budget_is_untouched(tmp_path):
    body = "# Trader Profile\n- Sim only. Never assume live.\n"
    (tmp_path / "trader-profile.md").write_text(body, encoding="utf-8")
    out = LearnedStore(str(tmp_path)).format_for_prompt(profile_chars=1400)
    sec = out.split("=== TRADER PROFILE ===")[1].split("===")[0]
    assert "Never assume live." in out and "…" not in sec
