"""
Block 1: 月間予定への固定パターン展開 (expand コマンド)

全利用者に対して「週間訪問パターンから展開」を実行し、
週間パターンを月間スケジュールに展開します。

使い方:
    python main.py expand --month 2026-04
    python main.py expand --month 2026-04 --headed
"""

import sys
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from lib.common import (
    create_browser_context,
    login,
    dismiss_popup,
    goto_monthly_schedule,
    set_service_month,
    save_artifacts,
)


def expand_weekly_pattern(page, wait_after_expand: float = 2.0) -> str:
    """
    現在表示中の利用者に対して「週間訪問パターンから展開」を実行

    Args:
        page: Playwrightのページオブジェクト
        wait_after_expand: 展開後の待機時間（秒）

    Returns:
        str: "success" = 展開成功, "skipped" = すでに展開済み（スキップ）, "failed" = 失敗
    """
    try:
        # 「週間訪問パターンから展開」ボタンをクリック
        expand_button = page.locator("text=週間訪問パターンから展開")

        if not expand_button.is_visible():
            print("  「週間訪問パターンから展開」ボタンが見つかりません")
            return "failed"

        expand_button.click()
        page.wait_for_timeout(1000)

        # 「上書きしますか？」確認ダイアログが表示されたらキャンセル（上書きしない）
        overwrite_dialog = page.locator("text=上書きしますか")
        cancel_buttons = [
            "button:has-text('いいえ')",
            "button:has-text('キャンセル')",
            "button:has-text('No')",
            "button:has-text('Cancel')",
        ]

        if overwrite_dialog.is_visible():
            print("  すでに展開済み - 上書きせずスキップ")
            for selector in cancel_buttons:
                try:
                    cancel_btn = page.locator(selector).first
                    if cancel_btn.is_visible():
                        cancel_btn.click()
                        page.wait_for_timeout(500)
                        break
                except Exception:
                    continue
            return "skipped"

        page.wait_for_timeout(int(wait_after_expand * 1000))
        return "success"

    except Exception as e:
        print(f"  展開に失敗しました: {e}")
        return "failed"


def get_current_user_info(page) -> str:
    """現在表示中の利用者情報を取得"""
    try:
        # 利用者名を取得（画面によってセレクタが異なる可能性あり）
        user_name_selectors = [
            ".user-name",
            "[data-user-name]",
            ".patient-name",
            "h2",
            "h3",
        ]
        for selector in user_name_selectors:
            try:
                elem = page.locator(selector).first
                if elem.is_visible():
                    return elem.text_content().strip()[:50]
            except Exception:
                continue
        return "（名前取得失敗）"
    except Exception:
        return "（名前取得失敗）"


def has_next_button(page) -> bool:
    """「次へ」ボタンが存在するかチェック"""
    try:
        next_button = page.locator("text=次へ")
        # ボタンが存在し、かつ visible かどうか
        return next_button.count() > 0 and next_button.first.is_visible()
    except Exception:
        return False


def click_next_button(page) -> bool:
    """「次へ」ボタンをクリック"""
    try:
        next_button = page.locator("text=次へ").first
        if next_button.is_visible():
            next_button.click()
            page.wait_for_timeout(1000)
            return True
        return False
    except Exception:
        return False


def run_expand(month: str = "2026-04", headless: bool = True, dry_run: bool = False) -> dict:
    """
    Block 1: 全利用者に対して週間パターンを展開

    Args:
        month: 対象月（"2026-04" 形式）
        headless: ヘッドレスモードで実行するか
        dry_run: テスト実行（実際の展開は行わない）

    Returns:
        dict: 実行結果 {success: int, skipped: int, failed: int, total: int}
    """
    result = {"success": 0, "skipped": 0, "failed": 0, "total": 0, "users": []}

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)

        try:
            # ログイン
            login(page, save_state=True, context=context)
            page.wait_for_timeout(1000)

            # ポップアップを閉じる
            dismiss_popup(page)

            # 月間スケジュール管理画面に遷移
            goto_monthly_schedule(page)

            # サービス提供月を設定（4月固定）
            set_service_month(page, month)
            page.wait_for_timeout(1000)

            print(f"\n=== Block 1: 週間パターン展開開始 ===")
            print(f"対象月: {month}")
            print("")

            user_index = 1

            while True:
                result["total"] += 1
                user_name = get_current_user_info(page)
                print(f"[{user_index}] {user_name}")

                if dry_run:
                    print("  (dry-run: 展開をスキップ)")
                    status = "success"
                else:
                    status = expand_weekly_pattern(page)

                if status == "success":
                    result["success"] += 1
                    result["users"].append({"index": user_index, "name": user_name, "status": "success"})
                    print("  → 展開完了")
                elif status == "skipped":
                    result["skipped"] += 1
                    result["users"].append({"index": user_index, "name": user_name, "status": "skipped"})
                    print("  → スキップ（展開済み）")
                else:
                    result["failed"] += 1
                    result["users"].append({"index": user_index, "name": user_name, "status": "failed"})
                    print("  → 展開失敗")

                # 「次へ」ボタンがなければ終了（最後の利用者）
                if not has_next_button(page):
                    print(f"\n最後の利用者に到達しました（「次へ」ボタンなし）")
                    break

                # 次の利用者へ
                if not click_next_button(page):
                    print("「次へ」ボタンのクリックに失敗しました")
                    break

                user_index += 1

                # 安全のため、61件を超えたら停止
                if user_index > 70:
                    print("警告: 想定以上の利用者数のため停止")
                    break

            print(f"\n=== Block 1: 完了 ===")
            print(f"成功: {result['success']} / スキップ: {result['skipped']} / 失敗: {result['failed']} / 合計: {result['total']}")

        except Exception as e:
            print(f"エラーが発生しました: {e}")
            save_artifacts(page, Path("artifacts"), "expand_error")
            raise

        finally:
            context.close()
            browser.close()

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Block 1: 週間パターン展開")
    parser.add_argument("--month", default="2026-04", help="対象月 (デフォルト: 2026-04)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    parser.add_argument("--dry-run", action="store_true", help="テスト実行（実際の展開は行わない）")
    args = parser.parse_args()

    # 4月以外は警告
    if not args.month.endswith("-04"):
        print("警告: 4月以外の月が指定されています！")
        confirm = input("続行しますか？ (y/N): ")
        if confirm.lower() != "y":
            print("キャンセルしました")
            sys.exit(0)

    run_expand(month=args.month, headless=not args.headed, dry_run=args.dry_run)
