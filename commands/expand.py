"""
Block 1: 月間予定への固定パターン展開 (expand コマンド)

全利用者に対して「週間訪問パターンから展開」を実行し、
週間パターンを月間スケジュールに展開します。

動作:
  - 利用者を先頭から順に「次へ」で移動
  - 各利用者で「週間訪問パターンから展開」ボタンをクリック
  - 既に展開済みの場合はネイティブconfirmダイアログが出る → OK（上書き）
  - 「次へ」リンクがなくなったら全利用者完了

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
    goto_receipt,
    goto_yoriyori,
    goto_monthly_schedule,
    set_service_month,
    save_artifacts,
)
from lib.stop_signal import is_stop_requested, clear_stop

# 最大利用者数（安全リミット: 現在約70名、将来の増加に備えて200）
MAX_USERS = 200


def get_current_user_info(page) -> str:
    """
    現在表示中の利用者名を取得

    カイポケの月間スケジュール画面の利用者名ドロップダウン（td.pulldownUser）
    またはページ見出しから名前を取得します。
    """
    # 方法1: ドロップダウンの選択テキストから取得
    try:
        select = page.locator("td.pulldownUser select")
        if select.is_visible(timeout=2000):
            name = select.evaluate(
                "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''"
            )
            if name and name.strip():
                return name.strip()
    except Exception:
        pass

    # 方法2: ページ見出しから取得（「○○様　月間スケジュール一覧」）
    try:
        heading = page.locator("text=/.*様.*月間スケジュール/").first
        if heading.is_visible(timeout=1000):
            text = heading.text_content()
            if "様" in text:
                return text.split("様")[0].strip()
    except Exception:
        pass

    # 方法3: フォールバック
    return "（名前取得失敗）"


def has_next_user(page) -> bool:
    """
    「次へ」リンクが存在するか（最後の利用者でないか）

    td.linkNextUser 内にリンクがあれば次の利用者が存在する。
    最後の利用者ではリンクが存在しない。
    """
    try:
        next_link = page.locator("td.linkNextUser a")
        return next_link.count() > 0 and next_link.first.is_visible(timeout=2000)
    except Exception:
        return False


def click_next_user(page) -> bool:
    """
    「次へ」リンクをクリックして次の利用者に移動

    td.linkNextUser a をクリックし、ページ遷移を待ちます。
    """
    try:
        next_link = page.locator("td.linkNextUser a").first
        if not next_link.is_visible(timeout=2000):
            return False

        # 次の利用者名をログ出力（「次へ(飯塚 大輝)」のテキスト）
        try:
            next_text = next_link.text_content().strip()
            print(f"  → {next_text}")
        except Exception:
            pass

        next_link.click()

        # ページ遷移を待つ
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)

        return True

    except Exception as e:
        print(f"  次へクリック失敗: {e}")
        return False


def expand_weekly_pattern(page, dialog_tracker: dict) -> str:
    """
    現在表示中の利用者に対して「週間訪問パターンから展開」を実行

    ネイティブconfirmダイアログ:
      「既に当月の月間パターン割当が登録されています。上書きしますか？」
      → page.on('dialog') ハンドラで自動的にOKを押す

    Args:
        page: Playwrightのページオブジェクト
        dialog_tracker: ダイアログ出現トラッカー {"appeared": bool, "message": str}

    Returns:
        str: "success" = 新規展開, "overwritten" = 上書き展開, "failed" = 失敗
    """
    # ダイアログトラッカーをリセット
    dialog_tracker["appeared"] = False
    dialog_tracker["message"] = ""

    try:
        # 「週間訪問パターンから展開」ボタンを探す
        expand_btn = page.locator("button[title='週間訪問パターンから展開']")

        if not expand_btn.is_visible(timeout=2000):
            # フォールバック: テキストで探す
            expand_btn = page.locator("text=週間訪問パターンから展開")
            if not expand_btn.is_visible(timeout=1000):
                print("  「週間訪問パターンから展開」ボタンが見つかりません")
                return "failed"

        # ボタンが disabled なら即スキップ（週間パターン未作成の利用者）
        is_disabled = expand_btn.evaluate(
            "el => el.disabled || el.classList.contains('disabled') || el.getAttribute('aria-disabled') === 'true'"
        )
        if is_disabled:
            print("  週間パターン未設定 → スキップ")
            return "skipped"

        # ボタンをクリック（→ ネイティブconfirmダイアログが出る場合あり）
        expand_btn.click()

        # ダイアログ処理 + ページ遷移を待つ
        page.wait_for_timeout(1000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        if dialog_tracker["appeared"]:
            return "overwritten"
        else:
            return "success"

    except Exception as e:
        print(f"  展開失敗: {e}")
        # エラー時のスクリーンショット
        try:
            save_artifacts(page, Path("artifacts"), "expand_user_error")
        except Exception:
            pass
        return "failed"


def run_expand(month: str = "2026-04", headless: bool = True, dry_run: bool = False) -> dict:
    """
    Block 1: 全利用者に対して週間パターンを展開

    Args:
        month: 対象月（"2026-04" 形式）
        headless: ヘッドレスモードで実行するか
        dry_run: テスト実行（実際の展開は行わない）

    Returns:
        dict: 実行結果
            success: int  - 成功数（新規 + 上書き）※GAS互換
            skipped: int  - スキップ数（今回は常に0）※GAS互換
            failed: int   - 失敗数
            total: int    - 処理した利用者数
            details: dict - {new: int, overwritten: int}
            users: list   - 各利用者の処理結果
    """
    # 前回の停止フラグをクリア
    clear_stop()

    result = {
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "total": 0,
        "details": {
            "new": 0,
            "overwritten": 0,
        },
        "users": [],
    }

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)

        # ネイティブconfirmダイアログのハンドラ
        # 「既に当月の月間パターン割当が登録されています。上書きしますか？」→ OK
        dialog_tracker = {"appeared": False, "message": ""}

        def handle_dialog(dialog):
            dialog_tracker["appeared"] = True
            dialog_tracker["message"] = dialog.message
            print(f"  [ダイアログ] {dialog.message} → OK（自動承認）")
            dialog.accept()

        page.on("dialog", handle_dialog)

        try:
            # ログイン
            login(page, save_state=True, context=context)
            page.wait_for_timeout(1000)

            # ポップアップを閉じる
            dismiss_popup(page)

            # レセプト画面に遷移
            goto_receipt(page)

            # 訪問看護/よりより画面に遷移
            goto_yoriyori(page)

            # 月間スケジュール管理画面に遷移
            goto_monthly_schedule(page)

            # サービス提供月を設定
            set_service_month(page, month)
            page.wait_for_timeout(1000)

            # 安全確認: 月が正しく設定されたか検証
            from lib.common import parse_month, to_reiwa
            target_year, target_month = parse_month(month)
            target_reiwa = to_reiwa(target_year)
            expected_month_text = f"令和{target_reiwa}年{target_month}月"

            # <select>から現在の月を再確認
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
                save_artifacts(page, Path("artifacts"), "expand_wrong_month")
                raise RuntimeError(error_msg)

            print(f"\n=== Block 1: 週間パターン展開開始 ===")
            print(f"対象月: {month}（確認済み: {actual_month or expected_month_text}）")
            print(f"最大利用者数: {MAX_USERS}")
            print("")

            user_index = 1

            while True:
                # 非常停止チェック
                if is_stop_requested():
                    print(f"\n非常停止が要求されました！処理を中断します。")
                    print(f"（{user_index - 1}人目まで処理済み）")
                    result["stopped"] = True
                    break

                result["total"] += 1
                user_name = get_current_user_info(page)
                print(f"[{user_index}] {user_name}")

                if dry_run:
                    print("  (dry-run: 展開をスキップ)")
                    status = "success"
                else:
                    status = expand_weekly_pattern(page, dialog_tracker)

                # 結果を記録
                if status == "success":
                    result["success"] += 1
                    result["details"]["new"] += 1
                    print("  → 新規展開完了")
                elif status == "overwritten":
                    result["success"] += 1
                    result["details"]["overwritten"] += 1
                    print("  → 上書き展開完了")
                elif status == "skipped":
                    result["skipped"] += 1
                    print("  → スキップ（パターン未設定）")
                else:
                    result["failed"] += 1
                    print("  → 展開失敗")

                result["users"].append({
                    "index": user_index,
                    "name": user_name,
                    "status": status,
                })

                # 最後の利用者かチェック（「次へ」リンクの有無）
                if not has_next_user(page):
                    print(f"\n最後の利用者に到達しました（「次へ」リンクなし）")
                    break

                # 次の利用者へ
                if not click_next_user(page):
                    print("「次へ」リンクのクリックに失敗しました")
                    break

                user_index += 1

                # 安全リミット
                if user_index > MAX_USERS:
                    print(f"警告: 利用者数が{MAX_USERS}を超えたため停止")
                    break

            print(f"\n=== Block 1: 完了 ===")
            print(f"成功: {result['success']}（新規: {result['details']['new']} / 上書き: {result['details']['overwritten']}）")
            print(f"スキップ: {result['skipped']}（パターン未設定）")
            print(f"失敗: {result['failed']} / 合計: {result['total']}")

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
