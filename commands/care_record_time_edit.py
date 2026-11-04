"""
記録書Ⅱの時刻の書き換え - 記録書Ⅱの「実際の訪問時間」を、実績と同じ時刻へそろえる

らく助 (CareFlow) の QR 実績をカイポケの実績へ書いた後 (actual_time_edit.py)、
同じ訪問の記録書Ⅱの「実際の訪問時間」(時・分・時・分の 4 つの select) も同じ時刻にする。
設計: CareFlow02 docs/plans/kaipoke-kiroku2-time-design-2026-11-04.md (§2-2・§3・§4)。

1 件の手順:
  月間スケジュール一覧で実績の行 (日付・実績の開始/終了・職員1) を特定
  → 同じ行の記録書のマーク (div.record-time a[onclick^=submitForm123]) を押す
  → 記録書Ⅱの編集画面 (/bizhnc/careRecordEdit/) で 記録書の番号・利用者 (userSurname+userName)・
    訪問日 (serviceDate)・職員1・旧の時刻 を照らす
  → 4 つの時刻だけを選ぶ (ほかの欄には触らない)。選ぶ前と後でフォーム全体を比べ、時刻の 4 つ以外が変われば戻す
  → dry_run: 時刻を元へ戻し、保存せず「戻る」(#backButton)
     本番: 「保存する」(#editButton) → confirm('入力した内容で変更しますか？') を OK
           → 月間スケジュールへ戻り、同じ行の記録書Ⅱの時刻 (span.time-difference-care-record-document2) を読み直す
           → もう一度その記録書Ⅱを開き、時刻が新しい値で、ほかの欄が保存前と同じか確かめて「戻る」

記録書Ⅱが無い訪問は作らない (skipped / no_care_record)。

使い方 (API から呼ばれる):
    run_care_record_time_edit(items, month="2026-10", dry_run=True)
"""

import re
import sys
import time
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from playwright.sync_api import sync_playwright
    from lib.common import (
        create_browser_context,
        setup_monthly_schedule_page,
        ensure_session,
        save_artifacts,
        set_service_month,
        verify_service_month,
    )
except ImportError as _pw_import_err:  # pragma: no cover
    sync_playwright = None
    create_browser_context = setup_monthly_schedule_page = ensure_session = save_artifacts = None
    set_service_month = verify_service_month = None
    print(f"[warn] Playwright未導入のため実行機能は無効です: {_pw_import_err}", file=sys.stderr)

from commands.auto_apply import (
    _staff_text_matches,
    _check_current_user,
    _wait_schedule_table,
    _remove_floating_overlays,
    _click_with_scroll,
    _wait_visible,
    select_user,
)
from commands.actual_time_edit import (
    MAX_ITEMS,
    ST_SUCCESS,
    ST_DRY_RUN_OK,
    ST_SKIPPED,
    ST_FAILED,
    ST_ERROR,
    normalize_hhmm,
    parse_time_range,
    parse_onclick_params,
    find_target,
    observed_for_day,
    group_by_user,
    collect_actual_entries,
    _collect_with_retry,
    _parse_day,
)
from lib.stop_signal import is_stop_requested


# =============================================================================
# 純粋関数 (ブラウザなしで試験できる部分)
# =============================================================================

def hhmm_to_select_values(hhmm: str) -> list:
    """'9:05' → ['09', '05']。解釈できなければ ValueError"""
    n = normalize_hhmm(hhmm)
    if not n:
        raise ValueError(f"時刻を解釈できません: {hhmm!r}")
    return n.split(":")


def selects_to_hhmm(values) -> tuple:
    """4 つの select の値 ['14', '33', '15', '33'] → ('14:33', '15:33')。読めなければ None"""
    if not values or len(values) != 4:
        return None
    nums = []
    for v in values:
        s = str(v if v is not None else "").strip()
        if not re.fullmatch(r"\d{1,2}", s):
            return None
        nums.append(int(s))
    sh, sm, eh, em = nums
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        return None
    return f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"


def parse_record_time(text: str) -> tuple:
    """一覧の記録書Ⅱの時刻 '14:33 ～ 15:33' (～ / 〜 / ~ / - の揺れ) → ('14:33', '15:33')。無ければ ('', '')"""
    return parse_time_range(text or "")


_SUBMIT_RE = re.compile(r"submitForm123\(\s*'?(\d+)'?\s*,\s*'?(\d+)'?\s*\)")


def parse_submit_form123(onclick: str) -> tuple:
    """'submitForm123(11834626, 1196884772);' → ('11834626', '1196884772')。無ければ ('', '')"""
    m = _SUBMIT_RE.search(onclick or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def normalize_service_date(value) -> str:
    """'2026/10/1' / '2026-10-01' → '2026/10/01'。解釈できなければ ''"""
    m = re.fullmatch(r"\s*(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\s*", str(value or ""))
    if not m:
        return ""
    return f"{int(m.group(1)):04d}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"


def expected_service_date(month: str, day: int) -> str:
    """('2026-10', 1) → '2026/10/01'"""
    y, mo = month.split("-")
    return f"{int(y):04d}/{int(mo):02d}/{int(day):02d}"


def normalize_person_name(value) -> str:
    """名前の空白 (半角・全角) を除く。'青栁 あい' → '青栁あい'"""
    return re.sub(r"[\s\u3000]", "", str(value or ""))


def check_edit_screen(screen: dict, month: str, item: dict, doc_id: str, patient: str) -> str:
    """開いた記録書Ⅱの編集画面が、項目の訪問のものか照らす。

    None = 照らし合わせが通り、旧の時刻のまま (書き換えてよい)。
    それ以外は理由コード:
      doc_unreadable (記録書の番号が読めない) / doc_mismatch (押したマークと違う) /
      patient_unreadable / patient_mismatch (画面の利用者名と patient が空白を除いて完全一致しない) /
      date_mismatch / staff_mismatch /
      time_unreadable / already_applied (すでに新しい時刻) / time_mismatch (旧の時刻と違う)
    """
    shown_doc = str(screen.get("doc_id") or "").strip()
    if not shown_doc:
        return "doc_unreadable"
    if shown_doc != str(doc_id or "").strip():
        return "doc_mismatch"
    shown_patient = normalize_person_name(
        f"{screen.get('user_surname') or ''}{screen.get('user_name') or ''}")
    if not shown_patient:
        return "patient_unreadable"
    if shown_patient != normalize_person_name(patient):
        return "patient_mismatch"
    if normalize_service_date(screen.get("service_date")) != expected_service_date(month, item["day"]):
        return "date_mismatch"
    if not _staff_text_matches(item["staff1"], screen.get("staff_text") or ""):
        return "staff_mismatch"
    times = selects_to_hhmm(screen.get("times"))
    if times is None:
        return "time_unreadable"
    if times == (item["new_start"], item["new_end"]):
        return "already_applied"
    if times != (item["old_start"], item["old_end"]):
        return "time_mismatch"
    return None


# 戻るときのダイアログ: 「登録せずに戻る」側だと文言から分かるものだけ OK する
_DISCARD_WORDS = ("せずに", "破棄", "保存されません", "登録されません", "反映されません", "失われ", "消えます")
# 保存・変更・登録を尋ねる文は、ほかの言葉があっても OK しない
_SAVE_ASK_WORDS = ("保存しますか", "変更しますか", "登録しますか")


def back_dialog_action(dialog_type: str, message: str) -> str:
    """戻るときに出たダイアログをどう閉じるか。'accept' / 'dismiss_unknown'

    - beforeunload: accept = 保存せずにページを離れる
    - alert: OK しか無い (文言は記録する)
    - confirm: 「保存せず」の文言のときだけ OK。それ以外は保存になるおそれがあるのでキャンセルし、失敗で止める
      「保存しますか」「変更しますか」「登録しますか」を含む文は OK しない
    """
    t = (dialog_type or "").lower()
    msg = message or ""
    if t in ("beforeunload", "alert"):
        return "accept"
    if (t == "confirm" and not any(w in msg for w in _SAVE_ASK_WORDS)
            and any(w in msg for w in _DISCARD_WORDS)):
        return "accept"
    return "dismiss_unknown"


def save_dialog_action(dialog_type: str, message: str) -> str:
    """保存のときに出たダイアログをどう閉じるか。'accept_save' / 'accept' / 'dismiss_unknown'"""
    t = (dialog_type or "").lower()
    msg = message or ""
    if t == "confirm" and "変更しますか" in msg and "せずに" not in msg:
        return "accept_save"
    if t == "alert":
        return "accept"
    return "dismiss_unknown"


def form_diff(before: dict, after: dict, ignore=()) -> dict:
    """フォームの値 (名前 → 値、同名は配列) を比べる。{名前: [前, 後]} (無い欄は None)"""
    before, after = before or {}, after or {}
    out = {}
    for k in sorted(set(before) | set(after)):
        if k in ignore:
            continue
        b, a = before.get(k), after.get(k)
        if b != a:
            out[k] = [b, a]
    return out


def validate_request(month, items):
    """API 入力の検査 (/api/care-record-time/apply)。(正規化した items, エラーの一覧)。

    項目: {item_id?, date, patient, staff1, actual_start, actual_end, old_start, old_end, new_start, new_end}
      actual_*: いまのカイポケの実績 (行の鍵) / old_*: いまの記録書Ⅱ / new_*: そろえる時刻
    """
    errors = []
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        errors.append("month は 'YYYY-MM' 形式で指定してください")
        month = ""
    if not isinstance(items, list) or not items:
        return [], errors + ["items (非空配列) が必要です"]
    if len(items) > MAX_ITEMS:
        return [], errors + [f"items は {MAX_ITEMS} 件までです"]

    clean = []
    for i, it in enumerate(items):
        prefix = f"items[{i}]"
        if not isinstance(it, dict):
            errors.append(f"{prefix}: オブジェクトではありません")
            continue
        user = str(it.get("patient") or "").strip()
        staff1 = str(it.get("staff1") or "").strip()
        day = _parse_day(it.get("date"), month)
        times = {k: normalize_hhmm(it.get(k)) for k in
                 ("actual_start", "actual_end", "old_start", "old_end", "new_start", "new_end")}
        bad = []
        if not user:
            bad.append("patient")
        if not staff1:
            bad.append("staff1")
        if day is None:
            bad.append("date")
        bad.extend(k for k, v in times.items() if not v)
        for a, b in (("actual_start", "actual_end"), ("old_start", "old_end"), ("new_start", "new_end")):
            if times[a] and times[b] and times[a] >= times[b]:
                bad.append(f"{a}<{b}")
        if bad:
            errors.append(f"{prefix}: 不正な項目 {bad}")
            continue
        clean.append({"item_id": it.get("item_id"), "user_name": user, "day": day,
                      "staff1": staff1, **times})
    if errors:
        return [], errors
    return clean, []


# =============================================================================
# 画面の操作
# =============================================================================

_RECORD_ANCHOR = "div.record-time a[onclick^='submitForm123']"
_TIME_NAMES = ["visitYmdStartHour", "visitYmdStartMin", "visitYmdEndHour", "visitYmdEndMin"]
_EDIT_SCREEN_MARK = "select[name='visitYmdStartHour']"
_FORM_SELECTOR = "#careRecordUpdateForm"
# 保存で変わって当然の隠し値。保存の後に開き直して比べるときだけ除く (時刻を選ぶ前後の比べには使わない)
_SAVE_VOLATILE_NAMES = frozenset({
    "updateDateAndTimeDocument2",      # 最終更新日時
    "updateUserFullNameDocument2",     # 最終更新者
    "org.apache.struts.taglib.html.TOKEN",
    "_csrf", "csrfToken", "token",
})

_READ_EDIT_SCREEN_JS = """(names) => {
  const byName = n => document.querySelector(`[name="${n}"]`);
  const staff = document.querySelector("select[name='careDocument2Dto.staffMemberInternalId']");
  const so = staff && staff.selectedIndex >= 0 ? staff.options[staff.selectedIndex] : null;
  const docSel = document.querySelector('select#visitDateItemList');
  const sels = names.map(n => document.querySelector(`select[name="${n}"]`));
  const upd = byName('updateDateAndTimeDocument2');
  const sd = byName('serviceDate');
  const us = document.querySelector('input[name="userSurname"]');
  const un = document.querySelector('input[name="userName"]');
  return {
    url: location.href,
    service_date: sd ? sd.value : '',
    user_surname: us ? us.value : '',
    user_name: un ? un.value : '',
    staff_text: so ? so.text.trim() : '',
    doc_id: docSel ? (docSel.value || '') : '',
    times: sels.map(el => el ? el.value : null),
    time_disabled: sels.some(el => !el || el.disabled),
    updated_at: upd ? upd.value : '',
  };
}"""

_READ_FORM_JS = """(sel) => {
  const f = document.querySelector(sel);
  if (!f) return null;
  const out = {};
  for (const [k, v] of new FormData(f)) {
    const s = typeof v === 'string' ? v : ((v && v.name) || '');
    if (Object.prototype.hasOwnProperty.call(out, k)) {
      if (!Array.isArray(out[k])) out[k] = [out[k]];
      out[k].push(s);
    } else {
      out[k] = s;
    }
  }
  return out;
}"""

_PAGE_ERRORS_JS = """() => Array.from(document.querySelectorAll(
    '.error, .alert-danger, .text-danger, .err_msg, .message_error, .errorMessage'))
    .map(e => e.textContent.trim()).filter(t => t && t.length > 2 && t !== '*')
    .map(t => t.substring(0, 200))"""


def _on_edit_screen(page) -> bool:
    """記録書Ⅱの編集画面にいるか (時刻の select がある)"""
    try:
        return page.locator(_EDIT_SCREEN_MARK).count() > 0
    except Exception:
        return False


def _read_edit_screen(page) -> dict:
    return page.evaluate(_READ_EDIT_SCREEN_JS, _TIME_NAMES) or {}


def _read_form(page):
    """フォーム #careRecordUpdateForm の全項目 (new FormData・同名は配列)。読めなければ None"""
    try:
        return page.evaluate(_READ_FORM_JS, _FORM_SELECTOR)
    except Exception:
        return None


def _page_errors(page) -> list:
    try:
        return page.evaluate(_PAGE_ERRORS_JS) or []
    except Exception:
        return []


def _set_care_times(page, start: str, end: str) -> None:
    """4 つの時刻の select を選ぶ。選択肢の値は 2 桁 ('00'〜) とみられるが、数で照らして選ぶ"""
    targets = hhmm_to_select_values(start) + hhmm_to_select_values(end)
    for name, target in zip(_TIME_NAMES, targets):
        sel = f"select[name='{name}']"
        value = page.evaluate("""([s, t]) => {
            const el = document.querySelector(s);
            if (!el) return null;
            const o = Array.from(el.options).find(o => /^\\d+$/.test(o.value) && parseInt(o.value, 10) === t);
            return o ? o.value : null;
        }""", [sel, int(target)])
        if value is None:
            raise RuntimeError(f"{name} に {target} の選択肢がありません")
        page.locator(sel).select_option(value=value, timeout=5000)
        page.wait_for_timeout(200)


def _times_now(page):
    return selects_to_hhmm(_read_edit_screen(page).get("times"))


def _wait_load(page, timeout_ms: int = 15000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


def _ensure_user_and_month(page, user: str, month: str) -> str:
    """月間スケジュールの利用者・月が対象のままか確かめ、違えば合わせる。合わなければ理由コード"""
    _wait_schedule_table(page)
    if not _check_current_user(page, user) and not select_user(page, user):
        return "user_lost"
    try:
        verify_service_month(page, month)
    except Exception:
        try:
            set_service_month(page, month)
            page.wait_for_timeout(1000)
            verify_service_month(page, month)
            if not _check_current_user(page, user) and not select_user(page, user):
                return "user_lost"
        except Exception:
            return "month_lost"
    return None


def _back_to_schedule(page, user: str, month: str) -> tuple:
    """編集画面から「戻る」で月間スケジュールへ。(理由コード or None, 出たダイアログの文言)

    ダイアログは back_dialog_action で「保存せずに戻る」側だと分かるときだけ OK する。
    分からなければキャンセルし back_dialog_unknown で止める (呼び出し側が失敗にする)。
    """
    messages, unknown = [], []

    def on_dialog(dialog):
        messages.append(f"[{dialog.type}] {dialog.message}")
        if back_dialog_action(dialog.type, dialog.message) == "accept":
            dialog.accept()
        else:
            unknown.append(dialog.message)
            dialog.dismiss()

    if _on_edit_screen(page):
        page.on("dialog", on_dialog)
        try:
            btn = page.locator("#backButton")
            try:
                _click_with_scroll(page, btn)
            except Exception as e:
                return "back_failed", messages + [str(e)[:200]]
            _wait_load(page)
            for _ in range(20):
                if not _on_edit_screen(page):
                    break
                page.wait_for_timeout(500)
        finally:
            try:
                page.remove_listener("dialog", on_dialog)
            except Exception:
                pass
        if unknown:
            return "back_dialog_unknown", messages
        if _on_edit_screen(page):
            return "back_failed", messages
    why = _ensure_user_and_month(page, user, month)
    return why, messages


def _save(page) -> tuple:
    """「保存する」を押し、確認 (入力した内容で変更しますか？) を OK する。

    (確認を OK したか, 確認の前に分からないダイアログがあったか,
     確認を OK した後に分からないダイアログがあったか (保存済みの可能性あり), 出たダイアログの文言)
    """
    messages = []
    state = {"confirmed": False, "unknown_before": False, "unknown_after": False}

    def on_dialog(dialog):
        messages.append(f"[{dialog.type}] {dialog.message}")
        act = save_dialog_action(dialog.type, dialog.message)
        if act == "accept_save":
            state["confirmed"] = True
            dialog.accept()
        elif act == "accept":
            dialog.accept()
        else:
            state["unknown_after" if state["confirmed"] else "unknown_before"] = True
            dialog.dismiss()

    page.on("dialog", on_dialog)
    try:
        btn = page.locator("#editButton")
        try:
            btn.click(timeout=10000)
        except Exception:
            btn.click(force=True, no_wait_after=True, timeout=3000)
        _wait_load(page, 20000)
        page.wait_for_timeout(1000)
    finally:
        try:
            page.remove_listener("dialog", on_dialog)
        except Exception:
            pass
    return state["confirmed"], state["unknown_before"], state["unknown_after"], messages


def _save_artifacts(page, prefix: str) -> None:
    try:
        save_artifacts(page, Path("artifacts"), prefix)
    except Exception:
        pass


def _open_record(page, target: dict, rec_onclick: str) -> tuple:
    """一覧の行の記録書のマークを押して編集画面を開く。(理由コード or None, 補足)"""
    idx = target.get("rec_idx", -1)
    # 押す前に、nth の要素が読んだものと同じかを確かめる (描画の入れ替わり対策)
    link = page.locator(_RECORD_ANCHOR).nth(idx)
    try:
        if idx < 0 or (link.get_attribute("onclick") or "") != rec_onclick:
            return "entry_not_found", "一覧が読み直しの間に変わりました"
        _click_with_scroll(page, link)
    except Exception as e:
        return "open_failed", str(e)[:200]
    _wait_load(page)
    if not _wait_visible(page, page.locator(_EDIT_SCREEN_MARK), timeout_ms=20000, label="記録書Ⅱの編集画面"):
        _save_artifacts(page, "care_record_open_failed")
        return "open_failed", None
    return None, None


def _detail(user: str, item: dict, status: str, reason: str = None, **extra) -> dict:
    d = {
        "item_id": item.get("item_id"),
        "user": user,
        "date": str(item["day"]),
        "staff1": item["staff1"],
        "actual_start": item["actual_start"],
        "actual_end": item["actual_end"],
        "old_start": item["old_start"],
        "old_end": item["old_end"],
        "new_start": item["new_start"],
        "new_end": item["new_end"],
        "status": status,
    }
    if reason:
        d["reason"] = reason
    d.update({k: v for k, v in extra.items() if v not in (None, [], "", {})})
    return d


def _screen_summary(screen: dict) -> dict:
    t = selects_to_hhmm(screen.get("times"))
    return {"service_date": screen.get("service_date"), "staff": screen.get("staff_text"),
            "time": "-".join(t) if t else screen.get("times"), "doc_id": screen.get("doc_id")}


def edit_care_record_time(page, user: str, item: dict, month: str, dry_run: bool = True) -> dict:
    """1 件の記録書Ⅱの時刻をそろえる。結果の detail を返す。

    item: {item_id, day, staff1, actual_start, actual_end (行の鍵・actual_end は空なら絞らない),
           old_start, old_end (いまの記録書Ⅱ), new_start, new_end}
    呼び出し前に、画面が対象の月・利用者の月間スケジュールになっていること。
    戻ったときも月間スケジュール (同じ利用者・月) にいる。いられなければ編集画面に残る
    (呼び出し側は _on_edit_screen を見て全体を止める)。
    """
    day = item["day"]
    old, new = (item["old_start"], item["old_end"]), (item["new_start"], item["new_end"])
    print(f"  記録書Ⅱ {day}日 実績 {item['actual_start']}-{item['actual_end']} ({item['staff1']}) "
          f"{old[0]}-{old[1]} → {new[0]}-{new[1]}{' [dry_run]' if dry_run else ''}")

    if _on_edit_screen(page):
        print("    記録書Ⅱの編集画面が開いたままです → 処理を止めます")
        return _detail(user, item, ST_FAILED, "edit_screen_left_open")
    if old == new:
        return _detail(user, item, ST_SKIPPED, "no_change")

    _remove_floating_overlays(page)
    _wait_schedule_table(page)
    entries = _collect_with_retry(page, day)
    target, why = find_target(entries, day, item["actual_start"], item["staff1"], item["actual_end"])
    if why:
        print(f"    実績の行を 1 件に決められません ({why}) → 触らない")
        return _detail(user, item, ST_FAILED, why, observed=observed_for_day(entries, day))

    rec_onclick = target.get("record_onclick") or ""
    if not rec_onclick:
        print("    この訪問に記録書Ⅱがありません → 飛ばす (作らない)")
        return _detail(user, item, ST_SKIPPED, "no_care_record")
    uid, doc_id = parse_submit_form123(rec_onclick)
    params = parse_onclick_params(target.get("onclick", ""))
    if not uid or len(params) < 2 or uid != params[1]:
        print(f"    記録書のマークが実績の行の利用者と合いません ({rec_onclick}) → 触らない")
        return _detail(user, item, ST_FAILED, "record_link_mismatch", message=rec_onclick)

    listed = parse_record_time(target.get("record_time") or "")
    observed_record = "-".join(listed) if listed[0] else ""
    if listed == new:
        print("    一覧の記録書Ⅱはすでに新しい時刻です → スキップ")
        return _detail(user, item, ST_SKIPPED, "already_applied")
    if listed[0] and listed != old:
        print(f"    一覧の記録書Ⅱの時刻 {observed_record} が旧の時刻と違います → 触らない")
        return _detail(user, item, ST_FAILED, "record_time_mismatch", observed_record=observed_record)

    why, note = _open_record(page, target, rec_onclick)
    if why:
        return _detail(user, item, ST_FAILED, why, message=note)

    def back_and_fail(reason, **extra):
        why_back, msgs = _back_to_schedule(page, user, month)
        if why_back:
            print(f"    月間スケジュールへ戻れません ({why_back}) {msgs}")
            _save_artifacts(page, "care_record_back_failed")
            extra["back_reason"] = why_back
        if msgs:
            extra["back_messages"] = msgs
        return _detail(user, item, ST_FAILED, reason, observed_record=observed_record, **extra)

    screen = _read_edit_screen(page)
    why = check_edit_screen(screen, month, item, doc_id, user)
    if why == "already_applied":
        why_back, msgs = _back_to_schedule(page, user, month)
        if why_back:
            return _detail(user, item, ST_FAILED, why_back, back_messages=msgs,
                           screen=_screen_summary(screen))
        print("    記録書Ⅱはすでに新しい時刻です → スキップ")
        return _detail(user, item, ST_SKIPPED, "already_applied")
    if why:
        print(f"    記録書Ⅱが項目と違います ({why}): {_screen_summary(screen)} → 保存せず戻る")
        return back_and_fail(why, screen=_screen_summary(screen))
    if screen.get("time_disabled"):
        print("    時刻の欄が無い・ロックされています → 保存せず戻る")
        return back_and_fail("locked")

    def restore_old():
        try:
            _set_care_times(page, old[0], old[1])
        except Exception as e:
            print(f"    時刻を元へ戻せませんでした: {e}")

    before = _read_form(page)
    if not before:
        print("    フォームを読めません → 保存せず戻る")
        return back_and_fail("form_unreadable")

    try:
        _set_care_times(page, new[0], new[1])
    except Exception as e:
        print(f"    時刻の選択でエラー: {e}")
    if _times_now(page) != new:
        print(f"    時刻を入れられませんでした: {_times_now(page)}")
        restore_old()
        return back_and_fail("time_set_failed")

    # 時刻を選んだ後のフォームを読み比べ、時刻の 4 つ以外が変わっていたら戻す (dry_run でも同じ)
    after = _read_form(page)
    if not after:
        print("    時刻を選んだ後のフォームを読めません → 時刻を戻して保存せず戻る")
        restore_old()
        return back_and_fail("form_unreadable")
    diff = form_diff(before, after)
    others = [k for k in diff if k not in _TIME_NAMES]
    if others:
        print(f"    時刻のほかの欄が変わりました {others} → 時刻を戻して保存せず戻る")
        restore_old()
        return back_and_fail("other_field_changed", fields=others,
                             form_diff={k: diff[k] for k in others})

    if dry_run:
        # 保存しない。変更の確認ダイアログを避けるため、時刻を元へ戻してから戻る
        restore_old()
        why_back, msgs = _back_to_schedule(page, user, month)
        if why_back:
            print(f"    dry_run: 保存せずに戻れません ({why_back}) {msgs}")
            _save_artifacts(page, "care_record_back_failed")
            return _detail(user, item, ST_FAILED, why_back, back_messages=msgs,
                           observed_record=observed_record)
        print("    dry_run: 保存せずに戻りました")
        return _detail(user, item, ST_DRY_RUN_OK, observed_record=observed_record, back_messages=msgs,
                       form_diff=diff)

    confirmed, unknown_before, unknown_after, messages = _save(page)
    if unknown_after:
        print(f"    保存の確認を OK した後に分からないダイアログが出ました {messages} → 保存済みの可能性あり")
        _save_artifacts(page, "care_record_save_unknown")
        return back_and_fail("save_maybe_saved", messages=messages)
    if unknown_before or not confirmed:
        reason = "save_dialog_unknown" if unknown_before else "save_not_confirmed"
        print(f"    保存の確認が想定と違います ({reason}) {messages} → 保存せず戻る")
        _save_artifacts(page, "care_record_save_unknown")
        return back_and_fail(reason, messages=messages)
    if _on_edit_screen(page):
        errs = _page_errors(page)
        if errs:
            print(f"    保存が通りません: {errs}")
            return back_and_fail("save_failed", messages=messages + errs)

    why_back, back_msgs = _back_to_schedule(page, user, month)
    if why_back:
        print(f"    保存の後で月間スケジュールへ戻れません ({why_back})")
        _save_artifacts(page, "care_record_back_failed")
        return _detail(user, item, ST_FAILED, "verify_failed", back_reason=why_back,
                       messages=messages, back_messages=back_msgs, observed_record=observed_record)

    # 一覧を読み直して確かめる (行の鍵は実績の時刻のまま)
    got, t = ("", ""), None
    for attempt in range(3):
        entries = collect_actual_entries(page)
        t, _ = find_target(entries, day, item["actual_start"], item["staff1"], item["actual_end"])
        got = parse_record_time(t.get("record_time") or "") if t else ("", "")
        if got == new:
            print("    一覧で記録書Ⅱの新しい時刻を確認しました")
            break
        if attempt < 2:
            _wait_load(page, 5000)
            page.wait_for_timeout(1500)
    if got != new:
        print(f"    一覧で記録書Ⅱの新しい時刻を確認できません (読めた時刻 {got})")
        return _detail(user, item, ST_FAILED, "verify_failed", observed_record=observed_record,
                       observed_after="-".join(got) if got[0] else "", messages=messages)

    # もう一度その記録書Ⅱを開き、時刻が新しい値で、ほかの欄が保存前と同じか確かめる (保存済み)
    saved = dict(observed_record=observed_record, observed_after="-".join(got), messages=messages)
    why, note = _open_record(page, t, rec_onclick)
    if why:
        print(f"    確かめるために記録書Ⅱを開き直せません ({why})")
        return _detail(user, item, ST_FAILED, "verify_failed", message=f"開き直せません ({why}) {note or ''}".strip(),
                       **saved)
    reopened = _read_edit_screen(page)
    form_after = _read_form(page)
    ident = check_edit_screen(reopened, month, item, doc_id, user)
    diff = form_diff(before, form_after, ignore=_SAVE_VOLATILE_NAMES) if form_after else {}
    others = [k for k in diff if k not in _TIME_NAMES]
    why_back, back_msgs = _back_to_schedule(page, user, month)
    if why_back:
        print(f"    確かめた後に月間スケジュールへ戻れません ({why_back})")
        _save_artifacts(page, "care_record_back_failed")
        saved.update(back_reason=why_back, back_messages=back_msgs)
    if ident != "already_applied" or not form_after:
        # already_applied = 番号・利用者・訪問日・職員1 が合い、時刻が新しい値
        print(f"    開き直した記録書Ⅱを確かめられません ({ident or 'time_not_new'}): {_screen_summary(reopened)}")
        return _detail(user, item, ST_FAILED, "verify_failed", screen=_screen_summary(reopened),
                       message=("フォームを読めません" if not form_after else f"開き直した画面: {ident or 'time_not_new'}"),
                       **saved)
    if others:
        print(f"    保存の後、時刻のほかの欄が変わっています {others} (保存済み)")
        return _detail(user, item, ST_FAILED, "verify_other_field_changed", fields=others, form_diff=diff, **saved)
    if why_back:
        return _detail(user, item, ST_FAILED, "verify_failed", form_diff=diff,
                       message="保存と時刻は確認済み・戻れない", **saved)
    print("    開き直して、時刻が新しい値でほかの欄が変わっていないことを確認しました")
    return _detail(user, item, ST_SUCCESS, form_diff=diff, **saved)


# =============================================================================
# 実行
# =============================================================================

def run_care_record_time_edit(items: list, month: str, dry_run: bool = True, headless: bool = True,
                              progress_callback: callable = None) -> dict:
    """items (validate_request 済み) を利用者ごとに続けて処理する (記録書Ⅱだけ)"""
    start_ts = time.time()
    groups = group_by_user(items)
    result = {
        "month": month, "dry_run": dry_run, "total": len(items),
        "success": 0, "dry_run_ok": 0, "skipped": 0, "failed": 0, "error": 0,
        "details": [],
    }

    def record(d):
        result["details"].append(d)
        result[d["status"]] = result.get(d["status"], 0) + 1
        if progress_callback:
            progress_callback({
                "processed": len(result["details"]), "total": result["total"],
                "current_name": d["user"],
                **{k: result[k] for k in ("success", "dry_run_ok", "skipped", "failed", "error")},
            })

    print(f"\n=== 記録書Ⅱの時刻の書き換え開始 (month={month}, {len(items)}件, "
          f"利用者 {len(groups)}名, dry_run={dry_run}) ===")

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)
        try:
            setup_monthly_schedule_page(page, context, month)
            stopped, stop_reason = False, "stopped"
            for user, user_items in groups:
                if not stopped and is_stop_requested():
                    print("非常停止が要求されました。中断します。")
                    stopped = True
                if not stopped and _on_edit_screen(page):
                    print("    記録書Ⅱの編集画面が開いたままです → 処理を止めます")
                    stopped, stop_reason = True, "edit_screen_left_open"
                if stopped:
                    for it in user_items:
                        record(_detail(user, it, ST_SKIPPED, stop_reason))
                    continue

                print(f"\n--- 利用者: {user} ({len(user_items)}件) ---")
                ensure_session(page, context, month)
                if not select_user(page, user):
                    for it in user_items:
                        record(_detail(user, it, ST_SKIPPED, "user_not_found"))
                    continue

                for it in user_items:
                    if is_stop_requested():
                        stopped = True
                    if stopped:
                        record(_detail(user, it, ST_SKIPPED, stop_reason))
                        continue
                    try:
                        if _on_edit_screen(page):
                            record(_detail(user, it, ST_FAILED, "edit_screen_left_open"))
                            print("    記録書Ⅱの編集画面が開いたままです → 処理を止めます")
                            stopped, stop_reason = True, "edit_screen_left_open"
                            continue
                        if not _check_current_user(page, user) and not select_user(page, user):
                            record(_detail(user, it, ST_SKIPPED, "user_not_found"))
                            continue
                        record(edit_care_record_time(page, user, it, month, dry_run=dry_run))
                    except Exception as e:
                        print(f"    エラー: {e}")
                        _save_artifacts(page, "care_record_time_edit_exception")
                        record(_detail(user, it, ST_ERROR, "exception", message=str(e)[:300]))
                        if not _on_edit_screen(page):
                            try:
                                ensure_session(page, context, month)
                                select_user(page, user)
                            except Exception:
                                pass
                    if not stopped and _on_edit_screen(page):
                        # 戻れなかった (保存したか分からない画面) → 次の行と取り違えないよう全体を止める
                        print("    記録書Ⅱの編集画面から戻れません → 処理を止めます")
                        stopped, stop_reason = True, "edit_screen_left_open"
                    page.wait_for_timeout(500)
            if stopped:
                result["stopped"] = True
        except Exception:
            _save_artifacts(page, "care_record_time_edit_error")
            raise
        finally:
            context.close()
            browser.close()

    result["execution_time_sec"] = round(time.time() - start_ts, 1)
    result["completed_at"] = datetime.datetime.now().isoformat()
    print(f"\n=== 記録書Ⅱの時刻の書き換え完了: success={result['success']} "
          f"dry_run_ok={result['dry_run_ok']} skipped={result['skipped']} "
          f"failed={result['failed']} error={result['error']} ===")
    return result
