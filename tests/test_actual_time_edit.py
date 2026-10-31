# -*- coding: utf-8 -*-
"""実績時刻の書き換え (commands/actual_time_edit.py・/api/actual-time/*) の単体テスト

ブラウザもカイポケも使わない。一覧の読み取り結果 (_COLLECT_JS が返す形の dict) と
モックの page で、行の判定・特定・dry_run・理由コード・API の入力検査を確かめる。

実績の入った行の保存済み HTML が無いため、エントリは
artifacts/debug_click_entry_day2_20260210_183454.html の実績側の行
(onclick="showHNC097807Edit('202604', '11834626', '02', '', '', '', 'HNC097802')"、
 div.achievement-service / div.record-time / td.staff-list) の形から作った最小の見本。

    python -m pytest tests/test_actual_time_edit.py -q
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import commands.actual_time_edit as ate  # noqa: E402


def entry(day, start, end, staff1, plan_actual="02", idx=0, record="", ach=None):
    oc = (f"showHNC097807Edit('202610', '11834626', '{plan_actual}', '6224999{idx:02d}', "
          f"'{day}', '88', 'HNC097802')")
    ach_text = f"{start} ～ {end}" if ach is None else ach
    return {
        "idx": idx, "onclick": oc, "day": str(day), "onclick_day": str(day),
        "achievement": ach_text, "record": record,
        "area_text": f"精神基本療養費Ⅰ・正看 {ach_text} {record}".strip(),
        "staff1": staff1, "staff_all": [staff1],
    }


ITEM = {
    "item_id": "v-1", "user_name": "前川七海", "day": 10,
    "old_start": "16:45", "old_end": "17:20", "staff1": "熊澤妙子",
    "new_start": "16:50", "new_end": "17:28",
}


class TestOnclick(unittest.TestCase):
    def test_only_02_is_actual(self):
        self.assertTrue(ate.is_actual_onclick(entry(3, "9:00", "9:30", "a")["onclick"]))
        self.assertFalse(ate.is_actual_onclick(entry(3, "9:00", "9:30", "a", plan_actual="01")["onclick"]))
        self.assertFalse(ate.is_actual_onclick("submitLinkTabHelper(4465191);"))
        self.assertFalse(ate.is_actual_onclick("div_show_pubup_7806('02');"))

    def test_empty_actual_from_saved_html(self):
        # 保存済み HTML の「実績なし」の行: '02' だが日付・時刻は空 → 行の特定には使われない
        oc = "showHNC097807Edit('202604', '11834626', '02', '', '', '', 'HNC097802')"
        e = {"idx": 0, "onclick": oc, "day": "3", "onclick_day": "", "achievement": "",
             "record": "", "area_text": "", "staff1": ""}
        self.assertTrue(ate.is_actual_onclick(oc))
        self.assertEqual(ate.match_actual_entries([e], 3, "16:00", "川名千恵"), [])


class TestTimes(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(ate.normalize_hhmm("9:05"), "09:05")
        self.assertEqual(ate.normalize_hhmm("09：05"), "09:05")
        self.assertEqual(ate.normalize_hhmm("24:00"), "")
        self.assertEqual(ate.normalize_hhmm("9-05"), "")

    def test_achievement_preferred_over_record(self):
        e = entry(10, "16:45", "17:20", "熊澤妙子", record="16:40 ～ 17:25")
        self.assertEqual(ate.entry_times(e), ("16:45", "17:20"))
        e2 = dict(e, achievement="")
        e2["area_text"] = "精神基本療養費Ⅰ・正看 16:45 ～ 17:20 16:40 ～ 17:25"
        self.assertEqual(ate.entry_times(e2), ("16:45", "17:20"))


class TestMatch(unittest.TestCase):
    def test_day_start_staff(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子", idx=1),
              entry(10, "16:45", "17:20", "高岡はるか", idx=2),
              entry(11, "16:45", "17:20", "熊澤妙子", idx=3),
              entry(10, "16:45", "17:20", "熊澤妙子", plan_actual="01", idx=4)]
        hits = ate.match_actual_entries(es, 10, "16:45", "熊澤　妙子", "17:20")
        self.assertEqual([h["idx"] for h in hits], [1])

    def test_staff_with_annotation(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子(重複)")]
        self.assertEqual(len(ate.match_actual_entries(es, 10, "16:45", "熊澤妙子")), 1)

    def test_ambiguous_narrowed_by_end(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子", idx=1),
              entry(10, "16:45", "17:50", "熊澤妙子", idx=2)]
        self.assertEqual([h["idx"] for h in ate.match_actual_entries(es, 10, "16:45", "熊澤妙子", "17:50")], [2])
        self.assertEqual(len(ate.match_actual_entries(es, 10, "16:45", "熊澤妙子")), 2)

    def test_ambiguous_same_end(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子", idx=1),
              entry(10, "16:45", "17:20", "熊澤妙子", idx=2)]
        self.assertEqual(len(ate.match_actual_entries(es, 10, "16:45", "熊澤妙子", "17:20")), 2)

    def test_staff_diff(self):
        self.assertEqual(ate.staff_snapshot_diff({"chargeStaff1Id1": "1", "a": "0"},
                                                 {"chargeStaff1Id1": "2", "a": "0"}),
                         ["chargeStaff1Id1"])


class FakePage:
    """edit_actual_time が使う page の最小のモック (画面の操作は ate の関数ごとモックする)"""

    def __init__(self, onclick):
        self._onclick = onclick

    def locator(self, sel):
        page = self

        class L:
            def nth(self, i):
                return self

            def get_attribute(self, name):
                return page._onclick

        return L()

    def evaluate(self, *a, **k):
        return {"chargeStaff1Id1": "100"}

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, *a, **k):
        pass


class TestEditFlow(unittest.TestCase):
    def _patches(self, entries, **over):
        p = {
            "_remove_floating_overlays": mock.DEFAULT, "_wait_schedule_table": mock.DEFAULT,
            "_collect_with_retry": mock.Mock(return_value=entries),
            "collect_actual_entries": mock.Mock(return_value=entries),
            "_click_with_scroll": mock.DEFAULT,
            "_wait_visible": mock.Mock(return_value=True),
            "_is_actual_dialog": mock.Mock(return_value=True),
            "_dialog_open": mock.Mock(return_value=False),
            "_wait_dialog_matches": mock.Mock(return_value=True),
            "_time_selects_locked": mock.Mock(return_value=False),
            "_set_time_values": mock.DEFAULT,
            "_read_dialog_staff": mock.Mock(return_value=[["100", "熊澤妙子"], ["-999", "-"], ["-999", "-"]]),
            "_read_time_values": mock.Mock(return_value=[16, 5, 0, 17, 2, 8]),
            "_click_staff_info_button": mock.Mock(return_value=True),
            "_register_disabled": mock.Mock(return_value=False),
            "_register": mock.Mock(return_value=(True, [])),
            "_close": mock.DEFAULT,
            "_check_current_user": mock.Mock(return_value=True),
        }
        p.update(over)
        p = {k: (mock.Mock() if v is mock.DEFAULT else v) for k, v in p.items()}

        class Ctx:
            def __enter__(self_inner):
                self_inner.cm = mock.patch.multiple(ate, **p)
                self_inner.cm.__enter__()
                return p

            def __exit__(self_inner, *exc):
                return self_inner.cm.__exit__(*exc)

        return Ctx()

    def test_dialog_left_open_touches_nothing(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        with self._patches(es, _dialog_open=mock.Mock(return_value=True)) as m:
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "dialog_left_open")
        m["_click_with_scroll"].assert_not_called()
        m["_set_time_values"].assert_not_called()

    def test_dialog_mismatch(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        with self._patches(es, _wait_dialog_matches=mock.Mock(return_value=False)) as m:
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "dialog_mismatch")
        # 一覧の行の旧の時刻で待っている
        self.assertEqual(m["_wait_dialog_matches"].call_args.args[1], [16, 4, 5, 17, 2, 0])
        m["_set_time_values"].assert_not_called()
        m["_close"].assert_called_once()

    def test_locked(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        with self._patches(es, _time_selects_locked=mock.Mock(return_value=True)) as m:
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "locked")
        m["_set_time_values"].assert_not_called()
        m["_register"].assert_not_called()

    def test_staff_missing(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        empty = mock.Mock(return_value=[["-999", "-"], None, None])
        with self._patches(es, _read_dialog_staff=empty) as m:
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "staff_missing")
        m["_register"].assert_not_called()

    def test_staff_differs_from_row(self):
        # 一覧の行は 熊澤＋佐藤(同行) なのに、職員情報入力の後は職員2 が空
        e = entry(10, "16:45", "17:20", "熊澤妙子")
        e["staff_all"] = ["熊澤妙子", "佐藤憲二"]
        with self._patches([e]) as m:
            d = ate.edit_actual_time(FakePage(e["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual((d["reason"], d["fields"]), ("staff_changed", ["chargeStaff2Id1"]))
        m["_register"].assert_not_called()

    def test_end_mismatch(self):
        es = [entry(10, "16:45", "17:40", "熊澤妙子")]
        with self._patches(es) as m:
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "end_mismatch")
        m["_click_with_scroll"].assert_not_called()

    def test_dry_run_does_not_register(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        with self._patches(es) as m:
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=True)
        self.assertEqual(d["status"], "dry_run_ok")
        m["_register"].assert_not_called()
        m["_close"].assert_called_once()

    def test_live_registers_and_verifies(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        after = [entry(10, "16:50", "17:28", "熊澤妙子")]
        with self._patches(es, collect_actual_entries=mock.Mock(return_value=after)) as m:
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["status"], "success")
        m["_register"].assert_called_once()

    def test_verify_failed(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        with self._patches(es):  # 登録後も旧の時刻のまま
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual((d["status"], d["reason"]), ("failed", "verify_failed"))

    def test_entry_not_found(self):
        es = [entry(10, "09:00", "09:30", "熊澤妙子")]
        with self._patches(es) as m:
            d = ate.edit_actual_time(FakePage(""), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "entry_not_found")
        self.assertEqual(d["observed"], ["09:00-09:30/熊澤妙子"])
        m["_click_with_scroll"].assert_not_called()

    def test_already_applied(self):
        es = [entry(10, "16:50", "17:28", "熊澤妙子")]
        with self._patches(es) as m:
            d = ate.edit_actual_time(FakePage(""), "前川七海", ITEM, dry_run=False)
        self.assertEqual((d["status"], d["reason"]), ("skipped", "already_applied"))
        m["_click_with_scroll"].assert_not_called()

    def test_ambiguous(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子", idx=1),
              entry(10, "16:45", "17:20", "熊澤妙子", idx=2)]
        with self._patches(es) as m:
            d = ate.edit_actual_time(FakePage(""), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "ambiguous")
        m["_click_with_scroll"].assert_not_called()

    def test_not_actual(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        with self._patches(es, _is_actual_dialog=mock.Mock(return_value=False)) as m:
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "not_actual")
        m["_set_time_values"].assert_not_called()
        m["_register"].assert_not_called()

    def test_register_disabled(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        with self._patches(es, _register_disabled=mock.Mock(return_value=True)) as m:
            d = ate.edit_actual_time(FakePage(es[0]["onclick"]), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "register_disabled")
        m["_register"].assert_not_called()

    def test_staff_changed_and_not_restorable(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        page = FakePage(es[0]["onclick"])
        snaps = iter([{"chargeStaff1Id1": "100"}, {"chargeStaff1Id1": "-999"}, {"chargeStaff1Id1": "-999"}])
        page.evaluate = lambda *a, **k: next(snaps)
        with self._patches(es, _restore_staff=mock.Mock()) as m:
            d = ate.edit_actual_time(page, "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "staff_changed")
        m["_restore_staff"].assert_called_once()
        m["_register"].assert_not_called()

    def test_onclick_changed_before_click(self):
        es = [entry(10, "16:45", "17:20", "熊澤妙子")]
        with self._patches(es) as m:
            d = ate.edit_actual_time(FakePage("other"), "前川七海", ITEM, dry_run=False)
        self.assertEqual(d["reason"], "entry_not_found")
        m["_click_with_scroll"].assert_not_called()


class TestFindTarget(unittest.TestCase):
    def test_reasons(self):
        one = [entry(10, "16:45", "17:20", "熊澤妙子", idx=1)]
        self.assertEqual(ate.find_target(one, 10, "16:45", "熊澤妙子", "17:20")[0]["idx"], 1)
        self.assertEqual(ate.find_target(one, 10, "16:45", "熊澤妙子", "")[0]["idx"], 1)
        self.assertEqual(ate.find_target(one, 10, "16:45", "熊澤妙子", "17:30"), (None, "end_mismatch"))
        self.assertEqual(ate.find_target(one, 10, "09:00", "熊澤妙子"), (None, "entry_not_found"))
        two = one + [entry(10, "16:45", "17:20", "熊澤妙子", idx=2)]
        self.assertEqual(ate.find_target(two, 10, "16:45", "熊澤妙子", "17:20"), (None, "ambiguous"))
        self.assertEqual(ate.find_target(two, 10, "16:45", "熊澤妙子"), (None, "ambiguous"))

    def test_staff_names_mismatch(self):
        self.assertEqual(ate.staff_names_mismatch(["熊澤妙子(重複)"], ["熊澤　妙子", "", ""]), [])
        self.assertEqual(ate.staff_names_mismatch(["熊澤妙子"], ["熊澤妙子", "佐藤憲二"]), [2])
        self.assertEqual(ate.staff_names_mismatch(["熊澤妙子"], ["高岡はるか"]), [1])
        self.assertEqual(ate._dialog_staff_names([["-999", "-"], None, ["5", "佐藤憲二"]]),
                         ["", "", "佐藤憲二"])


class TestRunStopsOnOpenDialog(unittest.TestCase):
    def test_remaining_items_are_not_touched(self):
        items, _ = ate.validate_request("2026-10", [
            {"user_name": "A", "date": "1", "old_start": "9:00", "staff1": "S",
             "new_start": "9:05", "new_end": "9:40"},
            {"user_name": "A", "date": "2", "old_start": "9:00", "staff1": "S",
             "new_start": "9:05", "new_end": "9:40"},
            {"user_name": "B", "date": "1", "old_start": "9:00", "staff1": "S",
             "new_start": "9:05", "new_end": "9:40"}])
        page, ctx, browser = mock.Mock(), mock.Mock(), mock.Mock()
        pw = mock.MagicMock()
        pw.__enter__.return_value = mock.Mock()
        # 1 件目のあとダイアログが閉じずに残った
        opened = mock.Mock(side_effect=[False, False, True, True, True])
        edit = mock.Mock(side_effect=lambda pg, u, it, dry_run: ate._detail(u, it, "failed", "register_failed"))
        with mock.patch.multiple(
                ate, sync_playwright=mock.Mock(return_value=pw),
                create_browser_context=mock.Mock(return_value=(browser, ctx, page)),
                setup_monthly_schedule_page=mock.Mock(), ensure_session=mock.Mock(),
                select_user=mock.Mock(return_value=True), _check_current_user=mock.Mock(return_value=True),
                _dialog_open=opened, edit_actual_time=edit, is_stop_requested=mock.Mock(return_value=False)):
            res = ate.run_actual_time_edit(items, "2026-10", dry_run=False)
        self.assertEqual(edit.call_count, 1)
        self.assertEqual([d["reason"] for d in res["details"]],
                         ["register_failed", "dialog_left_open", "dialog_left_open"])
        self.assertEqual([d["status"] for d in res["details"]], ["failed", "failed", "skipped"])
        self.assertTrue(res["stopped"])


class TestValidateAndGroup(unittest.TestCase):
    def _item(self, **k):
        base = {"item_id": 1, "user_name": "前川七海", "date": "10", "old_start": "16:45",
                "old_end": "17:20", "staff1": "熊澤妙子", "new_start": "16:50", "new_end": "17:28"}
        base.update(k)
        return base

    def test_ok(self):
        items, errs = ate.validate_request("2026-10", [self._item(date="2026-10-10", old_end="")])
        self.assertEqual(errs, [])
        self.assertEqual(items[0]["day"], 10)
        self.assertEqual(items[0]["old_end"], "")

    def test_errors(self):
        self.assertTrue(ate.validate_request("2026/10", [self._item()])[1])
        self.assertTrue(ate.validate_request("2026-10", [])[1])
        self.assertTrue(ate.validate_request("2026-10", [self._item(date="2026-11-10")])[1])
        self.assertTrue(ate.validate_request("2026-10", [self._item(new_start="18:00")])[1])
        self.assertTrue(ate.validate_request("2026-10", [self._item(staff1="")])[1])
        # 1 件でも不正なら全体を動かさない
        items, errs = ate.validate_request("2026-10", [self._item(), self._item(old_start="x")])
        self.assertEqual(items, [])
        self.assertTrue(errs)

    def test_group_keeps_user_rows_together(self):
        items, _ = ate.validate_request("2026-10", [
            self._item(user_name="A", date="5"), self._item(user_name="B", date="1"),
            self._item(user_name="A", date="2")])
        groups = ate.group_by_user(items)
        self.assertEqual([u for u, _ in groups], ["A", "B"])
        self.assertEqual([i["day"] for i in groups[0][1]], [2, 5])


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api_server
        cls.api = api_server
        cls.client = api_server.app.test_client()
        cls.headers = {"Authorization": f"Bearer {api_server.API_TOKEN}"}

    def test_400_on_bad_input(self):
        r = self.client.post("/api/actual-time/apply", json={"month": "2026-10", "items": []},
                             headers=self.headers)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/actual-time/apply", headers=self.headers, json={
            "month": "2026-10", "dry_run": "false",
            "items": [{"user_name": "A", "date": "1", "old_start": "9:00", "staff1": "B",
                       "new_start": "9:05", "new_end": "9:40"}]})
        self.assertEqual(r.status_code, 400)

    def test_409_when_running(self):
        with self.api.job_state_lock:
            saved = dict(self.api.current_task)
            self.api.current_task.update({"running": True, "command": "apply"})
        try:
            r = self.client.post("/api/actual-time/apply", headers=self.headers, json={
                "month": "2026-10",
                "items": [{"user_name": "A", "date": "1", "old_start": "9:00", "staff1": "B",
                           "new_start": "9:05", "new_end": "9:40"}]})
            self.assertEqual(r.status_code, 409)
        finally:
            with self.api.job_state_lock:
                self.api.current_task.clear()
                self.api.current_task.update(saved)

    def test_202_and_result(self):
        done = {"total": 1, "dry_run_ok": 1, "details": []}
        with mock.patch("commands.actual_time_edit.run_actual_time_edit", return_value=done) as run, \
                mock.patch.object(self.api, "clear_stop"):
            r = self.client.post("/api/actual-time/apply", headers=self.headers, json={
                "month": "2026-10", "job_id": "j1",
                "items": [{"user_name": "A", "date": "1", "old_start": "9:00", "staff1": "B",
                           "new_start": "9:05", "new_end": "9:40"}]})
            self.assertEqual(r.status_code, 202)
            self.assertTrue(r.get_json()["dry_run"])  # 既定は dry_run
            for _ in range(50):
                res = self.client.get("/api/actual-time/result", headers=self.headers).get_json()
                if res["status"] != "running":
                    break
                import time
                time.sleep(0.05)
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["job_id"], "j1")
        self.assertTrue(run.call_args.kwargs["dry_run"])


if __name__ == "__main__":
    unittest.main()
