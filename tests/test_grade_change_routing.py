# -*- coding: utf-8 -*-
"""請求区分(正看/准看)が変わる修正のルーティング単体テスト

カイポケの編集ダイアログはサービス内容を変更できないため、職員のみの変更でも
grade_change=True なら削除→再追加に回す必要がある (課金欠陥の根治)。

    python -m pytest tests/test_grade_change_routing.py -q
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import commands.auto_apply as aa  # noqa: E402
from commands.auto_apply import (  # noqa: E402
    _needs_add_first,
    _build_rollback_correction,
    _is_delete_done,
    _pop_reason,
)
from lib.diff_engine import Correction, correction_from_dict, coerce_bool  # noqa: E402


BASE_PAYLOAD = {
    "user_name": "前川七海",
    "date_from": "10", "date_to": "10",
    "start_time_from": "16:45", "start_time_to": "16:45",
    "end_time_from": "17:20", "end_time_to": "17:20",
    "staff1_from": "熊澤妙子", "staff1_to": "高岡はるか",
    "staff2_from": "", "staff2_to": "",
    "service_type": "精神基本療養費Ⅰ・准看",
    "action": "edit",
    "business_type": "医療保険",
    "remarks": "",
}


def payload(**kwargs):
    item = dict(BASE_PAYLOAD)
    item.update(kwargs)
    return item


def make_correction(**kwargs):
    return correction_from_dict(payload(**kwargs))


class FakePage:
    """_apply_move_with_reorder が触る page API だけを持つスタブ"""

    def __init__(self):
        self.waits = 0

    def wait_for_load_state(self, *_a, **_kw):
        pass

    def wait_for_timeout(self, *_a, **_kw):
        self.waits += 1


# =============================================================================
# (a) ペイロードのパース
# =============================================================================

class TestGradeChangeParsing(unittest.TestCase):
    def test_true_flag(self):
        self.assertTrue(correction_from_dict(payload(grade_change=True)).grade_change)

    def test_missing_key_defaults_false(self):
        c = correction_from_dict(payload())
        self.assertFalse(c.grade_change)

    def test_string_and_int_truthy_values(self):
        for raw in (True, "true", "True", " TRUE ", 1, "1"):
            with self.subTest(raw=raw):
                self.assertTrue(correction_from_dict(payload(grade_change=raw)).grade_change)

    def test_falsy_values(self):
        for raw in (False, "false", "False", 0, "0", "", None):
            with self.subTest(raw=raw):
                self.assertFalse(correction_from_dict(payload(grade_change=raw)).grade_change)

    def test_unknown_key_does_not_raise(self):
        c = correction_from_dict(payload(grade_change=True,
                                         future_be_field="なにか",
                                         another_unknown=123))
        self.assertTrue(c.grade_change)
        self.assertEqual(c.user_name, "前川七海")

    def test_service_type_from_is_optional(self):
        self.assertEqual(correction_from_dict(payload()).service_type_from, "")
        c = correction_from_dict(payload(service_type_from="精神基本療養費Ⅰ・正看"))
        self.assertEqual(c.service_type_from, "精神基本療養費Ⅰ・正看")

    def test_coerce_bool_direct(self):
        self.assertTrue(coerce_bool("yes"))
        self.assertFalse(coerce_bool("なし"))
        self.assertFalse(coerce_bool(None))


# =============================================================================
# (b) ルーティング
# =============================================================================

class TestGradeChangeRouting(unittest.TestCase):
    def setUp(self):
        _pop_reason()

    def _run(self, correction):
        """apply_correction を、外部依存を全部差し替えて実行する"""
        calls = {"reorder": 0, "edit_staff": 0, "click_entry": 0, "register": 0}

        def fake_reorder(page, c, dry_run=False, month_str=None):
            calls["reorder"] += 1
            return True

        def fake_click_entry(page, day, start_time, staff_name=None):
            calls["click_entry"] += 1
            return True

        def fake_edit_staff(page, s1, s2="", *a, **kw):
            calls["edit_staff"] += 1
            return True

        def fake_register(page):
            calls["register"] += 1
            return True

        with mock.patch.object(aa, "_apply_move_with_reorder", fake_reorder), \
                mock.patch.object(aa, "click_schedule_entry", fake_click_entry), \
                mock.patch.object(aa, "edit_staff", fake_edit_staff), \
                mock.patch.object(aa, "click_register_button", fake_register), \
                mock.patch.object(aa, "close_edit_dialog", lambda page: None):
            ok = aa.apply_correction(FakePage(), correction, dry_run=False)
        return ok, calls

    def test_staff_only_edit_with_grade_change_goes_through_reorder(self):
        ok, calls = self._run(make_correction(grade_change=True))
        self.assertTrue(ok)
        self.assertEqual(calls["reorder"], 1)
        self.assertEqual(calls["edit_staff"], 0)
        self.assertEqual(calls["click_entry"], 0)

    def test_staff_only_edit_without_grade_change_uses_edit_dialog(self):
        ok, calls = self._run(make_correction(grade_change=False))
        self.assertTrue(ok)
        self.assertEqual(calls["reorder"], 0)
        self.assertEqual(calls["edit_staff"], 1)
        self.assertEqual(calls["click_entry"], 1)
        self.assertEqual(calls["register"], 1)

    def test_time_change_still_goes_through_reorder(self):
        ok, calls = self._run(make_correction(end_time_to="17:35"))
        self.assertTrue(ok)
        self.assertEqual(calls["reorder"], 1)
        self.assertEqual(calls["edit_staff"], 0)

    def test_dry_run_grade_change_prints_routing_and_does_not_register(self):
        c = make_correction(grade_change=True)
        printed = []
        with mock.patch.object(aa, "_apply_move_with_reorder", lambda *a, **kw: True), \
                mock.patch("builtins.print", lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
            ok = aa.apply_correction(FakePage(), c, dry_run=True)
        self.assertTrue(ok)
        self.assertTrue(any("請求区分" in line and "削除→再追加" in line for line in printed),
                        f"ルーティング判断がログに出ていません: {printed}")


# =============================================================================
# (c) 同一キーの削除→追加 とロールバック
# =============================================================================

class TestSameKeyReorder(unittest.TestCase):
    def setUp(self):
        _pop_reason()

    def test_identical_keys_are_delete_first(self):
        self.assertFalse(_needs_add_first("10", "16:45", "10", "16:45"))

    def _run_reorder(self, correction, add_ok, exists=lambda *a, **kw: False,
                     delete_ok=True, order=None):
        order = [] if order is None else order

        def fake_delete(page, day, start_time, dry_run=False, staff_name=None):
            order.append(("delete", day, start_time, staff_name))
            return delete_ok

        def fake_add(page, c, dry_run=False, _retry=0):
            order.append(("add", c.date_to, c.start_time_to, c.staff1_to, c.service_type))
            return add_ok(c) if callable(add_ok) else add_ok

        with mock.patch.object(aa, "delete_schedule_entry", fake_delete), \
                mock.patch.object(aa, "add_schedule_entry", fake_add), \
                mock.patch.object(aa, "_schedule_entry_exists", exists), \
                mock.patch.object(aa, "_recover_schedule_page", lambda page, m=None: None), \
                mock.patch.object(aa, "select_user", lambda page, name: True):
            ok = aa._apply_move_with_reorder(FakePage(), correction, dry_run=False,
                                             month_str="2026-09")
        return ok, order

    def test_same_key_deletes_then_adds_with_new_service_type(self):
        c = make_correction(grade_change=True, service_type="精神基本療養費Ⅰ・准看")
        ok, order = self._run_reorder(c, add_ok=True)
        self.assertTrue(ok)
        self.assertEqual([step[0] for step in order], ["delete", "add"])
        self.assertEqual(order[0][1:], (10, "16:45", "熊澤妙子"))
        self.assertEqual(order[1][3], "高岡はるか")
        self.assertEqual(order[1][4], "精神基本療養費Ⅰ・准看")

    def test_add_failure_rolls_back_original_row(self):
        c = make_correction(grade_change=True,
                            service_type="精神基本療養費Ⅰ・准看",
                            service_type_from="精神基本療養費Ⅰ・正看")
        # 1回目 (新しい行) は失敗、2回目 (ロールバック) は成功
        results = iter([False, True])
        ok, order = self._run_reorder(c, add_ok=lambda _c: next(results))
        self.assertFalse(ok)
        self.assertEqual([step[0] for step in order], ["delete", "add", "add"])
        # ロールバックは元の職員・元のサービス内容で再追加する
        self.assertEqual(order[2][3], "熊澤妙子")
        self.assertEqual(order[2][4], "精神基本療養費Ⅰ・正看")
        self.assertEqual(_pop_reason(), "grade_change_rollback")

    def test_rollback_correction_uses_service_type_from(self):
        c = make_correction(grade_change=True,
                            service_type="精神基本療養費Ⅰ・准看",
                            service_type_from="精神基本療養費Ⅰ・正看")
        rb = _build_rollback_correction(c)
        self.assertEqual(rb.action, "add")
        self.assertEqual(rb.service_type, "精神基本療養費Ⅰ・正看")
        self.assertEqual(rb.staff1_to, "熊澤妙子")

    def test_rollback_falls_back_to_service_type_when_from_missing(self):
        rb = _build_rollback_correction(make_correction(grade_change=True))
        self.assertEqual(rb.service_type, "精神基本療養費Ⅰ・准看")

    def test_add_failure_without_grade_change_keeps_existing_reason(self):
        c = make_correction(end_time_to="17:35")  # 時間変更 → 別キー扱いにならない終了時刻のみ変更
        results = iter([False, True])
        ok, order = self._run_reorder(c, add_ok=lambda _c: next(results))
        self.assertFalse(ok)
        self.assertEqual(_pop_reason(), "add_failed_rolled_back")

    def test_delete_failure_never_attempts_add(self):
        """削除が失敗したら追加は一切しない（削除検証が追加の前提という契約）"""
        c = make_correction(grade_change=True)
        ok, order = self._run_reorder(c, add_ok=True, delete_ok=False)
        self.assertFalse(ok)
        self.assertEqual([step[0] for step in order], ["delete"])
        self.assertNotIn("add", [step[0] for step in order])
        self.assertFalse(_is_delete_done(c))

    def test_grade_change_rollback_without_source_service_type_aborts(self):
        """請求区分が変わるのに変更前のサービス内容が不明 → 推測で復元しない"""
        c = make_correction(grade_change=True, service_type="精神基本療養費Ⅰ・准看")
        self.assertEqual(c.service_type_from, "")
        ok, order = self._run_reorder(c, add_ok=False)
        self.assertFalse(ok)
        # 削除 → 追加(失敗) まで。ロールバックの追加は行わない
        self.assertEqual([step[0] for step in order], ["delete", "add"])
        self.assertEqual(_pop_reason(), "grade_change_rollback_no_source")

    def test_exception_after_delete_resumes_at_add_on_retry(self):
        """削除後に例外 → リトライは削除をやり直さず追加から再開する

        run_auto_apply は例外時に apply_correction を頭から再実行する。印が無いと
        リトライ側の削除が entry_not_found になり、行が消えたまま誤報告になる。
        """
        c = make_correction(grade_change=True, service_type_from="精神基本療養費Ⅰ・正看")

        def boom(_c):
            raise RuntimeError("カイポケが応答しません")

        order = []
        with self.assertRaises(RuntimeError):
            self._run_reorder(c, add_ok=boom, order=order)
        self.assertEqual([step[0] for step in order], ["delete", "add"])
        self.assertTrue(_is_delete_done(c), "削除済みの印が残っていません")

        # --- リトライ（同じ correction オブジェクトで再実行） ---
        retry_order = []
        ok, retry_order = self._run_reorder(c, add_ok=True, order=retry_order)
        self.assertTrue(ok)
        self.assertEqual([step[0] for step in retry_order], ["add"])
        self.assertNotIn("delete", [step[0] for step in retry_order])
        # 決着がついたので印は消える
        self.assertFalse(_is_delete_done(c))

    def test_marker_is_cleared_after_successful_first_pass(self):
        c = make_correction(grade_change=True)
        ok, order = self._run_reorder(c, add_ok=True)
        self.assertTrue(ok)
        self.assertFalse(_is_delete_done(c))

    def test_no_rollback_when_new_row_already_exists(self):
        c = make_correction(grade_change=True)
        ok, order = self._run_reorder(
            c, add_ok=False,
            exists=lambda page, day, start_time, staff_name=None: staff_name == "高岡はるか")
        self.assertFalse(ok)
        self.assertEqual([step[0] for step in order], ["delete", "add"])
        self.assertEqual(_pop_reason(), "add_may_have_registered")


if __name__ == "__main__":
    unittest.main()
