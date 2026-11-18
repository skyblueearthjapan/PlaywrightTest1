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
    def _run(self, selectable_users, corrections):
        """selectable_users: 選べる利用者名の集合"""
        page = FakePage()
        selected_calls = []
        applied = []

        def fake_select(p, name, month=None, context=None):
            selected_calls.append(name)
            return name in selectable_users

        def fake_apply(p, c, dry_run=False, month_str=None):
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
                                  lambda p, c, dry_run=False, month_str=None:
                                  applied.append(c.user_name) or True), \
                mock.patch("builtins.print", lambda *a, **k: None):
            result = aa.run_auto_apply("sheet.json", month="2026-12")
        self.assertEqual(result.get("stop_reason"), "page_broken")
        self.assertEqual(applied, ["A", "A"])
        self.assertNotIn("D", calls)
        self.assertEqual(calls, {"A": 1, "B": 2, "C": 2})
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


if __name__ == "__main__":
    unittest.main()
