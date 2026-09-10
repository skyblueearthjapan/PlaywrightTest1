"""
Block 2: CSV出力 (export コマンド)

月間スケジュールをCSV形式でエクスポートします。

使い方:
    python main.py export --month 2026-04 --out data/current_202604.csv
    python main.py export --month 2026-04 --out data/current_202604.csv --headed
    python main.py export --month 2026-04 --division actual   # 実績CSV

ナビゲーションフロー:
    1. ログイン
    2. レセプト
    3. 訪問看護/1260192047
    4. 上部ナビゲーション「各種情報出力▼」→「出力対象選択」
    5. 「スケジュール表」をクリック
    6. サービス提供年月を設定（令和8年4月）
    7. 予定/実績ラジオを選択（division="actual" のときのみ実績を選ぶ）
    8. CSV出力ボタンをクリック

予定と実績はファイルを分ける（実績が予定ファイルを上書きしない）:
    予定: data/current_202604.csv
    実績: data/current_202604_actual.csv
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


# 予定/実績ラジオ（input[name=planAchievementsDivision]）
PLAN_RADIO_SELECTOR = "#planAchievementsDivision01"    # 予定（既定）
ACTUAL_RADIO_SELECTOR = "#planAchievementsDivision02"  # 実績

VALID_DIVISIONS = ("plan", "actual")

ACTUAL_RADIO_ERROR = "実績ラジオが見つかりません/選択できません"

# 実績ラジオの操作タイムアウト（既定の30秒は同期exportの予算を超えるため短縮）
ACTUAL_RADIO_TIMEOUT_MS = 10000

# ダウンロード一時置き場（data/ 直下に落として既存CSVを踏むのを防ぐ）
DOWNLOAD_SUBDIR = ".download"


def _save_artifacts_quietly(page, name: str) -> None:
    """スクショ保存は失敗しても本処理を止めない"""
    try:
        save_artifacts(page, Path("artifacts"), name)
    except Exception:
        pass


def validate_division(division: str) -> str:
    """division が "plan" / "actual" のいずれかであることを検証する。

    Raises:
        ValueError: 不正な値のとき
    """
    if division not in VALID_DIVISIONS:
        raise ValueError(
            f"division は {' または '.join(VALID_DIVISIONS)} を指定してください (received={division!r})"
        )
    return division


def default_export_path(month: str, division: str = "plan") -> Path:
    """出力先の既定パスを返す（純粋関数・単体テスト対象）。

    予定: data/current_{YYYYMM}.csv
    実績: data/current_{YYYYMM}_actual.csv
    """
    validate_division(division)
    month_str = month.replace("-", "")
    suffix = "_actual" if division == "actual" else ""
    return Path("data") / f"current_{month_str}{suffix}.csv"


def drive_filename(month: str, division: str = "plan") -> str:
    """Google Drive 上のファイル名を返す（純粋関数・単体テスト対象）。"""
    validate_division(division)
    month_str = month.replace("-", "")
    suffix = "_actual" if division == "actual" else ""
    return f"current_{month_str}{suffix}.csv"


def select_division_radio(page, division: str) -> bool:
    """予定/実績ラジオを選択する。

    division="actual" のときは実績ラジオを check し、実際に選択されたことを検証する。
    検証できない場合は False を返す（呼び出し側は CSV出力ボタンを押してはならない）。
    division="plan" のときは画面に一切触れない（画面既定が予定のため。従来と同じ挙動）。
    """
    validate_division(division)

    # 予定（既定）: 画面は既定で予定が選択済み。触らない＝待ち時間ゼロで従来どおり。
    if division != "actual":
        return True

    print("予定/実績: 実績を選択しています...")
    try:
        page.check(ACTUAL_RADIO_SELECTOR, timeout=ACTUAL_RADIO_TIMEOUT_MS)
        checked = page.is_checked(ACTUAL_RADIO_SELECTOR)
    except Exception as e:
        print(f"実績ラジオの選択に失敗: {e}")
        _save_artifacts_quietly(page, "export_division_error")
        return False

    if not checked:
        print("実績ラジオが選択状態になりませんでした")
        _save_artifacts_quietly(page, "export_division_error")
        return False

    print("予定/実績: 実績を選択しました")
    return True


def is_division_selected(page, division: str) -> bool:
    """CSV出力ボタンを押す直前の再確認。

    実績のときだけ検査する（予定は画面既定なので検査対象外）。
    postback などでラジオが戻っていた場合に予定データを実績名で保存するのを防ぐ。
    """
    if division != "actual":
        return True

    try:
        return bool(page.is_checked(ACTUAL_RADIO_SELECTOR))
    except Exception as e:
        print(f"実績ラジオの再確認に失敗: {e}")
        return False


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
    division: str = "plan",
) -> dict:
    """
    Block 2: 月間スケジュールをCSVでエクスポート

    Args:
        month: 対象月（"2026-04" 形式）
        out_path: 出力ファイルパス（省略時は自動生成）
        headless: ヘッドレスモードで実行するか
        upload_to_drive: Google Driveにアップロードするか
        drive_folder_id: アップロード先のDriveフォルダID
        division: "plan"（予定・既定）または "actual"（実績）

    Returns:
        dict: 実行結果 {success: bool, file_path: str, drive_file_id: str, division: str}

    Raises:
        ValueError: division が不正なとき
    """
    validate_division(division)

    if out_path is None:
        out_path = str(default_export_path(month, division))

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "success": False,
        "file_path": str(out_file),
        "drive_file_id": None,
        "division": division,
    }

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

            # 予定/実績ラジオを選択（年月設定のpostback後に行う）
            # 実績が選べない場合は CSV出力ボタンを押さない
            if not select_division_radio(page, division):
                print(f"CSV出力を中止しました: {ACTUAL_RADIO_ERROR}")
                result["error"] = ACTUAL_RADIO_ERROR
                return result

            print(f"\n=== Block 2: CSV出力開始 ===")
            print(f"対象月: {month}")
            print(f"division: {division}")
            print(f"出力先: {out_path}")
            print("")

            # ダウンロード直前の再確認（遅れて来たpostbackで予定に戻っていないか）
            if not is_division_selected(page, division):
                print(f"CSV出力を中止しました: {ACTUAL_RADIO_ERROR}")
                _save_artifacts_quietly(page, "export_division_error")
                result["error"] = ACTUAL_RADIO_ERROR
                return result

            # CSV出力ボタンをクリック（一時ディレクトリに受けてから本来の名前へ移動）
            download_dir = out_file.parent / DOWNLOAD_SUBDIR
            downloaded_file = click_csv_export_button(page, download_dir)

            if downloaded_file:
                # ダウンロードしたファイルを指定のパスに移動/リネーム
                if downloaded_file != out_file:
                    import shutil
                    shutil.move(str(downloaded_file), str(out_file))
                    try:
                        download_dir.rmdir()  # 空のときだけ消える
                    except OSError:
                        pass

                result["success"] = True
                print(f"CSV出力完了 division={division} path={out_file}")

                # Google Driveにアップロード
                if upload_to_drive and drive_folder_id:
                    print(f"\nGoogle Driveにアップロード中...")
                    try:
                        from lib.google_drive import upload_to_drive as drive_upload
                        drive_name = drive_filename(month, division)
                        file_id = drive_upload(
                            str(out_file),
                            drive_folder_id,
                            filename=drive_name
                        )
                        if file_id:
                            result["drive_file_id"] = file_id
                            print(f"Driveアップロード完了: {drive_name}")
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
    parser.add_argument(
        "--division",
        default="plan",
        choices=list(VALID_DIVISIONS),
        help="予定(plan・既定) / 実績(actual)",
    )
    args = parser.parse_args()

    # 4月以外は警告
    if not args.month.endswith("-04"):
        print("警告: 4月以外の月が指定されています！")
        confirm = input("続行しますか？ (y/N): ")
        if confirm.lower() != "y":
            print("キャンセルしました")
            sys.exit(0)

    run_export(
        month=args.month,
        out_path=args.out,
        headless=not args.headed,
        division=args.division,
    )
