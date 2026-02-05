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
"""

import sys
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
from lib.diff_engine import load_correction_sheet, Correction


def select_user(page, user_name: str) -> bool:
    """
    利用者を選択（ドロップダウンまたは「次へ」ボタン）

    Args:
        page: Playwrightのページオブジェクト
        user_name: 利用者名

    Returns:
        bool: 成功したかどうか
    """
    print(f"利用者を選択しています: {user_name}")

    # 現在の画面に表示されている利用者を確認
    page_content = page.content()
    if user_name in page_content:
        # すでに表示されている場合
        # タイトルや利用者名表示エリアを確認
        title_elem = page.locator("h3, h2, .user-name, .patient-name").first
        if title_elem.is_visible():
            title_text = title_elem.text_content()
            if user_name in title_text:
                print(f"  すでに選択されています: {user_name}")
                return True

    # 方法1: ドロップダウンから選択
    try:
        user_selects = page.locator("select").all()
        for select in user_selects:
            options = select.locator("option").all_text_contents()
            for opt in options:
                if user_name in opt:
                    select.select_option(label=opt)
                    page.wait_for_timeout(2000)
                    print(f"  ドロップダウンから選択: {user_name}")
                    return True
    except Exception:
        pass

    # 方法2: 「次へ」ボタンで移動
    max_attempts = 70  # 最大61件の利用者
    for i in range(max_attempts):
        # 現在の利用者を確認
        page_content = page.content()
        if user_name in page_content:
            print(f"  {i+1}回目で発見: {user_name}")
            return True

        # 次へボタンをクリック
        next_btn = page.locator("text=次へ").first
        if not next_btn.is_visible():
            break
        next_btn.click()
        page.wait_for_timeout(1500)

    print(f"  利用者が見つかりません: {user_name}")
    return False


def click_schedule_entry(page, day: int, start_time: str) -> bool:
    """
    指定した日付・開始時間のスケジュールエントリをクリック

    Args:
        page: Playwrightのページオブジェクト
        day: 日付（1-31）
        start_time: 開始時間（"HH:MM"）

    Returns:
        bool: 成功したかどうか
    """
    print(f"  予定をクリック: {day}日 {start_time}")

    try:
        # テーブル行を探す
        rows = page.locator("table tr").all()

        for row in rows:
            try:
                row_text = row.text_content()
                # 日付と時間を含む行を探す
                if str(day) in row_text and start_time in row_text:
                    # リンクをクリック
                    link = row.locator("a").first
                    if link.is_visible():
                        link.click()
                        page.wait_for_timeout(2000)
                        return True
            except Exception:
                continue

        # 時間を含むリンクを直接探す
        time_link = page.locator(f"a:has-text('{start_time}')").first
        if time_link.is_visible():
            time_link.click()
            page.wait_for_timeout(2000)
            return True

        return False

    except Exception as e:
        print(f"    エラー: {e}")
        return False


def edit_schedule_time(page, start_hour: int, start_min: int, end_hour: int, end_min: int) -> bool:
    """
    スケジュールの時間を編集

    カイポケの編集ポップアップでは、時間は6つのセレクトで構成
    """
    print(f"  時間変更: {start_hour:02d}:{start_min:02d} - {end_hour:02d}:{end_min:02d}")

    try:
        # 分を十の位と一の位に分解
        start_min_tens = start_min // 10
        start_min_ones = start_min % 10
        end_min_tens = end_min // 10
        end_min_ones = end_min % 10

        # 開始時
        start_hour_select = page.locator("#inPopupStartHour")
        if start_hour_select.is_visible():
            start_hour_select.select_option(value=str(start_hour))
            page.wait_for_timeout(200)

        # 開始分（十の位）
        start_min1_select = page.locator("#inPopupStartMinute1")
        if start_min1_select.is_visible():
            start_min1_select.select_option(value=str(start_min_tens))
            page.wait_for_timeout(200)

        # 開始分（一の位）
        start_min2_select = page.locator("#inPopupStartMinute2")
        if start_min2_select.is_visible():
            start_min2_select.select_option(value=str(start_min_ones))
            page.wait_for_timeout(200)

        # 終了時
        end_hour_select = page.locator("#inPopupEndHour")
        if end_hour_select.is_visible():
            end_hour_select.select_option(value=str(end_hour))
            page.wait_for_timeout(200)

        # 終了分（十の位）
        end_min1_select = page.locator("#inPopupEndMinute1")
        if end_min1_select.is_visible():
            end_min1_select.select_option(value=str(end_min_tens))
            page.wait_for_timeout(200)

        # 終了分（一の位）
        end_min2_select = page.locator("#inPopupEndMinute2")
        if end_min2_select.is_visible():
            end_min2_select.select_option(value=str(end_min_ones))
            page.wait_for_timeout(200)

        return True

    except Exception as e:
        print(f"    時間変更エラー: {e}")
        return False


def edit_staff(page, staff1_name: str, staff2_name: str = "") -> bool:
    """
    職員を編集

    Args:
        page: Playwrightのページオブジェクト
        staff1_name: 職員1名（空文字の場合は変更しない）
        staff2_name: 職員2名（空文字の場合は削除）

    Returns:
        bool: 成功したかどうか
    """
    try:
        # 職員のセレクトを探す
        all_selects = page.locator("select").all()

        # 職員2を空にする（削除）
        if staff2_name == "":
            for i, select in enumerate(all_selects):
                try:
                    selected_option = select.locator("option:checked")
                    if selected_option.count() > 0:
                        selected_text = selected_option.text_content()
                        # 職員2として設定されているものを探す
                        # 職員1以外で、名前が入っているもの
                        if selected_text and staff1_name not in selected_text and selected_text != "-":
                            # 空（最初のオプション）を選択
                            select.select_option(index=0)
                            page.wait_for_timeout(300)
                            print(f"    職員2を削除: {selected_text}")
                            return True
                except Exception:
                    continue

        return True

    except Exception as e:
        print(f"    職員変更エラー: {e}")
        return False


def click_register_button(page) -> bool:
    """
    「登録する」ボタンをクリック
    """
    try:
        register_button = page.locator("button:has-text('登録する'), a:has-text('登録する'), input[value='登録する']").first
        if register_button.is_visible():
            register_button.click()
            page.wait_for_timeout(2000)

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

        return False

    except Exception as e:
        print(f"    登録エラー: {e}")
        return False


def close_edit_dialog(page) -> bool:
    """
    編集ダイアログを閉じる（キャンセル）
    """
    try:
        cancel_buttons = [
            "button:has-text('キャンセル')",
            "button:has-text('閉じる')",
            "a:has-text('キャンセル')",
            ".close",
        ]
        for selector in cancel_buttons:
            btn = page.locator(selector).first
            if btn.is_visible():
                btn.click()
                page.wait_for_timeout(1000)
                return True
        return False
    except Exception:
        return False


def change_date(page, new_day: int) -> bool:
    """
    スケジュールの日付を変更

    Args:
        page: Playwrightのページオブジェクト
        new_day: 新しい日付（1-31）

    Returns:
        bool: 成功したかどうか
    """
    print(f"  日付変更: → {new_day}日")

    try:
        # 日付セレクトを探す（#inPopupDay または類似のID）
        day_selectors = [
            "#inPopupDay",
            "select[name*='day']",
            "select[name*='Day']",
        ]

        for selector in day_selectors:
            day_select = page.locator(selector).first
            if day_select.is_visible():
                day_select.select_option(value=str(new_day))
                page.wait_for_timeout(300)
                print(f"    日付を {new_day} に変更")
                return True

        # セレクトが見つからない場合、すべてのセレクトを確認
        all_selects = page.locator("select").all()
        for select in all_selects:
            try:
                options = select.locator("option").all_text_contents()
                # 1-31の日付を含むセレクトを探す
                if any(str(new_day) in opt for opt in options) and len(options) >= 28:
                    select.select_option(value=str(new_day))
                    page.wait_for_timeout(300)
                    print(f"    日付を {new_day} に変更")
                    return True
            except Exception:
                continue

        print("    日付セレクトが見つかりません")
        return False

    except Exception as e:
        print(f"    日付変更エラー: {e}")
        return False


def delete_schedule_entry(page, day: int, start_time: str, dry_run: bool = False) -> bool:
    """
    スケジュールエントリを削除

    Args:
        page: Playwrightのページオブジェクト
        day: 日付（1-31）
        start_time: 開始時間（"HH:MM"）
        dry_run: テスト実行

    Returns:
        bool: 成功したかどうか
    """
    print(f"  削除: {day}日 {start_time}")

    # 予定をクリックして編集画面を開く
    if not click_schedule_entry(page, day, start_time):
        print("    予定が見つかりません")
        return False

    if dry_run:
        print("  [dry-run] 削除をスキップ")
        close_edit_dialog(page)
        return True

    try:
        # 削除ボタンを探す
        delete_buttons = [
            "button:has-text('削除')",
            "a:has-text('削除')",
            "input[value='削除']",
            "button:has-text('予定を削除')",
        ]

        for selector in delete_buttons:
            btn = page.locator(selector).first
            if btn.is_visible():
                btn.click()
                page.wait_for_timeout(1000)

                # 確認ダイアログ
                try:
                    ok_btn = page.locator("button:has-text('OK'), button:has-text('はい'), button:has-text('削除する')").first
                    if ok_btn.is_visible(timeout=2000):
                        ok_btn.click()
                        page.wait_for_timeout(1000)
                except Exception:
                    pass

                print("    削除完了")
                return True

        print("    削除ボタンが見つかりません")
        close_edit_dialog(page)
        return False

    except Exception as e:
        print(f"    削除エラー: {e}")
        return False


def add_schedule_entry(page, day: int, start_time: str, end_time: str,
                       staff1: str, staff2: str, service_type: str,
                       dry_run: bool = False) -> bool:
    """
    新しいスケジュールエントリを追加

    Args:
        page: Playwrightのページオブジェクト
        day: 日付（1-31）
        start_time: 開始時間（"HH:MM"）
        end_time: 終了時間（"HH:MM"）
        staff1: 職員1名
        staff2: 職員2名
        service_type: サービス内容
        dry_run: テスト実行

    Returns:
        bool: 成功したかどうか
    """
    print(f"  追加: {day}日 {start_time}-{end_time}")

    try:
        # 「追加」または「新規登録」ボタンを探す
        add_buttons = [
            "button:has-text('追加')",
            "a:has-text('追加')",
            "button:has-text('新規登録')",
            "a:has-text('新規登録')",
            "button:has-text('予定を追加')",
        ]

        clicked = False
        for selector in add_buttons:
            btn = page.locator(selector).first
            if btn.is_visible():
                btn.click()
                page.wait_for_timeout(2000)
                clicked = True
                break

        if not clicked:
            print("    追加ボタンが見つかりません")
            return False

        # 日付を設定
        change_date(page, day)

        # 時間を設定
        start_parts = start_time.split(":")
        end_parts = end_time.split(":")
        if len(start_parts) >= 2 and len(end_parts) >= 2:
            edit_schedule_time(
                page,
                int(start_parts[0]), int(start_parts[1]),
                int(end_parts[0]), int(end_parts[1])
            )

        # 職員を設定（TODO: 実装が必要）

        if dry_run:
            print("  [dry-run] 登録をスキップ")
            close_edit_dialog(page)
            return True
        else:
            return click_register_button(page)

    except Exception as e:
        print(f"    追加エラー: {e}")
        return False


def apply_correction(page, correction: Correction, dry_run: bool = False) -> bool:
    """
    1件の修正を適用

    Args:
        page: Playwrightのページオブジェクト
        correction: 修正データ
        dry_run: テスト実行（実際には保存しない）

    Returns:
        bool: 成功したかどうか
    """
    action = correction.action

    # アクションタイプごとに処理を分岐
    if action == "delete":
        print(f"\n=== 削除: {correction.user_name} {correction.date_from}日 ===")
        day = int(correction.date_from) if correction.date_from.isdigit() else 1
        return delete_schedule_entry(page, day, correction.start_time_from, dry_run)

    elif action == "add":
        print(f"\n=== 追加: {correction.user_name} {correction.date_to}日 ===")
        day = int(correction.date_to) if correction.date_to.isdigit() else 1
        return add_schedule_entry(
            page, day,
            correction.start_time_to, correction.end_time_to,
            correction.staff1_to, correction.staff2_to,
            correction.service_type, dry_run
        )

    elif action == "date_change":
        print(f"\n=== 日付変更: {correction.user_name} {correction.date_from}日 → {correction.date_to}日 ===")
        # 既存エントリを開く
        day = int(correction.date_from) if correction.date_from.isdigit() else 1
        if not click_schedule_entry(page, day, correction.start_time_from):
            print("  予定が見つかりません")
            return False

        # 日付を変更
        new_day = int(correction.date_to) if correction.date_to.isdigit() else 1
        change_date(page, new_day)

        # 時間変更があれば適用
        if correction.has_time_change():
            start_parts = correction.start_time_to.split(":")
            end_parts = correction.end_time_to.split(":")
            if len(start_parts) >= 2 and len(end_parts) >= 2:
                edit_schedule_time(
                    page,
                    int(start_parts[0]), int(start_parts[1]),
                    int(end_parts[0]), int(end_parts[1])
                )

        # 職員変更があれば適用
        if correction.has_staff_change():
            edit_staff(page, correction.staff1_to, correction.staff2_to)

        if dry_run:
            print("  [dry-run] 登録をスキップ")
            close_edit_dialog(page)
            return True
        else:
            return click_register_button(page)

    else:  # edit
        print(f"\n=== 修正適用: {correction.user_name} {correction.date_from}日 ===")

        # 予定をクリックして編集画面を開く
        day = int(correction.date_from) if correction.date_from.isdigit() else 1
        if not click_schedule_entry(page, day, correction.start_time_from):
            print("  予定が見つかりません")
            return False

        # 時間変更
        if correction.has_time_change():
            # 時間をパース
            start_parts = correction.start_time_to.split(":")
            end_parts = correction.end_time_to.split(":")
            if len(start_parts) >= 2 and len(end_parts) >= 2:
                start_hour = int(start_parts[0])
                start_min = int(start_parts[1])
                end_hour = int(end_parts[0])
                end_min = int(end_parts[1])
                edit_schedule_time(page, start_hour, start_min, end_hour, end_min)

        # 職員変更
        if correction.has_staff_change():
            edit_staff(page, correction.staff1_to, correction.staff2_to)

        # 登録
        if dry_run:
            print("  [dry-run] 登録をスキップ")
            close_edit_dialog(page)
            return True
        else:
            return click_register_button(page)


def run_auto_apply(
    correction_sheet: str,
    month: str = "2026-04",
    headless: bool = True,
    dry_run: bool = False,
    limit: int = None,
) -> dict:
    """
    修正シートに基づいてスケジュールを自動適用

    Args:
        correction_sheet: 修正シートのパス（JSON）
        month: 対象月（"2026-04" 形式）
        headless: ヘッドレスモードで実行するか
        dry_run: テスト実行（実際には保存しない）
        limit: 適用する件数の上限

    Returns:
        dict: 実行結果
    """
    # 修正シートを読み込む
    corrections = load_correction_sheet(correction_sheet)
    if limit:
        corrections = corrections[:limit]

    print(f"\n=== 自動適用開始 ===")
    print(f"修正シート: {correction_sheet}")
    print(f"対象月: {month}")
    print(f"修正件数: {len(corrections)}")
    print(f"dry-run: {dry_run}")
    print("")

    result = {
        "total": len(corrections),
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "details": [],
    }

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)

        try:
            # ログイン
            login(page, save_state=True, context=context)
            page.wait_for_timeout(1000)
            dismiss_popup(page)

            # ナビゲーション
            goto_receipt(page)
            goto_yoriyori(page)
            goto_monthly_schedule(page)
            set_service_month(page, month)
            page.wait_for_timeout(1000)

            # 利用者ごとにグループ化
            users = {}
            for c in corrections:
                if c.user_name not in users:
                    users[c.user_name] = []
                users[c.user_name].append(c)

            # 利用者ごとに処理
            for user_name, user_corrections in users.items():
                print(f"\n--- 利用者: {user_name} ({len(user_corrections)}件) ---")

                # 利用者を選択
                if not select_user(page, user_name):
                    print(f"  利用者の選択に失敗: {user_name}")
                    for c in user_corrections:
                        result["skipped"] += 1
                        result["details"].append({
                            "user": user_name,
                            "date": c.date_from,
                            "status": "skipped",
                            "reason": "user_not_found",
                        })
                    continue

                # 各修正を適用
                for correction in user_corrections:
                    try:
                        success = apply_correction(page, correction, dry_run)
                        if success:
                            result["success"] += 1
                            result["details"].append({
                                "user": user_name,
                                "date": correction.date_from,
                                "status": "success",
                            })
                        else:
                            result["failed"] += 1
                            result["details"].append({
                                "user": user_name,
                                "date": correction.date_from,
                                "status": "failed",
                            })
                    except Exception as e:
                        print(f"  エラー: {e}")
                        result["failed"] += 1
                        result["details"].append({
                            "user": user_name,
                            "date": correction.date_from,
                            "status": "error",
                            "reason": str(e),
                        })

                    page.wait_for_timeout(1000)

            print(f"\n=== 自動適用完了 ===")
            print(f"成功: {result['success']} / 失敗: {result['failed']} / スキップ: {result['skipped']} / 合計: {result['total']}")

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
    args = parser.parse_args()

    run_auto_apply(
        correction_sheet=args.sheet,
        month=args.month,
        headless=not args.headed,
        dry_run=args.dry_run,
        limit=args.limit,
    )
