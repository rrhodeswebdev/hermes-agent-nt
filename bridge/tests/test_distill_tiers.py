"""Per-tier budgets for the distilled artifact, enforced in CODE.

DISTILL_SYSTEM asks for three sections -- hard rules, conditional heuristics (what WORKS,
with its regime/session conditions), then watch-items (patterns still gathering data with
evidence counts). Prose alone did not hold: ordering them produced hard-rules-only, and
after the sections were made REQUIRED with explicit character shares the model still wrote
12 hard-rule bullets to the heuristic tier's 1 and no watch-items at all.

So the split is applied deterministically after the model replies: the veto tier is capped
so it cannot crowd out the tiers that carry positive edge, and a missing tier is REPORTED
rather than silently absent -- an unattended session must be able to see it.
"""

from hermes_bridge.reflect import apportion_distilled

H = "## HARD RULES\n"
C = "## CONDITIONAL HEURISTICS\n"
W = "## WATCH-ITEMS\n"


def _bullets(n, ch="x", width=100):
    return "".join(f"- **Rule {i}:** {ch * width}\n" for i in range(n))


def test_hard_rules_cannot_crowd_out_the_positive_tiers():
    text = H + _bullets(40) + C + _bullets(10, "c") + W + _bullets(6, "w")
    out, rep = apportion_distilled(text, 4000)
    assert len(out) <= 4000
    # the veto tier is capped at its share, so the other two survive
    assert "CONDITIONAL HEURISTICS" in out and "WATCH-ITEMS" in out
    assert rep["dropped"]["HARD RULES"] > 0
    assert out.index("## CONDITIONAL HEURISTICS") < out.index("## WATCH-ITEMS")


def test_positive_tiers_are_not_truncated_when_they_fit():
    text = H + _bullets(40) + C + _bullets(2, "c") + W + _bullets(1, "w")
    out, rep = apportion_distilled(text, 4000)
    assert rep["dropped"].get("CONDITIONAL HEURISTICS", 0) == 0
    assert rep["dropped"].get("WATCH-ITEMS", 0) == 0
    assert "c" * 100 in out and "w" * 100 in out


def test_hard_rules_may_use_the_slack_when_the_other_tiers_are_small():
    # Budget must not be wasted: with tiny tiers 2/3, tier 1 keeps more than its bare share.
    small = H + _bullets(40) + C + "- tiny\n" + W + "- tiny\n"
    out, _ = apportion_distilled(small, 4000)
    strict = H + _bullets(40) + C + _bullets(10, "c") + W + _bullets(6, "w")
    out2, _ = apportion_distilled(strict, 4000)
    assert out.index("## CONDITIONAL HEURISTICS") > out2.index("## CONDITIONAL HEURISTICS")


def test_a_missing_tier_is_reported():
    text = H + _bullets(20)          # the historical failure shape: vetoes only
    out, rep = apportion_distilled(text, 4000)
    assert set(rep["missing"]) == {"CONDITIONAL HEURISTICS", "WATCH-ITEMS"}
    assert len(out) <= 4000


def test_no_sections_at_all_degrades_to_a_plain_truncate():
    text = _bullets(60)
    out, rep = apportion_distilled(text, 1000)
    assert len(out) <= 1000
    assert rep["missing"]  # still flags that the structure was absent


def test_bullets_are_never_cut_mid_line():
    text = H + _bullets(40) + C + _bullets(10, "c") + W + _bullets(6, "w")
    out, _ = apportion_distilled(text, 4000)
    for line in out.splitlines():
        if line.startswith("- **Rule"):
            assert line.rstrip().endswith(("x", "c", "w")), f"cut mid-bullet: {line[-40:]}"


def test_result_never_exceeds_the_limit_for_any_shape():
    for n in (1, 5, 20, 60):
        for limit in (600, 1500, 4000):
            text = H + _bullets(n) + C + _bullets(n, "c") + W + _bullets(n, "w")
            out, _ = apportion_distilled(text, limit)
            assert len(out) <= limit, f"n={n} limit={limit} -> {len(out)}"


def test_an_earlier_tier_cannot_starve_a_later_one():
    # Every tier oversized: each must still get ~its share, not first-come-first-served.
    # (2026-09-10: the first cut handed each non-HARD tier ALL remaining budget, so
    # CONDITIONAL HEURISTICS consumed the rest and WATCH-ITEMS came back 2 chars.)
    text = H + _bullets(40) + C + _bullets(40, "c") + W + _bullets(40, "w")
    out, rep = apportion_distilled(text, 4000)
    kept = rep["kept"]
    assert kept["WATCH-ITEMS"] >= int(4000 * 0.15) * 0.8, f"starved: {kept}"
    assert kept["CONDITIONAL HEURISTICS"] >= int(4000 * 0.25) * 0.8, f"starved: {kept}"
    assert kept["HARD RULES"] <= int(4000 * 0.50) + 50, f"over cap: {kept}"
    assert len(out) <= 4000


def test_leftover_is_redistributed_not_wasted():
    # HARD RULES tiny -> the positive tiers should absorb the slack, not leave it unused.
    text = H + "- small\n" + C + _bullets(40, "c") + W + _bullets(40, "w")
    out, rep = apportion_distilled(text, 4000)
    assert len(out) > 3000, f"wasted budget: {len(out)}"


def test_a_heading_with_no_content_counts_as_missing():
    # A tier the model emitted as a bare heading is, in practice, absent — an unattended
    # session must see that in `missing`, not a reassuring empty entry in `kept`.
    text = H + _bullets(5) + C + _bullets(2, "c") + W + "\n"
    _, rep = apportion_distilled(text, 4000)
    assert "WATCH-ITEMS" in rep["missing"]


def test_a_bare_ellipsis_tier_counts_as_missing():
    # The model habitually writes a lone "…" continuation marker. A tier carrying only that
    # is empty in practice, and reporting it as present hid an absent WATCH-ITEMS tier
    # behind `missing: []` (2026-09-10).
    text = H + _bullets(5) + C + _bullets(2, "c") + W + "…\n"
    _, rep = apportion_distilled(text, 4000)
    assert "WATCH-ITEMS" in rep["missing"]
    assert "CONDITIONAL HEURISTICS" not in rep["missing"]
