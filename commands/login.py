"""
ログインコマンド

カイポケにログインし、セッション状態を state.json に保存します。
他のコマンド実行前に一度実行しておくと、以降はログインをスキップできます。

使い方:
    python main.py login
    python main.py login --headed
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
    save_artifacts,
)


def run_login(headless: bool = True) -> dict:
    """
    カイポケにログインしてセッション状態を保存

    Args:
        headless: ヘッドレスモードで実行するか

    Returns:
        dict: 実行結果 {success: bool}
    """
    result = {"success": False}

    with sync_playwright() as p:
        # use_state=False で新規ログイン
        browser, context, page = create_browser_context(p, headless=headless, use_state=False)

        try:
            print("=== ログイン開始 ===")

            # ログイン実行（state.json に保存）
            login(page, save_state=True, context=context)
            page.wait_for_timeout(1000)

            # ポップアップを閉じる
            dismiss_popup(page)

            # レセプト画面に遷移
            goto_receipt(page)

            # 訪問看護/よりより画面に遷移
            goto_yoriyori(page)

            print("\n=== ログイン完了 ===")
            print("state.json にセッション状態を保存しました。")
            print("以降のコマンドでは自動的にログイン状態が復元されます。")

            # 5秒待機（目視確認用）
            print("5秒後にブラウザを閉じます...")
            page.wait_for_timeout(5000)

            result["success"] = True

        except Exception as e:
            print(f"ログインに失敗しました: {e}")
            save_artifacts(page, Path("artifacts"), "login_error")
            raise

        finally:
            context.close()
            browser.close()

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ログイン")
    parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    args = parser.parse_args()

    run_login(headless=not args.headed)
