"""
包括的ドライランテスト

各操作を個別にテストし、詳細な診断情報を出力する。
実際の登録・削除は一切行わない。

テスト項目:
  1. ログイン・ナビゲーション → 月間スケジュール画面到達
  2. 利用者選択 → ドロップダウンの動作確認
  3. スケジュールエントリのクリック → 編集ダイアログの確認
  4. 編集ダイアログ内の各フィールド確認
     - 時間セレクト (6個)
     - 職員セレクト (chargeStaff1Id1, chargeStaff2Id1)
     - 保険区分ラジオ
     - 日付デートピッカー
     - 登録/削除ボタン
  5. 新規追加ダイアログの確認
     - 医療保険フィールド (Estimate1/2/3)
     - 介護保険フィールド (ServiceKindId, Estimate1/4)
  6. 職員別タブ → 職員選択 → イベント追加ダイアログの確認
  7. 修正シートの適用シミュレーション（登録スキップ）

使い方:
    python commands/dry_run_test.py --test all
    python commands/dry_run_test.py --test navigate
    python commands/dry_run_test.py --test edit
    python commands/dry_run_test.py --test add-medical
    python commands/dry_run_test.py --test add-nursing
    python commands/dry_run_test.py --test delete
    python commands/dry_run_test.py --test event
    python commands/dry_run_test.py --test date-change
    python commands/dry_run_test.py --sheet data/correction_sheet.json --test apply
"""

import sys
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
    to_reiwa,
)
from commands.auto_apply import (
    normalize_name,
    name_matches,
    select_user,
    click_schedule_entry,
    edit_schedule_time,
    change_date,
    edit_staff,
    click_new_add_button,
    fill_medical_insurance_fields,
    fill_nursing_insurance_fields,
    close_edit_dialog,
    navigate_to_staff_tab,
    navigate_to_user_tab,
    select_staff_member,
    add_event_entry,
    click_register_button,
    delete_schedule_entry,
    calculate_santei_time,
    _accept_dialog,
)


# =============================================================================
# 診断ユーティリティ
# =============================================================================

def diagnose_element(page, selector: str, label: str) -> dict:
    """要素の存在・可視性・属性を診断"""
    result = {"label": label, "selector": selector, "exists": False, "visible": False}
    try:
        elem = page.locator(selector)
        count = elem.count()
        result["count"] = count
        result["exists"] = count > 0
        if count > 0:
            result["visible"] = elem.first.is_visible(timeout=1000)
            if result["visible"]:
                try:
                    result["tag"] = elem.first.evaluate("el => el.tagName")
                except Exception:
                    pass
                try:
                    result["text"] = elem.first.text_content()[:80] if elem.first.text_content() else ""
                except Exception:
                    pass
    except Exception as e:
        result["error"] = str(e)
    return result


def diagnose_select(page, selector: str, label: str) -> dict:
    """セレクト要素の選択肢を診断"""
    result = diagnose_element(page, selector, label)
    if result.get("visible"):
        try:
            options = page.locator(selector).first.evaluate("""el => {
                return Array.from(el.options).map((opt, i) => ({
                    index: i,
                    value: opt.value,
                    text: opt.text.substring(0, 40),
                    selected: opt.selected
                }));
            }""")
            result["options_count"] = len(options)
            result["selected"] = next((o for o in options if o["selected"]), None)
            result["options_preview"] = options[:5]
        except Exception as e:
            result["options_error"] = str(e)
    return result


def print_diag(diag: dict):
    """診断結果を表示"""
    status = "OK" if diag.get("visible") else ("EXISTS" if diag.get("exists") else "NOT FOUND")
    print(f"  [{status}] {diag['label']}: {diag['selector']}")
    if diag.get("count", 0) > 1:
        print(f"         count={diag['count']}")
    if diag.get("options_count"):
        selected = diag.get("selected", {})
        print(f"         options={diag['options_count']}件, "
              f"selected='{selected.get('text', 'N/A')}' (value={selected.get('value', 'N/A')})")
    if diag.get("error"):
        print(f"         error: {diag['error']}")


def save_test_artifacts(page, test_name: str):
    """テスト用アーティファクトを保存"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    save_artifacts(page, Path("artifacts"), f"dryrun_{test_name}_{ts}")


# =============================================================================
# テスト関数
# =============================================================================

def test_navigate(page, month="2026-04"):
    """テスト1: ナビゲーション確認"""
    print("\n" + "=" * 60)
    print("テスト: ナビゲーション → 月間スケジュール画面")
    print("=" * 60)

    login(page, save_state=True, context=page.context)
    page.wait_for_timeout(1000)
    dismiss_popup(page)
    goto_receipt(page)
    goto_yoriyori(page)
    goto_monthly_schedule(page)
    set_service_month(page, month)
    page.wait_for_timeout(1000)

    # 月の検証
    year, month_num = parse_month(month)
    reiwa = to_reiwa(year)
    expected = f"令和{reiwa}年{month_num}月"

    actual = ""
    try:
        selects = page.locator("select")
        for i in range(selects.count()):
            select = selects.nth(i)
            if select.is_visible(timeout=1000):
                text = select.evaluate(
                    "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''"
                )
                if text and "令和" in text and "月" in text:
                    actual = text.strip()
                    break
    except Exception:
        pass

    if expected in actual:
        print(f"  [OK] 月の設定: {actual}")
    else:
        print(f"  [NG] 月の設定: 期待={expected}, 実際={actual}")

    # タブの確認
    print("\n--- タブ構造の確認 ---")
    for tab_name in ["利用者別", "日付別", "職員別"]:
        diag = diagnose_element(page, f".nav-tabs a:has-text('{tab_name}')", f"タブ: {tab_name}")
        print_diag(diag)

    # 利用者ドロップダウンの確認
    print("\n--- 利用者ドロップダウンの確認 ---")
    print_diag(diagnose_select(page, "div#user_search select.form-control", "利用者セレクト"))

    # ボタンエリアの確認
    print("\n--- ボタンエリアの確認 ---")
    print_diag(diagnose_element(page, "button[title='新規追加']", "新規追加ボタン"))
    print_diag(diagnose_element(page, "button[title='前月割当コピー']", "前月割当コピー"))

    save_test_artifacts(page, "navigate")
    print("\n  ナビゲーションテスト完了")
    return True


def test_user_select(page, user_name="青柳 あい"):
    """テスト2: 利用者選択"""
    print("\n" + "=" * 60)
    print(f"テスト: 利用者選択 → {user_name}")
    print("=" * 60)

    success = select_user(page, user_name)
    if success:
        print(f"  [OK] 利用者 '{user_name}' を選択")
    else:
        print(f"  [NG] 利用者 '{user_name}' の選択に失敗")
        save_test_artifacts(page, "user_select_fail")

    return success


def test_edit_dialog(page, day=1, start_time="16:00"):
    """テスト3: 編集ダイアログの確認"""
    print("\n" + "=" * 60)
    print(f"テスト: 編集ダイアログ → {day}日 {start_time}")
    print("=" * 60)

    if not click_schedule_entry(page, day, start_time):
        print("  [NG] スケジュールエントリのクリックに失敗")
        save_test_artifacts(page, "edit_dialog_click_fail")
        return False

    print("  [OK] 編集ダイアログが開きました")

    # ダイアログ内の要素を診断
    print("\n--- 編集ダイアログ内の要素 ---")

    # モーダル自体
    print_diag(diagnose_element(page, "#registModal", "モーダルコンテナ"))

    # 時間セレクト
    for sel_id, label in [
        ("#inPopupStartHour", "開始時"),
        ("#inPopupStartMinute1", "開始分(十)"),
        ("#inPopupStartMinute2", "開始分(一)"),
        ("#inPopupEndHour", "終了時"),
        ("#inPopupEndMinute1", "終了分(十)"),
        ("#inPopupEndMinute2", "終了分(一)"),
    ]:
        print_diag(diagnose_select(page, sel_id, label))

    # 職員セレクト
    print_diag(diagnose_select(page, "select#chargeStaff1Id1", "職員1"))
    print_diag(diagnose_select(page, "select#chargeStaff2Id1", "職員2"))
    print_diag(diagnose_select(page, "select#chargeStaff3Id1", "職員3"))

    # 保険区分ラジオ
    for radio_id, label in [
        ("#inPopupInsuranceDivision01", "介護保険ラジオ"),
        ("#inPopupInsuranceDivision02", "医療保険ラジオ"),
        ("#inPopupInsuranceDivision03", "自費ラジオ"),
    ]:
        diag = diagnose_element(page, radio_id, label)
        if diag.get("exists"):
            try:
                checked = page.locator(radio_id).is_checked()
                diag["checked"] = checked
                print(f"  [{'CHECKED' if checked else 'OK'}] {label}: {radio_id}")
            except Exception:
                print_diag(diag)
        else:
            print_diag(diag)

    # デートピッカー
    print_diag(diagnose_element(page, "#simple-select-days-range", "デートピッカー"))
    print_diag(diagnose_element(page, ".hasMultiDatepicker", "マルチデートピッカー"))

    # ボタン
    print_diag(diagnose_element(page, "input#btnRegisPop", "登録ボタン"))
    print_diag(diagnose_element(page, "input#inPopupBtnDel", "削除ボタン"))
    print_diag(diagnose_element(page, "button.close.closeOn", "閉じるボタン"))

    # Estimate セレクト
    for sel_id, label in [
        ("#inPopupEstimate1", "サービス区分"),
        ("#inPopupEstimate2", "基本療養費"),
        ("#inPopupEstimate3", "職員資格"),
        ("#inPopupEstimate4", "算定時間"),
        ("#inPopupServiceKindId", "サービス種類"),
    ]:
        print_diag(diagnose_select(page, sel_id, label))

    save_test_artifacts(page, "edit_dialog")

    # 時間変更テスト（13:50-14:45）
    print("\n--- 時間変更テスト ---")
    if edit_schedule_time(page, 13, 50, 14, 45):
        print("  [OK] 時間変更成功: 13:50-14:45")
    else:
        print("  [NG] 時間変更失敗")

    # 日付変更テスト（デートピッカー）
    print("\n--- 日付変更テスト（デートピッカー） ---")
    if change_date(page, 15):
        print("  [OK] 日付変更成功: → 15日")
    else:
        print("  [NG] 日付変更失敗")

    save_test_artifacts(page, "edit_dialog_after_changes")

    # ダイアログを閉じる
    print("\n--- ダイアログ閉じテスト ---")
    if close_edit_dialog(page):
        print("  [OK] ダイアログを閉じました")
    else:
        print("  [NG] ダイアログを閉じられません")

    return True


def test_add_medical(page):
    """テスト4: 新規追加（医療保険）"""
    print("\n" + "=" * 60)
    print("テスト: 新規追加（医療保険）")
    print("=" * 60)

    if not click_new_add_button(page):
        print("  [NG] 新規追加ボタンのクリックに失敗")
        save_test_artifacts(page, "add_medical_btn_fail")
        return False

    print("  [OK] 新規追加ダイアログが開きました")
    save_test_artifacts(page, "add_medical_dialog_opened")

    # 医療保険フィールドを設定
    if fill_medical_insurance_fields(page):
        print("  [OK] 医療保険フィールド設定完了")
    else:
        print("  [NG] 医療保険フィールド設定失敗")

    save_test_artifacts(page, "add_medical_fields_set")

    # 日付・時間・職員を設定
    change_date(page, 10)
    edit_schedule_time(page, 9, 0, 9, 35)
    edit_staff(page, "川名 千恵", "佐藤 憲二", for_new_entry=True)

    save_test_artifacts(page, "add_medical_complete")

    # 登録はスキップ
    print("  [dry-run] 登録をスキップ")
    close_edit_dialog(page)
    return True


def test_add_nursing(page):
    """テスト5: 新規追加（介護保険）"""
    print("\n" + "=" * 60)
    print("テスト: 新規追加（介護保険）")
    print("=" * 60)

    if not click_new_add_button(page):
        print("  [NG] 新規追加ボタンのクリックに失敗")
        save_test_artifacts(page, "add_nursing_btn_fail")
        return False

    print("  [OK] 新規追加ダイアログが開きました")

    # 介護保険フィールドを設定
    if fill_nursing_insurance_fields(page, "09:00", "10:00"):
        print("  [OK] 介護保険フィールド設定完了")
        santei = calculate_santei_time("09:00", "10:00")
        print(f"       算定時間: {santei}")
    else:
        print("  [NG] 介護保険フィールド設定失敗")

    save_test_artifacts(page, "add_nursing_fields_set")

    # 日付・時間・職員を設定
    change_date(page, 5)
    edit_schedule_time(page, 9, 0, 10, 0)
    edit_staff(page, "川名 千恵", "", for_new_entry=True)

    save_test_artifacts(page, "add_nursing_complete")

    # 登録はスキップ
    print("  [dry-run] 登録をスキップ")
    close_edit_dialog(page)
    return True


def test_delete(page, day=1, start_time="16:00"):
    """テスト6: 削除"""
    print("\n" + "=" * 60)
    print(f"テスト: 削除 → {day}日 {start_time}")
    print("=" * 60)

    if not click_schedule_entry(page, day, start_time):
        print("  [NG] スケジュールエントリのクリックに失敗")
        return False

    print("  [OK] 編集ダイアログが開きました")

    # 削除ボタンの確認
    del_btn = page.locator("input#inPopupBtnDel")
    if del_btn.is_visible(timeout=3000):
        print("  [OK] 削除ボタンが見つかりました")
    else:
        print("  [NG] 削除ボタンが見つかりません")

    save_test_artifacts(page, "delete_dialog")

    # 削除はスキップ
    print("  [dry-run] 削除をスキップ")
    close_edit_dialog(page)
    return True


def test_date_change(page, day=1, start_time="16:00", new_day=15):
    """テスト7: 日付変更"""
    print("\n" + "=" * 60)
    print(f"テスト: 日付変更 → {day}日 → {new_day}日")
    print("=" * 60)

    if not click_schedule_entry(page, day, start_time):
        print("  [NG] スケジュールエントリのクリックに失敗")
        return False

    print("  [OK] 編集ダイアログが開きました")

    # 日付変更テスト
    if change_date(page, new_day):
        print(f"  [OK] 日付を {new_day}日 に変更")
    else:
        print(f"  [NG] 日付変更に失敗")

    save_test_artifacts(page, "date_change")

    # 登録はスキップ
    print("  [dry-run] 登録をスキップ")
    close_edit_dialog(page)
    return True


def test_event(page, staff_name="川名 千恵"):
    """テスト8: イベント追加（職員別タブ）"""
    print("\n" + "=" * 60)
    print(f"テスト: イベント追加 → 職員: {staff_name}")
    print("=" * 60)

    # 職員別タブに遷移
    if not navigate_to_staff_tab(page):
        print("  [NG] 職員別タブへの遷移に失敗")
        save_test_artifacts(page, "event_tab_fail")
        return False

    print("  [OK] 職員別タブに遷移しました")

    # 職員ドロップダウンの確認
    print("\n--- 職員ドロップダウンの確認 ---")
    print_diag(diagnose_select(page, "select#staffMemberInternalId", "職員セレクト"))
    print_diag(diagnose_select(page, "div#helper_search select.form-control", "職員セレクト(div)"))

    # 職員を選択
    if not select_staff_member(page, staff_name):
        print(f"  [NG] 職員 '{staff_name}' の選択に失敗")
        save_test_artifacts(page, "event_staff_fail")
        # 利用者別タブに戻る
        navigate_to_user_tab(page)
        return False

    print(f"  [OK] 職員 '{staff_name}' を選択しました")
    save_test_artifacts(page, "event_staff_selected")

    # イベント新規登録ボタンの確認
    print("\n--- イベント新規登録ボタンの確認 ---")
    print_diag(diagnose_element(page, "button#individualMonthlyShiftAssignPopup", "個別業務ボタン"))

    event_btn = page.locator("button#individualMonthlyShiftAssignPopup")
    if event_btn.is_visible(timeout=3000):
        event_btn.click()
        page.wait_for_timeout(2000)
        print("  [OK] イベント新規登録ダイアログが開きました")

        # ダイアログ内の要素を診断
        print("\n--- イベントダイアログ内の要素 ---")
        print_diag(diagnose_element(page, "input[name='popupSelectedIndividual']", "ラジオボタン"))
        print_diag(diagnose_element(page, "input#popupIndividualName", "イベント名入力"))
        print_diag(diagnose_element(page, "button#popupIndividualButtonAdd", "登録ボタン"))

        # ラジオボタンの詳細
        radios = page.locator("input[name='popupSelectedIndividual']").all()
        print(f"\n  ラジオボタン数: {len(radios)}")
        for i, radio in enumerate(radios):
            try:
                value = radio.get_attribute("value")
                checked = radio.is_checked()
                label_text = radio.evaluate("""el => {
                    const next = el.nextElementSibling;
                    const parent = el.parentElement;
                    if (next && next.tagName === 'LABEL') return next.textContent.trim();
                    const label = document.querySelector('label[for="' + el.id + '"]');
                    if (label) return label.textContent.trim();
                    return parent ? parent.textContent.trim().substring(0, 50) : '';
                }""")
                print(f"  ラジオ[{i}]: value={value}, checked={checked}, label='{label_text}'")
            except Exception as e:
                print(f"  ラジオ[{i}]: エラー: {e}")

        # 時間セレクタの確認（イベントポップアップ用）
        print("\n--- イベントポップアップの時間セレクタ ---")
        for sel_id, label in [
            ("#inPopupStartHour", "開始時"),
            ("#inPopupStartMinute1", "開始分(十)"),
            ("#inPopupEndHour", "終了時"),
        ]:
            print_diag(diagnose_select(page, sel_id, label))

        # カレンダーの確認
        print("\n--- イベントポップアップのカレンダー ---")
        print_diag(diagnose_element(page, "form#formIndividualMonthlyShiftAssignPopup", "イベントフォーム"))
        print_diag(diagnose_element(page, "form#formIndividualMonthlyShiftAssignPopup table td", "カレンダーセル"))

        save_test_artifacts(page, "event_dialog")

        # ダイアログを閉じる
        close_edit_dialog(page)
    else:
        print("  [NG] イベント新規登録ボタンが見つかりません")

    # 利用者別タブに戻る
    navigate_to_user_tab(page)
    return True


def test_apply_sheet(page, sheet_path: str):
    """テスト9: 修正シートの適用シミュレーション"""
    print("\n" + "=" * 60)
    print(f"テスト: 修正シートのドライラン適用")
    print(f"シート: {sheet_path}")
    print("=" * 60)

    from lib.diff_engine import load_correction_sheet

    corrections = load_correction_sheet(sheet_path)
    print(f"  総修正件数: {len(corrections)}")

    # 各修正の内容を表示
    for i, c in enumerate(corrections):
        biz = c.business_type or "(未設定)"
        print(f"\n  [{i+1}/{len(corrections)}] {c.action}: {c.user_name} "
              f"{c.date_from or c.date_to}日 [{biz}]")
        if c.has_time_change():
            print(f"    時間: {c.start_time_from}-{c.end_time_from} → "
                  f"{c.start_time_to}-{c.end_time_to}")
        if c.has_staff_change():
            print(f"    職員1: {c.staff1_from} → {c.staff1_to}")
            print(f"    職員2: {c.staff2_from or '(なし)'} → {c.staff2_to or '(なし)'}")
        if c.has_date_change():
            print(f"    日付: {c.date_from} → {c.date_to}")

        # business_type の警告
        if not c.business_type:
            print(f"    [警告] business_type が未設定です！")
            if c.action == "add":
                print(f"           → 新規追加時にデフォルトで医療保険が適用されます")
        if c.is_event():
            if not c.remarks:
                print(f"    [警告] イベントですが備考（イベント名）が未設定です")
            if c.action != "add":
                print(f"    [警告] イベントのaction={c.action}ですが、イベントはaddのみサポート")

    # バリデーション
    from lib.diff_engine import validate_correction_data
    validation = validate_correction_data(corrections)
    print(f"\n--- バリデーション結果 ---")
    print(f"  valid: {validation['valid']}")
    print(f"  warnings: {len(validation['warnings'])}件")
    print(f"  errors: {len(validation['errors'])}件")
    for w in validation['warnings']:
        print(f"    [警告] {w}")
    for e in validation['errors']:
        print(f"    [エラー] {e}")

    return True


# =============================================================================
# メイン
# =============================================================================

def run_dry_run_test(
    test_name: str = "all",
    month: str = "2026-04",
    headless: bool = False,
    sheet_path: str = None,
    user_name: str = "青柳 あい",
    staff_name: str = "川名 千恵",
    day: int = 1,
    start_time: str = "16:00",
):
    """ドライランテストを実行"""
    print(f"\n{'#' * 60}")
    print(f"# ドライランテスト: {test_name}")
    print(f"# 対象月: {month}")
    print(f"# 利用者: {user_name}")
    print(f"# 日付: {day}日 {start_time}")
    print(f"{'#' * 60}")

    # 修正シートのみのテストはブラウザ不要
    if test_name == "apply" and sheet_path:
        with sync_playwright() as p:
            browser, context, page = create_browser_context(p, headless=headless)
            try:
                login(page, save_state=True, context=context)
                page.wait_for_timeout(1000)
                dismiss_popup(page)
                goto_receipt(page)
                goto_yoriyori(page)
                goto_monthly_schedule(page)
                set_service_month(page, month)
                page.wait_for_timeout(1000)
                test_apply_sheet(page, sheet_path)
            except Exception as e:
                print(f"エラー: {e}")
                save_test_artifacts(page, "apply_error")
            finally:
                context.close()
                browser.close()
        return

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)

        try:
            results = {}

            # ナビゲーション（全テストで共通）
            results["navigate"] = test_navigate(page, month)

            if test_name in ("all", "navigate"):
                if test_name == "navigate":
                    return

            # 利用者選択
            if test_name in ("all", "edit", "add-medical", "add-nursing", "delete", "date-change"):
                results["user_select"] = test_user_select(page, user_name)
                if not results["user_select"]:
                    print("利用者選択に失敗したため、以降のテストをスキップ")
                    save_test_artifacts(page, "overall_fail")
                    return

            # 編集ダイアログ
            if test_name in ("all", "edit"):
                results["edit"] = test_edit_dialog(page, day, start_time)

            # 新規追加（医療保険）
            if test_name in ("all", "add-medical"):
                results["add_medical"] = test_add_medical(page)

            # 新規追加（介護保険）
            if test_name in ("all", "add-nursing"):
                results["add_nursing"] = test_add_nursing(page)

            # 削除
            if test_name in ("all", "delete"):
                results["delete"] = test_delete(page, day, start_time)

            # 日付変更
            if test_name in ("all", "date-change"):
                results["date_change"] = test_date_change(page, day, start_time, 15)

            # イベント追加
            if test_name in ("all", "event"):
                results["event"] = test_event(page, staff_name)

            # サマリー
            print(f"\n{'=' * 60}")
            print("テスト結果サマリー")
            print(f"{'=' * 60}")
            for name, result in results.items():
                status = "PASS" if result else "FAIL"
                print(f"  [{status}] {name}")

            # ブラウザ表示時は少し待つ
            if not headless:
                print("\n5秒後にブラウザを閉じます...")
                page.wait_for_timeout(5000)

        except Exception as e:
            print(f"\nエラーが発生しました: {e}")
            save_test_artifacts(page, "overall_error")
            raise

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="包括的ドライランテスト")
    parser.add_argument("--test", default="all",
                        choices=["all", "navigate", "edit", "add-medical",
                                 "add-nursing", "delete", "date-change",
                                 "event", "apply"],
                        help="実行するテスト")
    parser.add_argument("--month", default="2026-04", help="対象月")
    parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    parser.add_argument("--headless", action="store_true", help="ヘッドレスモード")
    parser.add_argument("--sheet", help="修正シートのパス（--test apply 時に使用）")
    parser.add_argument("--user", default="青柳 あい", help="テスト対象の利用者名")
    parser.add_argument("--staff", default="川名 千恵", help="テスト対象の職員名")
    parser.add_argument("--day", type=int, default=1, help="テスト対象の日付")
    parser.add_argument("--time", default="16:00", help="テスト対象の開始時間")

    args = parser.parse_args()

    headless = args.headless and not args.headed

    run_dry_run_test(
        test_name=args.test,
        month=args.month,
        headless=headless,
        sheet_path=args.sheet,
        user_name=args.user,
        staff_name=args.staff,
        day=args.day,
        start_time=args.time,
    )
