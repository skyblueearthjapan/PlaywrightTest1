"""
自動適用エンジン - 修正シートに基づいてスケジュールを自動編集

使い方:
    # Step 1: 差分比較して修正シートを生成
    python -m lib.diff_engine --current data/current_202604.csv --optimized data/optimized_202604.csv --output data/correction_sheet.json

    # Step 2: 修正シートを確認（手動）

    # Step 3: 修正を適用
    python commands/auto_apply.py --sheet data/correction_sheet.json --month 2026-04
    python commands/auto_apply.py --sheet data/correction_sheet.json --month 2026-04 --dry-run  # テスト実行
    python commands/auto_apply.py --sheet data/correction_sheet.json --month 2026-04 --headed   # ブラウザ表示

2フェーズ処理:
    Phase 1: スケジュール修正（利用者別タブ） - 医療保険/介護保険の変更・追加・削除
    Phase 2: イベント追加（職員別タブ） - 個別業務の新規登録
"""

import sys
import re
import datetime
import threading
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Playwright 未導入環境 (純Pythonのヘルパー単体テスト等) でも
# このモジュールを import できるようにする。実行時に None なら当然動かない。
try:
    from playwright.sync_api import sync_playwright
    from lib.common import (
        create_browser_context,
        setup_monthly_schedule_page,
        ensure_session,
        save_artifacts,
        parse_month,
        goto_monthly_schedule,
        set_service_month,
        verify_service_month,
    )
except ImportError as _pw_import_err:  # pragma: no cover
    sync_playwright = None
    create_browser_context = setup_monthly_schedule_page = ensure_session = None
    save_artifacts = parse_month = goto_monthly_schedule = set_service_month = None
    verify_service_month = None
    print(f"[warn] Playwright未導入のため実行機能は無効です: {_pw_import_err}", file=sys.stderr)
from lib.diff_engine import load_correction_sheet, Correction, parse_time
from lib.stop_signal import is_stop_requested, clear_stop


# =============================================================================
# ヘルパー関数
# =============================================================================

def normalize_name(name: str) -> str:
    """名前を正規化（全角/半角スペース統一 + 漢字の異体字を統一）"""
    # スペース正規化
    name = name.replace("\u3000", " ").strip()
    # 漢字の異体字を統一（カイポケで確認された異体字ペア）
    variant_map = {
        "栁": "柳",  # U+6801 → U+67F3
        "﨑": "崎",  # U+FA11 → U+5D0E
        "髙": "高",  # U+9AD9 → U+9AD8
        "濵": "浜",  # U+6FF5 → U+6D5C
        "邊": "辺",  # U+908A → U+8FBA
        "廣": "広",  # U+5EE3 → U+5E83
        "齋": "斎",  # U+9F4B → U+658E
        "齊": "斎",  # U+9F4A → U+658E
        "澤": "沢",  # U+6FA4 → U+6CA2
        "櫻": "桜",  # U+6AFB → U+685C
        "槇": "槙",  # U+69C7 → U+69D9 (2026-08-23 実データ: 槇 恵 / 槙　恵)
        "渡邉": "渡辺",
        "渡邊": "渡辺",
    }
    for old, new in variant_map.items():
        name = name.replace(old, new)
    # スペース完全除去 (2026-08-21 C2実機テストで発覚: らく助「髙梨桂子」と
    # カイポケ「髙梨　桂子」の空白差で職員選択が失敗し「-」登録になった)。
    # name_matches は包含判定のため、比較キーから空白を消すのが最も頑健。
    name = re.sub(r"[\s\u3000]+", "", name)
    return name


def name_matches(needle: str, haystack: str) -> bool:
    """名前が含まれているか判定（全角/半角スペースを無視して比較）"""
    return normalize_name(needle) in normalize_name(haystack)


# --- 失敗理由の記録 (run_auto_apply の details["reason"] 用) -------------------
# 2026-09-03: 本番178件中22件失敗した際、results から原因が読み取れず
# ログを目視するしかなかった。失敗地点で理由を積み、Phase 1 で回収する。
# run_auto_apply は API サーバのデーモンスレッドから呼ばれるため、
# スレッド間で理由が混ざらないよう threading.local に持つ。
_last_failure_reason = threading.local()


def _set_reason(reason: str) -> None:
    """直近の失敗理由を記録する（後勝ち: 最後に判明した理由が残る・スレッド毎）"""
    _last_failure_reason.value = reason


def _pop_reason():
    """記録済みの失敗理由を取り出してクリアする（未記録なら None）"""
    reason = getattr(_last_failure_reason, "value", None)
    _last_failure_reason.value = None
    return reason


# --- ページを開き直せたか (2026-11-19 レビューの指摘) ---------------------------
# 開き直しに失敗したページは、提供月も利用者も確かめられていない。
# この印が立っている間は書き込まず、利用者を選ぶ前に必ず開き直す。
_page_state = threading.local()


def _page_unverified() -> bool:
    """開き直しに失敗したまま (提供月を確かめられていない) なら True"""
    return getattr(_page_state, "unverified", False)


def _set_page_unverified(value: bool) -> None:
    _page_state.unverified = value


def _normalize_staff_text(text: str) -> str:
    """職員名を比較用に正規化（NFKC + 括弧注記除去 + 異体字/空白統一）

    カイポケの職員セルは「熊澤妙子(重複)」のように注記が付くため、
    括弧内を落としてから normalize_name に通す。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    return normalize_name(text)


def _staff_text_matches(staff_name: str, staff_text: str) -> bool:
    """職員1のセルテキストが指定職員と同一人物か判定（正規化後の完全一致）

    2026-09-03のレビュー指摘: 部分一致だと
      - 同行の職員2 (小西+熊澤) が職員1として一致してしまう
      - 短い姓 (森) が別人 (大森) に一致してしまう
    ため、_row_staff_text が返す「職員1の名前セルだけ」と完全一致で比較する。
    """
    if not staff_name or not staff_text:
        return False
    needle = _normalize_staff_text(staff_name)
    target = _normalize_staff_text(staff_text)
    return bool(needle) and needle == target


def _is_plan_onclick(onclick: str) -> bool:
    """エントリのaタグが「予定側」か判定

    showHNC097807Edit('202604','11834626','01',...) の3番目が
      '01' = 予定 / '02' = 実績。実績側を掴むと削除も編集も的外れになる。
    """
    if not onclick:
        return False
    params = re.findall(r"'([^']*)'", onclick)
    return len(params) >= 3 and params[2] == "01"


def _onclick_day(onclick: str) -> str:
    """エントリのaタグの onclick から日にち（5番目のパラメータ）を取り出す"""
    if not onclick:
        return ""
    params = re.findall(r"'([^']*)'", onclick)
    return params[4] if len(params) >= 5 else ""


def _needs_add_first(date_from, start_time_from, date_to, start_time_to) -> bool:
    """削除→追加ではなく「追加→削除」で処理すべきか

    旧行キー (日付, 開始時刻) と新行キー が異なる = 盤面上で別行になる場合は、
    先に追加しても旧行と衝突しないので追加を先に行える（追加失敗時も消失ゼロ）。
    同一キーの場合は先に削除しないと同じ行を二重に作るため、従来順のまま。
    """
    old_key = (str(date_from or "").strip(), str(start_time_from or "").strip())
    new_key = (str(date_to or "").strip(), str(start_time_to or "").strip())
    return new_key != old_key


# 削除→再追加の「削除だけ終わった」状態を修正1件に記録する印。
# run_auto_apply は例外時に apply_correction を頭から再実行する (max_retries=1) ため、
# 印が無いとリトライ側が既に消えた行を削除しようとして entry_not_found で終わり、
# 訪問が消えたまま「予定が見つかりません」と報告されてしまう。
_DELETE_DONE_ATTR = "_reorder_delete_done"


def _mark_delete_done(correction: "Correction") -> None:
    """検証済みの削除が完了したことを記録する（リトライは追加から再開する）"""
    try:
        setattr(correction, _DELETE_DONE_ATTR, True)
    except Exception:  # pragma: no cover - dataclassは通常setattr可能
        pass


def _is_delete_done(correction: "Correction") -> bool:
    return bool(getattr(correction, _DELETE_DONE_ATTR, False))


def _clear_delete_done(correction: "Correction") -> None:
    """この修正の決着がついた（もうリトライしない）ので印を消す"""
    try:
        setattr(correction, _DELETE_DONE_ATTR, False)
    except Exception:  # pragma: no cover
        pass


def _build_rollback_correction(correction: "Correction") -> "Correction":
    """削除→追加の追加が失敗した時、元の予定を復元するための Correction を組む"""
    return Correction(
        user_name=correction.user_name,
        date_from=correction.date_from,
        date_to=correction.date_from,
        start_time_from=correction.start_time_from,
        start_time_to=correction.start_time_from,
        end_time_from=correction.end_time_from,
        end_time_to=correction.end_time_from,
        staff1_from=correction.staff1_from,
        staff1_to=correction.staff1_from,
        staff2_from=correction.staff2_from,
        staff2_to=correction.staff2_from,
        # 請求区分が変わる修正では correction.service_type は「新しい値」なので、
        # 復元には変更前の値 (service_type_from) を優先して使う。
        service_type=correction.service_type_from or correction.service_type,
        action="add",
        business_type=correction.business_type,
        remarks=correction.remarks,
    )


def calculate_santei_time(start_time: str, end_time: str) -> str:
    """
    開始時間と終了時間から算定時間の選択値を計算（介護保険用）

    カイポケの算定時間セレクトの選択肢（実際の表記）:
    - "なし"
    - "20分未満"
    - "30分未満"
    - "30分以上～1時間未満"
    - "1時間以上～1時間半未満"
    - "1時間半以上"
    """
    sh, sm = parse_time(start_time)
    eh, em = parse_time(end_time)
    duration_minutes = (eh * 60 + em) - (sh * 60 + sm)

    if duration_minutes < 20:
        return "20分未満"
    elif duration_minutes < 30:
        return "30分未満"
    elif duration_minutes < 60:
        return "30分以上～1時間未満"
    elif duration_minutes < 90:
        return "1時間以上～1時間半未満"
    else:
        return "1時間半以上"


# =============================================================================
# 利用者選択・スケジュールエントリ操作
# =============================================================================

def _get_user_dropdown(page):
    """利用者選択ドロップダウンを取得 (div#user_search select.form-control)"""
    # スクリーンショットで確認済みの正確なセレクタ
    selectors = [
        "div#user_search select.form-control",
        "td.pulldownUser select.form-control",
        "div#user_search select",
    ]
    for selector in selectors:
        dropdown = page.locator(selector)
        try:
            if dropdown.count() > 0 and dropdown.first.is_visible(timeout=2000):
                return dropdown.first
        except Exception:
            continue
    return None


def _check_current_user(page, user_name: str) -> bool:
    """現在選択されている利用者かどうかを確認（ドロップダウンの選択値 + ページタイトル）"""
    # 方法1: ドロップダウンの選択中optionテキストで判定（最も正確）
    dropdown = _get_user_dropdown(page)
    if dropdown:
        try:
            # 選択中のoptionのテキストを取得
            selected_text = dropdown.evaluate(
                "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''"
            )
            if selected_text and name_matches(user_name, selected_text):
                return True
        except Exception:
            pass

    # 方法2: ページタイトル「○○様　月間スケジュール一覧」で判定
    try:
        # ページ上部のタイトルテキストを確認
        title_selectors = [
            "h1", "h2", "h3", "h4",
            ".user-name", ".patient-name",
            "#userName", ".page-title",
        ]
        for selector in title_selectors:
            elems = page.locator(selector).all()
            for elem in elems:
                if elem.is_visible(timeout=300):
                    text = elem.text_content()
                    if name_matches(user_name, text):
                        return True
    except Exception:
        pass

    return False


def _wait_schedule_table(page, timeout: int = 8000) -> None:
    """月間スケジュール表の行が描画されるまで待つ（失敗しても握り潰す）

    2026-09-03: 利用者切替直後は表が未描画で、直後の走査が
    「エントリが見つかりません」になった（吉川9/9・園田9/8・木村9/7）。
    """
    try:
        page.wait_for_selector("table tr td.tac.nowrap", timeout=timeout)
    except Exception:
        pass


def select_user(page, user_name: str) -> bool:
    """
    利用者を選択（div#user_search のドロップダウンを使用）

    Args:
        page: Playwrightのページオブジェクト
        user_name: 利用者名

    Returns:
        bool: 成功したかどうか
    """
    print(f"利用者を選択しています: {user_name}")

    # 現在の画面に表示されている利用者を確認
    if _check_current_user(page, user_name):
        print(f"  すでに選択されています: {user_name}")
        _wait_schedule_table(page)
        return True

    # 方法1: div#user_search のドロップダウンから直接選択（O(1)・最速）
    dropdown = _get_user_dropdown(page)
    if dropdown:
        try:
            # 全optionのテキストを取得して名前マッチ
            options_data = dropdown.evaluate("""el => {
                return Array.from(el.options).map((opt, i) => ({
                    index: i,
                    value: opt.value,
                    text: opt.text
                }));
            }""")
            for opt in options_data:
                if name_matches(user_name, opt["text"]):
                    dropdown.select_option(value=opt["value"])
                    page.wait_for_load_state("networkidle", timeout=10000)
                    page.wait_for_timeout(1000)
                    # 2026-09-03: 利用者切替直後に表がまだ描画されておらず、
                    # 直後の click_schedule_entry が「エントリが見つかりません」に
                    # なる事故が多発した（吉川9/9・園田9/8・木村9/7）。
                    _wait_schedule_table(page)
                    print(f"  ドロップダウンから直接選択: {user_name} (value={opt['value']})")
                    return True
            print(f"  ドロップダウンに利用者が見つかりません: {user_name}")
            print(f"  ドロップダウンの選択肢: {[o['text'] for o in options_data[:10]]}...")
        except Exception as e:
            print(f"  ドロップダウン選択でエラー: {e}")

    # 方法2: 「次へ」ボタンで移動（フォールバック）
    print(f"  フォールバック: 「次へ」ボタンで探索中...")
    max_attempts = 70
    for i in range(max_attempts):
        if _check_current_user(page, user_name):
            print(f"  {i+1}回目で発見: {user_name}")
            _wait_schedule_table(page)
            return True

        next_btn = page.locator("td.linkNextUser a, a:has-text('次へ')").first
        try:
            if not next_btn.is_visible(timeout=1000):
                break
            next_btn.click()
            page.wait_for_load_state("networkidle", timeout=10000)
            page.wait_for_timeout(1000)
        except Exception:
            break

    print(f"  利用者が見つかりません: {user_name}")
    return False


def _save_debug_on_failure(page, day: int, start_time: str):
    """click_schedule_entry失敗時のデバッグ情報を保存"""
    try:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        screenshot_path = f"artifacts/debug_click_entry_day{day}_{timestamp}.png"
        page.screenshot(path=screenshot_path)
        print(f"    デバッグ: スクリーンショット保存 → {screenshot_path}")

        html_path = f"artifacts/debug_click_entry_day{day}_{timestamp}.html"
        html_content = page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"    デバッグ: HTML保存 → {html_path}")
    except Exception as debug_err:
        print(f"    デバッグ保存エラー: {debug_err}")


def _remove_floating_overlays(page):
    """画面上のフローティング要素を除去（クリックブロック防止）"""
    try:
        page.evaluate("""() => {
            // ASPエラーバナー、お問合せボタン、KARTE等を除去
            const selectors = [
                '.karte-r', '.karte-g', '[id^="karte-"]',
                '[class*="asp-error"]', '[class*="floating"]',
                '[class*="chat-widget"]', '[id*="inquiry"]',
            ];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => el.remove());
            });
            // 高z-indexのオーバーレイを除去
            document.querySelectorAll('*').forEach(el => {
                const z = parseInt(window.getComputedStyle(el).zIndex);
                if (z > 2147483000) el.remove();
            });
            // position:fixedの小さな要素（バナー、チャット等）を除去
            document.querySelectorAll('*').forEach(el => {
                const style = window.getComputedStyle(el);
                if (style.position === 'fixed' && el.tagName !== 'HTML' && el.tagName !== 'BODY') {
                    const rect = el.getBoundingClientRect();
                    // ヘッダー（top=0, 幅が広い）は残す、小さいバナーは除去
                    if (rect.height < 100 && rect.bottom > 500) {
                        el.remove();
                    }
                }
            });
        }""")
    except Exception:
        pass


def _safe_click(page, locator, timeout=10000, description=""):
    """要素をクリック（タイムアウト時はforce+noWaitAfter→JSクリックにフォールバック）"""
    try:
        locator.click(timeout=timeout)
    except Exception:
        # Step 2: force + no_wait_after でPlaywrightクリック（DOMイベント正常発火、JSF対応）
        try:
            if description:
                print(f"    {description}: Playwrightクリックタイムアウト → force+noWaitAfterで再試行")
            locator.click(force=True, no_wait_after=True, timeout=3000)
            page.wait_for_timeout(1000)
        except Exception:
            # Step 3: 最終手段 - JSクリック（JSFサーバーサイド状態が不完全になるリスクあり）
            if description:
                print(f"    {description}: force+noWaitAfterも失敗 → JSクリック")
            else:
                print(f"    force+noWaitAfterも失敗 → JSクリック")
            locator.evaluate("el => el.click()")
            page.wait_for_timeout(500)


def _click_with_scroll(page, link, timeout=10000):
    """要素をスクロール表示してからクリック（フォールバック: force click → JS click）"""
    try:
        link.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(300)
        link.click(timeout=timeout)
        return True
    except Exception:
        try:
            # Step 2: force + no_wait_after でPlaywrightクリック（DOMイベント正常発火）
            link.click(force=True, no_wait_after=True, timeout=3000)
            page.wait_for_timeout(2000)
            return True
        except Exception:
            pass
        # Step 3: 最終手段 - JSで直接onclick実行
        try:
            onclick_attr = link.get_attribute("onclick")
            if onclick_attr:
                print(f"    Playwrightクリックタイムアウト → JSで直接onclick実行")
                page.evaluate(f"() => {{ {onclick_attr} }}")
                page.wait_for_timeout(3000)
                return True
            else:
                # onclick属性なし → 要素をJSクリック
                link.evaluate("el => el.click()")
                page.wait_for_timeout(3000)
                return True
        except Exception as e3:
            raise e3


def _wait_visible(page, locator, timeout_ms: int = 10000, step_ms: int = 500,
                  label: str = "") -> bool:
    """locator が表示されるまでポーリング待機（カイポケが遅い時間帯の対策）

    is_visible() は待機しない即時判定なので、明示的に刻んで待つ。
    """
    waited = 0
    while True:
        try:
            if locator.is_visible():
                if waited and label:
                    print(f"    {label}: {waited}ms 待機して表示を確認")
                return True
        except Exception:
            pass
        if waited >= timeout_ms:
            return False
        page.wait_for_timeout(step_ms)
        waited += step_ms


def _row_staff_text(row) -> str:
    """行(tr)の「予定側・職員1」の名前だけを取得（取得不可なら空文字）

    click_schedule_entry と _schedule_entry_exists の共通ヘルパー。

    カイポケの1訪問行は左が予定・右が実績で、td.staff-list が2つある
    (artifacts/test_edit_3_user_selected_page_20260128_060104.html で確認)。
    さらに1セルの中に職員1・職員2が入れ子の tr として縦に並ぶ:

        <td class="staff-list"><table><tbody>
          <tr><td class="job-type">…</td><td>川名千恵</td></tr>   ← 職員1
          <tr><td class="job-type">…</td><td>佐藤憲二</td></tr>   ← 職員2(同行)
        </tbody></table></td>

    セル全体を読むと同行者や実績側の職員まで混ざり、
    「職員1が誰か」の判定が壊れるため、予定側の先頭行の名前セルだけを返す。
    """
    try:
        cell = row.locator("td.staff-list").first
        if cell.count() == 0:
            return ""
        first_staff_row = cell.locator("tr").first
        if first_staff_row.count() > 0:
            name_cell = first_staff_row.locator("td:not(.job-type)").first
            if name_cell.count() > 0:
                text = name_cell.inner_text() or ""
                return text.replace("　", " ").strip()
        # 入れ子テーブルが無い形式へのフォールバック（セル全体）
        return (cell.inner_text() or "").replace("　", " ").strip()
    except Exception:
        return ""


def _entry_time_text(link) -> str:
    """エントリのaタグから、属する service-detail-area の時間テキストを取得"""
    try:
        detail_area = link.locator(
            "xpath=ancestor::div[contains(@class,'service-detail-area')]"
        ).first
        if detail_area.count() > 0:
            return detail_area.inner_text()
    except Exception:
        pass
    return ""


# --- 予定の時刻の読み分け (2026-11-19) -----------------------------------------
# 12/24 川上様: 11:00〜11:35 を 10:25〜11:00 へずらす修正で、「11:00」を含む行として
# 新しい行 (終わりが 11:00) を消した。開始は先頭の時刻だけ、終了は「～」の後だけと比べる。
_HHMM_RE = re.compile(r"(\d{1,2})\s*:\s*(\d{2})")
_RANGE_SEP_RE = re.compile(r"[~〜～]")


def _norm_hhmm(value):
    """"9:05" / "09:05" / "０９：０５" → "09:05"。読めなければ None"""
    if not value:
        return None
    m = _HHMM_RE.search(unicodedata.normalize("NFKC", str(value)))
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _parse_entry_times(text):
    """service-detail-area の文字から (開始, 終了) を取り出す (どちらも "HH:MM")

    例: "精神基本療養費Ⅰ・正看\\n10:25 ～ 11:00" → ("10:25", "11:00")
    開始 = 最初の時刻、終了 = 開始より後ろの「～」の直後の時刻 (無ければ None)。
    時刻が 1 つも無ければ None を返す。
    """
    if not text:
        return None
    norm = unicodedata.normalize("NFKC", text)
    first = _HHMM_RE.search(norm)
    if not first:
        return None
    start = f"{int(first.group(1)):02d}:{first.group(2)}"
    end = None
    sep = _RANGE_SEP_RE.search(norm, first.end())
    if sep:
        m = re.match(r"\s*(\d{1,2})\s*:\s*(\d{2})", norm[sep.end():])
        if m:
            end = f"{int(m.group(1)):02d}:{m.group(2)}"
    return (start, end)


def _time_text_matches(text, start_time, end_time=None) -> bool:
    """行の時刻の文字が、開始 (と、渡されれば終了) に合うか

    - 開始は先頭の時刻とだけ比べる (終わりの時刻には当てない)
    - end_time が読める値なら、終了も一致する行だけを残す
    - 時刻として読めない文字のときだけ、従来の部分一致 (開始の文字) に倒す
    """
    parsed = _parse_entry_times(text or "")
    if parsed is None:
        return bool(start_time) and str(start_time) in (text or "")
    start = _norm_hhmm(start_time)
    if start is None or parsed[0] != start:
        return False
    end = _norm_hhmm(end_time)
    if end is not None and parsed[1] != end:
        return False
    return True


def _select_target(candidates, start_time, end_time=None, staff_name=None):
    """候補 (time_text / staff_text を持つ dict) から押してよい 1 行を選ぶ

    Returns:
        (target, reason, time_matched)
        target: 押す候補 (選べなければ None)
        reason: 選べない理由 ("entry_not_found" / "ambiguous_entry")
        time_matched: 時刻で絞った後の候補 (ログ用)

    2 行以上残ったら押さない (ambiguous_entry)。誤って消すより残して人が見るほうが安全。
    """
    time_matched = [c for c in candidates
                    if _time_text_matches(c.get("time_text", ""), start_time, end_time)]
    if staff_name:
        if not time_matched:
            return None, "entry_not_found", time_matched
        staff_matched = [c for c in time_matched
                         if _staff_text_matches(staff_name, c.get("staff_text", ""))]
        if not staff_matched:
            return None, "entry_not_found", time_matched
        if len(staff_matched) > 1:
            return None, "ambiguous_entry", time_matched
        return staff_matched[0], None, time_matched
    if time_matched:
        if len(time_matched) > 1:
            return None, "ambiguous_entry", time_matched
        return time_matched[0], None, time_matched
    if len(candidates) == 1 and not (candidates[0].get("time_text") or "").strip():
        # 職員指定なし・候補1件・時間テキストが取れない → 従来どおり唯一の候補を採用
        return candidates[0], None, time_matched
    return None, "entry_not_found", time_matched


def _plan_detail_areas(row) -> list:
    """行(tr)の「予定側」の service-detail-area のみを返す（実績側は除外）

    1行に予定と実績の2つの service-detail-area があるため、実績側の時間を
    拾って「まだ残っている」と誤判定するのを防ぐ。
    """
    areas = []
    try:
        all_areas = row.locator("div.service-detail-area").all()
    except Exception:
        return areas
    for idx, area in enumerate(all_areas):
        onclick = ""
        try:
            link = area.locator("a[onclick]").first
            if link.count() > 0:
                onclick = link.get_attribute("onclick") or ""
        except Exception:
            onclick = ""
        if onclick:
            if _is_plan_onclick(onclick):
                areas.append(area)
        elif idx == 0:
            # onclickが読めない形式では先頭（＝予定側）だけを採用する
            areas.append(area)
    return areas


def _collect_entry_candidates(page, day_str: str) -> list:
    """指定日の「予定側」エントリ候補（link / time_text / staff_text）を収集

    Strategy 1: 行の日にち列(td.tac.nowrap)の完全一致で行を特定
    Strategy 2: onclick属性をパースして5番目のパラメータ（日にち）で特定

    どちらも onclick の3番目が '01'（予定側）のリンクだけを残す。
    実績側('02')や職員名リンク(submitLinkTabHelper)を掴むと誤操作になる。
    """
    candidate_links = []

    # --- Strategy 1: 行の日にち列(td.tac.nowrap)で特定 ---
    rows = page.locator("table tr").all()
    for row in rows:
        try:
            day_cells = row.locator("td.tac.nowrap").all()
            day_match = False
            for cell in day_cells:
                cell_text = (cell.text_content() or "").strip()
                if cell_text == day_str:
                    day_match = True
                    break

            if not day_match:
                continue

            staff_text = _row_staff_text(row)
            links = row.locator("a[onclick]").all()
            for link in links:
                try:
                    if not _is_plan_onclick(link.get_attribute("onclick") or ""):
                        continue
                except Exception:
                    continue
                if link.is_visible():
                    candidate_links.append({
                        "link": link,
                        "time_text": _entry_time_text(link),
                        "staff_text": staff_text,
                    })
        except Exception:
            continue

    # --- Strategy 2: onclick属性パース（fallback） ---
    if not candidate_links:
        print(f"    Strategy 1失敗 → onclick属性パースにフォールバック")
        all_links = page.locator("a[onclick*='Edit']").all()
        for link in all_links:
            try:
                onclick = link.get_attribute("onclick") or ""
                if not _is_plan_onclick(onclick):
                    continue
                if _onclick_day(onclick) == day_str:
                    staff_text = ""
                    try:
                        row = link.locator("xpath=ancestor::tr[1]").first
                        if row.count() > 0:
                            staff_text = _row_staff_text(row)
                    except Exception:
                        pass
                    candidate_links.append({
                        "link": link,
                        "time_text": _entry_time_text(link),
                        "staff_text": staff_text,
                    })
            except Exception:
                continue

    return candidate_links


def click_schedule_entry(page, day: int, start_time: str, staff_name: str = None,
                         end_time: str = None) -> bool:
    """
    指定した日付・開始時間（＋終了時間・職員）のスケジュールエントリをクリック

    2026-11-19: 開始は行の先頭の時刻だけと比べ、end_time を渡せば終了も比べる。
    時刻と職員で絞っても 2 行以上残るときは押さない (reason=ambiguous_entry)。

    Strategy 1: 行の日にち列(td.tac.nowrap)の完全一致で行を特定し、
                行内のaタグ(onclick付き)をクリック
    Strategy 2: onclick属性をパースして5番目のパラメータ（日にち）で特定
    同一日複数件: div.service-detail-area 内の時間テキストで絞り込み
    同一時間複数件: staff_name 指定時は td.staff-list の職員名で確定する

    2026-09-03の本番事故対策:
      - 表が未描画のまま走査して「見つかりません」になる → 最大3回リトライ
      - 同日同時刻に旧行/新行が並ぶと先頭を掴んで誤削除する → 職員で確定
    """
    label = f"  予定をクリック: {day}日 {start_time}"
    if staff_name:
        label += f" (職員: {staff_name})"
    print(label)
    day_str = str(day)

    # フローティング要素を除去（クリックブロック防止）
    _remove_floating_overlays(page)

    # 表が描画されるまで待つ（利用者切替直後の未描画対策）
    _wait_schedule_table(page)

    try:
        candidate_links = []
        max_attempts = 3
        for attempt in range(max_attempts):
            candidate_links = _collect_entry_candidates(page, day_str)
            if candidate_links:
                break
            if attempt < max_attempts - 1:
                print(f"    {day}日のエントリが0件 → 描画待ちで再走査 "
                      f"({attempt + 1}/{max_attempts - 1})")
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)

        if not candidate_links:
            print(f"    {day}日のエントリが見つかりません")
            _save_debug_on_failure(page, day, start_time)
            _set_reason("entry_not_found")
            return False

        # 時間での絞り込みは候補が1件でも必ず行う。
        # 2026-09-03のレビュー指摘: 1件だけの時に素通りさせると、
        # 別時刻・別職員の行を掴んで削除し、職員で絞った検証は
        # 「消えた」と答えてしまう（＝取り違えたまま成功扱い）。
        time_label = start_time + (f"～{end_time}" if _norm_hhmm(end_time) else "")
        print(f"    {day}日に{len(candidate_links)}件のエントリを発見 → 時間 '{time_label}' で絞り込み")
        # 開始は先頭の時刻だけ、終了は「～」の後だけと比べる (_time_text_matches)。
        # 2 行以上残ったら押さない (_select_target)。
        target, reason, time_matched = _select_target(
            candidate_links, start_time, end_time, staff_name)

        def _observed(cands):
            return [f"{c['time_text'].strip().replace(chr(10), ' ')}"
                    f"/{c['staff_text'].strip()}" for c in cands]

        if target is None:
            if reason == "ambiguous_entry":
                print(f"    警告: 時間 '{time_label}'"
                      + (f"・職員 '{staff_name}'" if staff_name else "")
                      + " に合う行が2件以上あります → どれも押さない")
                print(f"    時間一致行の時間/職員: {_observed(time_matched)}")
            elif staff_name and time_matched:
                print(f"    警告: 時間一致 {len(time_matched)} 件のうち"
                      f"職員 '{staff_name}' の行がありません → クリックしない")
                print(f"    時間一致行の時間/職員: {_observed(time_matched)}")
            else:
                print(f"    警告: 時間 '{time_label}' に一致する行がありません → クリックしない")
                print(f"    候補の時間/職員: {_observed(candidate_links)}")
            _save_debug_on_failure(page, day, start_time)
            _set_reason(reason)
            return False

        print(f"    時間一致{'+職員一致' if staff_name else ''}: "
              f"{target['time_text'].strip()} / {target['staff_text'].strip()}")

        _click_with_scroll(page, target["link"])
        page.wait_for_timeout(2000)
        return True

    except Exception as e:
        print(f"    エラー: {e}")
        _save_debug_on_failure(page, day, start_time)
        _set_reason("entry_not_found")
        return False


# =============================================================================
# 時間・日付・職員の編集
# =============================================================================

def _set_event_time(page, start_hour: int, start_min: int, end_hour: int, end_min: int) -> bool:
    """
    イベントポップアップ(form#formIndividualMonthlyShiftAssignPopup)の時間を設定

    イベントフォームの時間セレクトはname属性のみ（IDなし）:
    - popupIndividualStartHour: 開始時 (値: "00"-"23")
    - popupIndividualStartMinRight: 開始分・十の位 (値: "0"-"5")
    - popupIndividualStartMinLeft: 開始分・一の位 (値: "0"-"9")
    - popupIndividualEndHour: 終了時 (値: "00"-"23")
    - popupIndividualEndMinRight: 終了分・十の位 (値: "0"-"5")
    - popupIndividualEndMinLeft: 終了分・一の位 (値: "0"-"9")
    """
    print(f"  時間変更: {start_hour:02d}:{start_min:02d} - {end_hour:02d}:{end_min:02d}")

    start_min_tens = start_min // 10
    start_min_ones = start_min % 10
    end_min_tens = end_min // 10
    end_min_ones = end_min % 10

    try:
        form = page.locator("form#formIndividualMonthlyShiftAssignPopup")
        if form.count() > 0 and form.is_visible(timeout=2000):
            # name属性でセレクト要素を特定（時の値は"00"形式、分は"0"形式）
            name_value_pairs = [
                ("popupIndividualStartHour", f"{start_hour:02d}"),
                ("popupIndividualStartMinRight", str(start_min_tens)),
                ("popupIndividualStartMinLeft", str(start_min_ones)),
                ("popupIndividualEndHour", f"{end_hour:02d}"),
                ("popupIndividualEndMinRight", str(end_min_tens)),
                ("popupIndividualEndMinLeft", str(end_min_ones)),
            ]

            for name, val in name_value_pairs:
                sel = form.locator(f"select[name='{name}']")
                if sel.count() > 0 and sel.is_visible(timeout=1000):
                    sel.select_option(value=val)
                    page.wait_for_timeout(200)
                else:
                    print(f"    警告: select[name='{name}'] が見つかりません")

            print(f"    イベント時間設定完了")
            return True

        # フォールバック: 通常のスケジュール用セレクタ
        print(f"    イベントフォーム未検出、通常セレクタにフォールバック")
        return edit_schedule_time(page, start_hour, start_min, end_hour, end_min)

    except Exception as e:
        print(f"    イベント時間設定エラー: {e}")
        return edit_schedule_time(page, start_hour, start_min, end_hour, end_min)


def edit_schedule_time(page, start_hour: int, start_min: int, end_hour: int, end_min: int) -> bool:
    """
    スケジュールの時間を編集（6つのセレクト）

    セレクタ:
    - #inPopupStartHour, #inPopupStartMinute1, #inPopupStartMinute2
    - #inPopupEndHour, #inPopupEndMinute1, #inPopupEndMinute2

    注意: select_option だけでは Kaipoke の onchange バリデーションが発火しない
    場合があるため、JavaScript で値設定＋イベント発火を行う。
    option value は "9" / "09" 両形式に対応（parseInt で数値比較）。
    """
    print(f"  時間変更: {start_hour:02d}:{start_min:02d} - {end_hour:02d}:{end_min:02d}")

    try:
        start_min_tens = start_min // 10
        start_min_ones = start_min % 10
        end_min_tens = end_min // 10
        end_min_ones = end_min % 10

        selectors_and_values = [
            ("#inPopupStartHour", start_hour),
            ("#inPopupStartMinute1", start_min_tens),
            ("#inPopupStartMinute2", start_min_ones),
            ("#inPopupEndHour", end_hour),
            ("#inPopupEndMinute1", end_min_tens),
            ("#inPopupEndMinute2", end_min_ones),
        ]

        for selector, value in selectors_and_values:
            elem = page.locator(selector)
            if elem.is_visible(timeout=2000):
                # disabled チェック＆強制有効化
                is_disabled = page.evaluate(f"document.querySelector('{selector}')?.disabled")
                if is_disabled:
                    print(f"    {selector} がdisabled → 強制有効化")
                    page.evaluate(f"""() => {{
                        const el = document.querySelector('{selector}');
                        if (el) {{ el.disabled = false; el.removeAttribute('disabled'); }}
                    }}""")
                    page.wait_for_timeout(300)

                # 数値→文字列変換: まず非ゼロ埋め、ダメならゼロ埋めを試行
                try:
                    elem.select_option(value=str(value), timeout=5000)
                except Exception:
                    try:
                        elem.select_option(value=f"{value:02d}", timeout=5000)
                    except Exception:
                        # 最終手段: JSで直接値を設定
                        print(f"    {selector}: select_option失敗 → JS直接設定")
                        page.evaluate(f"""() => {{
                            const el = document.querySelector('{selector}');
                            if (el) {{
                                el.disabled = false;
                                el.removeAttribute('disabled');
                                // 値をマッチング（数値比較）
                                const target = {value};
                                for (let i = 0; i < el.options.length; i++) {{
                                    if (parseInt(el.options[i].value) === target) {{
                                        el.selectedIndex = i;
                                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                        break;
                                    }}
                                }}
                            }}
                        }}""")
                        page.wait_for_timeout(500)
                        continue
                # AJAX完了待機
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                page.wait_for_timeout(500)

        # 最終バリデーション完了待ち
        page.wait_for_timeout(1000)
        return True

    except Exception as e:
        print(f"    時間変更エラー: {e}")
        return False


def _set_add_modal_date(page, day: int) -> bool:
    """
    新規追加モーダルでの日付設定

    新規追加モーダルではインラインカレンダー（jQuery UI Datepicker）が
    直接表示されている。#simple-select-days-rangeはinputではなくdivの場合がある。
    戦略:
    1. モーダル内の既に表示されているカレンダーセルを直接クリック
    2. フォールバック: change_date（既存の汎用関数）
    """
    print(f"  日付変更: → {day}日")
    day_str = str(day)

    try:
        # Strategy 1: div#simple-select-days-range 内のインラインカレンダー
        # (新規追加モーダルでは #simple-select-days-range は DIV.hasMultiDatepicker)
        datepicker_div = page.locator("#simple-select-days-range .ui-datepicker-inline")
        if datepicker_div.count() > 0 and datepicker_div.is_visible(timeout=2000):
            cal_table = datepicker_div.locator("table.ui-datepicker-calendar")
            if cal_table.count() > 0:
                day_links = cal_table.locator("td a").all()
                for link in day_links:
                    if link.text_content().strip() == day_str:
                        _safe_click(page, link, timeout=5000, description="カレンダー日付クリック")
                        # AJAX完了待機（日付変更でサーバーサイド検証が走る）
                        try:
                            page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1000)
                        _close_datepicker(page)
                        print(f"    インラインカレンダーから {day} 日を選択")
                        return True
                print(f"    警告: インラインカレンダーに {day} 日が見つかりません")

        # Strategy 2: フォールバック
        print(f"    インラインカレンダー未検出、change_dateにフォールバック")
        return change_date(page, day)

    except Exception as e:
        print(f"    新規追加日付設定エラー: {e}")
        return change_date(page, day)


def _check_form_errors(page, step_label: str = ""):
    """モーダル内のエラーメッセージを検出してログ出力 + スクリーンショット"""
    try:
        errors = page.evaluate("""() => {
            const msgs = [];
            // エラーメッセージ要素を検索（*のみの必須マークは除外）
            const selectors = [
                '.error', '.alert-danger', '.alert-error', '.text-danger',
                '.validation-error', '.has-error', '[class*="error"]',
                '.errorMessage', '#errorMessage', '.err_msg',
                '.message_error',
            ];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    const text = el.textContent.trim();
                    // *のみや空文字は除外
                    if (text && text.length > 2 && text !== '*' && !msgs.includes(text)) {
                        msgs.push(sel + ': ' + text.substring(0, 200));
                    }
                });
            });
            // ピンク/赤背景の要素（実際のエラーバナー）
            document.querySelectorAll('div, p, span, li, ul').forEach(el => {
                const bg = window.getComputedStyle(el).backgroundColor;
                if (bg) {
                    const match = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                    if (match) {
                        const r = parseInt(match[1]), g = parseInt(match[2]), b = parseInt(match[3]);
                        // ピンク/赤系背景 (r > 200, g < 180)
                        if (r > 200 && g < 180 && b < 180) {
                            const text = el.textContent.trim();
                            if (text && text.length > 3 && text.length < 500) {
                                msgs.push('bg(' + bg + '): ' + text.substring(0, 200));
                            }
                        }
                    }
                }
            });
            return msgs;
        }""")
        if errors:
            print(f"    【診断:{step_label}】エラー検出:")
            for e in errors[:10]:
                print(f"      {e}")
        else:
            print(f"    【診断:{step_label}】エラーなし")

        # 毎回スクリーンショット保存（診断用）
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = step_label.replace(":", "_").replace("/", "_")
        page.screenshot(path=f"artifacts/diag_{safe_label}_{timestamp}.png")
        print(f"    【診断】スクリーンショット: artifacts/diag_{safe_label}_{timestamp}.png")

    except Exception as ex:
        print(f"    【診断:{step_label}】エラーチェック失敗: {ex}")


def _close_datepicker(page):
    """開いているデートピッカーを確実に閉じる"""
    try:
        page.evaluate("""() => {
            // ポップアップカレンダーを非表示
            const popup = document.querySelector('#ui-datepicker-div');
            if (popup) popup.style.display = 'none';
            // jQuery UIのdatepicker API で閉じる
            if (typeof $ !== 'undefined') {
                $('.hasDatepicker').datepicker('hide');
                $('.hasMultiDatepicker').datepicker('hide');
            }
        }""")
        page.wait_for_timeout(300)
    except Exception:
        pass


def change_date(page, new_day: int) -> bool:
    """
    スケジュールの日付を変更

    カイポケの編集ポップアップでは、日付選択は jQuery UI Datepicker
    (#simple-select-days-range.hasMultiDatepicker) で実装されている。
    <select> ではなく、カレンダーグリッド内の日付セル(td)をクリックする。

    Strategy 1: jQuery UI Datepicker のカレンダーセルをクリック
    Strategy 2: <select> タグからの選択（フォールバック）
    """
    print(f"  日付変更: → {new_day}日")
    day_str = str(new_day)

    try:
        datepicker_el = page.locator("#simple-select-days-range")
        if datepicker_el.is_visible(timeout=2000):
            # Strategy 1: インラインカレンダー（DIV.hasMultiDatepicker内）
            inline_cal = datepicker_el.locator(".ui-datepicker-inline")
            if inline_cal.count() > 0 and inline_cal.is_visible(timeout=1000):
                cal_table = inline_cal.locator("table.ui-datepicker-calendar")
                if cal_table.count() > 0:
                    day_links = cal_table.locator("td a").all()
                    for link in day_links:
                        if link.text_content().strip() == day_str:
                            _safe_click(page, link, timeout=5000, description="カレンダー日付クリック")
                            page.wait_for_timeout(500)
                            _close_datepicker(page)
                            print(f"    インラインカレンダーから {new_day} 日を選択")
                            return True
                    print(f"    警告: インラインカレンダーに {new_day} 日が見つかりません")

            # Strategy 2: INPUT要素の場合 - クリックしてポップアップカレンダーを開く
            tag_name = datepicker_el.evaluate("el => el.tagName")
            if tag_name == "INPUT":
                print(f"    デートピッカー input 検出")
                datepicker_el.click()
                page.wait_for_timeout(1000)

                calendar = None
                for cal_selector in ["#ui-datepicker-div", ".ui-datepicker", ".datepicker"]:
                    cal = page.locator(cal_selector)
                    if cal.count() > 0 and cal.first.is_visible(timeout=1000):
                        calendar = cal.first
                        print(f"    カレンダー検出: {cal_selector}")
                        break

                if calendar:
                    day_cells = calendar.locator("td a").all()
                    for cell in day_cells:
                        try:
                            cell_text = cell.text_content().strip()
                            if cell_text == day_str:
                                _safe_click(page, cell, timeout=5000, description="カレンダー日付クリック")
                                page.wait_for_timeout(500)
                                _close_datepicker(page)
                                print(f"    日付を {new_day} に変更（デートピッカー）")
                                return True
                        except Exception:
                            continue

                # Strategy 2b: JS datepicker API
                try:
                    current_val = datepicker_el.input_value()
                    result = page.evaluate(f"""() => {{
                        const el = document.querySelector('#simple-select-days-range');
                        if (!el) return 'element not found';
                        const currentVal = el.value;
                        const match = currentVal.match(/(\\d{{4}})[\\/-](\\d{{1,2}})/);
                        if (match) {{
                            const year = match[1];
                            const month = match[2].padStart(2, '0');
                            const day = '{day_str}'.padStart(2, '0');
                            const newVal = year + '/' + month + '/' + day;
                            el.value = newVal;
                            if (typeof $ !== 'undefined' && $(el).datepicker) {{
                                $(el).datepicker('setDate', newVal);
                            }}
                            $(el).trigger('change');
                            return 'set to ' + newVal;
                        }}
                        return 'could not parse: ' + currentVal;
                    }}""")
                    if result and result.startswith("set to"):
                        page.wait_for_timeout(500)
                        print(f"    日付を {new_day} に変更（JavaScript）")
                        return True
                except Exception as js_err:
                    print(f"    JS datepicker エラー: {js_err}")
            else:
                print(f"    #simple-select-days-range はDIVですがインラインカレンダーが見つかりません")
        else:
            print("    #simple-select-days-range が見つかりません")

        # Strategy 2: <select> タグ（フォールバック）
        day_selectors = [
            "#inPopupDay",
            "select[name*='day']",
            "select[name*='Day']",
        ]
        for selector in day_selectors:
            day_select = page.locator(selector).first
            try:
                if day_select.is_visible(timeout=1000):
                    day_select.select_option(value=day_str)
                    page.wait_for_timeout(300)
                    print(f"    日付を {new_day} に変更（セレクト）")
                    return True
            except Exception:
                continue

        print("    日付変更に失敗: デートピッカーもセレクトも見つかりません")
        return False

    except Exception as e:
        print(f"    日付変更エラー: {e}")
        return False


# =============================================================================
# 職種 (JobDivision) 自動設定 (2026-09-01)
# 経緯: edit_staff は chargeStaffNId1 (職員) だけ設定し、職員を変えると
# カイポケ側で 職種 セレクトが未選択に戻るため、RPA が触った行の職種が
# 全て「未選択」になっていた (2026-08-31 PO 指摘・9月第1週 51 行)。
# =============================================================================

# 氏名(正規化) → 職種。カイポケの職種セレクトの表記に完全一致させること。
STAFF_QUALIFICATIONS = {
    "熊澤妙子": "看護師",
    "本名大": "看護師",
    "髙梨桂子": "看護師",
    "宇田川優莉": "看護師",
    "川名千恵": "看護師",
    "小西彩稀": "看護師",
    "高岡真由美": "准看護師",
}


def _staff_qualification(name: str):
    n = normalize_name(name or "")
    for k, q in STAFF_QUALIFICATIONS.items():
        if normalize_name(k) == n:
            return q
    return None


def set_job_division(page, slot: int, staff_name: str = "", clear: bool = False) -> bool:
    """職員N の職種セレクト (chargeStaff{N}JobDivision1) を設定する。

    失敗しても呼び出し元のフローは止めない (ログのみ)。
    「看護師」は「准看護師」の部分文字列なので、部分一致は使わず完全一致のみ。
    """
    try:
        sel = page.locator(f"select#chargeStaff{slot}JobDivision1")
        if not sel.is_visible(timeout=2000):
            print(f"    職種{slot}: セレクト未表示のためスキップ")
            return False
        if clear:
            sel.select_option(index=0)
            page.wait_for_timeout(200)
            print(f"    職種{slot}クリア")
            return True
        qual = _staff_qualification(staff_name)
        if qual is None:
            print(f"    職種{slot}: '{staff_name}' の資格が未登録のため未選択のまま (STAFF_QUALIFICATIONS に追加を)")
            return False
        options = sel.locator("option").all()
        texts = [(o.text_content() or "").strip() for o in options]
        for idx, t in enumerate(texts):
            if t == qual:
                sel.select_option(index=idx)
                page.wait_for_timeout(200)
                print(f"    職種{slot}設定: {qual}")
                return True
        print(f"    職種{slot}: 選択肢に '{qual}' が無い。選択肢={texts}")
        return False
    except Exception as e:
        print(f"    職種{slot}設定エラー(続行): {e}")
        return False


def edit_staff(page, staff1_name: str, staff2_name: str = "",
               for_new_entry: bool = False) -> bool:
    """
    職員を編集

    PDF仕様セレクタ: select#chargeStaff1Id1, select#chargeStaff2Id1
    新規追加時は「職員情報入力」ボタンを先にクリック

    Args:
        page: Playwrightのページオブジェクト
        staff1_name: 職員1名（空文字の場合は変更しない）
        staff2_name: 職員2名（空文字の場合はクリア/未設定）
        for_new_entry: 新規追加時はTrue（職員情報入力ボタンを押す）
    """
    try:
        # 新規追加の場合、職員情報入力ボタンをクリック
        # デバッグで判明: #btnInputStaff(input)は hidden、#input_staff_on(span)が visible
        if for_new_entry:
            staff_btn_found = False
            for btn_selector in [
                "span#input_staff_on",
                "input[value='職員情報入力']",
                "input#btnInputStaff",
                "#btnInputStaff",
                "button:has-text('職員情報入力')",
                "a:has-text('職員情報入力')",
            ]:
                staff_info_btn = page.locator(btn_selector).first
                try:
                    if staff_info_btn.is_visible(timeout=2000):
                        _click_with_scroll(page, staff_info_btn)
                        page.wait_for_timeout(2000)
                        print(f"    「職員情報入力」ボタンをクリック ({btn_selector})")
                        staff_btn_found = True
                        break
                except Exception:
                    continue
            if not staff_btn_found:
                # デバッグ: モーダル内のボタン/inputを列挙
                try:
                    buttons = page.locator(".modal:visible input[type='button'], .modal:visible input[type='submit'], .modal:visible button").all()
                    btn_info = []
                    for b in buttons[:15]:
                        val = b.get_attribute("value") or b.text_content() or ""
                        bid = b.get_attribute("id") or ""
                        btn_info.append(f"{bid}={val.strip()[:20]}")
                    print(f"    警告: 職員情報入力ボタンが見つかりません。モーダル内ボタン: {btn_info}")
                except Exception:
                    print("    警告: 職員情報入力ボタンが見つかりません")

        # 2026-09-03: 「職員情報入力」クリック後の固定2秒待機では足りず、
        # select#chargeStaff1Id1 が間に合わないまま強制有効化 → 登録失敗が9件出た。
        # 診断列挙やJSフォールバックの前に、最大12秒ポーリングして待つ。
        if for_new_entry:
            if not _wait_visible(page, page.locator("select#chargeStaff1Id1"),
                                 timeout_ms=12000, label="select#chargeStaff1Id1"):
                print("    ※ 12秒待っても select#chargeStaff1Id1 が表示されません")
                _check_form_errors(page, "Step5:職員ボタン後")

        # 職員情報入力ボタンクリック後のデバッグ＋リトライ
        if for_new_entry:
            try:
                # DOM内の全selectを列挙
                all_selects = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('select')).map(s => ({
                        id: s.id,
                        name: s.name,
                        visible: s.offsetParent !== null,
                        optCount: s.options.length
                    })).filter(s => s.visible);
                }""")
                print(f"    【診断】職員ボタン後の表示中select一覧:")
                for s in all_selects:
                    print(f"      id='{s['id']}' name='{s['name']}' options={s['optCount']}")

                # chargeStaff1Id1 が見つからない場合のリトライ
                has_staff = any(s['id'] == 'chargeStaff1Id1' for s in all_selects)
                if not has_staff:
                    print("    ※ chargeStaff1Id1 未表示 → JSで職員セクション強制表示を試行")
                    # 方法1: JSでinput_staff_onをクリック
                    page.evaluate("""() => {
                        const btn = document.querySelector('#input_staff_on');
                        if (btn) btn.click();
                        // hidden inputもクリック
                        const btnInput = document.querySelector('#btnInputStaff');
                        if (btnInput) btnInput.click();
                    }""")
                    page.wait_for_timeout(2000)

                    # 方法2: 職員セクションを直接表示
                    page.evaluate("""() => {
                        // 職員入力セクションのdisplay:noneを解除
                        const staffSection = document.querySelector('#staffDetailArea, #staff_detail, .staffArea, #staffArea');
                        if (staffSection) {
                            staffSection.style.display = '';
                            staffSection.style.visibility = 'visible';
                        }
                        // 全ての chargeStaff select を表示
                        ['chargeStaff1Id1', 'chargeStaff2Id1', 'chargeStaff3Id1'].forEach(id => {
                            const el = document.getElementById(id);
                            if (el) {
                                el.closest('tr,div,.form-group')?.style && (el.closest('tr,div,.form-group').style.display = '');
                                el.disabled = false;
                                el.removeAttribute('disabled');
                            }
                        });
                    }""")
                    page.wait_for_timeout(1000)

                    # 再確認
                    all_selects2 = page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('select')).map(s => ({
                            id: s.id, visible: s.offsetParent !== null, optCount: s.options.length
                        })).filter(s => s.visible && s.id.startsWith('chargeStaff'));
                    }""")
                    if all_selects2:
                        print(f"    リトライ成功: 職員select表示確認 {[s['id'] for s in all_selects2]}")
                    else:
                        print("    リトライ後もchargeStaff1Id1未表示")

            except Exception as diag_err:
                print(f"    【診断】select列挙失敗: {diag_err}")

        # 職員1を設定
        if staff1_name:
            staff1_select = page.locator("select#chargeStaff1Id1")
            if staff1_select.is_visible(timeout=3000):
                if staff1_name == "未割当":
                    # 「未割当」の場合は '-'（未設定）を選択
                    staff1_select.select_option(index=0)
                    page.wait_for_timeout(300)
                    print(f"    職員1設定: 未割当（'-'を選択）")
                    set_job_division(page, 1, clear=True)
                else:
                    options = staff1_select.locator("option").all()
                    selected = False
                    for opt in options:
                        opt_text = opt.text_content()
                        if name_matches(staff1_name, opt_text):
                            staff1_select.select_option(label=opt_text)
                            page.wait_for_timeout(300)
                            print(f"    職員1設定: {staff1_name}")
                            selected = True
                            set_job_division(page, 1, staff1_name)
                            break
                    if not selected:
                        print(f"    警告: 職員1 '{staff1_name}' が選択肢に見つかりません")
                        opt_names = [o.text_content().strip() for o in options[:10]]
                        print(f"    選択肢: {opt_names}")
            else:
                print("    警告: select#chargeStaff1Id1 が見つかりません")
                _set_reason("staff_select_not_shown")

        # 職員2を設定
        staff2_select = page.locator("select#chargeStaff2Id1")
        if staff2_select.is_visible(timeout=3000):
            if staff2_name:
                options = staff2_select.locator("option").all()
                selected = False
                for opt in options:
                    opt_text = opt.text_content()
                    if name_matches(staff2_name, opt_text):
                        staff2_select.select_option(label=opt_text)
                        page.wait_for_timeout(300)
                        print(f"    職員2設定: {staff2_name}")
                        selected = True
                        set_job_division(page, 2, staff2_name)
                        break
                if not selected:
                    print(f"    警告: 職員2 '{staff2_name}' が選択肢に見つかりません")
            else:
                # 職員2をクリア（最初のオプション = 空白）
                staff2_select.select_option(index=0)
                page.wait_for_timeout(300)
                print("    職員2クリア")
                set_job_division(page, 2, clear=True)

        return True

    except Exception as e:
        print(f"    職員変更エラー: {e}")
        return False


# =============================================================================
# 登録・削除・ダイアログ操作
# =============================================================================

def click_register_button(page) -> bool:
    """
    「登録する」ボタンをクリック（input#btnRegisPop）

    onclick="preventDoubleClick(); regisShiftAssign();" が
    ネイティブ confirm() ダイアログを発火する場合があるため、
    dialog イベントハンドラで自動応答する。
    """
    try:
        # regisShiftAssign() がネイティブ confirm() を発火するため
        # dialog イベントハンドラで自動応答
        page.on("dialog", _accept_dialog)

        # Primary: PDF仕様のセレクタ
        register_button = page.locator("input#btnRegisPop")
        if register_button.is_visible(timeout=3000):
            # 事前に全フォームフィールドのdisabledを解除（JS click経由で壊れた場合の保護）
            page.evaluate("""() => {
                ['#inPopupStartHour', '#inPopupStartMinute1', '#inPopupStartMinute2',
                 '#inPopupEndHour', '#inPopupEndMinute1', '#inPopupEndMinute2',
                 '#inPopupServicePlant', '#inPopupEstimate1', '#inPopupEstimate2', '#inPopupEstimate3',
                 '#chargeStaff1Id1', '#chargeStaff2Id1'].forEach(sel => {
                    const el = document.querySelector(sel);
                    if (el && el.disabled) {
                        el.disabled = false;
                        el.removeAttribute('disabled');
                    }
                });
            }""")

            # ボタンが disabled の場合、フォームの change イベントを再発火
            is_disabled = register_button.get_attribute("disabled")
            if is_disabled:
                print("    登録ボタンが無効です。changeイベントを再発火します...")
                form_state = page.evaluate("""() => {
                    const fields = {};
                    ['#inPopupStartHour', '#inPopupStartMinute1', '#inPopupStartMinute2',
                     '#inPopupEndHour', '#inPopupEndMinute1', '#inPopupEndMinute2',
                     '#chargeStaff1Id1', '#chargeStaff2Id1'].forEach(sel => {
                        const el = document.querySelector(sel);
                        if (el) fields[sel] = el.value;
                    });
                    return fields;
                }""")
                print(f"    フォーム状態: {form_state}")
                # 全フィールドの change イベントを再発火
                page.evaluate("""() => {
                    ['#inPopupStartHour', '#inPopupStartMinute1', '#inPopupStartMinute2',
                     '#inPopupEndHour', '#inPopupEndMinute1', '#inPopupEndMinute2',
                     '#chargeStaff1Id1', '#chargeStaff2Id1'].forEach(sel => {
                        const el = document.querySelector(sel);
                        if (el) {
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            if (typeof $ !== 'undefined' && $(el).trigger) $(el).trigger('change');
                        }
                    });
                }""")
                page.wait_for_timeout(3000)

                # まだ disabled なら強制有効化を試みる
                is_disabled = register_button.get_attribute("disabled")
                if is_disabled:
                    print("    警告: 登録ボタンを強制有効化します")
                    page.evaluate("""() => {
                        const btn = document.querySelector('#btnRegisPop');
                        if (btn) { btn.disabled = false; btn.removeAttribute('disabled'); }
                    }""")
                    page.wait_for_timeout(500)

            try:
                register_button.click(timeout=10000)
            except Exception as click_err:
                # Step 2: force + no_wait_after（正常DOMイベント発火）
                try:
                    print(f"    登録ボタン: Playwrightタイムアウト → force+noWaitAfterで再試行")
                    register_button.click(force=True, no_wait_after=True, timeout=3000)
                except Exception:
                    # Step 3: JSクリック
                    print(f"    登録ボタン: force+noWaitAfterも失敗 → JSクリック")
                    page.evaluate("document.querySelector('#btnRegisPop')?.click()")
            page.wait_for_timeout(3000)
        else:
            # Fallback
            register_button = page.locator(
                "button:has-text('登録する'), a:has-text('登録する'), input[value='登録する']"
            ).first
            if register_button.is_visible():
                try:
                    register_button.click(timeout=10000)
                except Exception:
                    page.evaluate("document.querySelector('#btnRegisPop')?.click()")
                page.wait_for_timeout(3000)
            else:
                print("    登録ボタンが見つかりません")
                page.remove_listener("dialog", _accept_dialog)
                _set_reason("register_failed")
                return False

        page.remove_listener("dialog", _accept_dialog)

        # 登録後にモーダルが閉じるまで待機
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        # エラーメッセージチェック
        error_msgs = page.evaluate("""() => {
            const msgs = [];
            document.querySelectorAll('.error, .alert-danger, .text-danger, .err_msg, .message_error').forEach(el => {
                const text = el.textContent.trim();
                if (text && text.length > 2 && text !== '*') msgs.push(text.substring(0, 200));
            });
            return msgs;
        }""")
        if error_msgs:
            print(f"    登録エラー検出: {error_msgs}")
            close_edit_dialog(page)
            _set_reason("register_failed")
            return False

        # モーダルが閉じたか確認（閉じていなければ失敗）
        try:
            if register_button.is_visible(timeout=2000):
                print("    警告: 登録後もモーダルが開いたままです → 登録失敗と判定")
                close_edit_dialog(page)
                _set_reason("register_failed")
                return False
        except Exception:
            pass

        print("    登録完了")
        return True

    except Exception as e:
        print(f"    登録エラー: {e}")
        # 診断スクリーンショット
        try:
            timestamp = __import__('datetime').datetime.now().strftime("%Y%m%d_%H%M%S")
            page.screenshot(path=f"artifacts/debug_register_error_{timestamp}.png")
            print(f"    診断スクリーンショット保存")
        except Exception:
            pass
        try:
            page.remove_listener("dialog", _accept_dialog)
        except Exception:
            pass
        _set_reason("register_failed")
        return False


def _accept_dialog(dialog):
    """confirmダイアログを自動的にOKする"""
    dialog.accept()


def close_edit_dialog(page) -> bool:
    """
    編集ダイアログを閉じる（キャンセル）

    カイポケの閉じるボタン(button.close.closeOn)は
    confirm('入力内容を登録せずに終了しますか？') を発火するため、
    dialog イベントハンドラでOKを自動応答する。
    モーダルが閉じられない場合はEscapeキーやページリロードでリカバリする。
    """
    try:
        # confirmダイアログの自動応答を設定
        page.on("dialog", _accept_dialog)

        cancel_buttons = [
            "button.close.closeOn",
            "button:has-text('キャンセル')",
            "button:has-text('閉じる')",
            "a:has-text('キャンセル')",
            ".close",
        ]
        for selector in cancel_buttons:
            btn = page.locator(selector).first
            try:
                if btn.is_visible(timeout=1000):
                    try:
                        btn.click(timeout=5000)
                    except Exception:
                        try:
                            btn.click(force=True, no_wait_after=True, timeout=3000)
                        except Exception:
                            btn.evaluate("el => el.click()")
                    page.wait_for_timeout(1500)
                    # モーダルが実際に閉じたか確認
                    modal_still_open = False
                    try:
                        reg_btn = page.locator("input#btnRegisPop")
                        if reg_btn.is_visible(timeout=500):
                            modal_still_open = True
                    except Exception:
                        pass
                    if not modal_still_open:
                        page.remove_listener("dialog", _accept_dialog)
                        return True
            except Exception:
                continue

        # ボタンが見つからない場合、Escapeキーで閉じる
        page.keyboard.press("Escape")
        page.wait_for_timeout(1000)

        # それでもモーダルが残っている場合、JavaScriptで直接閉じる
        try:
            page.evaluate("document.querySelector('.modal, .popup, [role=dialog]')?.remove()")
            page.evaluate("document.querySelector('.modal-backdrop')?.remove()")
            page.wait_for_timeout(500)
        except Exception:
            pass

        page.remove_listener("dialog", _accept_dialog)
        return True
    except Exception:
        try:
            page.remove_listener("dialog", _accept_dialog)
        except Exception:
            pass
        return False


def _count_plan_entries(page, day: int, start_time: str, staff_name: str = None,
                        end_time: str = None) -> int:
    """指定日・指定開始時間の「予定側」エントリ件数を数える (クリックしない).

    2026-09-03のレビュー指摘への対応:
      - 実績側(02)の service-detail-area を数えると、実績があるだけで
        「まだ残っている」と誤判定するため予定側だけを見る (_plan_detail_areas)。
      - staff_name 指定時は職員1が一致する行のみ数える。
    2026-11-19: 部分一致をやめ、click_schedule_entry と同じ物差し (_time_text_matches)
    で数える。終わりの時刻が start_time と同じ別の行を数えない。
    """
    day_str = str(day)
    count = 0
    try:
        rows = page.locator("table tr").all()
        for row in rows:
            try:
                day_cells = row.locator("td.tac.nowrap").all()
                if not any((c.text_content() or "").strip() == day_str for c in day_cells):
                    continue
                if staff_name and not _staff_text_matches(staff_name, _row_staff_text(row)):
                    continue
                for a in _plan_detail_areas(row):
                    if _time_text_matches(a.inner_text() or "", start_time, end_time):
                        count += 1
            except Exception:
                continue
    except Exception:
        pass
    return count


def _schedule_entry_exists(page, day: int, start_time: str, staff_name: str = None,
                           end_time: str = None) -> bool:
    """指定日の行に start_time のエントリが残っているか (削除検証用・クリックしない).

    2026-08-21 C2実機テストで発覚: 削除ボタンのクリックが無反応でも成功扱いになり、
    時間変更編集 (削除→再追加) が「元行残存 + 新行追加」の二重化に化けた。
    削除後にこの関数で実在検証し、残っていれば失敗として扱う。

    2026-09-03: staff_name 指定時は職員1が一致する行のみ数える。
    同日同時刻に別職員の新行が既にある場合に、偽の「まだ残っている」を出さないため。
    """
    return _count_plan_entries(page, day, start_time, staff_name, end_time=end_time) > 0


def delete_schedule_entry(page, day: int, start_time: str, dry_run: bool = False,
                          staff_name: str = None, end_time: str = None) -> bool:
    """
    スケジュールエントリを削除（医療保険・介護保険共通）

    PDF仕様: input#inPopupBtnDel

    Args:
        staff_name: 指定すると同日同時刻の別行を誤って削除しない（旧行の職員1を渡す）
        end_time: 指定すると終了の時刻も合う行だけを消す（旧行の終了を渡す）
    """
    print(f"  削除: {day}日 {start_time}" + (f"～{end_time}" if end_time else ""))

    if not click_schedule_entry(page, day, start_time, staff_name=staff_name,
                                end_time=end_time):
        print("    予定が見つかりません")
        # ambiguous_entry など click_schedule_entry が積んだ理由を上書きしない
        if getattr(_last_failure_reason, "value", None) is None:
            _set_reason("entry_not_found")
        return False

    if dry_run:
        print("  [dry-run] 削除をスキップ")
        close_edit_dialog(page)
        return True

    # 削除前の同時刻・予定側の行数を記録 (M-1)。
    # 職員テキストが読めない等で職員フィルタが空振りしても、
    # 「行数が減っていない = 実際には消えていない」を検出できるようにする。
    count_before = _count_plan_entries(page, day, start_time, end_time=end_time)
    print(f"    削除前の同時刻エントリ数: {count_before}")

    try:
        # 削除ボタンのクリックでネイティブ confirm() が発火する場合に備える
        page.on("dialog", _accept_dialog)

        # Primary: PDF仕様の削除ボタン
        # 2026-09-03: モーダルの表示が遅いだけで「削除ボタンが見つかりません」に
        # なる事故が5件あったため、3秒即断ではなく10秒ポーリングで待つ。
        delete_btn = page.locator("input#inPopupBtnDel")
        if _wait_visible(page, delete_btn, timeout_ms=10000, label="削除ボタン"):
            try:
                delete_btn.click(timeout=10000)
            except Exception:
                # Step 2: force + no_wait_after
                try:
                    print("    削除ボタンクリックタイムアウト → force+noWaitAfterで再試行")
                    delete_btn.click(force=True, no_wait_after=True, timeout=3000)
                except Exception:
                    # Step 3: JSクリック
                    print("    削除ボタン: force+noWaitAfterも失敗 → JSクリック")
                    page.evaluate("document.querySelector('#inPopupBtnDel')?.click()")
            page.wait_for_timeout(2000)
        else:
            # Fallback
            delete_btn = page.locator(
                "button:has-text('削除'), a:has-text('削除'), input[value*='削除']"
            ).first
            if delete_btn.is_visible():
                try:
                    delete_btn.click(timeout=10000)
                except Exception:
                    try:
                        print("    削除ボタンクリックタイムアウト → force+noWaitAfterで再試行")
                        delete_btn.click(force=True, no_wait_after=True, timeout=3000)
                    except Exception:
                        print("    削除ボタン: force+noWaitAfterも失敗 → JSクリック")
                        page.evaluate("document.querySelector('#inPopupBtnDel')?.click()")
                page.wait_for_timeout(2000)
            else:
                print("    削除ボタンが見つかりません")
                page.remove_listener("dialog", _accept_dialog)
                close_edit_dialog(page)
                _set_reason("delete_button_not_found")
                return False

        # HTML確認ダイアログ（ネイティブ confirm でない場合のフォールバック）
        try:
            # 2026-08-21: 素の「削除」ボタン/inputも対象に (確認モーダルの
            # 文言が「削除」だけのケースを取りこぼし、削除が無反応になっていた疑い)。
            # 元の #inPopupBtnDel を再度押さないよう :not で除外する。
            ok_btn = page.locator(
                "button:has-text('OK'), button:has-text('はい'), button:has-text('削除する'), "
                "button:has-text('削除'), input[value='削除']:not(#inPopupBtnDel), "
                "input[value='OK'], input[value='はい']"
            ).first
            if ok_btn.is_visible(timeout=2000):
                _safe_click(page, ok_btn, timeout=5000, description="削除確認OKボタン")
                page.wait_for_timeout(1000)
        except Exception:
            pass

        page.remove_listener("dialog", _accept_dialog)

        # 削除後のAJAX完了待機
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        # モーダルが閉じるまでポーリング (最大15秒)
        # 2026-09-03: 削除クリック直後のスクリーンショットでモーダルが開いたままでも
        # 実際には直後に削除が通っていた。即断せず閉じるのを待ってから検証する。
        delete_btn_check = page.locator("input#inPopupBtnDel")
        modal_closed = False
        waited = 0
        while True:
            try:
                if not delete_btn_check.is_visible():
                    modal_closed = True
            except Exception:
                # 判定できない間は「閉じた」とみなさず待ち続ける (L-1)
                pass
            if modal_closed or waited >= 15000:
                break
            page.wait_for_timeout(500)
            waited += 500
        if not modal_closed:
            print("    警告: 削除後15秒経ってもモーダルが開いたままです (削除未受理として扱う)")
            close_edit_dialog(page)
            _set_reason("delete_not_accepted")
            return False
        if waited:
            print(f"    モーダルが閉じるまで {waited}ms 待機しました")

        # 削除検証 (上記 _schedule_entry_exists docstring 参照)。
        # 2026-09-03: 反映が遅れて偽NGになるため、最大4回 (約8秒) ポーリングする。
        # 判定は2条件:
        #   (a) 対象職員の行が消えていること
        #   (b) 同時刻の予定側の行数が削除前より減っていること (M-1)
        # (b) により、職員テキストが読めず(a)が空振りしても
        #     「実は1件も消えていない」無反応削除を取りこぼさない。
        deleted = False
        count_after = count_before
        for check in range(4):
            count_after = _count_plan_entries(page, day, start_time, end_time=end_time)
            gone = not _schedule_entry_exists(page, day, start_time, staff_name,
                                              end_time=end_time)
            decreased = count_after < count_before if count_before > 0 else True
            if gone and decreased:
                deleted = True
                break
            if check < 3:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)

        if not deleted:
            if count_before > 0 and count_after >= count_before:
                print(f"    削除検証NG: 同時刻エントリ数が減っていません "
                      f"({count_before} → {count_after})")
            print("    削除検証NG: エントリがまだ残っています (削除失敗として扱う)")
            # デバッグ保存 (2026-08-21): 何のダイアログ/状態で止まったかを残す。
            try:
                import datetime as _dt
                _ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                page.screenshot(path=f"/app/artifacts/delete_verify_ng_{day}_{_ts}.png", full_page=True)
                with open(f"/app/artifacts/delete_verify_ng_{day}_{_ts}.html", "w", encoding="utf-8") as _f:
                    _f.write(page.content())
                print(f"    デバッグ保存: delete_verify_ng_{day}_{_ts}.png/.html")
            except Exception as _e:
                print(f"    デバッグ保存失敗: {_e}")
            close_edit_dialog(page)
            _set_reason("delete_not_verified")
            return False
        print("    削除完了 (検証OK)")
        return True

    except Exception as e:
        print(f"    削除エラー: {e}")
        try:
            page.remove_listener("dialog", _accept_dialog)
        except Exception:
            pass
        close_edit_dialog(page)
        _set_reason("delete_error")
        return False


# =============================================================================
# 新規追加: 保険種別フォーム入力
# =============================================================================

MONTHLY_SCHEDULE_URL = "https://r.kaipoke.biz/bizhnc/monthlyShiftsList?isFromMenuBizhnc=true"


def _recover_schedule_page(page, month_str: str = None, context=None) -> bool:
    """失敗後の立て直し: 月間スケジュールのページを開き直し、提供月を合わせて確かめる

    2026-11-19 (設計 B-1): 以前は開いていた窓を JavaScript でページから取り除いていた。
    12/1 齊藤様の失敗の後、そのページのまま進んだため利用者の選択欄まで働かなくなり、
    続く 72 件が全部飛ばされた。ページの部品は取り除かず、ページごと開き直す。

    呼び出し元は、この後に利用者を選び直してから次の 1 件へ進むこと。

    Args:
        month_str: 提供月 ("2026-12")。None なら月は合わせない
        context: ブラウザコンテキスト。渡されたらセッション切れのときに再ログインする

    Returns:
        bool: 開き直して提供月を確かめられたら True
    """
    print("  ※ 立て直し開始: 月間スケジュールのページを開き直します")

    # Step 1: 編集の窓が開いていれば、ふつうの手順で閉じる (ページの部品は取り除かない)
    try:
        modal_open = False
        for sel in ("input#btnRegisPop", "input#inPopupBtnDel"):
            try:
                if page.locator(sel).is_visible(timeout=500):
                    modal_open = True
                    break
            except Exception:
                continue
        if modal_open:
            close_edit_dialog(page)
    except Exception as e:
        print(f"    窓を閉じる途中で例外 (開き直しで捨てるので続けます): {e}")

    # 開き直しが終わるまでは、提供月を確かめられていないページとして扱う
    _set_page_unverified(True)
    try:
        # Step 2: セッションが切れていれば再ログインして月間スケジュールまで戻る
        if context is not None and month_str:
            if not ensure_session(page, context, month_str):
                print("  ※ 立て直し失敗: セッションを戻せません")
                return False

        # Step 3: ページごと開き直す (壊れた DOM を捨てる)
        page.goto(MONTHLY_SCHEDULE_URL, timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        # Step 4: 提供月を合わせて確かめる (初めのセットアップと同じ関数)
        if month_str:
            set_service_month(page, month_str)
            page.wait_for_timeout(1000)
            verify_service_month(page, month_str)  # 月が違えば RuntimeError

        # Step 5: 利用者の選択欄が働く状態か
        if _get_user_dropdown(page) is None:
            print("  ※ 立て直し失敗: 利用者の選択欄が見つかりません")
            return False

        _set_page_unverified(False)
        print("  ※ 立て直し完了: 月間スケジュールを開き直しました")
        return True
    except Exception as e:
        print(f"  ※ 立て直し失敗: {e}")
        return False


def click_new_add_button(page) -> bool:
    """
    「新規追加」ボタンをクリック

    カイポケ実際の構造:
      <button type="button" class="btn btn-sms btn-sm"
              onclick="showHNC097807Add(...)" title="新規追加">新規追加</button>
    """
    # ページの状態が安定するまで待機
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    # ページトップにスクロール（背景スクロール対策）
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
    except Exception:
        pass

    add_buttons = [
        "button[title='新規追加']",
        "#btn_area button:has-text('新規追加')",
        "button:has-text('新規追加')",
        "text=新規追加",
    ]
    for selector in add_buttons:
        btn = page.locator(selector).first
        try:
            if btn.is_visible(timeout=5000):
                _safe_click(page, btn, timeout=5000, description="新規追加ボタン")
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue

    # デバッグ: ボタンが見つからない場合の状態を記録
    try:
        current_url = page.url
        print(f"    デバッグ: 現在のURL = {current_url}")
        timestamp = __import__('datetime').datetime.now().strftime("%Y%m%d_%H%M%S")
        page.screenshot(path=f"artifacts/debug_no_add_btn_{timestamp}.png")
        print(f"    デバッグ: スクリーンショット保存")
    except Exception:
        pass
    print("    新規追加ボタンが見つかりません")
    return False


def _select_and_wait(page, select_locator, label: str, field_name: str) -> bool:
    """selectを選択してAJAX完了まで待機（Kaipoke JSF対応）"""
    try:
        select_locator.select_option(label=label)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(1000)
        print(f"    {field_name}: {label}")
        return True
    except Exception as e:
        print(f"    {field_name}設定エラー: {e}")
        return False



# ---------------------------------------------------------------------------
# S3 (2026-08-23): らく助のサービス内容に応じて 医療保険の3選択を切り替える。
# Phase 0 プローブ (commands/probe_service_options.py) で採取した value:
#   サービス区分 #inPopupEstimate1: 01=訪問看護 / 02=精神科訪問看護
#   基本療養費   #inPopupEstimate2: 01=基本療養費Ⅰ (一般) / 01=精神科基本療養費Ⅰ (精神科)
#   職員資格     #inPopupEstimate3: 01=看護師等 / 03=准看護師 (02 はPT等/OTで らく助は未使用)
# 文言ではなく value で選ぶ (文言変更に強い)。未知の service_type は従来既定 (精神科×Ⅰ×看護師等)。
# ---------------------------------------------------------------------------
SERVICE_SELECT_VALUES = {
    "estimate1": {"psychiatric": "02", "general": "01"},
    "estimate2": {"psychiatric": "01", "general": "01"},
    "estimate3": {"nurse": "01", "assistant_nurse": "03"},
}


def resolve_medical_selects(service_type=None) -> dict:
    """service_type (例: 精神基本療養費Ⅰ・准看) → 各 select の value と期待されるサービス内容."""
    st = (service_type or "").strip()
    category = "general" if (st and "精神" not in st) else "psychiatric"
    grade = "assistant_nurse" if "准看" in st else "nurse"
    return {
        "category": category,
        "grade": grade,
        "estimate1": SERVICE_SELECT_VALUES["estimate1"][category],
        "estimate2": SERVICE_SELECT_VALUES["estimate2"][category],
        "estimate3": SERVICE_SELECT_VALUES["estimate3"][grade],
        "expected_content": (
            ("精神基本療養費Ⅰ" if category == "psychiatric" else "基本療養費Ⅰ")
            + "・" + ("准看" if grade == "assistant_nurse" else "正看")
        ),
    }


SELECT_OPTION_WAIT_MS = 45000


def _select_value_and_wait(page, select_locator, value: str, field_name: str) -> bool:
    """value 指定で select してAJAX完了まで待機 (Kaipoke JSF対応)."""
    if not value:
        print("    %s: 選ぶ値が空です → 選びません" % field_name)
        return False
    try:
        # 2026-11-19 (設計 B-4): 職員資格の選択肢が 30 秒出ずに失敗したことがある
        # (12/1 齊藤様・カイポケの応答の遅れ)。選ぶ値の選択肢が出るまで最大 45 秒待つ。
        # 値は CSS に埋め込まず、引数として渡して比べる (引用符などで壊れないように)。
        # 選択欄は AJAX で描き直されることがあるので、毎回 locator から引き直して確かめる。
        found = False
        deadline = time.monotonic() + SELECT_OPTION_WAIT_MS / 1000
        while True:
            try:
                found = bool(select_locator.evaluate(
                    "(sel, v) => Array.from(sel.options).some(o => o.value === v)",
                    value, timeout=2000))
            except Exception:
                found = False
            if found or time.monotonic() >= deadline:
                break
            page.wait_for_timeout(500)
        if not found:
            print("    %s: 選択肢 (value=%s) が %d 秒待っても出ません" % (
                field_name, value, SELECT_OPTION_WAIT_MS // 1000))
            return False
        select_locator.select_option(value=value)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(1000)
        label = select_locator.evaluate(
            "(sel, v) => { const o = Array.from(sel.options).find(o => o.value === v);"
            " return o ? o.textContent : null; }", value) or value
        print("    %s: %s (value=%s)" % (field_name, label.strip(), value))
        return True
    except Exception as e:
        print("    %s設定エラー (value=%s): %s" % (field_name, value, e))
        return False


def fill_medical_insurance_fields(page, service_type=None) -> bool:
    """
    医療保険の新規追加: 上部セクションのフィールドを設定

    PDF「02_新規追加・医療保険」仕様:
    - 保険区分: radio#inPopupInsuranceDivision02 (医療保険)
    - サービス区分: select#inPopupEstimate1 → "精神科訪問看護"
    - 基本療養費: select#inPopupEstimate2 → "Ⅰ"
    - 職員資格: select#inPopupEstimate3 → "看護師等"

    注意: 各フィールド変更はKaipokeのJSF AJAXをトリガーするため、
    networkidle を待機してサーバーサイドのバリデーションを通す。
    """
    try:
        # 保険区分: 医療保険を選択
        medical_radio = page.locator("input#inPopupInsuranceDivision02")
        if medical_radio.is_visible(timeout=3000):
            # まずPlaywrightクリックを試行
            pw_click_ok = False
            try:
                medical_radio.click(timeout=5000)
                pw_click_ok = True
            except Exception:
                # Step 2: force + no_wait_after（DOMイベント正常発火、JSF対応）
                try:
                    print("    医療保険ラジオ: Playwrightタイムアウト → force+noWaitAfterで再試行")
                    medical_radio.click(force=True, no_wait_after=True, timeout=3000)
                    pw_click_ok = True  # force clickは正常なDOMイベントを発火
                except Exception:
                    # Step 3: 最終手段 - JSクリック + changeDivision()
                    print("    医療保険ラジオ: force+noWaitAfterも失敗 → JSクリック+changeDivision()")
                    page.evaluate("""() => {
                        const radio = document.querySelector('#inPopupInsuranceDivision02');
                        if (radio) {
                            radio.checked = true;
                            radio.click();
                        }
                        // changeDivision()を明示的に呼び出し（AJAXチェーン確保）
                        if (typeof changeDivision === 'function') {
                            changeDivision();
                        }
                    }""")

            # AJAX: フォーム構造が介護→医療に切り替わる
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(2000)

            # フィールドが有効化されたか確認（最大5秒待機）
            if not pw_click_ok:
                for retry_i in range(10):
                    is_disabled = page.evaluate("document.querySelector('#inPopupStartHour')?.disabled")
                    if not is_disabled:
                        break
                    page.wait_for_timeout(500)
                else:
                    # まだdisabled → changeDivision()を再度呼び出し
                    print("    警告: フォームフィールドがdisabled → changeDivision()再実行")
                    page.evaluate("""() => {
                        if (typeof changeDivision === 'function') changeDivision();
                    }""")
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    page.wait_for_timeout(3000)

            print("    保険区分: 医療保険 を選択")
        else:
            print("    警告: radio#inPopupInsuranceDivision02 が見つかりません")
            return False

        # S3: らく助のサービス内容に応じて value で選ぶ (既定は 精神科×Ⅰ×看護師等)
        sel = resolve_medical_selects(service_type)
        print("    サービス内容の分岐: %s → 区分=%s 資格=%s (期待: %s)" % (
            service_type or "(未指定→既定)", sel["category"], sel["grade"], sel["expected_content"]))

        estimate1 = page.locator("select#inPopupEstimate1")
        if estimate1.is_visible(timeout=5000):
            if not _select_value_and_wait(page, estimate1, sel["estimate1"], "サービス区分"):
                return False

        estimate2 = page.locator("select#inPopupEstimate2")
        if estimate2.is_visible(timeout=5000):
            if not _select_value_and_wait(page, estimate2, sel["estimate2"], "基本療養費"):
                return False

        estimate3 = page.locator("select#inPopupEstimate3")
        if estimate3.is_visible(timeout=5000):
            if not _select_value_and_wait(page, estimate3, sel["estimate3"], "職員資格"):
                return False

        return True

    except Exception as e:
        print(f"    医療保険フィールド設定エラー: {e}")
        return False


def fill_nursing_insurance_fields(page, start_time: str, end_time: str) -> bool:
    """
    介護保険の新規追加: 上部セクションのフィールドを設定

    PDF「05_新規追加・介護保険」仕様:
    - 保険区分: radio#inPopupInsuranceDivision01 (介護保険, デフォルト)
    - サービス種類: select#inPopupServiceKindId → "訪問看護"
    - サービス区分: select#inPopupEstimate1 → "通常の算定"
    - 算定時間: select#inPopupEstimate4 → 時間から計算
    - サービス内容: auto-populates (read-only)
    """
    try:
        # 保険区分: 介護保険（デフォルトのはずだが念のため）
        nursing_radio = page.locator("input#inPopupInsuranceDivision01")
        if nursing_radio.is_visible(timeout=3000):
            _safe_click(page, nursing_radio, timeout=5000, description="介護保険ラジオボタン")
            # AJAX: フォーム構造が切り替わる可能性
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            print("    保険区分: 介護保険 を選択")

        # サービス種類: 訪問看護（AJAX後にオプションが読み込まれる）
        service_kind = page.locator("select#inPopupServiceKindId")
        if service_kind.is_visible(timeout=5000):
            # オプションが読み込まれるまで待つ（空optionのみの場合がある）
            for _ in range(15):
                options = service_kind.locator("option").all()
                if len(options) > 1:
                    break
                page.wait_for_timeout(500)

            options = service_kind.locator("option").all()
            # デバッグ: 選択肢を出力
            opt_texts = [o.text_content().strip() for o in options[:10]]
            print(f"    ServiceKindId 選択肢: {opt_texts}")

            selected = False
            for opt in options:
                text = opt.text_content().strip()
                # "訪問看護" にマッチ（"介護予防訪問看護" も可能だが通常は "訪問看護"）
                if "訪問看護" in text and "予防" not in text:
                    _select_and_wait(page, service_kind, text, "サービス種類")
                    selected = True
                    break
            # サービス種類選択後、次のselectオプション読み込み待機
            if selected:
                page.wait_for_timeout(1000)
            if not selected:
                # フォールバック: "訪問看護" を含む最初のオプションを選択
                for opt in options:
                    text = opt.text_content().strip()
                    if "訪問看護" in text:
                        _select_and_wait(page, service_kind, text, "サービス種類(fallback)")
                        selected = True
                        page.wait_for_timeout(1000)
                        break
            if not selected:
                print("    警告: '訪問看護' オプションが見つかりません")

        # サービス区分: 通常の算定
        estimate1 = page.locator("select#inPopupEstimate1")
        if estimate1.is_visible(timeout=5000):
            # オプションが読み込まれるまで待つ
            for _ in range(15):
                options = estimate1.locator("option").all()
                if len(options) > 1:
                    break
                page.wait_for_timeout(500)
            options = estimate1.locator("option").all()
            for opt in options:
                if "通常の算定" in opt.text_content():
                    _select_and_wait(page, estimate1, opt.text_content(), "サービス区分")
                    break

        # 算定時間: 開始・終了時間から計算
        santei_time = calculate_santei_time(start_time, end_time)
        estimate4 = page.locator("select#inPopupEstimate4")
        if estimate4.is_visible(timeout=5000):
            # オプションが読み込まれるまで待つ
            for _ in range(15):
                options = estimate4.locator("option").all()
                if len(options) > 1:
                    break
                page.wait_for_timeout(500)
            options = estimate4.locator("option").all()
            selected = False
            for opt in options:
                if santei_time in opt.text_content():
                    _select_and_wait(page, estimate4, opt.text_content().strip(), "算定時間")
                    selected = True
                    break
            if not selected:
                opt_texts = [o.text_content().strip() for o in options]
                print(f"    警告: 算定時間 '{santei_time}' が選択肢に見つかりません")
                print(f"    選択肢: {opt_texts}")
        else:
            print("    警告: select#inPopupEstimate4 が見つかりません")

        return True

    except Exception as e:
        print(f"    介護保険フィールド設定エラー: {e}")
        return False


def add_schedule_entry(page, correction: Correction, dry_run: bool = False, _retry: int = 0) -> bool:
    """
    新しいスケジュールエントリを追加（保険種別による分岐あり）

    Args:
        page: Playwrightのページオブジェクト
        correction: 修正データ（business_type で分岐）
        dry_run: テスト実行
        _retry: 内部リトライカウント（ポップアップ再試行用）
    """
    day = int(correction.date_to) if correction.date_to.isdigit() else 1
    print(f"  追加: {day}日 {correction.start_time_to}-{correction.end_time_to} "
          f"(業務種別: {correction.business_type})")

    try:
        # Step 1: 新規追加ボタンをクリック
        if not click_new_add_button(page):
            return False

        # Step 2: 保険種別に応じた上部フィールド設定
        if correction.is_medical_insurance():
            if not fill_medical_insurance_fields(page, correction.service_type):
                close_edit_dialog(page)
                return False
        elif correction.is_nursing_insurance():
            if not fill_nursing_insurance_fields(
                page, correction.start_time_to, correction.end_time_to
            ):
                close_edit_dialog(page)
                return False
        else:
            # business_type が空 or 不明 → 医療保険をデフォルトとする
            print(f"    警告: 業務種別 '{correction.business_type}' が不明です。医療保険として処理します。")
            if not fill_medical_insurance_fields(page, correction.service_type):
                close_edit_dialog(page)
                return False

        # 診断: Step 2後のエラーメッセージ確認
        _check_form_errors(page, "Step2:保険フィールド設定後")

        # Step 3: 日付を設定（新規追加用: インラインカレンダーを使用）
        _set_add_modal_date(page, day)

        # 診断: Step 3後のエラーメッセージ確認
        _check_form_errors(page, "Step3:日付設定後")

        # Step 4: 時間を設定
        start_parts = correction.start_time_to.split(":")
        end_parts = correction.end_time_to.split(":")
        if len(start_parts) >= 2 and len(end_parts) >= 2:
            edit_schedule_time(
                page,
                int(start_parts[0]), int(start_parts[1]),
                int(end_parts[0]), int(end_parts[1])
            )

        # 診断: Step 4後のエラーメッセージ確認
        _check_form_errors(page, "Step4:時間設定後")

        # Step 4.5: フォームバリデーション確認 & リトライ
        # 職員情報入力ボタンが表示されるか確認（表示 = サーバーバリデーションOK）
        staff_btn = page.locator("span#input_staff_on")
        staff_btn_visible = False
        try:
            staff_btn_visible = staff_btn.is_visible(timeout=3000)
        except Exception:
            pass

        if not staff_btn_visible:
            print("    ※ 職員情報入力ボタンが未表示 → フォームリトライ")
            # フォームをリフレッシュ: 一度介護保険に切替えてから医療保険に戻す
            if correction.is_medical_insurance() or (not correction.is_nursing_insurance()):
                try:
                    # 介護保険に切替（JSで確実に切替）
                    page.evaluate("""() => {
                        const radio = document.querySelector('#inPopupInsuranceDivision01');
                        if (radio) {
                            radio.checked = true;
                            radio.click();
                            if (typeof changeDivision === 'function') changeDivision();
                        }
                    }""")
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)

                    # 医療保険に戻す
                    if not fill_medical_insurance_fields(page, correction.service_type):
                        print("    リトライ: 医療保険フィールド再設定失敗")
                        close_edit_dialog(page)
                        return False

                    # 日付を再設定
                    _set_add_modal_date(page, day)

                    # 時間を再設定
                    if len(start_parts) >= 2 and len(end_parts) >= 2:
                        edit_schedule_time(
                            page,
                            int(start_parts[0]), int(start_parts[1]),
                            int(end_parts[0]), int(end_parts[1])
                        )

                    # 再確認
                    try:
                        staff_btn_visible = staff_btn.is_visible(timeout=3000)
                    except Exception:
                        pass
                    if staff_btn_visible:
                        print("    リトライ成功: 職員情報入力ボタン表示確認")
                    else:
                        print("    リトライ後も職員情報入力ボタン未表示")
                except Exception as e:
                    print(f"    リトライエラー: {e}")

        # Step 5: 職員を設定（新規追加なので for_new_entry=True）
        edit_staff(
            page,
            correction.staff1_to,
            correction.staff2_to,
            for_new_entry=True
        )

        # Step 5.5: chargeStaff1Id1チェック → 見つからなければポップアップ再試行
        if correction.staff1_to and correction.staff1_to != "未割当":
            staff1_visible = False
            try:
                staff1_visible = page.locator("select#chargeStaff1Id1").is_visible(timeout=1000)
            except Exception:
                pass
            if not staff1_visible and _retry < 1:
                print("  ※ 職員selectが未表示 → ポップアップを閉じて再試行（1回目）")
                close_edit_dialog(page)
                page.wait_for_timeout(2000)
                return add_schedule_entry(page, correction, dry_run, _retry=_retry + 1)

        # Step 6: 登録
        if dry_run:
            print("  [dry-run] 登録をスキップ")
            close_edit_dialog(page)
            return True
        else:
            success = click_register_button(page)
            if not success:
                # 登録失敗時のスクリーンショット
                try:
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    page.screenshot(path=f"artifacts/debug_add_failed_{timestamp}.png")
                    print(f"    診断スクリーンショット保存")
                except Exception:
                    pass
                close_edit_dialog(page)
            return success

    except Exception as e:
        print(f"    追加エラー: {e}")
        close_edit_dialog(page)
        return False


# =============================================================================
# イベント作成（職員別タブ）
# =============================================================================

def navigate_to_staff_tab(page) -> bool:
    """
    職員別タブに遷移

    カイポケの実際の構造:
      <ul class="nav nav-tabs">
        <li><a>利用者別</a></li>
        <li><a onclick="submitForm(3);">職員別</a></li>
      </ul>
    """
    try:
        tab_selectors = [
            ".nav-tabs a:has-text('職員別')",
            "a[onclick*='submitForm(3)']",
            "text=職員別",
        ]
        for selector in tab_selectors:
            staff_tab = page.locator(selector).first
            try:
                if staff_tab.is_visible(timeout=2000):
                    _safe_click(page, staff_tab, timeout=5000, description="職員別タブ")
                    page.wait_for_load_state("networkidle", timeout=10000)
                    page.wait_for_timeout(2000)
                    print("  職員別タブに遷移しました")
                    return True
            except Exception:
                continue
        print("  職員別タブが見つかりません")
        return False
    except Exception as e:
        print(f"  職員別タブ遷移エラー: {e}")
        return False


def navigate_to_user_tab(page) -> bool:
    """
    利用者別タブに戻る

    カイポケの実際の構造:
      <ul class="nav nav-tabs">
        <li class="active"><a>利用者別</a></li>
      </ul>
    """
    try:
        tab_selectors = [
            ".nav-tabs a:has-text('利用者別')",
            "text=利用者別",
        ]
        for selector in tab_selectors:
            user_tab = page.locator(selector).first
            try:
                if user_tab.is_visible(timeout=2000):
                    _safe_click(page, user_tab, timeout=5000, description="利用者別タブ")
                    page.wait_for_load_state("networkidle", timeout=10000)
                    page.wait_for_timeout(2000)
                    print("  利用者別タブに遷移しました")
                    return True
            except Exception:
                continue
        print("  利用者別タブが見つかりません")
        return False
    except Exception as e:
        print(f"  利用者別タブ遷移エラー: {e}")
        return False


def select_staff_member(page, staff_name: str) -> bool:
    """
    職員別タブで職員を選択

    スクリーンショットで確認済みの構造:
      div#helper_search > table > tbody > tr > td >
        select#staffMemberInternalId.form-control
        onchange="submitStaffInternalId(this.value)"
    """
    print(f"  職員を選択しています: {staff_name}")

    # 方法1: select#staffMemberInternalId から直接選択
    selectors = [
        "select#staffMemberInternalId",
        "div#helper_search select.form-control",
    ]
    for selector in selectors:
        try:
            staff_select = page.locator(selector).first
            if not staff_select.is_visible(timeout=3000):
                continue

            # 現在選択中の職員を確認
            current_text = staff_select.evaluate(
                "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''"
            )
            if current_text and name_matches(staff_name, current_text):
                print(f"  すでに選択されています: {staff_name}")
                return True

            # 全optionを取得して名前マッチ
            options_data = staff_select.evaluate("""el => {
                return Array.from(el.options).map((opt, i) => ({
                    index: i,
                    value: opt.value,
                    text: opt.text
                }));
            }""")
            for opt in options_data:
                if name_matches(staff_name, opt["text"]):
                    staff_select.select_option(value=opt["value"])
                    page.wait_for_load_state("networkidle", timeout=10000)
                    page.wait_for_timeout(1000)
                    print(f"  ドロップダウンから職員を選択: {staff_name} (value={opt['value']})")
                    return True

            print(f"  ドロップダウンに職員が見つかりません: {staff_name}")
            print(f"  選択肢: {[o['text'] for o in options_data[:10]]}...")
            return False
        except Exception as e:
            print(f"  セレクタ '{selector}' でエラー: {e}")
            continue

    print(f"  職員が見つかりません: {staff_name}")
    return False


def click_calendar_day(page, day: int, popup_selector: str = "") -> bool:
    """
    カレンダーグリッドから指定日をクリック（イベントポップアップ用）

    イベントポップアップのカレンダーはtable内のtdに日付が表示されており、
    selectドロップダウンではなくクリック操作で日付を選択する。

    Args:
        page: Playwrightのページオブジェクト
        day: クリックする日（1-31）
        popup_selector: ポップアップのCSSセレクタ（スコープ限定用）
    """
    print(f"  カレンダー日付クリック: {day}日")
    day_str = str(day)

    try:
        # ポップアップ内のカレンダーテーブルを探す
        scope = page
        if popup_selector:
            popup = page.locator(popup_selector)
            if popup.is_visible(timeout=2000):
                scope = popup

        # Strategy 1: カレンダーセル（td）から日付テキスト完全一致でクリック
        # カレンダーのtdは通常 class="calendar-day" や数字のみを含む
        calendar_cells = scope.locator("table td").all()
        for cell in calendar_cells:
            try:
                cell_text = cell.text_content().strip()
                if cell_text == day_str:
                    # クリック可能かチェック（ヘッダー行やdisabledでないか）
                    tag = cell.evaluate("el => el.tagName")
                    if tag.upper() == "TH":
                        continue
                    # aタグやリンクが中にあればそちらをクリック
                    inner_link = cell.locator("a")
                    if inner_link.count() > 0:
                        _safe_click(page, inner_link.first, timeout=5000, description="カレンダー日付クリック")
                    else:
                        _safe_click(page, cell, timeout=5000, description="カレンダー日付クリック")
                    page.wait_for_timeout(500)
                    print(f"    カレンダーから {day}日 をクリック")
                    return True
            except Exception:
                continue

        # Strategy 2: aタグ内のテキストで探す（カレンダーがa[href]で構成されている場合）
        calendar_links = scope.locator("table td a").all()
        for link in calendar_links:
            try:
                link_text = link.text_content().strip()
                if link_text == day_str:
                    _safe_click(page, link, timeout=5000, description="カレンダーリンク")
                    page.wait_for_timeout(500)
                    print(f"    カレンダーリンクから {day}日 をクリック")
                    return True
            except Exception:
                continue

        print(f"    カレンダーに {day}日 が見つかりません")
        return False

    except Exception as e:
        print(f"    カレンダー日付クリックエラー: {e}")
        return False


def add_event_entry(page, correction: Correction, dry_run: bool = False) -> bool:
    """
    イベント（個別業務）を新規登録

    PDF「07_イベントリクエストの作成」仕様:
    - button#individualMonthlyShiftAssignPopup → 個別業務の新規登録
    - input[name="popupSelectedIndividual"] → 新しく登録する
    - input#popupIndividualName → イベント名（備考欄の値）
    - 時間・日付設定
    - button#popupIndividualButtonAdd → 登録
    """
    day = int(correction.date_to) if correction.date_to.isdigit() else 1
    event_name = correction.remarks or correction.business_type
    print(f"  イベント追加: {day}日 {correction.start_time_to}-{correction.end_time_to} "
          f"イベント名: '{event_name}'")

    try:
        # Step 1: 「個別業務の新規登録」ボタンをクリック
        _remove_floating_overlays(page)
        new_event_btn = page.locator("button#individualMonthlyShiftAssignPopup")
        if not new_event_btn.is_visible(timeout=5000):
            # Fallback
            new_event_btn = page.locator("text=個別業務の新規登録").first
            if not new_event_btn.is_visible(timeout=3000):
                print("    「個別業務の新規登録」ボタンが見つかりません")
                return False
        _click_with_scroll(page, new_event_btn)
        page.wait_for_timeout(2000)

        # Step 2: 「新しく登録する」ラジオを選択
        # ラジオボタンは2つ: 「既存から選択」/ 「新しく登録する」
        # 「新しく登録する」を選択すると input#popupIndividualName が enabled になる
        # 順序に依存しないよう、ラベルテキストまたはvalue属性で特定する
        selected_radio = False

        # 方法1: ラベルテキストで「新しく登録する」を特定
        new_register_label = page.locator("label:has-text('新しく登録')").first
        if new_register_label.is_visible(timeout=2000):
            # ラベルのfor属性またはラベル内のinputをクリック
            label_for = new_register_label.get_attribute("for")
            if label_for:
                radio = page.locator(f"input#{label_for}")
                if radio.count() > 0:
                    _safe_click(page, radio, timeout=5000, description="ラジオボタン")
                    page.wait_for_timeout(1000)
                    print("    「新しく登録する」を選択（ラベルfor属性経由）")
                    selected_radio = True
            if not selected_radio:
                _safe_click(page, new_register_label, timeout=5000, description="ラベルクリック")
                page.wait_for_timeout(1000)
                print("    「新しく登録する」ラベルをクリック")
                selected_radio = True

        # 方法2: ラジオボタンから隣接ラベルを確認して特定
        if not selected_radio:
            radios = page.locator("input[name='popupSelectedIndividual']").all()
            print(f"    ラジオボタン数: {len(radios)}")
            for i, radio in enumerate(radios):
                try:
                    # 隣接するラベルのテキストを確認
                    label_text = radio.evaluate("""el => {
                        const label = el.nextElementSibling || el.parentElement;
                        return label ? label.textContent.trim() : '';
                    }""")
                    if "新しく登録" in label_text:
                        _safe_click(page, radio, timeout=5000, description="ラジオボタン")
                        page.wait_for_timeout(1000)
                        print(f"    「新しく登録する」を選択（ラジオ[{i}]）")
                        selected_radio = True
                        break
                except Exception:
                    continue

            # フォールバック: 最後のラジオ（通常は「新しく登録する」）
            if not selected_radio and radios:
                _safe_click(page, radios[-1], timeout=5000, description="ラジオボタン")
                page.wait_for_timeout(1000)
                print(f"    最後のラジオボタンを選択（フォールバック）")

        # Step 3: イベント名を入力（ラジオ選択後にenabledになるのを待つ）
        event_name_input = page.locator("input#popupIndividualName")
        try:
            event_name_input.wait_for(state="attached", timeout=5000)
            # disabled属性が解除されるのを待つ
            page.wait_for_function(
                "() => !document.querySelector('#popupIndividualName').disabled",
                timeout=5000
            )
            event_name_input.fill(event_name)
            page.wait_for_timeout(300)
            print(f"    イベント名入力: {event_name}")
        except Exception as e:
            print(f"    イベント名入力に失敗（フィールドがdisabled）: {e}")
            # デバッグ: ラジオボタンの状態を出力
            for i, r in enumerate(radios):
                try:
                    checked = r.is_checked()
                    val = r.get_attribute("value")
                    print(f"    ラジオ[{i}]: value={val}, checked={checked}")
                except:
                    pass
            close_edit_dialog(page)
            return False

        # Step 4: 時間を設定（イベントポップアップ用セレクタを調査・使用）
        start_parts = correction.start_time_to.split(":")
        end_parts = correction.end_time_to.split(":")
        if len(start_parts) >= 2 and len(end_parts) >= 2:
            _set_event_time(
                page,
                int(start_parts[0]), int(start_parts[1]),
                int(end_parts[0]), int(end_parts[1])
            )

        # Step 5: 日付を設定（イベントポップアップはカレンダーグリッド）
        click_calendar_day(page, day, "form#formIndividualMonthlyShiftAssignPopup")

        # Step 6: 登録
        if dry_run:
            print("  [dry-run] イベント登録をスキップ")
            close_edit_dialog(page)
            return True

        # イベント用の登録ボタン: button#popupIndividualButtonAdd
        add_btn = page.locator("button#popupIndividualButtonAdd")
        if not add_btn.is_visible(timeout=3000):
            add_btn = page.locator("text=登録する").first
        if add_btn.is_visible():
            _safe_click(page, add_btn, timeout=5000, description="イベント登録ボタン")
            page.wait_for_timeout(2000)

            # 確認ダイアログ
            try:
                ok_btn = page.locator("button:has-text('OK'), button:has-text('はい')").first
                if ok_btn.is_visible(timeout=2000):
                    _safe_click(page, ok_btn, timeout=5000, description="OKボタン")
                    page.wait_for_timeout(1000)
            except Exception:
                pass

            print("    イベント登録完了")
            return True
        else:
            print("    イベント登録ボタンが見つかりません")
            return False

    except Exception as e:
        print(f"    イベント追加エラー: {e}")
        return False


# =============================================================================
# 修正適用のルーティング
# =============================================================================

def _apply_move_with_reorder(page, correction: Correction, dry_run: bool = False,
                             month_str: str = None, context=None) -> bool:
    """削除と再追加が伴う変更 (日付変更 / 時間変更) を、消失させない順序で適用する

    2026-09-03の本番事故: 削除→追加の追加側が失敗し、旧行は既に消えていたため
    訪問がカイポケから消失した (並木9/9・安永9/9・前川七海9/10・前川心愛9/10)。

    対策:
      - 旧行キー(日付,開始時刻) と新行キーが異なる → 「追加→削除」に反転。
        追加が失敗しても旧行は無傷 (reason=add_failed_nothing_deleted)。
        削除は旧日付・旧開始時刻・旧職員で行うため、直前に追加した行は掴まない。
      - 同一キー (終了時刻や職員のみ変更) → 同じ行を二重に作れないので従来の
        削除→追加。追加が失敗したら元の予定を再追加してロールバックする。
    """
    # 日付が読めない場合に1日へフォールバックすると、無関係な行を消しかねない (M-4)
    if not (correction.date_from or "").strip().isdigit():
        print(f"  ※ 変更前の日付が不正です: '{correction.date_from}' → 何もせず中止します")
        _set_reason("invalid_date_from")
        return False

    day_from = int(correction.date_from)
    staff_from = correction.staff1_from or None
    add_first = _needs_add_first(correction.date_from, correction.start_time_from,
                                 correction.date_to, correction.start_time_to)

    if add_first:
        print("  ※ 新旧が別の行になるため、先に追加してから旧行を削除します (消失防止)")
        if not add_schedule_entry(page, correction, dry_run):
            if not _recover_schedule_page(page, month_str, context=context):
                # 提供月を確かめられないページでは何もしない (呼び出し元が開き直す)
                print("  ※ 追加に失敗し、ページの開き直しにも失敗しました。"
                      "旧行は削除していません (データ消失なし)")
                _set_reason("recovery_failed")
                return False
            print("  ※ 追加に失敗しました。旧行は削除していません (データ消失なし)")
            # リカバリでページが遷移している可能性があるため利用者を選び直す (C-2)
            if not select_user(page, correction.user_name):
                print(f"  ※ 警告: リカバリ後に利用者 '{correction.user_name}' を再選択できません")
            _set_reason("add_failed_nothing_deleted")
            return False
        # 追加後: networkidle待機 + カレンダー再描画待機
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        if not delete_schedule_entry(page, day_from, correction.start_time_from, dry_run,
                                     staff_name=staff_from,
                                     end_time=correction.end_time_from):
            print("  ※ 警告: 追加は成功しましたが旧行の削除に失敗しました。"
                  "この利用者には新旧2件が残っています。手動で旧行を削除してください")
            if getattr(_last_failure_reason, "value", None) == "ambiguous_entry":
                # 消す候補が 2 行以上あって押さなかった。どれを消すか人が見る
                print("  ※ 旧行の候補が 2 行以上あるため消していません (ambiguous_entry)")
                _set_reason("old_row_remains_ambiguous")
            else:
                _set_reason("old_row_remains_duplicate")
            return False
        return True

    # 新旧が同一キー → 従来どおり削除→追加。追加失敗時はロールバックで復元する。
    if _is_delete_done(correction):
        # 前の試行が削除の後で例外を出し、run_auto_apply がリトライしてきた場合。
        # ここで削除をやり直すと「予定が見つかりません」で終わり、旧行が消えたまま
        # 誤った理由が報告される。削除は済んでいるので追加から再開する。
        print("  ※ 前回の試行で旧行は削除済みです → 削除を再実行せず追加から再開します")
    else:
        print("  ※ 新旧が同一の行になるため、削除→再追加で処理します")
        if not delete_schedule_entry(page, day_from, correction.start_time_from, dry_run,
                                     staff_name=staff_from,
                                     end_time=correction.end_time_from):
            return False
        # delete_schedule_entry は実在検証つきなので、True = 旧行はもう無い。
        _mark_delete_done(correction)
        # 削除後: networkidle待機 + カレンダー再描画待機
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

    if add_schedule_entry(page, correction, dry_run):
        _clear_delete_done(correction)
        return True

    if not _recover_schedule_page(page, month_str, context=context):
        # 提供月を確かめられないページでロールバックを書き込まない
        print("  ※ 中止: 追加に失敗し、ページの開き直しにも失敗しました。ロールバックしません。"
              "旧行は削除済みのため、元の予定を手動で復元してください")
        _set_reason("recovery_failed")
        _clear_delete_done(correction)
        return False
    # リカバリは画面遷移を伴うことがあり、利用者が変わったまま書き込むと
    # 別人の予定を作ってしまう。再選択できなければ一切書き込まない (C-2)。
    if not select_user(page, correction.user_name):
        print(f"  ※ 中止: リカバリ後に利用者 '{correction.user_name}' を再選択できないため"
              f"ロールバックしません。元の予定を手動で復元してください")
        _set_reason("add_failed_row_lost")
        _clear_delete_done(correction)
        return False

    # 登録ボタンが失敗を返しても実際には保存されている場合がある。
    # 盤面を見て、新しい行 or 元の行が残っているならロールバックしない (H-3)。
    if _schedule_entry_exists(page, day_from, correction.start_time_from,
                              staff_name=correction.staff1_to or None):
        print("  ※ 追加は失敗扱いですが新しい職員の行が存在します → ロールバックしません")
        _set_reason("add_may_have_registered")
        _clear_delete_done(correction)
        return False
    if _schedule_entry_exists(page, day_from, correction.start_time_from,
                              staff_name=staff_from):
        print("  ※ 元の行が残っています (削除が効いていない) → ロールバックしません")
        _set_reason("add_failed_old_row_intact")
        _clear_delete_done(correction)
        return False

    # 請求区分が変わる修正では correction.service_type は「新しい値」。変更前の値が
    # 分からないまま復元すると、誤った請求区分の行を作ってしまう (課金事故の再生産)。
    if correction.grade_change and not (correction.service_type_from or "").strip():
        print("  ※ 中止: 請求区分が変わる修正ですが変更前のサービス内容 (service_type_from) が"
              "不明です。誤った請求区分で復元しないためロールバックしません。手動で復元してください")
        _set_reason("grade_change_rollback_no_source")
        _clear_delete_done(correction)
        return False

    print("  ※ ロールバック: 元の予定を再追加")
    if add_schedule_entry(page, _build_rollback_correction(correction), dry_run):
        print("  ※ ロールバック成功: 元の予定を復元しました")
        _set_reason("grade_change_rollback" if correction.grade_change
                    else "add_failed_rolled_back")
    else:
        print("  ※ ロールバック失敗: 元の予定が失われました。手動で復元してください")
        # 失敗すれば印が立ち、次の 1 件の前に呼び出し元が開き直す
        _recover_schedule_page(page, month_str, context=context)
        _set_reason("add_failed_row_lost")
    _clear_delete_done(correction)
    return False


def apply_correction(page, correction: Correction, dry_run: bool = False, month_str: str = None,
                     context=None) -> bool:
    """
    1件の修正を適用（スケジュール系のみ。イベントは別フロー）

    Args:
        page: Playwrightのページオブジェクト
        correction: 修正データ
        dry_run: テスト実行
    """
    action = correction.action
    biz_type = correction.business_type or "(不明)"

    if action == "delete":
        # 削除は医療保険・介護保険で操作が完全に同じ
        print(f"\n=== 削除: {correction.user_name} {correction.date_from}日 [{biz_type}] ===")
        day = int(correction.date_from) if correction.date_from.isdigit() else 1
        return delete_schedule_entry(page, day, correction.start_time_from, dry_run,
                                     staff_name=correction.staff1_from or None,
                                     end_time=correction.end_time_from)

    elif action == "add":
        print(f"\n=== 追加: {correction.user_name} {correction.date_to}日 [{biz_type}] ===")
        if correction.is_event():
            # イベントは apply_correction からは呼ばれない（Phase 2で処理）
            print("    警告: イベントは apply_correction 経由ではなく、Phase 2で処理してください")
            return False
        return add_schedule_entry(page, correction, dry_run)

    elif action == "date_change":
        print(f"\n=== 日付変更: {correction.user_name} "
              f"{correction.date_from}日 → {correction.date_to}日 [{biz_type}] ===")
        # Kaipokeは日付変更時に登録ボタンが無効化されるため、削除と再追加で処理
        print(f"  ※ 日付変更のため、削除と再追加で処理します")
        return _apply_move_with_reorder(page, correction, dry_run, month_str, context=context)

    else:  # edit
        # 変更は医療保険・介護保険共通（上部フィールドはREAD-ONLY）
        print(f"\n=== 変更: {correction.user_name} {correction.date_from}日 [{biz_type}] ===")

        # Kaipokeは時間変更時に登録ボタンが無効化されるため、
        # 時間変更がある場合は削除→再追加で処理する。
        # また、編集ダイアログはサービス内容を変更できないため、請求区分
        # (正看/准看) が変わる修正も削除→再追加に回す (職員だけの変更でも)。
        if correction.has_time_change() or correction.grade_change:
            if correction.has_time_change():
                print(f"  ※ 時間変更があるため、削除と再追加で処理します")
            if correction.grade_change:
                print("  ※ 請求区分（正看/准看）が変わるため、削除→再追加で処理します")
            return _apply_move_with_reorder(page, correction, dry_run, month_str, context=context)

        # スタッフのみ変更の場合は通常の編集フロー
        day = int(correction.date_from) if correction.date_from.isdigit() else 1
        if not click_schedule_entry(page, day, correction.start_time_from,
                                    staff_name=correction.staff1_from or None,
                                    end_time=correction.end_time_from):
            print("  予定が見つかりません")
            if getattr(_last_failure_reason, "value", None) is None:
                _set_reason("entry_not_found")
            return False

        if correction.has_staff_change():
            edit_staff(page, correction.staff1_to, correction.staff2_to)

        if dry_run:
            print("  [dry-run] 登録をスキップ")
            close_edit_dialog(page)
            return True
        else:
            success = click_register_button(page)
            if not success:
                close_edit_dialog(page)
            return success


# =============================================================================
# メインエンジン: 2フェーズ処理
# =============================================================================

def _select_user_with_recovery(page, user_name: str, month_str: str = None,
                               context=None) -> bool:
    """利用者を選ぶ。失敗したら 1 回だけページを開き直して選び直す (設計 B-2)

    前の開き直しに失敗したまま (提供月を確かめていない) なら、選ぶ前に開き直す。
    開き直しに失敗したら選ばずに False を返す (提供月を確かめないまま書き込まない)。
    """
    if not _page_unverified():
        if select_user(page, user_name):
            return True
        print(f"  ※ 利用者 '{user_name}' を選べません → ページを開き直して選び直します")
    else:
        print("  ※ 前の開き直しに失敗したままです → 利用者を選ぶ前にページを開き直します")
    if not _recover_schedule_page(page, month_str, context=context):
        print("  ※ 開き直しに失敗しました → 利用者を選びません")
        return False
    return select_user(page, user_name)


class _SelectFailureTracker:
    """利用者の選択の失敗 (開き直した後も) が続いた人数を数える (設計 B-3)

    limit 人続いたら、ページが壊れているとみなして送信を止める。
    1 人でも選べたら数え直す。
    """

    def __init__(self, limit: int = 2):
        self.limit = limit
        self.consecutive = 0

    def record(self, selected: bool) -> bool:
        """選択の結果を記録し、止めるべきなら True を返す"""
        if selected:
            self.consecutive = 0
            return False
        self.consecutive += 1
        return self.consecutive >= self.limit


def run_auto_apply(
    correction_sheet: str,
    month: str = "2026-04",
    headless: bool = True,
    dry_run: bool = False,
    limit: int = None,
    action_filter: str = None,
    business_type_filter: str = None,
    target_users: list = None,
    progress_callback: callable = None,
) -> dict:
    """
    修正シートに基づいてスケジュールを自動適用

    2フェーズ処理:
      Phase 1: スケジュール修正（利用者別タブ） - 利用者ごとにグループ化
      Phase 2: イベント追加（職員別タブ） - 職員ごとにグループ化

    Args:
        correction_sheet: 修正シートのパス（JSON）
        month: 対象月（"2026-04" 形式）
        headless: ヘッドレスモードで実行するか
        dry_run: テスト実行（実際には保存しない）
        limit: 適用する件数の上限
        action_filter: アクションでフィルタ（"edit", "add", "delete", "date_change"）
        business_type_filter: 業務種別でフィルタ（"医療保険", "介護保険", "イベント"）
        target_users: 対象利用者名リスト（指定時はこのリストの利用者のみ処理）

    Returns:
        dict: 実行結果
    """
    # 前回の停止フラグをクリア
    clear_stop()

    # 修正シートを読み込む
    all_corrections = load_correction_sheet(correction_sheet)

    # フィルタ適用
    corrections = all_corrections

    if action_filter:
        corrections = [c for c in corrections if c.action == action_filter]
        print(f"[フィルタ] action={action_filter}: {len(corrections)}/{len(all_corrections)}件")

    if business_type_filter:
        if business_type_filter == "イベント":
            corrections = [c for c in corrections if c.is_event()]
        else:
            corrections = [c for c in corrections if c.business_type == business_type_filter]
        print(f"[フィルタ] business_type={business_type_filter}: {len(corrections)}件")

    if target_users:
        normalized_targets = [normalize_name(u) for u in target_users]
        corrections = [c for c in corrections
                       if normalize_name(c.user_name) in normalized_targets]
        print(f"[フィルタ] users={target_users}: {len(corrections)}件")

    if limit:
        corrections = corrections[:limit]
        print(f"[フィルタ] limit={limit}: {len(corrections)}件")

    # Phase分離: スケジュール系 vs イベント系
    schedule_corrections = [c for c in corrections if c.is_schedule()]
    event_corrections = [c for c in corrections if c.is_event() and c.action == "add"]

    print(f"\n=== 自動適用開始 ===")
    print(f"修正シート: {correction_sheet}")
    print(f"対象月: {month}")
    print(f"元の総件数: {len(all_corrections)}")
    print(f"フィルタ後: {len(corrections)}件")
    print(f"  スケジュール修正: {len(schedule_corrections)}件 (利用者別タブ)")
    print(f"  イベント追加: {len(event_corrections)}件 (職員別タブ)")
    print(f"dry-run: {dry_run}")
    if action_filter:
        print(f"action: {action_filter}")
    if business_type_filter:
        print(f"business_type: {business_type_filter}")
    if target_users:
        print(f"target_users: {target_users}")
    print("")

    import time as _time
    _start_time = _time.time()

    # 進捗追跡用カウンタ
    _processed_count = 0
    _total_count = len(corrections)

    def _report_progress(phase: str, current_name: str):
        """進捗をコールバックに報告"""
        nonlocal _processed_count
        _processed_count += 1
        if progress_callback:
            progress_callback({
                "processed": _processed_count,
                "total": _total_count,
                "phase": phase,
                "current_name": current_name,
                "success": result["success"],
                "failed": result["failed"],
                "skipped": result["skipped"],
            })

    result = {
        "total": len(corrections),
        "schedule_total": len(schedule_corrections),
        "event_total": len(event_corrections),
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "warnings": [],
        "details": [],
    }

    # 初期進捗を報告
    if progress_callback:
        progress_callback({
            "processed": 0,
            "total": _total_count,
            "phase": "initializing",
            "current_name": "",
            "success": 0,
            "failed": 0,
            "skipped": 0,
        })

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)

        try:
            # 共通セットアップ（ログイン→ナビゲーション→月設定→月検証）
            if progress_callback:
                progress_callback({
                    "processed": 0, "total": _total_count,
                    "phase": "setup", "current_name": "ログイン・ナビゲーション中",
                    "success": 0, "failed": 0, "skipped": 0,
                })
            setup_monthly_schedule_page(page, context, month)
            # 新しいページを開いたので、前の送信で立った印を持ち越さない
            _set_page_unverified(False)
            if progress_callback:
                progress_callback({
                    "processed": 0, "total": _total_count,
                    "phase": "setup_done", "current_name": "セットアップ完了",
                    "success": 0, "failed": 0, "skipped": 0,
                })

            # ========================================
            # Phase 1: スケジュール修正（利用者別タブ）
            # ========================================
            if schedule_corrections:
                print(f"\n{'='*50}")
                print(f"Phase 1: スケジュール修正 ({len(schedule_corrections)}件)")
                print(f"{'='*50}")

                # 利用者ごとにグループ化
                users = {}
                for c in schedule_corrections:
                    if c.user_name not in users:
                        users[c.user_name] = []
                    users[c.user_name].append(c)

                # 同日correction順序の確定: 日付→アクション優先度でソート
                # delete→date_change→edit→addの順で処理することで、
                # editの「削除→再追加」がaddのエントリを誤って削除しない
                ACTION_ORDER = {"delete": 0, "date_change": 1, "edit": 2, "add": 3}
                for user_name in users:
                    users[user_name].sort(key=lambda c: (
                        int(c.date_from) if c.date_from and c.date_from.isdigit() else
                        int(c.date_to) if c.date_to and c.date_to.isdigit() else 99,
                        ACTION_ORDER.get(c.action, 9)
                    ))
                    # ソート結果をログ出力
                    for c in users[user_name]:
                        day = c.date_from or c.date_to
                        print(f"  [ソート済] {user_name} {day}日 {c.action} ({c.business_type})")

                def _skip_phase1(user, cs, reason):
                    for c in cs:
                        result["skipped"] += 1
                        result["details"].append({
                            "user": user,
                            "date": c.date_from or c.date_to,
                            "action": c.action,
                            "business_type": c.business_type,
                            "status": "skipped",
                            "reason": reason,
                        })
                        _report_progress("phase1", user)

                # 利用者の選択の失敗が続いたら止める (設計 B-3)。
                # 12/1 は 1 件の失敗の後、続く 72 件が全部「利用者が見つかりません」で飛んだ。
                select_failures = _SelectFailureTracker(limit=2)
                user_items = list(users.items())

                def _stop_page_broken(after_index):
                    print("\n※ 利用者の選択が続けて失敗しました。ページが壊れているとみなし、"
                          "送信を止めます (残りは送らずに未送信のまま残します)")
                    result["stopped"] = True
                    result["stop_reason"] = "page_broken"
                    for rest_user, rest_cs in user_items[after_index + 1:]:
                        _skip_phase1(rest_user, rest_cs, "not_attempted_page_broken")

                for user_index, (user_name, user_corrections) in enumerate(user_items):
                    if result.get("stop_reason") == "page_broken":
                        break  # 前の利用者の途中で止めた (残りは _stop_page_broken で記録済み)
                    if is_stop_requested():
                        print(f"\n非常停止が要求されました！Phase 1を中断します。")
                        result["stopped"] = True
                        break

                    print(f"\n--- 利用者: {user_name} ({len(user_corrections)}件) ---")

                    selected = _select_user_with_recovery(page, user_name, month, context)
                    if not selected:
                        print(f"  利用者の選択に失敗: {user_name}")
                        _skip_phase1(user_name, user_corrections, "user_not_found")
                        if select_failures.record(False):
                            _stop_page_broken(user_index)
                            break
                        continue

                    for correction_index, correction in enumerate(user_corrections):
                        if is_stop_requested():
                            result["stopped"] = True
                            break

                        # 前件の失敗理由を持ち越さないようクリア
                        _pop_reason()

                        # リカバリ等でページが別利用者に飛んでいないか確認 (C-2)。
                        # ここを外すと別人の予定を消し書きする恐れがある。
                        # 前の 1 件で開き直しに失敗していたら、画面の利用者が合って見えても
                        # 提供月を確かめていないので、必ず選び直す (開き直しから)。
                        if _page_unverified() or not _check_current_user(page, user_name):
                            print(f"  ※ 画面の利用者が '{user_name}' と確かめられません → 選択し直します")
                            reselected = _select_user_with_recovery(page, user_name, month, context)
                            if not reselected:
                                # 開き直しても選べない利用者は、残りの修正もまとめて飛ばす
                                print(f"  利用者の再選択に失敗: {user_name}")
                                _skip_phase1(user_name, user_corrections[correction_index:],
                                             "user_not_found")
                                if select_failures.record(False):
                                    _stop_page_broken(user_index)
                                break
                        # 画面の利用者を確かめられたので、続けての失敗を数え直す
                        select_failures.record(True)

                        max_retries = 1
                        retry_user_lost = False
                        for attempt in range(max_retries + 1):
                            # 試行ごとに前試行の理由をクリアする (L-2)
                            _pop_reason()
                            try:
                                # リトライ時はセッション復旧を試みる
                                if attempt > 0:
                                    print(f"  リトライ ({attempt}/{max_retries})")
                                    if not ensure_session(page, context, month):
                                        raise RuntimeError("セッション復旧に失敗")
                                    # やり直しでも、利用者を確かめてから書き込む
                                    if not (_select_user_with_recovery(page, user_name, month, context)
                                            and _check_current_user(page, user_name)):
                                        print(f"  ※ やり直しで利用者 '{user_name}' を確かめられません"
                                              " → この利用者の残りを飛ばします")
                                        _skip_phase1(user_name, user_corrections[correction_index:],
                                                     "user_not_confirmed_on_retry")
                                        retry_user_lost = True
                                        break

                                success = apply_correction(page, correction, dry_run, month_str=month,
                                                           context=context)
                                if success:
                                    result["success"] += 1
                                    result["details"].append({
                                        "user": user_name,
                                        "date": correction.date_from or correction.date_to,
                                        "action": correction.action,
                                        "business_type": correction.business_type,
                                        "status": "success",
                                    })
                                    # 未割当の警告を追加
                                    if correction.staff1_to == "未割当":
                                        result["warnings"].append({
                                            "type": "unassigned_staff",
                                            "user": user_name,
                                            "date": correction.date_from or correction.date_to,
                                            "action": correction.action,
                                            "message": "職員1を未割当（'-'）で登録しました",
                                        })
                                else:
                                    result["failed"] += 1
                                    result["details"].append({
                                        "user": user_name,
                                        "date": correction.date_from or correction.date_to,
                                        "action": correction.action,
                                        "business_type": correction.business_type,
                                        "status": "failed",
                                        "reason": _pop_reason() or "unknown",
                                    })
                                break  # 成功 or 通常失敗 → リトライしない
                            except Exception as e:
                                # 安全ネット: 例外発生時もモーダルを確実に閉じる
                                close_edit_dialog(page)
                                if attempt < max_retries:
                                    print(f"  エラー（リトライします）: {e}")
                                    continue
                                print(f"  エラー: {e}")
                                result["failed"] += 1
                                result["details"].append({
                                    "user": user_name,
                                    "date": correction.date_from or correction.date_to,
                                    "action": correction.action,
                                    "business_type": correction.business_type,
                                    "status": "error",
                                    "reason": str(e),
                                })

                        if retry_user_lost:
                            # _skip_phase1 で進み具合も報告済み
                            if select_failures.record(False):
                                _stop_page_broken(user_index)
                            break

                        _report_progress("phase1", user_name)
                        page.wait_for_timeout(1000)

            # ========================================
            # Phase 2: イベント追加（職員別タブ）
            # ========================================
            if event_corrections and result.get("stop_reason") == "page_broken":
                # ページが壊れて止めたときは Phase 2 も行わない。件数が合うよう飛ばしとして残す
                for c in event_corrections:
                    result["skipped"] += 1
                    result["details"].append({
                        "staff": c.staff1_to,
                        "date": c.date_to,
                        "action": "event_add",
                        "business_type": c.business_type,
                        "status": "skipped",
                        "reason": "not_attempted_page_broken",
                    })
                    _report_progress("phase2", c.staff1_to)
            elif event_corrections and not result.get("stopped"):
                print(f"\n{'='*50}")
                print(f"Phase 2: イベント追加 ({len(event_corrections)}件)")
                print(f"{'='*50}")

                # 職員別タブに遷移
                if progress_callback:
                    progress_callback({
                        "processed": _processed_count, "total": _total_count,
                        "phase": "phase2_nav", "current_name": "職員別タブへ遷移中",
                        "success": result["success"], "failed": result["failed"],
                        "skipped": result["skipped"],
                    })
                if not navigate_to_staff_tab(page):
                    print("  職員別タブへの遷移に失敗。イベント追加をスキップします。")
                    for c in event_corrections:
                        result["skipped"] += 1
                        result["details"].append({
                            "staff": c.staff1_to,
                            "date": c.date_to,
                            "action": "event_add",
                            "business_type": c.business_type,
                            "status": "skipped",
                            "reason": "staff_tab_navigation_failed",
                        })
                        _report_progress("phase2", c.staff1_to)
                else:
                    # 職員ごとにグループ化
                    staff_groups = {}
                    for c in event_corrections:
                        staff_key = c.staff1_to
                        if staff_key not in staff_groups:
                            staff_groups[staff_key] = []
                        staff_groups[staff_key].append(c)

                    for staff_name, staff_corrections in staff_groups.items():
                        if is_stop_requested():
                            print(f"\n非常停止が要求されました！Phase 2を中断します。")
                            result["stopped"] = True
                            break

                        print(f"\n--- 職員: {staff_name} ({len(staff_corrections)}件) ---")

                        if not select_staff_member(page, staff_name):
                            print(f"  職員の選択に失敗: {staff_name}")
                            for c in staff_corrections:
                                result["skipped"] += 1
                                result["details"].append({
                                    "staff": staff_name,
                                    "date": c.date_to,
                                    "action": "event_add",
                                    "business_type": c.business_type,
                                    "status": "skipped",
                                    "reason": "staff_not_found",
                                })
                                _report_progress("phase2", staff_name)
                            continue

                        # 職員切替後のページ安定待機
                        page.wait_for_timeout(2000)
                        _remove_floating_overlays(page)

                        for correction in staff_corrections:
                            if is_stop_requested():
                                result["stopped"] = True
                                break

                            try:
                                success = add_event_entry(page, correction, dry_run)
                                if success:
                                    result["success"] += 1
                                    result["details"].append({
                                        "staff": staff_name,
                                        "date": correction.date_to,
                                        "action": "event_add",
                                        "business_type": correction.business_type,
                                        "event_name": correction.remarks,
                                        "status": "success",
                                    })
                                else:
                                    result["failed"] += 1
                                    result["details"].append({
                                        "staff": staff_name,
                                        "date": correction.date_to,
                                        "action": "event_add",
                                        "business_type": correction.business_type,
                                        "status": "failed",
                                    })
                            except Exception as e:
                                print(f"  エラー: {e}")
                                result["failed"] += 1
                                result["details"].append({
                                    "staff": staff_name,
                                    "date": correction.date_to,
                                    "action": "event_add",
                                    "business_type": correction.business_type,
                                    "status": "error",
                                    "reason": str(e),
                                })

                            _report_progress("phase2", staff_name)
                            page.wait_for_timeout(1000)

            # 実行時間・タイムスタンプを記録
            result["execution_time_sec"] = round(_time.time() - _start_time, 1)
            result["completed_at"] = datetime.datetime.now().isoformat()

            print(f"\n=== 自動適用完了 ===")
            print(f"成功: {result['success']} / 失敗: {result['failed']} / "
                  f"スキップ: {result['skipped']} / 合計: {result['total']}")
            if result["warnings"]:
                print(f"警告: {len(result['warnings'])}件")

        except Exception as e:
            print(f"エラーが発生しました: {e}")
            save_artifacts(page, Path("artifacts"), "auto_apply_error")
            raise

        finally:
            context.close()
            browser.close()

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="自動適用エンジン")
    parser.add_argument("--sheet", required=True, help="修正シートのパス（JSON）")
    parser.add_argument("--month", default="2026-04", help="対象月 (デフォルト: 2026-04)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    parser.add_argument("--dry-run", action="store_true", help="テスト実行（実際には保存しない）")
    parser.add_argument("--limit", type=int, help="適用する件数の上限")
    parser.add_argument("--action", choices=["edit", "add", "delete", "date_change"],
                        help="アクションでフィルタ")
    parser.add_argument("--business-type",
                        help="業務種別でフィルタ (医療保険, 介護保険, イベント)")
    parser.add_argument("--user", action="append", dest="users",
                        help="対象利用者名（複数指定可: --user 山田太郎 --user 佐藤花子）")
    args = parser.parse_args()

    run_auto_apply(
        correction_sheet=args.sheet,
        month=args.month,
        headless=not args.headed,
        dry_run=args.dry_run,
        limit=args.limit,
        action_filter=args.action,
        business_type_filter=args.business_type,
        target_users=args.users,
    )
