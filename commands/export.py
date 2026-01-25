"""
Block 2: CSV出力 (export コマンド)

月間スケジュールをCSV形式でエクスポートします。

使い方:
    python main.py export --month 2026-04 --out data/current_202604.csv
    python main.py export --month 2026-04 --out data/current_202604.csv --headed
"""

import sys
import os
from pathlib import Path
from datetime import datetime

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from lib.common import (
    create_browser_context,
    login,
    dismiss_popup,
    goto_receipt,
    goto_yoriyori,
    goto_export_page,
    set_service_month,
    save_artifacts,
)


def wait_for_download(page, download_dir: Path, timeout: int = 30000) -> Path | None:
    """
    ダウンロードを待機してファイルパスを返す

    Args:
        page: Playwrightのページオブジェクト
        download_dir: ダウンロード先ディレクトリ
        timeout: タイムアウト（ミリ秒）

    Returns:
        Path: ダウンロードされたファイルのパス、または None
    """
    download_dir.mkdir(parents=True, exist_ok=True)

    try:
        with page.expect_download(timeout=timeout) as download_info:
            # CSVダウンロードボタンをクリック
            download_button = page.locator("text=CSV出力")
            if download_button.is_visible():
                download_button.click()
            else:
                # 別のセレクタを試す
                page.click("button:has-text('ダウンロード')")

        download = download_info.value
        # ダウンロード先に保存
        downloaded_path = download_dir / download.suggested_filename
        download.save_as(str(downloaded_path))

        return downloaded_path

    except Exception as e:
        print(f"ダウンロードに失敗しました: {e}")
        return None


def run_export(
    month: str = "2026-04",
    out_path: str = None,
    headless: bool = True
) -> dict:
    """
    Block 2: 月間スケジュールをCSVでエクスポート

    Args:
        month: 対象月（"2026-04" 形式）
        out_path: 出力ファイルパス（省略時は自動生成）
        headless: ヘッドレスモードで実行するか

    Returns:
        dict: 実行結果 {success: bool, file_path: str}
    """
    if out_path is None:
        month_str = month.replace("-", "")
        out_path = f"data/current_{month_str}.csv"

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    result = {"success": False, "file_path": str(out_file)}

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)

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

            # 各種情報出力画面に遷移
            goto_export_page(page)

            # サービス提供月を設定（4月固定）
            set_service_month(page, month)
            page.wait_for_timeout(1000)

            print(f"\n=== Block 2: CSV出力開始 ===")
            print(f"対象月: {month}")
            print(f"出力先: {out_path}")
            print("")

            # スケジュール情報の出力オプションを選択
            # （画面によって操作が異なる可能性があるため、複数パターンを試す）

            # CSV出力ボタンをクリック
            downloaded_file = wait_for_download(page, out_file.parent)

            if downloaded_file:
                # ダウンロードしたファイルを指定のパスに移動/リネーム
                if downloaded_file != out_file:
                    import shutil
                    shutil.move(str(downloaded_file), str(out_file))

                result["success"] = True
                print(f"CSV出力完了: {out_file}")
            else:
                print("CSV出力に失敗しました")
                save_artifacts(page, Path("artifacts"), "export_error")

            print(f"\n=== Block 2: 完了 ===")

        except Exception as e:
            print(f"エラーが発生しました: {e}")
            save_artifacts(page, Path("artifacts"), "export_error")
            raise

        finally:
            context.close()
            browser.close()

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Block 2: CSV出力")
    parser.add_argument("--month", default="2026-04", help="対象月 (デフォルト: 2026-04)")
    parser.add_argument("--out", default=None, help="出力ファイルパス")
    parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    args = parser.parse_args()

    # 4月以外は警告
    if not args.month.endswith("-04"):
        print("警告: 4月以外の月が指定されています！")
        confirm = input("続行しますか？ (y/N): ")
        if confirm.lower() != "y":
            print("キャンセルしました")
            sys.exit(0)

    run_export(month=args.month, out_path=args.out, headless=not args.headed)
