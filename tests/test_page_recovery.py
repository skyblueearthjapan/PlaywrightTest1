# -*- coding: utf-8 -*-
"""1 件の失敗が後ろへ広がらないための手当て (設計 2026-11-19 B-1〜B-3) の単体テスト

Playwright もブラウザも使わない。外側の依存は mock で差し替える。

    python -m pytest tests/test_page_recovery.py -q
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import commands.auto_apply as aa  # noqa: E402
from commands.auto_apply import _SelectFailureTracker, _pop_reason  # noqa: E402
from lib.diff_engine import Correction  # noqa: E402


def make_correction(**kwargs):
    base = dict(
        user_name="川上",
        date_from="24", date_to="24",
        start_time_from="11:00", start_time_to="10:25",
        end_time_from="11:35", end_time_to="11:00",
        staff1_from="熊澤妙子", staff1_to="熊澤妙子",
        staff2_from="", staff2_to="",
        service_type="精神基本療養費Ⅰ・正看",
        action="edit", business_type="医療保険", remarks="",
    )
    base.update(kwargs)
    return Correction(**base)


class FakeLoc:
    def __init__(self, visible=False):
        self.visible = visible

    def is_visible(self, timeout=None):
        return self.visible


class FakePage:
    def __init__(self):
        self.calls = []

    def locator(self, selector):
        return FakeLoc(False)

    def goto(self, url, timeout=None):
        self.calls.append(("goto", url))

    def evaluate(self, *a, **kw):
        self.calls.append(("evaluate", a))

    def wait_for_load_state(self, *a, **kw):
        pass

    def wait_for_timeout(self, ms):
        pass


# =============================================================================
# B-3: 続けて失敗した人数
# =============================================================================

class TestSelectFailureTracker(unittest.TestCase):
    def test_stops_at_two_consecutive(self):
        t = _SelectFailureTracker(limit=2)
        self.assertFalse(t.record(False))
        self.assertTrue(t.record(False))

    def test_success_resets(self):
        t = _SelectFailureTracker(limit=2)
        self.assertFalse(t.record(False))
        self.assertFalse(t.record(True))
        self.assertFalse(t.record(False))
        self.assertTrue(t.record(False))

    def test_only_successes_never_stop(self):
        t = _SelectFailureTracker()
        for _ in range(5):
            self.assertFalse(t.record(True))


# =============================================================================
# B-1: 立て直しはページごと開き直す
# =============================================================================

class TestRecoverSchedulePage(unittest.TestCase):
    def _run(self, verify=lambda page, m: None, dropdown=object(), context=None,
             ensure=lambda page, ctx, m: True):
        page = FakePage()
        months = []
        with mock.patch.object(aa, "set_service_month",
                               lambda p, m: months.append(m)), \
                mock.patch.object(aa, "verify_service_month", verify), \
                mock.patch.object(aa, "ensure_session", ensure), \
                mock.patch.object(aa, "_get_user_dropdown", lambda p: dropdown), \
                mock.patch.object(aa, "close_edit_dialog", lambda p: True):
            ok = aa._recover_schedule_page(page, "2026-12", context=context)
        return ok, page, months

    def test_reopens_page_and_sets_month(self):
        ok, page, months = self._run()
        self.assertTrue(ok)
        self.assertIn(("goto", aa.MONTHLY_SCHEDULE_URL), page.calls)
        self.assertEqual(months, ["2026-12"])

    def test_does_not_remove_dom(self):
        _, page, _ = self._run()
        self.assertFalse([c for c in page.calls if c[0] == "evaluate"],
                         "立て直しで JavaScript による DOM の取り除きをしてはいけない")

    def test_wrong_month_is_failure(self):
        def bad_verify(page, m):
            raise RuntimeError("月の設定が不正です")
        ok, _, _ = self._run(verify=bad_verify)
        self.assertFalse(ok)

    def test_missing_user_dropdown_is_failure(self):
        ok, _, _ = self._run(dropdown=None)
        self.assertFalse(ok)

    def test_session_failure_is_failure(self):
        ok, page, _ = self._run(context=object(), ensure=lambda p, c, m: False)
        self.assertFalse(ok)
        self.assertEqual(page.calls, [])


# =============================================================================
# B-2: 選べなければ 1 回だけ開き直して選び直す
# =============================================================================

class TestSelectUserWithRecovery(unittest.TestCase):
    def setUp(self):
        aa._set_page_unverified(False)

    def tearDown(self):
        aa._set_page_unverified(False)

    def _run(self, select_results, recover_ok=True):
        results = iter(select_results)
        recovers = []
        with mock.patch.object(aa, "select_user", lambda p, n: next(results)), \
                mock.patch.object(aa, "_recover_schedule_page",
                                  lambda p, m=None, context=None:
                                  recovers.append((m, context)) or recover_ok):
            ok = aa._select_user_with_recovery(object(), "川上", "2026-12", "CTX")
        return ok, recovers

    def test_first_try_ok_no_recovery(self):
        ok, recovers = self._run([True])
        self.assertTrue(ok)
        self.assertEqual(recovers, [])

    def test_retry_after_recovery(self):
        ok, recovers = self._run([False, True])
        self.assertTrue(ok)
        self.assertEqual(recovers, [("2026-12", "CTX")])

    def test_fails_after_one_retry(self):
        ok, recovers = self._run([False, False])
        self.assertFalse(ok)
        self.assertEqual(len(recovers), 1)

    def test_recovery_failure_does_not_select(self):
        # 開き直しに失敗したら、選び直しが通りそうでも選ばない (提供月を確かめていない)
        ok, recovers = self._run([False, True], recover_ok=False)
        self.assertFalse(ok)
        self.assertEqual(len(recovers), 1)

    def test_unverified_page_recovers_before_select(self):
        # 前の開き直しに失敗したままなら、選ぶ前に開き直す
        aa._set_page_unverified(True)
        selects = []
        with mock.patch.object(aa, "select_user",
                               lambda p, n: selects.append(n) or True), \
                mock.patch.object(aa, "_recover_schedule_page",
                                  lambda p, m=None, context=None: False), \
                mock.patch("builtins.print", lambda *a, **k: None):
            ok = aa._select_user_with_recovery(object(), "川上", "2026-12", "CTX")
        self.assertFalse(ok)
        self.assertEqual(selects, [])

    def test_recover_sets_and_clears_unverified(self):
        page = FakePage()
        common = dict(set_service_month=lambda p, m: None,
                      ensure_session=lambda p, c, m: True,
                      _get_user_dropdown=lambda p: object(),
                      close_edit_dialog=lambda p: True)

        def bad_verify(p, m):
            raise RuntimeError("月の設定が不正です")

        with mock.patch.multiple(aa, verify_service_month=bad_verify, **common), \
                mock.patch("builtins.print", lambda *a, **k: None):
            self.assertFalse(aa._recover_schedule_page(page, "2026-12"))
        self.assertTrue(aa._page_unverified())
        with mock.patch.multiple(aa, verify_service_month=lambda p, m: None, **common), \
                mock.patch("builtins.print", lambda *a, **k: None):
            self.assertTrue(aa._recover_schedule_page(page, "2026-12"))
        self.assertFalse(aa._page_unverified())


# =============================================================================
# run_auto_apply: 2 人続けて選べなければ止め、件数を合わせる
# =============================================================================

class _FakeCM:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


class _FakeCtx:
    def close(self):
        pass


class TestRunAutoApplyStops(unittest.TestCase):
    def setUp(self):
        aa._set_page_unverified(False)

    def tearDown(self):
        aa._set_page_unverified(False)

    def _run(self, selectable_users, corrections):
        """selectable_users: 選べる利用者名の集合"""
        page = FakePage()
        selected_calls = []
        applied = []

        def fake_select(p, name, month=None, context=None):
            selected_calls.append(name)
            return name in selectable_users

        def fake_apply(p, c, dry_run=False, month_str=None, context=None):
            applied.append(c.user_name)
            return True

        with mock.patch.object(aa, "sync_playwright", lambda: _FakeCM()), \
                mock.patch.object(aa, "create_browser_context",
                                  lambda p, headless=True: (_FakeCtx(), _FakeCtx(), page)), \
                mock.patch.object(aa, "setup_monthly_schedule_page", lambda *a, **k: None), \
                mock.patch.object(aa, "load_correction_sheet", lambda path: corrections), \
                mock.patch.object(aa, "clear_stop", lambda: None), \
                mock.patch.object(aa, "is_stop_requested", lambda: False), \
                mock.patch.object(aa, "_select_user_with_recovery", fake_select), \
                mock.patch.object(aa, "_check_current_user", lambda p, n: True), \
                mock.patch.object(aa, "apply_correction", fake_apply), \
                mock.patch.object(aa, "navigate_to_staff_tab", lambda p: True), \
                mock.patch.object(aa, "select_staff_member", lambda p, n: True), \
                mock.patch.object(aa, "add_event_entry", lambda p, c, d=False: True), \
                mock.patch("builtins.print", lambda *a, **k: None):
            result = aa.run_auto_apply("sheet.json", month="2026-12")
        return result, selected_calls, applied

    def corrections(self):
        cs = []
        for user in ("A", "B", "C", "D"):
            for day in ("1", "2"):
                cs.append(make_correction(user_name=user, date_from=day, date_to=day,
                                          action="add"))
        cs.append(make_correction(user_name="", action="add", business_type="会議",
                                  staff1_to="熊澤妙子"))
        return cs

    def assert_counts_add_up(self, result):
        self.assertEqual(result["success"] + result["failed"] + result["skipped"],
                         result["total"])
        self.assertEqual(len(result["details"]), result["total"])

    def test_two_consecutive_failures_stop(self):
        result, selected, applied = self._run({"A"}, self.corrections())
        self.assertTrue(result.get("stopped"))
        self.assertEqual(result.get("stop_reason"), "page_broken")
        self.assertEqual(selected, ["A", "B", "C"])   # D は試さない
        self.assertEqual(applied, ["A", "A"])
        reasons = [d.get("reason") for d in result["details"] if d["status"] == "skipped"]
        self.assertEqual(reasons.count("user_not_found"), 4)          # B, C
        self.assertEqual(reasons.count("not_attempted_page_broken"), 3)  # D ×2 + 会議
        self.assert_counts_add_up(result)

    def test_single_failure_continues(self):
        result, selected, applied = self._run({"A", "C", "D"}, self.corrections())
        self.assertFalse(result.get("stopped"))
        self.assertNotIn("stop_reason", result)
        self.assertEqual(selected, ["A", "B", "C", "D"])
        self.assertEqual(applied, ["A", "A", "C", "C", "D", "D"])
        self.assert_counts_add_up(result)

    def test_reselect_failures_count_and_skip_rest_of_user(self):
        # 初めの選択は通るが、修正の前の確かめで画面の利用者が違い、選び直しにも失敗する
        page = FakePage()
        calls = {}
        applied = []

        def fake_select(p, name, month=None, context=None):
            calls[name] = calls.get(name, 0) + 1
            return name == "A" or calls[name] == 1

        with mock.patch.object(aa, "sync_playwright", lambda: _FakeCM()), \
                mock.patch.object(aa, "create_browser_context",
                                  lambda p, headless=True: (_FakeCtx(), _FakeCtx(), page)), \
                mock.patch.object(aa, "setup_monthly_schedule_page", lambda *a, **k: None), \
                mock.patch.object(aa, "load_correction_sheet",
                                  lambda path: self.corrections()), \
                mock.patch.object(aa, "clear_stop", lambda: None), \
                mock.patch.object(aa, "is_stop_requested", lambda: False), \
                mock.patch.object(aa, "_select_user_with_recovery", fake_select), \
                mock.patch.object(aa, "_check_current_user", lambda p, n: n == "A"), \
                mock.patch.object(aa, "apply_correction",
                                  lambda p, c, dry_run=False, month_str=None, context=None:
                                  applied.append(c.user_name) or True), \
                mock.patch("builtins.print", lambda *a, **k: None):
            result = aa.run_auto_apply("sheet.json", month="2026-12")
        self.assertEqual(result.get("stop_reason"), "page_broken")
        self.assertEqual(applied, ["A", "A"])
        self.assertNotIn("D", calls)
        self.assertEqual(calls, {"A": 1, "B": 2, "C": 2})
        self.assert_counts_add_up(result)

    def _run_with(self, corrections, select, check, apply):
        page = FakePage()
        with mock.patch.object(aa, "sync_playwright", lambda: _FakeCM()), \
                mock.patch.object(aa, "create_browser_context",
                                  lambda p, headless=True: (_FakeCtx(), _FakeCtx(), page)), \
                mock.patch.object(aa, "setup_monthly_schedule_page", lambda *a, **k: None), \
                mock.patch.object(aa, "load_correction_sheet", lambda path: corrections), \
                mock.patch.object(aa, "clear_stop", lambda: None), \
                mock.patch.object(aa, "is_stop_requested", lambda: False), \
                mock.patch.object(aa, "ensure_session", lambda p, c, m: True), \
                mock.patch.object(aa, "close_edit_dialog", lambda p: True), \
                mock.patch.object(aa, "select_user",
                                  lambda p, n: self.fail("素の select_user を呼んではいけない")), \
                mock.patch.object(aa, "_select_user_with_recovery", select), \
                mock.patch.object(aa, "_check_current_user", check), \
                mock.patch.object(aa, "apply_correction", apply), \
                mock.patch("builtins.print", lambda *a, **k: None):
            return aa.run_auto_apply("sheet.json", month="2026-12")

    def test_retry_after_exception_checks_current_user(self):
        # 1 回目の書き込みで例外 → やり直しの前に利用者を確かめ、違えば書き込まない
        cs = [make_correction(user_name="A", date_from=d, date_to=d, action="add")
              for d in ("1", "2")]
        selects, checks, applies = [], [], []

        def select(p, n, m=None, context=None):
            selects.append(n)
            return True

        def check(p, n):
            checks.append(n)
            return len(checks) == 1   # 初めの確かめだけ通る

        def apply(p, c, dry_run=False, month_str=None, context=None):
            applies.append(c.date_from)
            raise RuntimeError("画面が固まった")

        result = self._run_with(cs, select, check, apply)
        self.assertEqual(applies, ["1"])          # やり直しでは書き込まない
        self.assertEqual(selects, ["A", "A"])      # 初めの選択 + やり直しの選択
        self.assertEqual(len(checks), 2)
        reasons = [d.get("reason") for d in result["details"]]
        self.assertEqual(reasons, ["user_not_confirmed_on_retry"] * 2)
        self.assertEqual(result["skipped"], 2)
        self.assert_counts_add_up(result)

    def test_retry_failures_count_toward_stop(self):
        cs = [make_correction(user_name=u, date_from="1", date_to="1", action="add")
              for u in ("A", "B", "C")]
        # A のやり直しで確かめられず (1 人目)、B は選べない (2 人目) → 止まる
        checks = {}

        def select(p, n, m=None, context=None):
            return n != "B"

        def check(p, n):
            checks[n] = checks.get(n, 0) + 1
            return checks[n] == 1

        def apply(p, c, dry_run=False, month_str=None, context=None):
            raise RuntimeError("画面が固まった")

        result = self._run_with(cs, select, check, apply)
        self.assertEqual(result.get("stop_reason"), "page_broken")
        self.assertNotIn("C", checks)
        self.assert_counts_add_up(result)

    def test_recovery_failure_in_move_forces_reselect(self):
        # 付け替えの途中で開き直しに失敗したら、次の 1 件は画面の利用者が合って見えても選び直す
        cs = [make_correction(user_name="A", date_from=d, date_to=d, action="add")
              for d in ("1", "2")]
        selects, applies = [], []

        def select(p, n, m=None, context=None):
            selects.append(n)
            return len(selects) == 1   # 開き直しに失敗したまま → 選べない

        def apply(p, c, dry_run=False, month_str=None, context=None):
            applies.append(c.date_from)
            aa._set_page_unverified(True)
            aa._set_reason("recovery_failed")
            return False

        result = self._run_with(cs, select, lambda p, n: True, apply)
        self.assertEqual(applies, ["1"])
        self.assertEqual(selects, ["A", "A"])
        details = result["details"]
        self.assertEqual(details[0]["reason"], "recovery_failed")
        self.assertEqual(details[1]["reason"], "user_not_found")
        self.assert_counts_add_up(result)

    def test_alternating_failures_do_not_stop(self):
        result, selected, _ = self._run({"B", "D"}, self.corrections())
        self.assertFalse(result.get("stopped"))
        self.assertEqual(selected, ["A", "B", "C", "D"])
        self.assert_counts_add_up(result)


# =============================================================================
# A-1: 付け替えの削除は元の行の終了の時刻も渡す
# =============================================================================

class TestMoveDeletesOldRowByStartAndEnd(unittest.TestCase):
    def setUp(self):
        _pop_reason()

    def test_add_first_delete_passes_end_time_from(self):
        deletes = []

        def fake_delete(page, day, start_time, dry_run=False, staff_name=None, end_time=None):
            deletes.append((day, start_time, staff_name, end_time))
            return True

        c = make_correction()  # 11:00〜11:35 → 10:25〜11:00 (同じ職員)
        with mock.patch.object(aa, "add_schedule_entry", lambda *a, **k: True), \
                mock.patch.object(aa, "delete_schedule_entry", fake_delete), \
                mock.patch("builtins.print", lambda *a, **k: None):
            ok = aa._apply_move_with_reorder(FakePage(), c, dry_run=False, month_str="2026-12")
        self.assertTrue(ok)
        self.assertEqual(deletes, [(24, "11:00", "熊澤妙子", "11:35")])

    def test_same_start_delete_then_add_passes_end_time_from(self):
        deletes = []
        order = []

        def fake_delete(page, day, start_time, dry_run=False, staff_name=None, end_time=None):
            deletes.append((day, start_time, staff_name, end_time))
            order.append("delete")
            return True

        # 開始は同じで終了だけ変わる → 削除→追加
        c = make_correction(start_time_to="11:00", end_time_to="12:00")
        with mock.patch.object(aa, "add_schedule_entry",
                               lambda *a, **k: order.append("add") or True), \
                mock.patch.object(aa, "delete_schedule_entry", fake_delete), \
                mock.patch("builtins.print", lambda *a, **k: None):
            ok = aa._apply_move_with_reorder(FakePage(), c, dry_run=False, month_str="2026-12")
        self.assertTrue(ok)
        self.assertEqual(order, ["delete", "add"])
        self.assertEqual(deletes, [(24, "11:00", "熊澤妙子", "11:35")])

    def test_same_start_recovery_failure_does_not_roll_back(self):
        adds = []
        recovers = []
        c = make_correction(start_time_to="11:00", end_time_to="12:00")
        with mock.patch.object(aa, "add_schedule_entry",
                               lambda page, corr, d=False: adds.append(corr) and False), \
                mock.patch.object(aa, "delete_schedule_entry", lambda *a, **k: True), \
                mock.patch.object(aa, "_recover_schedule_page",
                                  lambda p, m=None, context=None:
                                  recovers.append(context) or False), \
                mock.patch.object(aa, "select_user",
                                  lambda p, n: self.fail("開き直しに失敗したら選ばない")), \
                mock.patch.object(aa, "_schedule_entry_exists",
                                  lambda *a, **k: self.fail("開き直しに失敗したら盤面を見ない")), \
                mock.patch("builtins.print", lambda *a, **k: None):
            ok = aa._apply_move_with_reorder(FakePage(), c, dry_run=False,
                                             month_str="2026-12", context="CTX")
        self.assertFalse(ok)
        self.assertEqual(len(adds), 1)            # ロールバックの追加はしない
        self.assertEqual(recovers, ["CTX"])       # context を渡して再ログインできる
        self.assertEqual(_pop_reason(), "recovery_failed")

    def test_add_first_recovery_failure_stops(self):
        c = make_correction()  # 別の行 → 追加→削除
        with mock.patch.object(aa, "add_schedule_entry", lambda *a, **k: False), \
                mock.patch.object(aa, "delete_schedule_entry",
                                  lambda *a, **k: self.fail("追加に失敗したら消さない")), \
                mock.patch.object(aa, "_recover_schedule_page",
                                  lambda p, m=None, context=None: False), \
                mock.patch.object(aa, "select_user",
                                  lambda p, n: self.fail("開き直しに失敗したら選ばない")), \
                mock.patch("builtins.print", lambda *a, **k: None):
            ok = aa._apply_move_with_reorder(FakePage(), c, dry_run=False, month_str="2026-12")
        self.assertFalse(ok)
        self.assertEqual(_pop_reason(), "recovery_failed")

    def test_ambiguous_delete_keeps_reason(self):
        def fake_delete(*a, **k):
            aa._set_reason("ambiguous_entry")
            return False

        c = make_correction()
        with mock.patch.object(aa, "add_schedule_entry", lambda *a, **k: True), \
                mock.patch.object(aa, "delete_schedule_entry", fake_delete), \
                mock.patch("builtins.print", lambda *a, **k: None):
            ok = aa._apply_move_with_reorder(FakePage(), c, dry_run=False, month_str="2026-12")
        self.assertFalse(ok)
        self.assertEqual(_pop_reason(), "old_row_remains_ambiguous")

    def test_plain_delete_failure_is_duplicate(self):
        c = make_correction()
        with mock.patch.object(aa, "add_schedule_entry", lambda *a, **k: True), \
                mock.patch.object(aa, "delete_schedule_entry", lambda *a, **k: False), \
                mock.patch("builtins.print", lambda *a, **k: None):
            ok = aa._apply_move_with_reorder(FakePage(), c, dry_run=False, month_str="2026-12")
        self.assertFalse(ok)
        self.assertEqual(_pop_reason(), "old_row_remains_duplicate")


# =============================================================================
# B-4: 選ぶ値が空なら選ばない・値を CSS に埋め込まない
# =============================================================================

class FakeSelect:
    def __init__(self, options):
        self.options = options
        self.selected = []
        self.locator_calls = []

    def locator(self, selector):
        self.locator_calls.append(selector)
        raise AssertionError("値を CSS に埋め込んではいけない")

    def evaluate(self, expression, arg=None, timeout=None):
        if ".some(" in expression:
            return arg in self.options
        return self.options.get(arg)

    def select_option(self, value=None):
        self.selected.append(value)


class TestSelectValueAndWait(unittest.TestCase):
    def test_empty_value_rejected(self):
        sel = FakeSelect({"": "-"})
        with mock.patch("builtins.print", lambda *a, **k: None):
            self.assertFalse(aa._select_value_and_wait(FakePage(), sel, "", "職員資格"))
        self.assertEqual(sel.selected, [])

    def test_value_with_quote_is_passed_as_argument(self):
        v = 'a"b'
        sel = FakeSelect({v: "正看"})
        with mock.patch("builtins.print", lambda *a, **k: None):
            self.assertTrue(aa._select_value_and_wait(FakePage(), sel, v, "職員資格"))
        self.assertEqual(sel.selected, [v])
        self.assertEqual(sel.locator_calls, [])

    def test_missing_option_times_out(self):
        sel = FakeSelect({"1": "正看"})
        with mock.patch.object(aa, "SELECT_OPTION_WAIT_MS", 0), \
                mock.patch("builtins.print", lambda *a, **k: None):
            self.assertFalse(aa._select_value_and_wait(FakePage(), sel, "9", "職員資格"))
        self.assertEqual(sel.selected, [])


if __name__ == "__main__":
    unittest.main()
