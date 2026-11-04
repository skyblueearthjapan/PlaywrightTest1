"""
実績時刻の書き換え - 月間スケジュール一覧の「実績側」の行の開始・終了時刻を変える

らく助 (CareFlow) の QR 実績の時刻を、カイポケの実績側の行
(onclick の showHNC097807Edit の 3 番目の引数が '02') へその場で書き込む。
削除→追加はしない (2026-11-01 人の手で確認: 時刻を変えると「登録する」が無効になるが、
「職員情報入力」を押すと再び押せる。登録で行の数・担当・記録書Ⅱは変わらず、
予定実績管理にも自動で反映される)。

1 件の手順:
  行の特定 (日付・旧の開始時刻・職員1 / 終了時刻は複数一致時の絞り込み)
  → 編集ダイアログ → 予定・実績が「実績」か確認 → 時刻を入れる
  → 「職員情報入力」 → 担当 (職員1〜3・職種・同行) が元のままか確認 (違えば戻す)
  → dry_run: 登録せず閉じる / 本番: 登録 → 一覧を読み直して新しい時刻を確認
  → 項目に care_record (記録書Ⅱの旧の時刻) があれば、続けて同じ行の記録書Ⅱの
    「実際の訪問時間」も新しい時刻へそろえる (commands/care_record_time_edit.py)。
    結果は care_record_status / care_record_reason に別に持つ。

使い方 (API から呼ばれる):
    run_actual_time_edit(items, month="2026-10", dry_run=True)
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
    )
except ImportError as _pw_import_err:  # pragma: no cover
    sync_playwright = None
    create_browser_context = setup_monthly_schedule_page = ensure_session = save_artifacts = None
    print(f"[warn] Playwright未導入のため実行機能は無効です: {_pw_import_err}", file=sys.stderr)

from commands.auto_apply import (
    _staff_text_matches,
    _check_current_user,
    _wait_schedule_table,
    _remove_floating_overlays,
    _click_with_scroll,
    _wait_visible,
    select_user,
    close_edit_dialog,
)
from lib.stop_signal import is_stop_requested


MAX_ITEMS = 3000

# 結果の status
ST_SUCCESS = "success"          # 本番で登録し、一覧で新しい時刻を確認した
ST_DRY_RUN_OK = "dry_run_ok"    # dry_run: 登録の直前まで進み、登録せずに閉じた
ST_SKIPPED = "skipped"          # 何も変えていない (already_applied / user_not_found / stopped)
ST_FAILED = "failed"            # 何も変えていない (登録前に止めた) — verify_failed だけは登録済みの可能性あり
ST_ERROR = "error"              # 例外。登録したかは不明 (要目視)


# =============================================================================
# 純粋関数 (ブラウザなしで試験できる部分)
# =============================================================================

def parse_onclick_params(onclick: str) -> list:
    """showHNC097807Edit('202610', '11834626', '02', '622499927', '3', ...) の引数一覧"""
    if not onclick:
        return []
    return re.findall(r"'([^']*)'", onclick)


def is_actual_onclick(onclick: str) -> bool:
    """実績側のリンクか ('showHNC097807Edit' かつ 3 番目が '02')"""
    if not onclick or "showHNC097807Edit" not in onclick:
        return False
    params = parse_onclick_params(onclick)
    return len(params) >= 3 and params[2] == "02"


def normalize_hhmm(value) -> str:
    """'9:05' / '09:05' / ' 9：05 ' → '09:05'。解釈できなければ ''"""
    if value is None:
        return ""
    s = str(value).strip().replace("：", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return ""
    return f"{h:02d}:{mi:02d}"


_RANGE_RE = re.compile(r"(\d{1,2})[:：](\d{2})\s*[～〜~\-－]\s*(\d{1,2})[:：](\d{2})")


def parse_time_range(text: str):
    """'16:00 ～ 16:35' を含む文字列から ('16:00', '16:35')。無ければ ('', '')"""
    if not text:
        return "", ""
    m = _RANGE_RE.search(text)
    if not m:
        return "", ""
    return (f"{int(m.group(1)):02d}:{m.group(2)}", f"{int(m.group(3)):02d}:{m.group(4)}")


def entry_times(entry: dict):
    """実績のエントリの (開始, 終了)。実績欄 (achievement-service) を優先し、
    無ければ記録書Ⅱの時刻を除いた行のテキストから読む。"""
    s, e = parse_time_range(entry.get("achievement") or "")
    if s:
        return s, e
    area = entry.get("area_text") or ""
    rec = entry.get("record") or ""
    if rec:
        area = area.replace(rec, " ")
    return parse_time_range(area)


def entry_day_matches(entry: dict, day: int) -> bool:
    d = str(day)
    return (entry.get("day") or "").strip() == d or (entry.get("onclick_day") or "").strip() == d


def match_actual_entries(entries: list, day: int, start: str, staff1: str, end: str = "") -> list:
    """実績側のエントリから、日付・開始時刻・職員1 が一致するものを返す。

    複数一致したときだけ終了時刻で絞る (end が空なら絞らない)。
    呼び出し側は 0 件=entry_not_found / 2 件以上=ambiguous として扱う。
    """
    start = normalize_hhmm(start)
    end = normalize_hhmm(end) if end else ""
    hits = []
    for e in entries:
        if not is_actual_onclick(e.get("onclick", "")):
            continue
        if not entry_day_matches(e, day):
            continue
        s, _ = entry_times(e)
        if s != start:
            continue
        if not _staff_text_matches(staff1, e.get("staff1", "")):
            continue
        hits.append(e)
    if len(hits) > 1 and end:
        narrowed = [e for e in hits if entry_times(e)[1] == end]
        if narrowed:
            hits = narrowed
    return hits


def find_target(entries: list, day: int, start: str, staff1: str, end: str = ""):
    """書き換える行を 1 件に決める。(エントリ, None) または (None, 理由コード)

    日付・開始・職員1 で一致したものを、old_end があれば終了時刻でも確かめる。
      0 件 → entry_not_found / 終了が違う → end_mismatch / 2 件以上 → ambiguous
    """
    hits = match_actual_entries(entries, day, start, staff1)
    if not hits:
        return None, "entry_not_found"
    end = normalize_hhmm(end) if end else ""
    if end:
        hits = [e for e in hits if entry_times(e)[1] == end]
        if not hits:
            return None, "end_mismatch"
    if len(hits) > 1:
        return None, "ambiguous"
    return hits[0], None


def is_empty_staff(value: str, text: str) -> bool:
    """担当の select が未選択か ('' / '-999' / 表示 '-')"""
    return (value or "") in ("", "-999") or (text or "").strip() in ("", "-")


def staff_names_mismatch(row_staff: list, dialog_staff: list) -> list:
    """一覧の行の職員1〜3 と、ダイアログの職員1〜3 (未選択は '') の食い違う番号 (1 始まり)"""
    out = []
    for i in range(3):
        r = row_staff[i] if i < len(row_staff) else ""
        d = dialog_staff[i] if i < len(dialog_staff) else ""
        if not r and not d:
            continue
        if not (r and d and _staff_text_matches(r, d)):
            out.append(i + 1)
    return out


def observed_for_day(entries: list, day: int) -> list:
    """失敗時に返す、その日の実績側エントリの一覧 (時刻/職員1)"""
    out = []
    for e in entries:
        if is_actual_onclick(e.get("onclick", "")) and entry_day_matches(e, day):
            s, en = entry_times(e)
            if s or e.get("staff1"):
                out.append(f"{s}-{en}/{(e.get('staff1') or '').strip()}")
    return out


def staff_snapshot_diff(before: dict, after: dict) -> list:
    """担当欄のスナップショットの差 (キー名の一覧)"""
    keys = sorted(set(before) | set(after))
    return [k for k in keys if before.get(k) != after.get(k)]


def _parse_day(value, month: str):
    """'15' / 15 / '2026-10-15' → 15。月が合わない・不正なら None"""
    if value is None:
        return None
    s = str(value).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        if month and f"{int(m.group(1)):04d}-{int(m.group(2)):02d}" != month:
            return None
        d = int(m.group(3))
    elif re.fullmatch(r"\d{1,2}", s):
        d = int(s)
    else:
        return None
    return d if 1 <= d <= 31 else None


def validate_request(month, items):
    """API 入力の検査。(正規化した items, エラーの一覧) を返す。エラーがあれば 1 件も動かさない。"""
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
        user = str(it.get("user_name") or "").strip()
        staff1 = str(it.get("staff1") or "").strip()
        day = _parse_day(it.get("date"), month)
        old_start = normalize_hhmm(it.get("old_start"))
        old_end = normalize_hhmm(it.get("old_end")) if it.get("old_end") not in (None, "") else ""
        new_start = normalize_hhmm(it.get("new_start"))
        new_end = normalize_hhmm(it.get("new_end"))
        bad = []
        if not user:
            bad.append("user_name")
        if not staff1:
            bad.append("staff1")
        if day is None:
            bad.append("date")
        if not old_start:
            bad.append("old_start")
        if it.get("old_end") not in (None, "") and not old_end:
            bad.append("old_end")
        if not new_start:
            bad.append("new_start")
        if not new_end:
            bad.append("new_end")
        if new_start and new_end and new_start >= new_end:
            bad.append("new_start<new_end")
        # 任意: 記録書Ⅱの旧の時刻。あれば実績の後に記録書Ⅱも新しい時刻へそろえる
        care_record = None
        cr = it.get("care_record")
        if cr is not None:
            if not isinstance(cr, dict):
                bad.append("care_record")
            else:
                care_record = {"old_start": normalize_hhmm(cr.get("old_start")),
                               "old_end": normalize_hhmm(cr.get("old_end"))}
                bad.extend(f"care_record.{k}" for k, v in care_record.items() if not v)
        if bad:
            errors.append(f"{prefix}: 不正な項目 {bad}")
            continue
        clean.append({
            "item_id": it.get("item_id"),
            "user_name": user,
            "day": day,
            "old_start": old_start,
            "old_end": old_end,
            "staff1": staff1,
            "new_start": new_start,
            "new_end": new_end,
            "care_record": care_record,
        })
    if errors:
        return [], errors
    return clean, []


def group_by_user(items: list) -> list:
    """利用者ごとにまとめる (最初に出た順)。利用者の中は日付・旧開始の順。"""
    groups = {}
    for it in items:
        groups.setdefault(it["user_name"], []).append(it)
    out = []
    for user, its in groups.items():
        its.sort(key=lambda x: (x["day"], x["old_start"]))
        out.append((user, its))
    return out


# =============================================================================
# 画面の操作
# =============================================================================

_EDIT_ANCHOR = "a[onclick*='showHNC097807Edit']"

# 一覧の showHNC097807Edit リンクを文書順に読み、実績側だけを返す。
# idx は _EDIT_ANCHOR の locator の nth と同じ並び (文書順)。
_COLLECT_JS = """() => {
  const norm = t => (t || '').replace(/\\s+/g, ' ').trim();
  const anchors = Array.from(document.querySelectorAll("a[onclick*='showHNC097807Edit']"));
  const recLinks = Array.from(document.querySelectorAll("div.record-time a[onclick^='submitForm123']"));
  const out = [];
  anchors.forEach((a, idx) => {
    const oc = a.getAttribute('onclick') || '';
    const params = (oc.match(/'([^']*)'/g) || []).map(x => x.slice(1, -1));
    if (params[2] !== '02') return;
    const area = a.closest('div.service-detail-area');
    const ach = area ? area.querySelector('div.achievement-service') : null;
    const rec = area ? area.querySelector('div.record-time') : null;
    // 記録書Ⅱのマーク (同じ行の div.record-time)。area が無いときだけ <tr> から 1 つに決まる場合に読む
    let recBox = rec;
    if (!area) {
      const tr = a.closest('tr');
      const boxes = tr ? tr.querySelectorAll('div.record-time') : [];
      if (boxes.length === 1) recBox = boxes[0];
    }
    const recA = recBox ? recBox.querySelector("a[onclick^='submitForm123']") : null;
    const recSpan = recBox ? recBox.querySelector('span.time-difference-care-record-document2') : null;
    const td = a.closest('td');
    let day = '';
    let sib = td ? td.previousElementSibling : null;
    while (sib) {
      if (sib.matches('td.tac.nowrap')) { day = norm(sib.textContent); break; }
      sib = sib.previousElementSibling;
    }
    const staff = [];
    sib = td ? td.nextElementSibling : null;
    while (sib) {
      if (sib.matches('td.staff-list')) {
        sib.querySelectorAll('tr').forEach(tr => {
          const c = tr.querySelector('td:not(.job-type)');
          if (c) staff.push(norm(c.textContent));
        });
        break;
      }
      sib = sib.nextElementSibling;
    }
    out.push({
      idx: idx, onclick: oc, day: day, onclick_day: params[4] || '',
      achievement: ach ? norm(ach.textContent) : '',
      record: rec ? norm(rec.textContent) : '',
      area_text: area ? norm(area.textContent) : '',
      staff1: staff[0] || '', staff_all: staff,
      record_onclick: recA ? (recA.getAttribute('onclick') || '') : '',
      rec_idx: recA ? recLinks.indexOf(recA) : -1,
      record_time: recSpan ? norm(recSpan.textContent) : (recBox ? norm(recBox.textContent) : ''),
    });
  });
  return out;
}"""

# 担当欄 (職員1〜3・職種・同行) のスナップショット
_STAFF_SNAPSHOT_JS = """() => {
  const snap = {};
  document.querySelectorAll('[id^="chargeStaff"]').forEach(el => {
    if (!el.id) return;
    if (el.tagName === 'SELECT') snap[el.id] = el.value;
    else if (el.type === 'checkbox' || el.type === 'radio') snap[el.id] = el.checked ? '1' : '0';
  });
  return snap;
}"""

_TIME_SELECTS = ["#inPopupStartHour", "#inPopupStartMinute1", "#inPopupStartMinute2",
                 "#inPopupEndHour", "#inPopupEndMinute1", "#inPopupEndMinute2"]


def collect_actual_entries(page) -> list:
    try:
        return page.evaluate(_COLLECT_JS) or []
    except Exception as e:
        print(f"    実績エントリの読み取りエラー: {e}")
        return []


def _collect_with_retry(page, day: int, attempts: int = 3) -> list:
    """その日の実績側エントリが読めるまで最大 3 回走査 (描画待ち)"""
    entries = []
    for attempt in range(attempts):
        entries = collect_actual_entries(page)
        if any(entry_day_matches(e, day) for e in entries):
            return entries
        if attempt < attempts - 1:
            print(f"    {day}日の実績エントリが0件 → 描画待ちで再走査 ({attempt + 1}/{attempts - 1})")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
    return entries


def _read_time_values(page) -> list:
    return page.evaluate("""(sels) => sels.map(s => {
        const el = document.querySelector(s);
        return el ? parseInt(el.value, 10) : null;
    })""", _TIME_SELECTS)


def _expected_time_values(start: str, end: str) -> list:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return [sh, sm // 10, sm % 10, eh, em // 10, em % 10]


def _dialog_open(page) -> bool:
    """編集ダイアログ (登録するボタン) が見えているか"""
    try:
        return page.locator("input#btnRegisPop").is_visible()
    except Exception:
        return False


def _read_dialog_staff(page) -> list:
    """ダイアログの職員1〜3 の [value, 表示名]。select が無い番号は None"""
    return page.evaluate("""() => [1, 2, 3].map(n => {
        const el = document.querySelector('#chargeStaff' + n + 'Id1');
        if (!el) return null;
        const o = el.options[el.selectedIndex];
        return [el.value || '', o ? o.text.trim() : ''];
    })""")


def _dialog_staff_names(raw: list) -> list:
    """_read_dialog_staff の結果を表示名の一覧へ (未選択・欄なしは '')"""
    return ["" if (not v or is_empty_staff(v[0], v[1])) else v[1] for v in (raw or [])]


def _time_selects_locked(page) -> bool:
    """時刻の select のどれかが無い・disabled (確定済み・請求済みのロック) か"""
    return bool(page.evaluate("""(sels) => sels.some(s => {
        const el = document.querySelector(s); return !el || el.disabled; })""", _TIME_SELECTS))


def _set_time_values(page, start: str, end: str) -> None:
    """時刻の 6 つの select を選ぶ。disabled を外すことはしない (edit_schedule_time と違う)"""
    for sel, target in zip(_TIME_SELECTS, _expected_time_values(start, end)):
        value = page.evaluate("""([s, t]) => {
            const el = document.querySelector(s);
            if (!el) return null;
            const o = Array.from(el.options).find(o => parseInt(o.value, 10) === t);
            return o ? o.value : null;
        }""", [sel, target])
        if value is None:
            raise RuntimeError(f"{sel} に {target} の選択肢がありません")
        page.locator(sel).select_option(value=value, timeout=5000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(300)


def _wait_dialog_matches(page, old_values: list, staff1: str, timeout_ms: int = 15000) -> bool:
    """開いたダイアログが特定した行のもの (旧の時刻・職員1) になるまで待つ。

    職員1 の select が無い・未選択の形 (職員情報入力の前) では職員1 は見ない。
    その場合は職員情報入力の後で一覧の行の職員と照らす。
    """
    waited = 0
    while True:
        try:
            if _dialog_open(page) and _read_time_values(page) == old_values:
                names = _dialog_staff_names(_read_dialog_staff(page))
                if not names or not names[0] or _staff_text_matches(staff1, names[0]):
                    return True
        except Exception:
            pass
        if waited >= timeout_ms:
            return False
        page.wait_for_timeout(500)
        waited += 500


def _is_actual_dialog(page) -> bool:
    try:
        return bool(page.evaluate(
            "() => { const r = document.querySelector('#inPopupPlanAchievementsDivision02');"
            " return !!(r && r.checked); }"))
    except Exception:
        return False


def _register_disabled(page) -> bool:
    return bool(page.evaluate(
        "() => { const b = document.querySelector('#btnRegisPop'); return !b || b.disabled; }"))


def _click_staff_info_button(page) -> bool:
    """「職員情報入力」を押す (edit_staff の for_new_entry と同じ候補)"""
    for sel in ["span#input_staff_on", "input[value='職員情報入力']", "input#btnInputStaff",
                "button:has-text('職員情報入力')", "a:has-text('職員情報入力')"]:
        btn = page.locator(sel).first
        try:
            if btn.is_visible(timeout=1500):
                _click_with_scroll(page, btn)
                page.wait_for_timeout(2000)
                _wait_visible(page, page.locator("select#chargeStaff1Id1"),
                              timeout_ms=12000, label="select#chargeStaff1Id1")
                print(f"    「職員情報入力」をクリック ({sel})")
                return True
        except Exception:
            continue
    return False


def _restore_staff(page, before: dict, keys: list) -> None:
    """担当欄を元の値へ戻す。職員 → 職種 → 同行 の順 (職員の onchange が職種を変えるため)"""
    def order(k):
        if re.fullmatch(r"chargeStaff\d+Id\d+", k):
            return 0
        if "JobDivision" in k:
            return 1
        return 2
    for k in sorted(keys, key=order):
        if k not in before:
            continue
        val = before[k]
        try:
            loc = page.locator(f"#{k}")
            tag = loc.evaluate("el => el.tagName")
            if tag == "SELECT":
                loc.select_option(value=val, timeout=5000)
            else:
                loc.set_checked(val == "1", timeout=5000)
            page.wait_for_timeout(300)
        except Exception as e:
            print(f"    担当欄 {k} を戻せませんでした: {e}")


def _close(page) -> None:
    try:
        close_edit_dialog(page)
    except Exception:
        pass


def _register(page) -> tuple:
    """「登録する」を押してモーダルが閉じるのを待つ。(成否, 出たダイアログの文言)

    click_register_button と違い、無効なボタンを強制的に有効にはしない
    (無効のときは register_disabled として呼び出し前に止めている)。
    """
    messages = []

    def on_dialog(dialog):
        messages.append(dialog.message)
        dialog.accept()

    page.on("dialog", on_dialog)
    try:
        btn = page.locator("input#btnRegisPop")
        try:
            btn.click(timeout=10000)
        except Exception:
            btn.click(force=True, no_wait_after=True, timeout=3000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        closed = False
        for _ in range(30):
            try:
                if not btn.is_visible():
                    closed = True
                    break
            except Exception:
                closed = True
                break
            page.wait_for_timeout(500)
        if not closed:
            errs = page.evaluate("""() => Array.from(document.querySelectorAll(
                '.error, .alert-danger, .text-danger, .err_msg, .message_error'))
                .map(e => e.textContent.trim()).filter(t => t && t.length > 2 && t !== '*')
                .map(t => t.substring(0, 200))""")
            messages.extend(errs or [])
        return closed, messages
    finally:
        try:
            page.remove_listener("dialog", on_dialog)
        except Exception:
            pass


def _detail(user: str, item: dict, status: str, reason: str = None, **extra) -> dict:
    d = {
        "item_id": item.get("item_id"),
        "user": user,
        "date": str(item["day"]),
        "old_start": item["old_start"],
        "old_end": item["old_end"],
        "staff1": item["staff1"],
        "new_start": item["new_start"],
        "new_end": item["new_end"],
        "status": status,
    }
    if reason:
        d["reason"] = reason
    if item.get("care_record"):
        # 記録書Ⅱの既定は「実績が書けていないので触らない」。そろえたときは _with_care_record が上書きする
        d["care_record_status"] = ST_SKIPPED
        d["care_record_reason"] = "actual_not_applied"
    d.update({k: v for k, v in extra.items() if v not in (None, [], "")})
    return d


# 実績がこの結果なら、行は新しい時刻になっている → 記録書Ⅱもそろえる
_ACTUAL_AT_NEW = {(ST_SUCCESS, None), (ST_SKIPPED, "already_applied"), (ST_SKIPPED, "no_change")}


def _with_care_record(page, user: str, item: dict, d: dict, month: str, dry_run: bool) -> dict:
    """実績の結果 d の後に、同じ行の記録書Ⅱを新しい時刻へそろえ、d に care_record_* を足す。

    実績が書けた (dry_run なら dry_run_ok) ときだけ動く。行の鍵の実績の時刻は、
    本番で書いた後は新しい時刻、dry_run では旧の時刻のまま。
    """
    cr = item.get("care_record")
    if not cr:
        return d
    if (d["status"], d.get("reason")) in _ACTUAL_AT_NEW:
        key = (item["new_start"], item["new_end"])
    elif d["status"] == ST_DRY_RUN_OK:
        key = (item["old_start"], item["old_end"])
    else:
        return d
    from commands import care_record_time_edit as crt
    cr_item = {"item_id": item.get("item_id"), "day": item["day"], "staff1": item["staff1"],
               "actual_start": key[0], "actual_end": key[1],
               "old_start": cr["old_start"], "old_end": cr["old_end"],
               "new_start": item["new_start"], "new_end": item["new_end"]}
    try:
        c = crt.edit_care_record_time(page, user, cr_item, month, dry_run=dry_run)
    except Exception as e:
        print(f"    記録書Ⅱでエラー: {e}")
        c = {"status": ST_ERROR, "reason": "exception", "message": str(e)[:300]}
    d["care_record_status"] = c["status"]
    d.pop("care_record_reason", None)
    if c.get("reason"):
        d["care_record_reason"] = c["reason"]
    extra = {k: c[k] for k in ("observed", "observed_record", "observed_after", "screen", "messages",
                               "message", "back_reason", "back_messages") if c.get(k)}
    if extra:
        d["care_record_detail"] = extra
    return d


def edit_actual_time(page, user: str, item: dict, dry_run: bool = True) -> dict:
    """実績側の 1 行の時刻を書き換える。結果の detail を返す。

    呼び出し前に、画面が対象の月・利用者になっていること。
    """
    day = item["day"]
    print(f"  実績 {day}日 {item['old_start']}-{item['old_end']} ({item['staff1']}) "
          f"→ {item['new_start']}-{item['new_end']}{' [dry_run]' if dry_run else ''}")

    if (item["old_start"], item["old_end"]) == (item["new_start"], item["new_end"]):
        return _detail(user, item, ST_SKIPPED, "no_change")

    # 閉じ損ねたダイアログが残っていたら、次の行と取り違えないよう触らない (呼び出し側が全体を止める)
    if _dialog_open(page):
        print("    編集ダイアログが開いたままです → 処理を止めます")
        return _detail(user, item, ST_FAILED, "dialog_left_open")

    _remove_floating_overlays(page)
    _wait_schedule_table(page)
    entries = _collect_with_retry(page, day)
    target, why = find_target(entries, day, item["old_start"], item["staff1"], item["old_end"])

    if why == "entry_not_found":
        done = match_actual_entries(entries, day, item["new_start"], item["staff1"], item["new_end"])
        if len(done) == 1 and entry_times(done[0])[1] == item["new_end"]:
            print("    すでに新しい時刻になっています → スキップ")
            return _detail(user, item, ST_SKIPPED, "already_applied")
        print(f"    実績の行が見つかりません。その日の実績: {observed_for_day(entries, day)}")
        return _detail(user, item, ST_FAILED, "entry_not_found",
                       observed=observed_for_day(entries, day))
    if why:
        print(f"    実績の行を 1 件に決められません ({why}) → 触らない")
        return _detail(user, item, ST_FAILED, why, observed=observed_for_day(entries, day))

    observed_old = "-".join(entry_times(target))

    # クリックする前に、nth の要素が読んだものと同じかを確かめる (描画の入れ替わり対策)
    link = page.locator(_EDIT_ANCHOR).nth(target["idx"])
    try:
        if (link.get_attribute("onclick") or "") != target["onclick"]:
            return _detail(user, item, ST_FAILED, "entry_not_found",
                           message="一覧が読み直しの間に変わりました")
        _click_with_scroll(page, link)
    except Exception as e:
        return _detail(user, item, ST_FAILED, "open_failed", message=str(e)[:200])

    if not _wait_visible(page, page.locator("input#btnRegisPop"), timeout_ms=15000,
                         label="編集ダイアログ"):
        _close(page)
        return _detail(user, item, ST_FAILED, "open_failed")

    old_s, old_e = entry_times(target)
    if not _wait_dialog_matches(page, _expected_time_values(old_s, old_e), item["staff1"]):
        print(f"    ダイアログが特定した行と違います: 時刻={_read_time_values(page)} → 閉じる")
        _close(page)
        return _detail(user, item, ST_FAILED, "dialog_mismatch", observed_old=observed_old)

    if not _is_actual_dialog(page):
        print("    予定・実績が「実績」ではありません → 閉じる")
        _close(page)
        return _detail(user, item, ST_FAILED, "not_actual")

    before = page.evaluate(_STAFF_SNAPSHOT_JS)

    if _time_selects_locked(page):
        print("    時刻の欄がロックされています → 閉じる")
        _close(page)
        return _detail(user, item, ST_FAILED, "locked")

    expected = _expected_time_values(item["new_start"], item["new_end"])
    try:
        _set_time_values(page, item["new_start"], item["new_end"])
    except Exception as e:
        print(f"    時刻の選択でエラー: {e}")
    if _read_time_values(page) != expected:
        print(f"    時刻を入れられませんでした: {_read_time_values(page)}")
        _close(page)
        return _detail(user, item, ST_FAILED, "time_set_failed")

    if not _click_staff_info_button(page):
        print("    「職員情報入力」が見つかりません → 閉じる")
        _close(page)
        return _detail(user, item, ST_FAILED, "staff_button_not_found")

    # 押す前に無かった欄は戻しようがないので、押す前にあった欄だけを比べる
    after = page.evaluate(_STAFF_SNAPSHOT_JS)
    diff = [k for k in staff_snapshot_diff(before, after) if k in before]
    if diff:
        print(f"    担当欄が変わりました {diff} → 元へ戻します")
        _restore_staff(page, before, diff)
        after = page.evaluate(_STAFF_SNAPSHOT_JS)
        diff = [k for k in staff_snapshot_diff(before, after) if k in before]
        if diff:
            print(f"    担当欄を戻せません {diff} → 閉じる")
            _close(page)
            return _detail(user, item, ST_FAILED, "staff_changed", fields=diff)

    # 職員1 は必須。押した後の職員1〜3 を一覧の行の職員1〜3 と照らす
    names = _dialog_staff_names(_read_dialog_staff(page))
    if not names or not names[0]:
        print("    職員1 が空です → 閉じる")
        _close(page)
        return _detail(user, item, ST_FAILED, "staff_missing")
    row_staff = target.get("staff_all") or [target.get("staff1", "")]
    bad = staff_names_mismatch(row_staff, names)
    if bad:
        print(f"    担当が一覧の行と違います 職員{bad}: {names} / {row_staff} → 閉じる")
        _close(page)
        return _detail(user, item, ST_FAILED, "staff_changed",
                       fields=[f"chargeStaff{n}Id1" for n in bad])

    if _read_time_values(page) != expected:
        print("    「職員情報入力」の後で時刻が変わりました → 閉じる")
        _close(page)
        return _detail(user, item, ST_FAILED, "time_set_failed",
                       message="職員情報入力の後で時刻が変わった")

    if _register_disabled(page):
        print("    「登録する」が無効のままです → 閉じる")
        _close(page)
        return _detail(user, item, ST_FAILED, "register_disabled")

    if dry_run:
        _close(page)
        print("    dry_run: 登録せずに閉じました")
        return _detail(user, item, ST_DRY_RUN_OK, observed_old=observed_old)

    ok, messages = _register(page)
    if not ok:
        print(f"    登録が通りません: {messages}")
        _close(page)
        return _detail(user, item, ST_FAILED, "register_failed", messages=messages,
                       observed_old=observed_old)
    if messages:
        print(f"    登録時のダイアログ: {messages}")

    # 一覧を読み直して確かめる
    if not _check_current_user(page, user):
        select_user(page, user)
    _wait_schedule_table(page)
    new_hits, old_hits = [], []
    for attempt in range(3):
        entries = collect_actual_entries(page)
        new_hits = match_actual_entries(entries, day, item["new_start"], item["staff1"])
        new_hits = [e for e in new_hits if entry_times(e)[1] == item["new_end"]]
        old_hits = []
        if (item["old_start"], item["old_end"]) != (item["new_start"], item["new_end"]):
            old_hits = [e for e in match_actual_entries(entries, day, item["old_start"], item["staff1"])
                        if not item["old_end"] or entry_times(e)[1] == item["old_end"]]
        if len(new_hits) == 1 and not old_hits:
            print("    一覧で新しい時刻を確認しました")
            return _detail(user, item, ST_SUCCESS, observed_old=observed_old, messages=messages)
        if attempt < 2:
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
    print(f"    一覧で新しい時刻を確認できません。その日の実績: {observed_for_day(entries, day)}")
    return _detail(user, item, ST_FAILED, "verify_failed", observed_old=observed_old,
                   observed=observed_for_day(entries, day), messages=messages)


def _on_care_record_screen(page) -> bool:
    from commands import care_record_time_edit as crt
    return crt._on_edit_screen(page)


# =============================================================================
# 実行
# =============================================================================

def run_actual_time_edit(items: list, month: str, dry_run: bool = True, headless: bool = True,
                         progress_callback: callable = None) -> dict:
    """items (validate_request 済み) を利用者ごとに続けて処理する"""
    start_ts = time.time()
    groups = group_by_user(items)
    result = {
        "month": month,
        "dry_run": dry_run,
        "total": len(items),
        "success": 0,
        "dry_run_ok": 0,
        "skipped": 0,
        "failed": 0,
        "error": 0,
        "details": [],
    }
    if any(it.get("care_record") for it in items):
        result["care_record"] = {k: 0 for k in ("success", "dry_run_ok", "skipped", "failed", "error")}

    def record(d):
        result["details"].append(d)
        result[d["status"]] = result.get(d["status"], 0) + 1
        if "care_record_status" in d:
            cs = result["care_record"]
            cs[d["care_record_status"]] = cs.get(d["care_record_status"], 0) + 1
        if progress_callback:
            progress_callback({
                "processed": len(result["details"]), "total": result["total"],
                "current_name": d["user"],
                **{k: result[k] for k in ("success", "dry_run_ok", "skipped", "failed", "error")},
                **({"care_record": dict(result["care_record"])} if "care_record" in result else {}),
            })

    print(f"\n=== 実績時刻の書き換え開始 (month={month}, {len(items)}件, "
          f"利用者 {len(groups)}名, dry_run={dry_run}) ===")

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)
        try:
            setup_monthly_schedule_page(page, context, month)
            stopped = False
            stop_reason = "stopped"
            for user, user_items in groups:
                if not stopped and is_stop_requested():
                    print("非常停止が要求されました。中断します。")
                    stopped = True
                if stopped:
                    for it in user_items:
                        record(_detail(user, it, ST_SKIPPED, stop_reason))
                    continue

                print(f"\n--- 利用者: {user} ({len(user_items)}件) ---")
                if _dialog_open(page):
                    # 前の利用者のダイアログが閉じていない → 利用者を切り替えずに全体を止める
                    print("    編集ダイアログが開いたままです → 処理を止めます")
                    stopped, stop_reason = True, "dialog_left_open"
                    for it in user_items:
                        record(_detail(user, it, ST_SKIPPED, stop_reason))
                    continue
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
                        if _dialog_open(page):
                            # 利用者の確認・切り替えより先に見る (ダイアログの裏で切り替えない)
                            d = _detail(user, it, ST_FAILED, "dialog_left_open")
                            record(d)
                            print("    編集ダイアログが開いたままです → 処理を止めます")
                            stopped, stop_reason = True, "dialog_left_open"
                            continue
                        # 別の利用者の行を書き換えないよう、毎件確かめる
                        if not _check_current_user(page, user) and not select_user(page, user):
                            record(_detail(user, it, ST_SKIPPED, "user_not_found"))
                            continue
                        d = edit_actual_time(page, user, it, dry_run=dry_run)
                        d = _with_care_record(page, user, it, d, month, dry_run)
                        record(d)
                        if d.get("reason") == "dialog_left_open":
                            stop_reason = "dialog_left_open"
                            stopped = True
                        elif it.get("care_record") and _on_care_record_screen(page):
                            # 記録書Ⅱの編集画面から戻れなかった → 次の行と取り違えないよう全体を止める
                            print("    記録書Ⅱの編集画面から戻れません → 処理を止めます")
                            stop_reason = "edit_screen_left_open"
                            stopped = True
                    except Exception as e:
                        print(f"    エラー: {e}")
                        _close(page)
                        record(_detail(user, it, ST_ERROR, "exception", message=str(e)[:300]))
                        try:
                            ensure_session(page, context, month)
                            select_user(page, user)
                        except Exception:
                            pass
                    page.wait_for_timeout(500)
            if stopped:
                result["stopped"] = True
        except Exception:
            try:
                save_artifacts(page, Path("artifacts"), "actual_time_edit_error")
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()

    result["execution_time_sec"] = round(time.time() - start_ts, 1)
    result["completed_at"] = datetime.datetime.now().isoformat()
    print(f"\n=== 実績時刻の書き換え完了: success={result['success']} dry_run_ok={result['dry_run_ok']} "
          f"skipped={result['skipped']} failed={result['failed']} error={result['error']} ===")
    return result
