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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from lib.common import (
    create_browser_context,
    login,
    dismiss_popup,
    goto_receipt,
    goto_yoriyori,
    goto_monthly_schedule,
    set_service_month,
    save_artifacts,
    parse_month,
)
from lib.diff_engine import load_correction_sheet, Correction, parse_time
from lib.stop_signal import is_stop_requested, clear_stop


# =============================================================================
# ヘルパー関数
# =============================================================================

def normalize_name(name: str) -> str:
    """名前のスペースを正規化（全角スペース↔半角スペースどちらでもマッチ可能に）"""
    return name.replace("\u3000", " ").strip()


def name_matches(needle: str, haystack: str) -> bool:
    """名前が含まれているか判定（全角/半角スペースを無視して比較）"""
    return normalize_name(needle) in normalize_name(haystack)

def calculate_santei_time(start_time: str, end_time: str) -> str:
    """
    開始時間と終了時間から算定時間の選択値を計算（介護保険用）

    カイポケの算定時間セレクトの選択肢:
    - "20分未満"
    - "30分未満"
    - "30分以上～1時間未満"
    - "1時間以上～1時間30分未満"
    - "1時間30分以上"
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
        return "1時間以上～1時間30分未満"
    else:
        return "1時間30分以上"


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


def click_schedule_entry(page, day: int, start_time: str) -> bool:
    """
    指定した日付・開始時間のスケジュールエントリをクリック

    Strategy 1: 行の日にち列(td.tac.nowrap)の完全一致で行を特定し、
                行内のaタグ(onclick付き)をクリック
    Strategy 2: onclick属性をパースして5番目のパラメータ（日にち）で特定
    同一日複数件: div.service-detail-area 内の時間テキストで絞り込み
    """
    print(f"  予定をクリック: {day}日 {start_time}")
    day_str = str(day)

    try:
        candidate_links = []

        # --- Strategy 1: 行の日にち列(td.tac.nowrap)で特定 ---
        rows = page.locator("table tr").all()
        for row in rows:
            try:
                day_cells = row.locator("td.tac.nowrap").all()
                day_match = False
                for cell in day_cells:
                    cell_text = cell.text_content().strip()
                    if cell_text == day_str:
                        day_match = True
                        break

                if not day_match:
                    continue

                links = row.locator("a[onclick]").all()
                for link in links:
                    if link.is_visible():
                        time_text = ""
                        try:
                            detail_area = link.locator(
                                "xpath=ancestor::div[contains(@class,'service-detail-area')]"
                            ).first
                            if detail_area.count() > 0:
                                time_text = detail_area.inner_text()
                        except Exception:
                            pass
                        candidate_links.append({"link": link, "time_text": time_text})
            except Exception:
                continue

        # --- Strategy 2: onclick属性パース（fallback） ---
        if not candidate_links:
            print(f"    Strategy 1失敗 → onclick属性パースにフォールバック")
            all_links = page.locator("a[onclick*='Edit']").all()
            for link in all_links:
                try:
                    onclick = link.get_attribute("onclick") or ""
                    params = re.findall(r"'([^']*)'", onclick)
                    if len(params) >= 5 and params[4] == day_str:
                        time_text = ""
                        try:
                            detail_area = link.locator(
                                "xpath=ancestor::div[contains(@class,'service-detail-area')]"
                            ).first
                            if detail_area.count() > 0:
                                time_text = detail_area.inner_text()
                        except Exception:
                            pass
                        candidate_links.append({"link": link, "time_text": time_text})
                except Exception:
                    continue

        if not candidate_links:
            print(f"    {day}日のエントリが見つかりません")
            _save_debug_on_failure(page, day, start_time)
            return False

        # 1件のみ → そのままクリック
        if len(candidate_links) == 1:
            print(f"    {day}日に1件のエントリを発見")
            candidate_links[0]["link"].click()
            page.wait_for_timeout(2000)
            return True

        # 複数件 → 時間で絞り込み
        print(f"    {day}日に{len(candidate_links)}件のエントリを発見 → 時間 '{start_time}' で絞り込み")
        for c in candidate_links:
            if start_time in c["time_text"]:
                print(f"    時間一致: {c['time_text'].strip()}")
                c["link"].click()
                page.wait_for_timeout(2000)
                return True

        # 時間で絞り込めなかった場合は最初の1件をクリック
        print(f"    警告: 時間での絞り込み失敗。最初のエントリをクリック")
        candidate_links[0]["link"].click()
        page.wait_for_timeout(2000)
        return True

    except Exception as e:
        print(f"    エラー: {e}")
        _save_debug_on_failure(page, day, start_time)
        return False


# =============================================================================
# 時間・日付・職員の編集
# =============================================================================

def edit_schedule_time(page, start_hour: int, start_min: int, end_hour: int, end_min: int) -> bool:
    """
    スケジュールの時間を編集（6つのセレクト）

    セレクタ:
    - #inPopupStartHour, #inPopupStartMinute1, #inPopupStartMinute2
    - #inPopupEndHour, #inPopupEndMinute1, #inPopupEndMinute2
    """
    print(f"  時間変更: {start_hour:02d}:{start_min:02d} - {end_hour:02d}:{end_min:02d}")

    try:
        start_min_tens = start_min // 10
        start_min_ones = start_min % 10
        end_min_tens = end_min // 10
        end_min_ones = end_min % 10

        selectors_and_values = [
            ("#inPopupStartHour", str(start_hour)),
            ("#inPopupStartMinute1", str(start_min_tens)),
            ("#inPopupStartMinute2", str(start_min_ones)),
            ("#inPopupEndHour", str(end_hour)),
            ("#inPopupEndMinute1", str(end_min_tens)),
            ("#inPopupEndMinute2", str(end_min_ones)),
        ]

        for selector, value in selectors_and_values:
            elem = page.locator(selector)
            if elem.is_visible():
                elem.select_option(value=value)
                page.wait_for_timeout(200)

        return True

    except Exception as e:
        print(f"    時間変更エラー: {e}")
        return False


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
        # Strategy 1: jQuery UI Datepicker（実際のカイポケの構造）
        datepicker = page.locator("#simple-select-days-range, .hasMultiDatepicker, .ui-datepicker").first
        if datepicker.is_visible(timeout=2000):
            # デートピッカー内の全tdからday_strに完全一致するセルを探す
            day_cells = datepicker.locator("td[data-handler] a, td a").all()
            for cell in day_cells:
                try:
                    cell_text = cell.text_content().strip()
                    if cell_text == day_str:
                        cell.click()
                        page.wait_for_timeout(500)
                        print(f"    日付を {new_day} に変更（デートピッカー）")
                        return True
                except Exception:
                    continue

            # aタグ内にない場合、td自体のテキストで探す
            all_cells = datepicker.locator("td").all()
            for cell in all_cells:
                try:
                    cell_text = cell.text_content().strip()
                    # "1" だけの場合と "1(水)" の場合に対応
                    if cell_text == day_str or cell_text.startswith(day_str + "("):
                        inner_a = cell.locator("a")
                        if inner_a.count() > 0:
                            inner_a.first.click()
                        else:
                            cell.click()
                        page.wait_for_timeout(500)
                        print(f"    日付を {new_day} に変更（デートピッカーセル）")
                        return True
                except Exception:
                    continue

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
        if for_new_entry:
            staff_info_btn = page.locator("input[value='職員情報入力']")
            if staff_info_btn.is_visible(timeout=3000):
                staff_info_btn.click()
                page.wait_for_timeout(1000)
                print("    「職員情報入力」ボタンをクリック")

        # 職員1を設定
        if staff1_name:
            staff1_select = page.locator("select#chargeStaff1Id1")
            if staff1_select.is_visible(timeout=3000):
                options = staff1_select.locator("option").all()
                selected = False
                for opt in options:
                    opt_text = opt.text_content()
                    if name_matches(staff1_name, opt_text):
                        staff1_select.select_option(label=opt_text)
                        page.wait_for_timeout(300)
                        print(f"    職員1設定: {staff1_name}")
                        selected = True
                        break
                if not selected:
                    print(f"    警告: 職員1 '{staff1_name}' が選択肢に見つかりません")
                    # デバッグ: 選択肢を出力
                    opt_names = [o.text_content().strip() for o in options[:10]]
                    print(f"    選択肢: {opt_names}")
            else:
                print("    警告: select#chargeStaff1Id1 が見つかりません")

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
                        break
                if not selected:
                    print(f"    警告: 職員2 '{staff2_name}' が選択肢に見つかりません")
            else:
                # 職員2をクリア（最初のオプション = 空白）
                staff2_select.select_option(index=0)
                page.wait_for_timeout(300)
                print("    職員2クリア")

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
    """
    try:
        # Primary: PDF仕様のセレクタ
        register_button = page.locator("input#btnRegisPop")
        if register_button.is_visible(timeout=3000):
            register_button.click()
            page.wait_for_timeout(2000)
        else:
            # Fallback
            register_button = page.locator(
                "button:has-text('登録する'), a:has-text('登録する'), input[value='登録する']"
            ).first
            if register_button.is_visible():
                register_button.click()
                page.wait_for_timeout(2000)
            else:
                print("    登録ボタンが見つかりません")
                return False

        # 確認ダイアログ
        try:
            ok_btn = page.locator("button:has-text('OK'), button:has-text('はい')").first
            if ok_btn.is_visible(timeout=2000):
                ok_btn.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

        print("    登録完了")
        return True

    except Exception as e:
        print(f"    登録エラー: {e}")
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
                    btn.click()
                    page.wait_for_timeout(1000)
                    page.remove_listener("dialog", _accept_dialog)
                    return True
            except Exception:
                continue

        page.remove_listener("dialog", _accept_dialog)
        return False
    except Exception:
        try:
            page.remove_listener("dialog", _accept_dialog)
        except Exception:
            pass
        return False


def delete_schedule_entry(page, day: int, start_time: str, dry_run: bool = False) -> bool:
    """
    スケジュールエントリを削除（医療保険・介護保険共通）

    PDF仕様: input#inPopupBtnDel
    """
    print(f"  削除: {day}日 {start_time}")

    if not click_schedule_entry(page, day, start_time):
        print("    予定が見つかりません")
        return False

    if dry_run:
        print("  [dry-run] 削除をスキップ")
        close_edit_dialog(page)
        return True

    try:
        # Primary: PDF仕様の削除ボタン
        delete_btn = page.locator("input#inPopupBtnDel")
        if delete_btn.is_visible(timeout=3000):
            delete_btn.click()
            page.wait_for_timeout(1000)
        else:
            # Fallback
            delete_btn = page.locator(
                "button:has-text('削除'), a:has-text('削除'), input[value*='削除']"
            ).first
            if delete_btn.is_visible():
                delete_btn.click()
                page.wait_for_timeout(1000)
            else:
                print("    削除ボタンが見つかりません")
                close_edit_dialog(page)
                return False

        # 確認ダイアログ
        try:
            ok_btn = page.locator(
                "button:has-text('OK'), button:has-text('はい'), button:has-text('削除する')"
            ).first
            if ok_btn.is_visible(timeout=2000):
                ok_btn.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

        print("    削除完了")
        return True

    except Exception as e:
        print(f"    削除エラー: {e}")
        return False


# =============================================================================
# 新規追加: 保険種別フォーム入力
# =============================================================================

def click_new_add_button(page) -> bool:
    """
    「新規追加」ボタンをクリック

    カイポケ実際の構造:
      <button type="button" class="btn btn-sms btn-sm"
              onclick="showHNC097807Add(...)" title="新規追加">新規追加</button>
    """
    add_buttons = [
        "button[title='新規追加']",
        "#btn_area button:has-text('新規追加')",
        "button:has-text('新規追加')",
        "text=新規追加",
    ]
    for selector in add_buttons:
        btn = page.locator(selector).first
        try:
            if btn.is_visible(timeout=2000):
                btn.click()
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    print("    新規追加ボタンが見つかりません")
    return False


def fill_medical_insurance_fields(page) -> bool:
    """
    医療保険の新規追加: 上部セクションのフィールドを設定

    PDF「02_新規追加・医療保険」仕様:
    - 保険区分: radio#inPopupInsuranceDivision02 (医療保険)
    - サービス区分: select#inPopupEstimate1 → "精神科訪問看護"
    - 基本療養費: select#inPopupEstimate2 → "Ⅰ"
    - 職員資格: select#inPopupEstimate3 → "看護師等"
    """
    try:
        # 保険区分: 医療保険を選択
        medical_radio = page.locator("input#inPopupInsuranceDivision02")
        if medical_radio.is_visible(timeout=3000):
            medical_radio.click()
            page.wait_for_timeout(1000)
            print("    保険区分: 医療保険 を選択")
        else:
            print("    警告: radio#inPopupInsuranceDivision02 が見つかりません")
            return False

        # サービス区分: 精神科訪問看護
        estimate1 = page.locator("select#inPopupEstimate1")
        if estimate1.is_visible(timeout=3000):
            options = estimate1.locator("option").all()
            for opt in options:
                if "精神科訪問看護" in opt.text_content():
                    estimate1.select_option(label=opt.text_content())
                    page.wait_for_timeout(500)
                    print(f"    サービス区分: {opt.text_content()}")
                    break

        # 基本療養費: Ⅰ
        estimate2 = page.locator("select#inPopupEstimate2")
        if estimate2.is_visible(timeout=3000):
            options = estimate2.locator("option").all()
            for opt in options:
                text = opt.text_content()
                if "Ⅰ" in text and "Ⅱ" not in text and "Ⅲ" not in text and "Ⅳ" not in text:
                    estimate2.select_option(label=text)
                    page.wait_for_timeout(500)
                    print(f"    基本療養費: {text}")
                    break

        # 職員資格: 看護師等
        estimate3 = page.locator("select#inPopupEstimate3")
        if estimate3.is_visible(timeout=3000):
            options = estimate3.locator("option").all()
            for opt in options:
                if "看護師等" in opt.text_content():
                    estimate3.select_option(label=opt.text_content())
                    page.wait_for_timeout(500)
                    print(f"    職員資格: {opt.text_content()}")
                    break

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
            nursing_radio.click()
            page.wait_for_timeout(1000)
            print("    保険区分: 介護保険 を選択")

        # サービス種類: 訪問看護
        service_kind = page.locator("select#inPopupServiceKindId")
        if service_kind.is_visible(timeout=3000):
            options = service_kind.locator("option").all()
            for opt in options:
                text = opt.text_content()
                # "訪問看護" にマッチ（"介護予防訪問看護" も可能だが通常は "訪問看護"）
                if "訪問看護" in text and "予防" not in text:
                    service_kind.select_option(label=text)
                    page.wait_for_timeout(500)
                    print(f"    サービス種類: {text}")
                    break

        # サービス区分: 通常の算定
        estimate1 = page.locator("select#inPopupEstimate1")
        if estimate1.is_visible(timeout=3000):
            options = estimate1.locator("option").all()
            for opt in options:
                if "通常の算定" in opt.text_content():
                    estimate1.select_option(label=opt.text_content())
                    page.wait_for_timeout(500)
                    print(f"    サービス区分: {opt.text_content()}")
                    break

        # 算定時間: 開始・終了時間から計算
        santei_time = calculate_santei_time(start_time, end_time)
        estimate4 = page.locator("select#inPopupEstimate4")
        if estimate4.is_visible(timeout=3000):
            options = estimate4.locator("option").all()
            for opt in options:
                if santei_time in opt.text_content():
                    estimate4.select_option(label=opt.text_content())
                    page.wait_for_timeout(500)
                    print(f"    算定時間: {opt.text_content()}")
                    break

        return True

    except Exception as e:
        print(f"    介護保険フィールド設定エラー: {e}")
        return False


def add_schedule_entry(page, correction: Correction, dry_run: bool = False) -> bool:
    """
    新しいスケジュールエントリを追加（保険種別による分岐あり）

    Args:
        page: Playwrightのページオブジェクト
        correction: 修正データ（business_type で分岐）
        dry_run: テスト実行
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
            if not fill_medical_insurance_fields(page):
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
            if not fill_medical_insurance_fields(page):
                close_edit_dialog(page)
                return False

        # Step 3: 日付を設定
        change_date(page, day)

        # Step 4: 時間を設定
        start_parts = correction.start_time_to.split(":")
        end_parts = correction.end_time_to.split(":")
        if len(start_parts) >= 2 and len(end_parts) >= 2:
            edit_schedule_time(
                page,
                int(start_parts[0]), int(start_parts[1]),
                int(end_parts[0]), int(end_parts[1])
            )

        # Step 5: 職員を設定（新規追加なので for_new_entry=True）
        edit_staff(
            page,
            correction.staff1_to,
            correction.staff2_to,
            for_new_entry=True
        )

        # Step 6: 登録
        if dry_run:
            print("  [dry-run] 登録をスキップ")
            close_edit_dialog(page)
            return True
        else:
            return click_register_button(page)

    except Exception as e:
        print(f"    追加エラー: {e}")
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
                    staff_tab.click()
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
                    user_tab.click()
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
                        inner_link.first.click()
                    else:
                        cell.click()
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
                    link.click()
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
        new_event_btn = page.locator("button#individualMonthlyShiftAssignPopup")
        if not new_event_btn.is_visible(timeout=3000):
            # Fallback
            new_event_btn = page.locator("text=個別業務の新規登録").first
            if not new_event_btn.is_visible(timeout=2000):
                print("    「個別業務の新規登録」ボタンが見つかりません")
                return False
        new_event_btn.click()
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
                    radio.click()
                    page.wait_for_timeout(1000)
                    print("    「新しく登録する」を選択（ラベルfor属性経由）")
                    selected_radio = True
            if not selected_radio:
                new_register_label.click()
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
                        radio.click()
                        page.wait_for_timeout(1000)
                        print(f"    「新しく登録する」を選択（ラジオ[{i}]）")
                        selected_radio = True
                        break
                except Exception:
                    continue

            # フォールバック: 最後のラジオ（通常は「新しく登録する」）
            if not selected_radio and radios:
                radios[-1].click()
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

        # Step 4: 時間を設定
        start_parts = correction.start_time_to.split(":")
        end_parts = correction.end_time_to.split(":")
        if len(start_parts) >= 2 and len(end_parts) >= 2:
            edit_schedule_time(
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
            add_btn.click()
            page.wait_for_timeout(2000)

            # 確認ダイアログ
            try:
                ok_btn = page.locator("button:has-text('OK'), button:has-text('はい')").first
                if ok_btn.is_visible(timeout=2000):
                    ok_btn.click()
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

def apply_correction(page, correction: Correction, dry_run: bool = False) -> bool:
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
        return delete_schedule_entry(page, day, correction.start_time_from, dry_run)

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
        day = int(correction.date_from) if correction.date_from.isdigit() else 1
        if not click_schedule_entry(page, day, correction.start_time_from):
            print("  予定が見つかりません")
            return False

        new_day = int(correction.date_to) if correction.date_to.isdigit() else 1
        change_date(page, new_day)

        if correction.has_time_change():
            start_parts = correction.start_time_to.split(":")
            end_parts = correction.end_time_to.split(":")
            if len(start_parts) >= 2 and len(end_parts) >= 2:
                edit_schedule_time(
                    page,
                    int(start_parts[0]), int(start_parts[1]),
                    int(end_parts[0]), int(end_parts[1])
                )

        if correction.has_staff_change():
            edit_staff(page, correction.staff1_to, correction.staff2_to)

        if dry_run:
            print("  [dry-run] 登録をスキップ")
            close_edit_dialog(page)
            return True
        else:
            return click_register_button(page)

    else:  # edit
        # 変更は医療保険・介護保険共通（上部フィールドはREAD-ONLY）
        print(f"\n=== 変更: {correction.user_name} {correction.date_from}日 [{biz_type}] ===")
        day = int(correction.date_from) if correction.date_from.isdigit() else 1
        if not click_schedule_entry(page, day, correction.start_time_from):
            print("  予定が見つかりません")
            return False

        if correction.has_time_change():
            start_parts = correction.start_time_to.split(":")
            end_parts = correction.end_time_to.split(":")
            if len(start_parts) >= 2 and len(end_parts) >= 2:
                edit_schedule_time(
                    page,
                    int(start_parts[0]), int(start_parts[1]),
                    int(end_parts[0]), int(end_parts[1])
                )

        if correction.has_staff_change():
            edit_staff(page, correction.staff1_to, correction.staff2_to)

        if dry_run:
            print("  [dry-run] 登録をスキップ")
            close_edit_dialog(page)
            return True
        else:
            return click_register_button(page)


# =============================================================================
# メインエンジン: 2フェーズ処理
# =============================================================================

def run_auto_apply(
    correction_sheet: str,
    month: str = "2026-04",
    headless: bool = True,
    dry_run: bool = False,
    limit: int = None,
    action_filter: str = None,
    business_type_filter: str = None,
    target_users: list = None,
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

    result = {
        "total": len(corrections),
        "schedule_total": len(schedule_corrections),
        "event_total": len(event_corrections),
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "details": [],
    }

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)

        try:
            # ログイン & ナビゲーション
            login(page, save_state=True, context=context)
            page.wait_for_timeout(1000)
            dismiss_popup(page)

            goto_receipt(page)
            goto_yoriyori(page)
            goto_monthly_schedule(page)
            set_service_month(page, month)
            page.wait_for_timeout(1000)

            # 月の検証
            from lib.common import to_reiwa
            target_year, target_month_num = parse_month(month)
            target_reiwa = to_reiwa(target_year)
            expected_month_text = f"令和{target_reiwa}年{target_month_num}月"

            actual_month = ""
            try:
                selects = page.locator("select")
                for i in range(selects.count()):
                    select = selects.nth(i)
                    if select.is_visible(timeout=1000):
                        text = select.evaluate(
                            "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''"
                        )
                        if text and "令和" in text and "月" in text:
                            actual_month = text.strip()
                            break
            except Exception:
                pass

            if actual_month and expected_month_text not in actual_month:
                error_msg = f"月の設定が不正です！期待: {expected_month_text}, 実際: {actual_month}"
                print(f"エラー: {error_msg}")
                save_artifacts(page, Path("artifacts"), "auto_apply_wrong_month")
                raise RuntimeError(error_msg)

            print(f"月の検証OK: {actual_month or expected_month_text}")

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

                for user_name, user_corrections in users.items():
                    if is_stop_requested():
                        print(f"\n非常停止が要求されました！Phase 1を中断します。")
                        result["stopped"] = True
                        break

                    print(f"\n--- 利用者: {user_name} ({len(user_corrections)}件) ---")

                    if not select_user(page, user_name):
                        print(f"  利用者の選択に失敗: {user_name}")
                        for c in user_corrections:
                            result["skipped"] += 1
                            result["details"].append({
                                "user": user_name,
                                "date": c.date_from or c.date_to,
                                "action": c.action,
                                "business_type": c.business_type,
                                "status": "skipped",
                                "reason": "user_not_found",
                            })
                        continue

                    for correction in user_corrections:
                        if is_stop_requested():
                            result["stopped"] = True
                            break

                        try:
                            success = apply_correction(page, correction, dry_run)
                            if success:
                                result["success"] += 1
                                result["details"].append({
                                    "user": user_name,
                                    "date": correction.date_from or correction.date_to,
                                    "action": correction.action,
                                    "business_type": correction.business_type,
                                    "status": "success",
                                })
                            else:
                                result["failed"] += 1
                                result["details"].append({
                                    "user": user_name,
                                    "date": correction.date_from or correction.date_to,
                                    "action": correction.action,
                                    "business_type": correction.business_type,
                                    "status": "failed",
                                })
                        except Exception as e:
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

                        page.wait_for_timeout(1000)

            # ========================================
            # Phase 2: イベント追加（職員別タブ）
            # ========================================
            if event_corrections and not result.get("stopped"):
                print(f"\n{'='*50}")
                print(f"Phase 2: イベント追加 ({len(event_corrections)}件)")
                print(f"{'='*50}")

                # 職員別タブに遷移
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
                            continue

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

                            page.wait_for_timeout(1000)

            print(f"\n=== 自動適用完了 ===")
            print(f"成功: {result['success']} / 失敗: {result['failed']} / "
                  f"スキップ: {result['skipped']} / 合計: {result['total']}")

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
