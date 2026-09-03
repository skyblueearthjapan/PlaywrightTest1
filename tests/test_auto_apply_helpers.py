# -*- coding: utf-8 -*-
"""commands/auto_apply.py のヘルパー単体テスト

Playwright を必要としない（auto_apply 側で import をガード済み）。
DOM が要る部分は、実HTML
  artifacts/test_edit_3_user_selected_page_20260128_060104.html
の構造を写した極小スタブ (FakeNode/FakeLocator/FakePage) で再現する。

    python -m unittest tests.test_auto_apply_helpers -v
"""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import commands.auto_apply as aa  # noqa: E402
from commands.auto_apply import (  # noqa: E402
    _normalize_staff_text,
    _staff_text_matches,
    _needs_add_first,
    _build_rollback_correction,
    _is_plan_onclick,
    _onclick_day,
    _row_staff_text,
    _plan_detail_areas,
    _set_reason,
    _pop_reason,
)
from lib.diff_engine import Correction  # noqa: E402


# =============================================================================
# 極小 DOM スタブ
# =============================================================================

class FakeNode:
    """タグ・class・属性・子（文字列 or FakeNode）を持つだけのノード"""

    def __init__(self, tag, classes=(), attrs=None, children=(), visible=True):
        self.tag = tag
        self.classes = list(classes)
        self.attrs = dict(attrs or {})
        self.children = list(children)
        self.visible = visible
        self.parent = None
        for c in self.children:
            if isinstance(c, FakeNode):
                c.parent = self

    def child_nodes(self):
        return [c for c in self.children if isinstance(c, FakeNode)]

    def inner_text(self):
        parts = []
        for c in self.children:
            parts.append(c if isinstance(c, str) else c.inner_text())
        return "\n".join(p for p in parts if p != "")


def _matches(node, sel):
    """'td.tac.nowrap' / 'a[onclick]' / 'td:not(.job-type)' 程度の単純セレクタ判定"""
    not_cls = None
    m = re.search(r":not\(\.([\w-]+)\)", sel)
    if m:
        not_cls = m.group(1)
        sel = sel[:m.start()] + sel[m.end():]
    attrs = re.findall(r"\[([\w-]+)\]", sel)
    sel = re.sub(r"\[[^\]]+\]", "", sel)
    parts = sel.split(".")
    tag = parts[0]
    classes = [c for c in parts[1:] if c]
    if tag and node.tag != tag:
        return False
    if any(c not in node.classes for c in classes):
        return False
    if not_cls and not_cls in node.classes:
        return False
    if any(a not in node.attrs for a in attrs):
        return False
    return True


def _descendants(nodes):
    out = []
    for n in nodes:
        for c in n.child_nodes():
            out.append(c)
            out.extend(_descendants([c]))
    return out


class FakeLocator:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    def locator(self, selector):
        # _entry_time_text が使う xpath=ancestor::div[contains(@class,'…')]
        m = re.match(r"xpath=ancestor::(\w+)\[contains\(@class,'([\w-]+)'\)\]", selector)
        if m:
            want_tag, want_cls = m.group(1), m.group(2)
            found = []
            for n in self._nodes:
                p = n.parent
                while p is not None:
                    if p.tag == want_tag and want_cls in p.classes:
                        found.append(p)
                        break
                    p = p.parent
            return FakeLocator(found)

        nodes = self._nodes
        for part in selector.split():
            cands = _descendants(nodes)
            nodes = [n for n in cands if _matches(n, part)]
        return FakeLocator(nodes)

    def all(self):
        return [FakeLocator([n]) for n in self._nodes]

    def count(self):
        return len(self._nodes)

    @property
    def first(self):
        return FakeLocator(self._nodes[:1])

    def inner_text(self):
        return self._nodes[0].inner_text() if self._nodes else ""

    def text_content(self):
        return self.inner_text()

    def get_attribute(self, name):
        return self._nodes[0].attrs.get(name) if self._nodes else None

    def is_visible(self, timeout=None):
        return bool(self._nodes) and self._nodes[0].visible

    def node(self):
        return self._nodes[0]


class FakePage:
    """click_schedule_entry が触るメソッドだけを持つページ"""

    def __init__(self, rows):
        table = FakeNode("table", children=rows)
        self.root = FakeNode("body", children=[table])

    def locator(self, selector):
        return FakeLocator([self.root]).locator(selector)

    def wait_for_selector(self, selector, timeout=None):
        loc = self.locator(selector)
        if loc.count() == 0:
            raise RuntimeError("timeout")
        return loc

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def evaluate(self, *args, **kwargs):
        return []

    def screenshot(self, **kwargs):
        pass

    def content(self):
        return "<html></html>"


def staff_cell(names):
    """td.staff-list（入れ子テーブルに職員1・職員2が縦に並ぶ）"""
    rows = []
    for name in names:
        rows.append(FakeNode("tr", children=[
            FakeNode("td", classes=["job-type"], children=[""]),
            FakeNode("td", children=[name]),
        ]))
    return FakeNode("td", classes=["staff-list"],
                    children=[FakeNode("table", children=rows)])


def detail_area(time_text, onclick, service_name="精神基本療養費Ⅰ・正看"):
    return FakeNode("div", classes=["service-detail-area"], children=[
        FakeNode("div", classes=["service-name"], children=[
            FakeNode("a", attrs={"onclick": onclick}, children=[service_name]),
        ]),
        time_text,
    ])


def visit_row(day, plan_time, plan_staff, actual_time="", actual_staff=(),
              detail_id="611637151"):
    """実HTMLと同じ「予定(01) + 実績(02)」の1訪問行"""
    plan_onclick = (f"showHNC097807Edit('202604', '11834626', '01', "
                    f"'{detail_id}', '{day}', '88', 'HNC097802')")
    actual_onclick = "showHNC097807Edit('202604', '11834626', '02', '', '', '', 'HNC097802')"
    return FakeNode("tr", children=[
        FakeNode("td", classes=["tac", "nowrap"], children=[str(day)]),
        FakeNode("td", children=[detail_area(plan_time, plan_onclick)]),
        staff_cell(plan_staff),
        # --- 実績側 ---
        FakeNode("td", classes=["tac", "nowrap"], children=[str(day)]),
        FakeNode("td", children=[detail_area(actual_time, actual_onclick, "")]),
        staff_cell(actual_staff),
    ])


def make_correction(**kwargs):
    base = dict(
        user_name="前川七海",
        date_from="10", date_to="10",
        start_time_from="16:45", start_time_to="16:45",
        end_time_from="17:20", end_time_to="17:35",
        staff1_from="熊澤妙子", staff1_to="高岡はるか",
        staff2_from="", staff2_to="",
        service_type="精神基本療養費Ⅰ・正看",
        action="edit", business_type="医療保険", remarks="",
    )
    base.update(kwargs)
    return Correction(**base)


# =============================================================================
# 純Pythonヘルパー
# =============================================================================

class TestNormalizeStaffText(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_normalize_staff_text(""), "")
        self.assertEqual(_normalize_staff_text(None), "")

    def test_removes_all_whitespace(self):
        # 澤 → 沢 は normalize_name の異体字統一による（比較キーなので実害なし）
        self.assertEqual(_normalize_staff_text("熊澤　妙子"), "熊沢妙子")
        self.assertEqual(_normalize_staff_text("熊澤 妙子\n"), "熊沢妙子")

    def test_removes_parenthesized_note(self):
        self.assertEqual(_normalize_staff_text("熊澤妙子(重複)"), "熊沢妙子")
        self.assertEqual(_normalize_staff_text("熊澤妙子（重複）"), "熊沢妙子")

    def test_variant_kanji_unified(self):
        self.assertEqual(_normalize_staff_text("髙梨桂子"), _normalize_staff_text("高梨桂子"))


class TestStaffTextMatches(unittest.TestCase):
    def test_equality_after_normalization(self):
        self.assertTrue(_staff_text_matches("熊澤妙子", "熊澤妙子(重複)"))
        self.assertTrue(_staff_text_matches("熊澤 妙子", "熊澤　妙子"))
        self.assertTrue(_staff_text_matches("高梨桂子", "髙梨　桂子"))

    def test_short_surname_does_not_match_longer_name(self):
        # H-2: 部分一致だと 森 が 大森 に一致してしまっていた
        self.assertFalse(_staff_text_matches("森", "大森"))
        self.assertFalse(_staff_text_matches("大森", "森"))

    def test_whole_cell_text_no_longer_matches(self):
        # 職種や同行者を含むセル全体を渡しても一致しない（職員1だけを比較する契約）
        self.assertFalse(_staff_text_matches("熊澤妙子", "訪問看護\n熊澤妙子\n小西"))

    def test_empty_inputs_never_match(self):
        self.assertFalse(_staff_text_matches("", "熊澤妙子"))
        self.assertFalse(_staff_text_matches("熊澤妙子", ""))
        self.assertFalse(_staff_text_matches(None, None))


class TestOnclickHelpers(unittest.TestCase):
    PLAN = "showHNC097807Edit('202604', '11834626', '01', '611637151', '7', '88', 'HNC097802')"
    ACTUAL = "showHNC097807Edit('202604', '11834626', '02', '', '', '', 'HNC097802')"

    def test_plan_only(self):
        self.assertTrue(_is_plan_onclick(self.PLAN))
        self.assertFalse(_is_plan_onclick(self.ACTUAL))
        self.assertFalse(_is_plan_onclick(""))
        self.assertFalse(_is_plan_onclick("submitLinkTabHelper('x','y')"))

    def test_day_param(self):
        self.assertEqual(_onclick_day(self.PLAN), "7")
        self.assertEqual(_onclick_day(self.ACTUAL), "")
        self.assertEqual(_onclick_day(""), "")


class TestNeedsAddFirst(unittest.TestCase):
    def test_date_change_is_add_first(self):
        self.assertTrue(_needs_add_first("9", "16:45", "10", "16:45"))

    def test_start_time_change_is_add_first(self):
        self.assertTrue(_needs_add_first("9", "16:45", "9", "17:00"))

    def test_same_key_is_delete_first(self):
        self.assertFalse(_needs_add_first("9", "16:45", "9", "16:45"))

    def test_whitespace_and_none_are_tolerated(self):
        self.assertFalse(_needs_add_first("9", " 16:45", "9", "16:45 "))
        self.assertFalse(_needs_add_first(None, None, "", ""))
        self.assertTrue(_needs_add_first("9", "16:45", None, None))

    def test_numeric_and_string_days_compare_as_text(self):
        self.assertFalse(_needs_add_first(9, "16:45", "9", "16:45"))


class TestBuildRollbackCorrection(unittest.TestCase):
    def test_restores_original_row(self):
        c = make_correction(date_from="9", date_to="10",
                            start_time_from="16:45", start_time_to="17:00",
                            end_time_from="17:20", end_time_to="17:35",
                            staff1_from="熊澤妙子", staff1_to="高岡はるか",
                            staff2_from="小西", staff2_to="", action="date_change")
        rb = _build_rollback_correction(c)
        self.assertEqual(rb.action, "add")
        # 追加は *_to を使うので、_to 側がすべて元(*_from)の値である必要がある
        self.assertEqual(rb.date_to, "9")
        self.assertEqual(rb.start_time_to, "16:45")
        self.assertEqual(rb.end_time_to, "17:20")
        self.assertEqual(rb.staff1_to, "熊澤妙子")
        self.assertEqual(rb.staff2_to, "小西")
        self.assertEqual(rb.business_type, "医療保険")
        self.assertEqual(rb.user_name, "前川七海")

    def test_rollback_row_is_not_a_move(self):
        rb = _build_rollback_correction(make_correction(date_from="9", date_to="9"))
        self.assertFalse(rb.has_date_change())
        self.assertFalse(rb.has_time_change())
        self.assertFalse(rb.has_staff_change())


class TestFailureReason(unittest.TestCase):
    def setUp(self):
        _pop_reason()

    def test_set_and_pop(self):
        _set_reason("delete_button_not_found")
        self.assertEqual(_pop_reason(), "delete_button_not_found")

    def test_pop_clears(self):
        _set_reason("register_failed")
        _pop_reason()
        self.assertIsNone(_pop_reason())

    def test_last_writer_wins(self):
        _set_reason("entry_not_found")
        _set_reason("add_failed_nothing_deleted")
        self.assertEqual(_pop_reason(), "add_failed_nothing_deleted")


# =============================================================================
# DOM を伴うヘルパー
# =============================================================================

class TestRowStaffText(unittest.TestCase):
    def test_returns_plan_side_staff1_only(self):
        row = visit_row(1, "16:00 ～ 16:35", ["川名千恵", "佐藤憲二"],
                        actual_staff=["別人太郎"])
        self.assertEqual(_row_staff_text(FakeLocator([row])), "川名千恵")

    def test_accompanying_staff2_is_not_matched(self):
        # H-2: 同行者(職員2)が職員1として一致してはいけない
        row = FakeLocator([visit_row(1, "16:00 ～ 16:35", ["小西", "熊澤妙子"])])
        text = _row_staff_text(row)
        self.assertTrue(_staff_text_matches("小西", text))
        self.assertFalse(_staff_text_matches("熊澤妙子", text))

    def test_actual_side_staff_is_ignored(self):
        row = FakeLocator([visit_row(1, "16:00 ～ 16:35", ["川名千恵"],
                                     actual_staff=["佐藤憲二"])])
        self.assertFalse(_staff_text_matches("佐藤憲二", _row_staff_text(row)))

    def test_missing_cell_returns_empty(self):
        self.assertEqual(_row_staff_text(FakeLocator([FakeNode("tr")])), "")


class TestPlanDetailAreas(unittest.TestCase):
    def test_only_plan_side_area(self):
        # M-2: 実績側の service-detail-area を数えない
        row = FakeLocator([visit_row(7, "16:45 ～ 17:20", ["熊澤妙子"],
                                     actual_time="18:00 ～ 18:30")])
        areas = _plan_detail_areas(row)
        self.assertEqual(len(areas), 1)
        self.assertIn("16:45", areas[0].inner_text())
        self.assertNotIn("18:00", areas[0].inner_text())


class TestClickScheduleEntry(unittest.TestCase):
    """C-1: 候補1件でも時間・職員のガードを素通りさせない"""

    def setUp(self):
        _pop_reason()
        self.clicked = []
        patches = [
            mock.patch.object(aa, "_click_with_scroll",
                              side_effect=lambda page, link, **kw: self.clicked.append(link)),
            mock.patch.object(aa, "_remove_floating_overlays", lambda page: None),
            mock.patch.object(aa, "_save_debug_on_failure", lambda page, day, st: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def clicked_onclick(self):
        return self.clicked[0].node().attrs.get("onclick", "")

    def test_single_candidate_wrong_time_is_refused(self):
        page = FakePage([visit_row(1, "16:00 ～ 16:35", ["川名千恵"])])
        ok = aa.click_schedule_entry(page, 1, "16:45", staff_name="川名千恵")
        self.assertFalse(ok)
        self.assertEqual(self.clicked, [])
        self.assertEqual(_pop_reason(), "entry_not_found")

    def test_single_candidate_wrong_staff_is_refused(self):
        # 佐藤憲二 は職員2（同行）なので職員1としては不一致
        page = FakePage([visit_row(1, "16:00 ～ 16:35", ["川名千恵", "佐藤憲二"])])
        ok = aa.click_schedule_entry(page, 1, "16:00", staff_name="佐藤憲二")
        self.assertFalse(ok)
        self.assertEqual(self.clicked, [])
        self.assertEqual(_pop_reason(), "entry_not_found")

    def test_single_candidate_matching_is_clicked(self):
        page = FakePage([visit_row(1, "16:00 ～ 16:35", ["川名千恵"])])
        self.assertTrue(aa.click_schedule_entry(page, 1, "16:00", staff_name="川名千恵"))
        self.assertEqual(len(self.clicked), 1)

    def test_same_time_two_rows_picks_by_staff(self):
        # 本番事故: 木村9/7 は 16:45 熊澤(旧) と 16:45 高岡(新) が並ぶ。
        # 先頭を掴む実装でも通ってしまわないよう、目的の行を2番目に置く。
        page = FakePage([
            visit_row(7, "16:45 ～ 17:20", ["高岡はるか"], detail_id="NEW"),
            visit_row(7, "16:45 ～ 17:20", ["熊澤妙子"], detail_id="OLD"),
        ])
        self.assertTrue(aa.click_schedule_entry(page, 7, "16:45", staff_name="熊澤妙子"))
        self.assertIn("'OLD'", self.clicked_onclick())

    def test_time_filter_picks_the_matching_row(self):
        # 同日2件・時間違い: 職員指定なしでも時間で正しく選ぶ（先頭固定ではない）
        page = FakePage([
            visit_row(7, "09:00 ～ 09:30", ["高岡はるか"], detail_id="MORNING"),
            visit_row(7, "16:45 ～ 17:20", ["熊澤妙子"], detail_id="EVENING"),
        ])
        self.assertTrue(aa.click_schedule_entry(page, 7, "16:45"))
        self.assertIn("'EVENING'", self.clicked_onclick())

    def test_actual_side_time_is_not_a_candidate(self):
        # 実績(02)の時間を掴んで削除しにいかない
        page = FakePage([visit_row(7, "16:45 ～ 17:20", ["熊澤妙子"],
                                   actual_time="18:00 ～ 18:30")])
        ok = aa.click_schedule_entry(page, 7, "18:00", staff_name="熊澤妙子")
        self.assertFalse(ok)
        self.assertEqual(self.clicked, [])

    def test_no_staff_name_keeps_first_match(self):
        page = FakePage([
            visit_row(7, "16:45 ～ 17:20", ["熊澤妙子"], detail_id="OLD"),
            visit_row(7, "16:45 ～ 17:20", ["高岡はるか"], detail_id="NEW"),
        ])
        self.assertTrue(aa.click_schedule_entry(page, 7, "16:45"))
        self.assertIn("'OLD'", self.clicked_onclick())


class TestApplyMoveGuards(unittest.TestCase):
    """M-4: 日付が読めないときは何も操作しない"""

    def setUp(self):
        _pop_reason()
        self.calls = []
        for name in ("add_schedule_entry", "delete_schedule_entry"):
            p = mock.patch.object(aa, name,
                                  side_effect=lambda *a, **k: self.calls.append("write") or True)
            p.start()
            self.addCleanup(p.stop)

    def test_invalid_date_from_aborts(self):
        for bad in ("", "  ", "-", "9日"):
            with self.subTest(bad=bad):
                _pop_reason()
                self.calls.clear()
                c = make_correction(date_from=bad, date_to="10",
                                    start_time_from="16:45", start_time_to="17:00")
                self.assertFalse(aa._apply_move_with_reorder(FakePage([]), c))
                self.assertEqual(self.calls, [])
                self.assertEqual(_pop_reason(), "invalid_date_from")


if __name__ == "__main__":
    unittest.main()
