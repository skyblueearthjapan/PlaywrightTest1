# -*- coding: utf-8 -*-
"""記録書Ⅱの時刻の書き換え (commands/care_record_time_edit.py・/api/care-record-time/*) と、
実績の書き換え (actual_time_edit) からのつなぎの単体テスト

ブラウザもカイポケも使わない。一覧の読み取り結果 (_COLLECT_JS が返す形の dict) と
モックの page で、照らし合わせ・理由コード・dry_run・API の入力検査を確かめる。

    python -m pytest tests/test_care_record_time_edit.py -q
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import commands.actual_time_edit as ate  # noqa: E402
import commands.care_record_time_edit as crt  # noqa: E402


def entry(day, start, end, staff1, record=None, rec_idx=0, uid="11834626", idx=0):
    """実績の行。record=None なら記録書Ⅱなし、文字なら記録書Ⅱの時刻 ('14:33 ～ 15:33')"""
    oc = (f"showHNC097807Edit('202610', '{uid}', '02', '6224999{idx:02d}', "
          f"'{day}', '88', 'HNC097802')")
    ach = f"{start} ～ {end}"
    e = {
        "idx": idx, "onclick": oc, "day": str(day), "onclick_day": str(day),
        "achievement": ach, "record": record or "",
        "area_text": f"精神基本療養費Ⅰ・正看 {ach} {record or ''}".strip(),
        "staff1": staff1, "staff_all": [staff1],
        "record_onclick": "", "rec_idx": -1, "record_time": "",
    }
    if record is not None:
        e.update(record_onclick=f"submitForm123(11834626, 11968847{rec_idx:02d});",
                 rec_idx=rec_idx, record_time=record)
    return e


ITEM = {
    "item_id": "v-1", "user_name": "青栁", "day": 1, "staff1": "熊澤妙子",
    "actual_start": "14:53", "actual_end": "15:33",
    "old_start": "14:33", "old_end": "15:33",
    "new_start": "14:53", "new_end": "15:33",
}


def screen(date="2026/10/01", staff="熊澤 妙子", times=("14", "33", "15", "33"),
           doc_id="1196884700", disabled=False):
    return {"service_date": date, "staff_text": staff, "times": list(times),
            "doc_id": doc_id, "time_disabled": disabled, "updated_at": "x"}


class TestPure(unittest.TestCase):
    def test_select_values(self):
        self.assertEqual(crt.hhmm_to_select_values("9:05"), ["09", "05"])
        self.assertEqual(crt.hhmm_to_select_values("14:33"), ["14", "33"])
        with self.assertRaises(ValueError):
            crt.hhmm_to_select_values("25:00")

    def test_selects_to_hhmm(self):
        self.assertEqual(crt.selects_to_hhmm(["14", "33", "15", "03"]), ("14:33", "15:03"))
        self.assertEqual(crt.selects_to_hhmm(["9", "5", "10", "0"]), ("09:05", "10:00"))
        self.assertIsNone(crt.selects_to_hhmm(["14", "33", "15", None]))
        self.assertIsNone(crt.selects_to_hhmm(["14", "", "15", "00"]))
        self.assertIsNone(crt.selects_to_hhmm(["24", "00", "15", "00"]))
        self.assertIsNone(crt.selects_to_hhmm(["14", "33"]))

    def test_record_time_wave_dash_variants(self):
        for sep in ["～", "〜", "~", "-", "－"]:
            self.assertEqual(crt.parse_record_time(f"14:33 {sep} 15:33"), ("14:33", "15:33"), sep)
        self.assertEqual(crt.parse_record_time("9:05～10:00"), ("09:05", "10:00"))
        self.assertEqual(crt.parse_record_time(""), ("", ""))
        self.assertEqual(crt.parse_record_time(None), ("", ""))

    def test_submit_form(self):
        self.assertEqual(crt.parse_submit_form123("submitForm123(11834626, 1196884772);"),
                         ("11834626", "1196884772"))
        self.assertEqual(crt.parse_submit_form123("submitForm123('1','2')"), ("1", "2"))
        self.assertEqual(crt.parse_submit_form123("showHNC097807Edit('1')"), ("", ""))

    def test_service_date(self):
        self.assertEqual(crt.normalize_service_date("2026/10/1"), "2026/10/01")
        self.assertEqual(crt.normalize_service_date("2026-10-01"), "2026/10/01")
        self.assertEqual(crt.normalize_service_date(""), "")
        self.assertEqual(crt.expected_service_date("2026-10", 1), "2026/10/01")

    def test_check_edit_screen(self):
        chk = crt.check_edit_screen
        self.assertIsNone(chk(screen(), "2026-10", ITEM, "1196884700"))
        self.assertIsNone(chk(screen(doc_id=""), "2026-10", ITEM, "1196884700"))
        self.assertEqual(chk(screen(doc_id="9"), "2026-10", ITEM, "1196884700"), "doc_mismatch")
        self.assertEqual(chk(screen(date="2026/10/02"), "2026-10", ITEM), "date_mismatch")
        self.assertEqual(chk(screen(date=""), "2026-10", ITEM), "date_mismatch")
        self.assertEqual(chk(screen(staff="高岡はるか"), "2026-10", ITEM), "staff_mismatch")
        self.assertEqual(chk(screen(staff="熊澤妙子(重複)"), "2026-10", ITEM), None)
        self.assertEqual(chk(screen(times=("14", "", "15", "33")), "2026-10", ITEM), "time_unreadable")
        self.assertEqual(chk(screen(times=("14", "53", "15", "33")), "2026-10", ITEM), "already_applied")
        self.assertEqual(chk(screen(times=("14", "30", "15", "33")), "2026-10", ITEM), "time_mismatch")

    def test_dialog_actions(self):
        self.assertEqual(crt.back_dialog_action("confirm", "入力内容を登録せずに終了しますか？"), "accept")
        self.assertEqual(crt.back_dialog_action("confirm", "変更内容が破棄されます。よろしいですか？"), "accept")
        self.assertEqual(crt.back_dialog_action("confirm", "変更内容を保存しますか？"), "dismiss_unknown")
        self.assertEqual(crt.back_dialog_action("beforeunload", ""), "accept")
        self.assertEqual(crt.back_dialog_action("alert", "x"), "accept")
        self.assertEqual(crt.back_dialog_action("prompt", "x"), "dismiss_unknown")
        self.assertEqual(crt.save_dialog_action("confirm", "入力した内容で変更しますか？"), "accept_save")
        self.assertEqual(crt.save_dialog_action("confirm", "削除しますか？"), "dismiss_unknown")
        self.assertEqual(crt.save_dialog_action("alert", "必須です"), "accept")


class TestValidate(unittest.TestCase):
    def _item(self, **k):
        base = {"item_id": 1, "patient": "青栁", "date": "2026-10-01", "staff1": "熊澤妙子",
                "actual_start": "14:53", "actual_end": "15:33", "old_start": "14:33",
                "old_end": "15:33", "new_start": "14:53", "new_end": "15:33"}
        base.update(k)
        return base

    def test_ok(self):
        items, errs = crt.validate_request("2026-10", [self._item(old_start="9:05")])
        self.assertEqual(errs, [])
        self.assertEqual(items[0]["user_name"], "青栁")
        self.assertEqual(items[0]["day"], 1)
        self.assertEqual(items[0]["old_start"], "09:05")

    def test_errors(self):
        v = crt.validate_request
        self.assertTrue(v("2026/10", [self._item()])[1])
        self.assertTrue(v("2026-10", [])[1])
        self.assertTrue(v("2026-10", None)[1])
        self.assertTrue(v("2026-10", [self._item(date="2026-11-01")])[1])
        self.assertTrue(v("2026-10", [self._item(patient="")])[1])
        self.assertTrue(v("2026-10", [self._item(actual_end="")])[1])
        self.assertTrue(v("2026-10", [self._item(old_end="x")])[1])
        self.assertTrue(v("2026-10", [self._item(new_start="16:00")])[1])
        self.assertTrue(v("2026-10", [self._item(actual_start="16:00")])[1])
        with mock.patch.object(crt, "MAX_ITEMS", 1):
            self.assertTrue(v("2026-10", [self._item(), self._item()])[1])
        items, errs = v("2026-10", [self._item(), self._item(staff1="")])
        self.assertEqual(items, [])
        self.assertTrue(errs)


class FakePage:
    def __init__(self, onclick=""):
        self._onclick = onclick

    def locator(self, sel):
        page = self

        class L:
            def nth(self, i):
                return self

            def get_attribute(self, name):
                return page._onclick

        return L()

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, *a, **k):
        pass


class TestEditFlow(unittest.TestCase):
    def _run(self, entries, after=None, dry_run=False, item=ITEM, onclick=None, **over):
        p = {
            "_on_edit_screen": mock.Mock(return_value=False),
            "_remove_floating_overlays": mock.Mock(), "_wait_schedule_table": mock.Mock(),
            "_collect_with_retry": mock.Mock(return_value=entries),
            "collect_actual_entries": mock.Mock(return_value=after if after is not None else entries),
            "_click_with_scroll": mock.Mock(), "_wait_visible": mock.Mock(return_value=True),
            "_read_edit_screen": mock.Mock(return_value=screen()),
            "_set_care_times": mock.Mock(),
            "_times_now": mock.Mock(return_value=("14:53", "15:33")),
            "_back_to_schedule": mock.Mock(return_value=(None, [])),
            "_save": mock.Mock(return_value=(True, False, ["[confirm] 入力した内容で変更しますか？"])),
            "_page_errors": mock.Mock(return_value=[]),
            "_save_artifacts": mock.Mock(),
        }
        p.update(over)
        if onclick is None:
            onclick = entries[0]["record_onclick"] if entries else ""
        with mock.patch.multiple(crt, **p):
            d = crt.edit_care_record_time(FakePage(onclick), "青栁", item, "2026-10", dry_run=dry_run)
        return d, p

    def test_no_care_record_is_skipped(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子")])
        self.assertEqual((d["status"], d["reason"]), ("skipped", "no_care_record"))
        m["_click_with_scroll"].assert_not_called()

    def test_row_found_by_actual_time(self):
        es = [entry(1, "14:33", "15:33", "熊澤妙子", record="14:33 ～ 15:33")]  # 実績はまだ旧
        d, m = self._run(es)
        self.assertEqual(d["reason"], "entry_not_found")
        m["_click_with_scroll"].assert_not_called()

    def test_record_link_other_user(self):
        e = entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33", uid="999")
        d, m = self._run([e])
        self.assertEqual(d["reason"], "record_link_mismatch")
        m["_click_with_scroll"].assert_not_called()

    def test_already_applied_in_list(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:53 〜 15:33")])
        self.assertEqual((d["status"], d["reason"]), ("skipped", "already_applied"))
        m["_click_with_scroll"].assert_not_called()

    def test_list_record_time_mismatch(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:40 ～ 15:33")])
        self.assertEqual((d["status"], d["reason"]), ("failed", "record_time_mismatch"))
        m["_click_with_scroll"].assert_not_called()

    def test_onclick_changed_before_click(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")], onclick="x")
        self.assertEqual(d["reason"], "entry_not_found")
        m["_click_with_scroll"].assert_not_called()

    def test_open_failed(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")],
                         _wait_visible=mock.Mock(return_value=False))
        self.assertEqual(d["reason"], "open_failed")
        m["_set_care_times"].assert_not_called()

    def test_screen_mismatch_goes_back_without_saving(self):
        es = [entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")]
        for scr, reason in [(screen(staff="高岡はるか"), "staff_mismatch"),
                            (screen(date="2026/10/02"), "date_mismatch"),
                            (screen(times=("14", "40", "15", "33")), "time_mismatch"),
                            (screen(doc_id="1"), "doc_mismatch")]:
            d, m = self._run(es, _read_edit_screen=mock.Mock(return_value=scr))
            self.assertEqual((d["status"], d["reason"]), ("failed", reason))
            m["_set_care_times"].assert_not_called()
            m["_save"].assert_not_called()
            m["_back_to_schedule"].assert_called_once()

    def test_screen_already_applied(self):
        es = [entry(1, "14:53", "15:33", "熊澤妙子", record="")]  # 一覧で読めない
        es[0].update(record_onclick="submitForm123(11834626, 1196884700);", rec_idx=0)
        d, m = self._run(es, _read_edit_screen=mock.Mock(return_value=screen(times=("14", "53", "15", "33"))))
        self.assertEqual((d["status"], d["reason"]), ("skipped", "already_applied"))
        m["_save"].assert_not_called()

    def test_locked(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")],
                         _read_edit_screen=mock.Mock(return_value=screen(disabled=True)))
        self.assertEqual(d["reason"], "locked")
        m["_set_care_times"].assert_not_called()

    def test_time_set_failed(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")],
                         _times_now=mock.Mock(return_value=("14:33", "15:33")))
        self.assertEqual(d["reason"], "time_set_failed")
        m["_save"].assert_not_called()

    def test_dry_run_restores_and_goes_back(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")], dry_run=True)
        self.assertEqual(d["status"], "dry_run_ok")
        m["_save"].assert_not_called()
        self.assertEqual([c.args[1:] for c in m["_set_care_times"].call_args_list],
                         [("14:53", "15:33"), ("14:33", "15:33")])
        m["_back_to_schedule"].assert_called_once()

    def test_dry_run_unknown_back_dialog(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")], dry_run=True,
                         _back_to_schedule=mock.Mock(return_value=("back_dialog_unknown", ["[confirm] 保存しますか？"])))
        self.assertEqual((d["status"], d["reason"]), ("failed", "back_dialog_unknown"))
        self.assertEqual(d["back_messages"], ["[confirm] 保存しますか？"])

    def test_live_saves_and_verifies(self):
        es = [entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")]
        after = [entry(1, "14:53", "15:33", "熊澤妙子", record="14:53 ～ 15:33")]
        d, m = self._run(es, after=after)
        self.assertEqual(d["status"], "success")
        self.assertEqual(d["observed_record"], "14:33-15:33")
        m["_save"].assert_called_once()

    def test_live_verify_failed(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")])
        self.assertEqual((d["status"], d["reason"]), ("failed", "verify_failed"))
        self.assertEqual(d["observed_after"], "14:33-15:33")

    def test_live_unknown_save_dialog(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")],
                         _save=mock.Mock(return_value=(False, True, ["[confirm] ?"])))
        self.assertEqual((d["status"], d["reason"]), ("failed", "save_dialog_unknown"))
        m["_back_to_schedule"].assert_called_once()

    def test_live_save_error_on_screen(self):
        d, m = self._run([entry(1, "14:53", "15:33", "熊澤妙子", record="14:33 ～ 15:33")],
                         _on_edit_screen=mock.Mock(side_effect=[False, True]),
                         _page_errors=mock.Mock(return_value=["バイタルは必須です"]))
        self.assertEqual((d["status"], d["reason"]), ("failed", "save_failed"))

    def test_edit_screen_left_open(self):
        d, m = self._run([], _on_edit_screen=mock.Mock(return_value=True))
        self.assertEqual(d["reason"], "edit_screen_left_open")
        m["_collect_with_retry"].assert_not_called()


class TestWithActual(unittest.TestCase):
    """actual_time_edit の項目に care_record があるときのつなぎ"""

    def _item(self, **cr):
        items, errs = ate.validate_request("2026-10", [{
            "item_id": "v-1", "user_name": "青栁", "date": "1", "old_start": "14:33",
            "old_end": "15:33", "staff1": "熊澤妙子", "new_start": "14:53", "new_end": "15:33",
            "care_record": cr or {"old_start": "14:33", "old_end": "15:33"}}])
        self.assertEqual(errs, [])
        return items[0]

    def test_validate_care_record(self):
        it = self._item()
        self.assertEqual(it["care_record"], {"old_start": "14:33", "old_end": "15:33"})
        base = {"user_name": "A", "date": "1", "old_start": "9:00", "staff1": "S",
                "new_start": "9:05", "new_end": "9:40"}
        self.assertIsNone(ate.validate_request("2026-10", [base])[0][0]["care_record"])
        self.assertTrue(ate.validate_request("2026-10", [dict(base, care_record=True)])[1])
        self.assertTrue(ate.validate_request("2026-10", [dict(base, care_record={"old_start": "9:00"})])[1])

    def test_live_success_uses_new_actual_time_as_key(self):
        it = self._item()
        d = ate._detail("青栁", it, "success")
        self.assertEqual((d["care_record_status"], d["care_record_reason"]), ("skipped", "actual_not_applied"))
        with mock.patch.object(crt, "edit_care_record_time",
                               return_value={"status": "success"}) as ed:
            d = ate._with_care_record(mock.Mock(), "青栁", it, d, "2026-10", False)
        cr_item = ed.call_args.args[2]
        self.assertEqual((cr_item["actual_start"], cr_item["actual_end"]), ("14:53", "15:33"))
        self.assertEqual((cr_item["old_start"], cr_item["new_start"]), ("14:33", "14:53"))
        self.assertEqual(d["care_record_status"], "success")
        self.assertNotIn("care_record_reason", d)
        self.assertEqual(d["status"], "success")

    def test_dry_run_uses_old_actual_time_as_key(self):
        it = self._item()
        d = ate._detail("青栁", it, "dry_run_ok")
        with mock.patch.object(crt, "edit_care_record_time",
                               return_value={"status": "dry_run_ok"}) as ed:
            d = ate._with_care_record(mock.Mock(), "青栁", it, d, "2026-10", True)
        cr_item = ed.call_args.args[2]
        self.assertEqual((cr_item["actual_start"], cr_item["actual_end"]), ("14:33", "15:33"))
        self.assertTrue(ed.call_args.kwargs["dry_run"])
        self.assertEqual(d["care_record_status"], "dry_run_ok")

    def test_actual_failed_does_not_touch_care_record(self):
        it = self._item()
        d = ate._detail("青栁", it, "failed", "register_failed")
        with mock.patch.object(crt, "edit_care_record_time") as ed:
            d = ate._with_care_record(mock.Mock(), "青栁", it, d, "2026-10", False)
        ed.assert_not_called()
        self.assertEqual((d["care_record_status"], d["care_record_reason"]), ("skipped", "actual_not_applied"))

    def test_actual_ok_but_care_record_failed_kept_separately(self):
        it = self._item()
        d = ate._detail("青栁", it, "skipped", "already_applied")
        with mock.patch.object(crt, "edit_care_record_time", return_value={
                "status": "failed", "reason": "staff_mismatch", "screen": {"staff": "x"}}):
            d = ate._with_care_record(mock.Mock(), "青栁", it, d, "2026-10", False)
        self.assertEqual((d["status"], d["reason"]), ("skipped", "already_applied"))
        self.assertEqual((d["care_record_status"], d["care_record_reason"]), ("failed", "staff_mismatch"))
        self.assertEqual(d["care_record_detail"], {"screen": {"staff": "x"}})

    def test_care_record_exception_is_error(self):
        it = self._item()
        d = ate._detail("青栁", it, "success")
        with mock.patch.object(crt, "edit_care_record_time", side_effect=RuntimeError("boom")):
            d = ate._with_care_record(mock.Mock(), "青栁", it, d, "2026-10", False)
        self.assertEqual((d["status"], d["care_record_status"], d["care_record_reason"]),
                         ("success", "error", "exception"))

    def test_without_care_record_no_fields(self):
        items, _ = ate.validate_request("2026-10", [{
            "user_name": "A", "date": "1", "old_start": "9:00", "staff1": "S",
            "new_start": "9:05", "new_end": "9:40"}])
        d = ate._detail("A", items[0], "success")
        self.assertNotIn("care_record_status", d)

    def _run_patches(self, edit_actual, on_screen):
        page, ctx, browser = mock.Mock(), mock.Mock(), mock.Mock()
        pw = mock.MagicMock()
        pw.__enter__.return_value = mock.Mock()
        return dict(sync_playwright=mock.Mock(return_value=pw),
                    create_browser_context=mock.Mock(return_value=(browser, ctx, page)),
                    setup_monthly_schedule_page=mock.Mock(), ensure_session=mock.Mock(),
                    select_user=mock.Mock(return_value=True),
                    _check_current_user=mock.Mock(return_value=True),
                    _dialog_open=mock.Mock(return_value=False), edit_actual_time=edit_actual,
                    is_stop_requested=mock.Mock(return_value=False),
                    _on_care_record_screen=on_screen)

    def test_run_counts_and_stops_when_left_on_edit_screen(self):
        items, _ = ate.validate_request("2026-10", [
            {"user_name": "A", "date": str(d), "old_start": "9:00", "staff1": "S", "new_start": "9:05",
             "new_end": "9:40", "care_record": {"old_start": "9:00", "old_end": "9:30"}} for d in (1, 2)])
        edit = mock.Mock(side_effect=lambda pg, u, it, dry_run: ate._detail(u, it, "success"))
        cr = mock.Mock(return_value={"status": "failed", "reason": "back_failed"})
        with mock.patch.multiple(ate, **self._run_patches(edit, mock.Mock(return_value=True))), \
                mock.patch.object(crt, "edit_care_record_time", cr):
            res = ate.run_actual_time_edit(items, "2026-10", dry_run=False)
        self.assertEqual(edit.call_count, 1)
        self.assertTrue(res["stopped"])
        self.assertEqual([d["reason"] if "reason" in d else None for d in res["details"]],
                         [None, "edit_screen_left_open"])
        self.assertEqual(res["care_record"]["failed"], 1)
        self.assertEqual(res["care_record"]["skipped"], 1)


class TestRunCareRecordOnly(unittest.TestCase):
    def test_stops_when_left_on_edit_screen(self):
        items, _ = crt.validate_request("2026-10", [
            {"patient": "A", "date": str(d), "staff1": "S", "actual_start": "9:05", "actual_end": "9:40",
             "old_start": "9:00", "old_end": "9:30", "new_start": "9:05", "new_end": "9:40"}
            for d in (1, 2)] + [
            {"patient": "B", "date": "1", "staff1": "S", "actual_start": "9:05", "actual_end": "9:40",
             "old_start": "9:00", "old_end": "9:30", "new_start": "9:05", "new_end": "9:40"}])
        page, ctx, browser = mock.Mock(), mock.Mock(), mock.Mock()
        pw = mock.MagicMock()
        pw.__enter__.return_value = mock.Mock()
        # 1 件目の後、編集画面から戻れなかった
        on_screen = mock.Mock(side_effect=[False, False, True] + [True] * 10)
        edit = mock.Mock(side_effect=lambda pg, u, it, month, dry_run: crt._detail(u, it, "failed", "back_failed"))
        with mock.patch.multiple(
                crt, sync_playwright=mock.Mock(return_value=pw),
                create_browser_context=mock.Mock(return_value=(browser, ctx, page)),
                setup_monthly_schedule_page=mock.Mock(), ensure_session=mock.Mock(),
                select_user=mock.Mock(return_value=True), _check_current_user=mock.Mock(return_value=True),
                _on_edit_screen=on_screen, edit_care_record_time=edit,
                is_stop_requested=mock.Mock(return_value=False)):
            res = crt.run_care_record_time_edit(items, "2026-10", dry_run=False)
        self.assertEqual(edit.call_count, 1)
        self.assertEqual([d["reason"] for d in res["details"]],
                         ["back_failed", "edit_screen_left_open", "edit_screen_left_open"])
        self.assertEqual([d["status"] for d in res["details"]], ["failed", "skipped", "skipped"])
        self.assertTrue(res["stopped"])


class TestApi(unittest.TestCase):
    ITEM = {"patient": "A", "date": "1", "staff1": "S", "actual_start": "9:05", "actual_end": "9:40",
            "old_start": "9:00", "old_end": "9:30", "new_start": "9:05", "new_end": "9:40"}

    @classmethod
    def setUpClass(cls):
        import api_server
        cls.api = api_server
        cls.client = api_server.app.test_client()
        cls.headers = {"Authorization": f"Bearer {api_server.API_TOKEN}"}

    def test_400_on_bad_input(self):
        r = self.client.post("/api/care-record-time/apply", json={"month": "2026-10", "items": []},
                             headers=self.headers)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/care-record-time/apply", headers=self.headers,
                             json={"month": "2026-10", "dry_run": "false", "items": [self.ITEM]})
        self.assertEqual(r.status_code, 400)

    def test_409_when_running(self):
        with self.api.job_state_lock:
            saved = dict(self.api.current_task)
            self.api.current_task.update({"running": True, "command": "actual_time"})
        try:
            r = self.client.post("/api/care-record-time/apply", headers=self.headers,
                                 json={"month": "2026-10", "items": [self.ITEM]})
            self.assertEqual(r.status_code, 409)
        finally:
            with self.api.job_state_lock:
                self.api.current_task.clear()
                self.api.current_task.update(saved)

    def test_202_and_result(self):
        done = {"total": 1, "dry_run_ok": 1, "details": []}
        with mock.patch("commands.care_record_time_edit.run_care_record_time_edit", return_value=done) as run, \
                mock.patch.object(self.api, "clear_stop"):
            r = self.client.post("/api/care-record-time/apply", headers=self.headers,
                                 json={"month": "2026-10", "job_id": "j2", "items": [self.ITEM]})
            self.assertEqual(r.status_code, 202)
            self.assertTrue(r.get_json()["dry_run"])
            for _ in range(50):
                res = self.client.get("/api/care-record-time/result", headers=self.headers).get_json()
                if res["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["job_id"], "j2")
        self.assertTrue(run.call_args.kwargs["dry_run"])
        self.assertEqual(run.call_args.args[0][0]["user_name"], "A")


if __name__ == "__main__":
    unittest.main()
