"""Regression tests for Codex review Bug C (C-10 wrap) and Bug D
(date equality via canonical key) in :mod:`lib.diff_engine`.

These tests intentionally live in their own file so the diff-engine
behaviour is verified independently of the allocation pipeline. Run
from repo root::

    pytest lib/test_diff_engine.py -v
"""
from __future__ import annotations

import pytest

from lib.diff_engine import compare_schedules_from_content


CSV_HEADER = (
    "職員名1,職種1,職員名2,職種2,同行2,職員名3,職種3,同行3,"
    "事業所名,日付,曜日,利用者,業務種別,サービス内容,"
    "開始時間,終了時間,提供時間,備考\n"
)


def _kaipoke_row(
    *, staff1="A田", staff2="", date="1", weekday="月", user="P1",
    business="医療保険", svc="訪問看護I", start="09:00", end="10:00", remarks="",
) -> str:
    return (
        f"{staff1},看護師,{staff2},,,,,,"  # 8 cols
        f"事業所A,{date},{weekday},{user},{business},{svc},"
        f"{start},{end},60,{remarks}\n"
    )


# ===========================================================================
# Bug C: month-spanning week ranges (e.g. 29..5)
# ===========================================================================


@pytest.mark.parametrize(
    "raw_date,start,end,expected_kept",
    [
        # Wrap range 29..5 — May has 31 days, week spans 29 May → 4 Jun
        ("30", 29, 5, True),    # day 30 inside [29..31] tail
        ("31", 29, 5, True),    # day 31 inside [29..31] tail
        ("2",  29, 5, True),    # day  2 inside [1..5]   head
        ("5",  29, 5, True),    # day  5 inside [1..5]   head (boundary)
        ("29", 29, 5, True),    # boundary start
        # Out-of-wrap days must still be excluded
        ("6",  29, 5, False),
        ("28", 29, 5, False),
        # yyyy/MM/dd formats also wrap correctly
        ("2026/05/30", 29, 5, True),
        ("2026/06/02", 29, 5, True),
        ("2026/06/06", 29, 5, False),
        # Non-wrap (start<=end) sanity preserved
        ("3", 1, 7, True),
        ("8", 1, 7, False),
    ],
)
def test_bug_c_wrap_range_filter(raw_date, start, end, expected_kept):
    """`compare_schedules_from_content` keeps only rows whose day-of-month
    falls inside [start..end], wrapping when start > end."""
    cur = CSV_HEADER + _kaipoke_row(date=raw_date, user="P1", svc="A")
    opt = CSV_HEADER + _kaipoke_row(date=raw_date, user="P1", svc="A",
                                    start="10:00", end="11:00")
    corrections = compare_schedules_from_content(
        cur, opt,
        target_week_start=start, target_week_end=end,
    )
    if expected_kept:
        assert any(c.action == "edit" for c in corrections), (
            f"Bug C: wrap range {start}..{end} dropped day '{raw_date}'"
        )
    else:
        assert corrections == [], (
            f"Bug C: wrap range {start}..{end} kept out-of-range day '{raw_date}': "
            f"{corrections}"
        )


def test_bug_c_wrap_range_aggregate():
    """A whole week with 5 entries spread across a month boundary survives
    the filter — every entry's day belongs in [29..5]."""
    rows = ""
    days = ["29", "30", "31", "1", "2"]
    for i, d in enumerate(days):
        rows += _kaipoke_row(
            date=d, user=f"P{i}", svc=f"S{i}",
            start="09:00", end="10:00",
        )
    cur = CSV_HEADER + rows
    # Optimized differs in time on every entry → 5 edits expected
    opt_rows = ""
    for i, d in enumerate(days):
        opt_rows += _kaipoke_row(
            date=d, user=f"P{i}", svc=f"S{i}",
            start="11:00", end="12:00",
        )
    opt = CSV_HEADER + opt_rows
    corrections = compare_schedules_from_content(
        cur, opt,
        target_week_start=29, target_week_end=5,
    )
    edits = [c for c in corrections if c.action == "edit"]
    assert len(edits) == 5, (
        f"Bug C: expected 5 edits across the wrap week, got {len(edits)} "
        f"({[c.user_name for c in edits]})"
    )


# ===========================================================================
# Bug D: mixed date-format equality (canonical key)
# ===========================================================================


def test_bug_d_yyyy_mm_dd_vs_day_only_no_false_date_change():
    """Current row dated `2026/05/04` and optimized row dated `4` must be
    treated as the same day. Prior to the fix this generated a spurious
    `date_change` correction. After the fix, only an `edit` (time change)
    should appear."""
    cur = CSV_HEADER + _kaipoke_row(
        date="2026/05/04", user="P1", svc="A",
        start="09:00", end="10:00",
    )
    opt = CSV_HEADER + _kaipoke_row(
        date="4", user="P1", svc="A",
        start="11:00", end="12:00",
    )
    corrections = compare_schedules_from_content(
        cur, opt,
        target_week_start=1, target_week_end=7,
    )
    actions = sorted(c.action for c in corrections)
    assert "date_change" not in actions, (
        f"Bug D: false date_change emitted for same-day mixed formats: {corrections}"
    )
    assert "edit" in actions, (
        f"Bug D: expected an edit (time change) but got: {corrections}"
    )


def test_bug_d_identical_schedules_in_mixed_formats_yield_no_corrections():
    """Two identical schedules expressed in different date formats must
    produce zero corrections — neither edit nor date_change."""
    cur = CSV_HEADER + _kaipoke_row(
        date="2026/05/04", user="P1", svc="A",
        start="09:00", end="10:00",
    )
    opt = CSV_HEADER + _kaipoke_row(
        date="4", user="P1", svc="A",
        start="09:00", end="10:00",
    )
    corrections = compare_schedules_from_content(
        cur, opt,
        target_week_start=1, target_week_end=7,
    )
    assert corrections == [], (
        f"Bug D: identical schedules in mixed formats produced corrections: "
        f"{corrections}"
    )


def test_bug_d_genuine_date_change_still_detected():
    """Negative control: a true date change (2 → 4) must still surface as
    a `date_change` action, even when one side uses yyyy/MM/dd."""
    cur = CSV_HEADER + _kaipoke_row(
        date="2", user="P1", svc="A",
        start="09:00", end="10:00",
    )
    opt = CSV_HEADER + _kaipoke_row(
        date="2026/05/04", user="P1", svc="A",
        start="09:00", end="10:00",
    )
    corrections = compare_schedules_from_content(
        cur, opt,
        target_week_start=1, target_week_end=7,
    )
    assert any(c.action == "date_change" for c in corrections), (
        f"Bug D: genuine date_change (2→4) was lost: {corrections}"
    )
