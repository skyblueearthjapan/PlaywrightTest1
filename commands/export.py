"""
Block 2: CSV出力 (export コマンド)

月間スケジュールをCSV形式でエクスポートします。

使い方:
    python main.py export --month 2026-04 --out data/current_202604.csv
    python main.py export --month 2026-04 --out data/current_202604.csv --headed

ナビゲーションフロー:
    1. ログイン
    2. レセプト
    3. 訪問看護/1260192047
    4. 上部ナビゲーション「各種情報出力▼」→「出力対象選択」
    5. 「スケジュール表」をクリック
    6. サービス提供年月を設定（令和8年4月）
    7. CSV出力ボタンをクリック
"""

import sys
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from lib.common import (
    create_browser_context,
    setup_yoriyori_page,
    goto_export_page,
    parse_month,
    to_reiwa,
    save_artifacts,
)


def click_schedule_table(page) -> bool:
    """
    出力対象選択画面で「スケジュール表」をクリック

    Returns:
        bool: 成功したかどうか
    """
    print("スケジュール表をクリックしています...")

    try:
        # 「スケジュール表」リンクをクリック
        # DevToolsで見ると span#schedule_tooltip がある
        schedule_link = page.locator("text=スケジュール表").first
        if schedule_link.is_visible():
            schedule_link.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)
            print("スケジュール表の設定画面を表示しました")
            return True

        # 別のセレクタを試す
        page.click("a:has-text('スケジュール表')")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)
        print("スケジュール表の設定画面を表示しました")
        return True

    except Exception as e:
        print(f"スケジュール表のクリックに失敗: {e}")
        return False


def set_export_month(page, month: str) -> bool:
    """
    訪問看護スケジュール表の設定画面でサービス提供年月を設定

    Args:
        page: Playwrightのページオブジェクト
        month: 対象月（"2026-04" 形式）

    Returns:
        bool: 成功したかどうか
    """
    year, month_num = parse_month(month)
    reiwa_year = to_reiwa(year)

    print(f"サービス提供年月を設定: 令和{reiwa_year}年{month_num}月")

    try:
        # サービス提供年月は3つのセレクトボックス（令和、年、月）
        # DevToolsから見ると、年のセレクタは dateMonthKing 付近にある

        # 年のプルダウンを探して設定
        # 「8」を選択（令和8年）
        year_selects = page.locator("select").all()
        year_set = False
        month_set = False

        for select in year_selects:
            try:
                # オプションの内容を確認
                options = select.locator("option").all_text_contents()

                # 年のセレクトボックス（1〜数値が並んでいる）
                if str(reiwa_year) in options and not year_set:
                    select.select_option(value=str(reiwa_year))
                    print(f"  年を選択: {reiwa_year}")
                    year_set = True
                    page.wait_for_timeout(300)
                    continue

                # 月のセレクトボックス（1〜12が並んでいる）
                if str(month_num) in options and "1" in options and "12" in options and not month_set:
                    select.select_option(value=str(month_num))
                    print(f"  月を選択: {month_num}")
                    month_set = True
                    page.wait_for_timeout(300)
                    continue

            except Exception:
                continue

        if year_set and month_set:
            print(f"サービス提供年月の設定完了: 令和{reiwa_year}年{month_num}月")
            return True
        else:
            print(f"警告: 年月の設定が不完全（年: {year_set}, 月: {month_set}）")
            save_artifacts(page, Path("artifacts"), "export_month_select")
            return False

    except Exception as e:
        print(f"年月設定でエラー: {e}")
        save_artifacts(page, Path("artifacts"), "export_month_error")
        return False


def click_csv_export_button(page, download_dir: Path, timeout: int = 30000) -> Path | None:
    """
    CSV出力ボタンをクリックしてダウンロードを待機

    Args:
        page: Playwrightのページオブジェクト
        download_dir: ダウンロード先ディレクトリ
        timeout: タイムアウト（ミリ秒）

    Returns:
        Path: ダウンロードされたファイルのパス、または None
    """
    download_dir.mkdir(parents=True, exist_ok=True)

    print("CSV出力ボタンをクリックしています...")

    try:
        with page.expect_download(timeout=timeout) as download_info:
            # CSV出力ボタンをクリック
            # DevToolsから見ると a.btn.btn-sms.ie_btn.pull-right で onclick="exportCsv();"
            csv_button = page.locator("text=CSV出力").first
            if csv_button.is_visible():
                csv_button.click()
            else:
                # 別のセレクタを試す
                page.click("a:has-text('CSV出力')")

        download = download_info.value
        # ダウンロード先に保存
        downloaded_path = download_dir / download.suggested_filename
        download.save_as(str(downloaded_path))

        print(f"CSVダウンロード完了: {downloaded_path}")
        return downloaded_path

    except Exception as e:
        print(f"CSVダウンロードに失敗: {e}")
        save_artifacts(page, Path("artifacts"), "export_download_error")
        return None


def run_export(
    month: str = "2026-04",
    out_path: str = None,
    headless: bool = True,
    upload_to_drive: bool = False,
    drive_folder_id: str = None,
) -> dict:
    """
    Block 2: 月間スケジュールをCSVでエクスポート

    Args:
        month: 対象月（"2026-04" 形式）
        out_path: 出力ファイルパス（省略時は自動生成）
        headless: ヘッドレスモードで実行するか
        upload_to_drive: Google Driveにアップロードするか
        drive_folder_id: アップロード先のDriveフォルダID

    Returns:
        dict: 実行結果 {success: bool, file_path: str, drive_file_id: str}
    """
    if out_path is None:
        month_str = month.replace("-", "")
        out_path = f"data/current_{month_str}.csv"

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    result = {"success": False, "file_path": str(out_file), "drive_file_id": None}

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)

        try:
            # ログイン → レセプト → 訪問看護（検証・リトライ付き）
            setup_yoriyori_page(page, context)

            # 出力対象選択画面に遷移（上部ナビゲーションの各種情報出力▼→出力対象選択）
            goto_export_page(page)

            # スケジュール表をクリック
            if not click_schedule_table(page):
                print("スケジュール表の選択に失敗しました")
                save_artifacts(page, Path("artifacts"), "export_schedule_error")
                return result

            # サービス提供年月を設定（令和8年4月）
            if not set_export_month(page, month):
                print("年月の設定に失敗しましたが、続行します")

            page.wait_for_timeout(1000)

            print(f"\n=== Block 2: CSV出力開始 ===")
            print(f"対象月: {month}")
            print(f"出力先: {out_path}")
            print("")

            # CSV出力ボタンをクリック
            downloaded_file = click_csv_export_button(page, out_file.parent)

            if downloaded_file:
                # ダウンロードしたファイルを指定のパスに移動/リネーム
                if downloaded_file != out_file:
                    import shutil
                    shutil.move(str(downloaded_file), str(out_file))

                result["success"] = True
                print(f"CSV出力完了: {out_file}")

                # Google Driveにアップロード
                if upload_to_drive and drive_folder_id:
                    print(f"\nGoogle Driveにアップロード中...")
                    try:
                        from lib.google_drive import upload_to_drive as drive_upload
                        month_str = month.replace("-", "")
                        drive_filename = f"current_{month_str}.csv"
                        file_id = drive_upload(
                            str(out_file),
                            drive_folder_id,
                            filename=drive_filename
                        )
                        if file_id:
                            result["drive_file_id"] = file_id
                            print(f"Driveアップロード完了: {drive_filename}")
                        else:
                            print("Driveアップロードに失敗しました")
                    except Exception as e:
                        print(f"Driveアップロードエラー: {e}")
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
