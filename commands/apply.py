"""
Block 3: 差分検出→差分適用 (apply コマンド)

現在のスケジュール（current CSV）と最適化後のスケジュール（optimized CSV）を比較し、
差分をPlaywrightで自動入力します。

使い方:
    python main.py apply --month 2026-04 --week-start 2026-04-20 --current data/current.csv --optimized data/opt_week.csv
    python main.py apply --month 2026-04 --week-start 2026-04-20 --current data/current.csv --optimized data/opt_week.csv --headed
"""

import sys
import csv
from pathlib import Path
from datetime import datetime, timedelta
from typing import NamedTuple

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


class ScheduleEntry(NamedTuple):
    """
    スケジュールエントリ（カイポケCSVフォーマット）

    列構成:
        A: 職員名1, B: 職種1, C: 職員名2, D: 職種2, E: 同行2,
        F: 職員名3, G: 職種3, H: 同行3, I: 事業所名,
        J: 日付, K: 曜日, L: 利用者, M: 業務種別, N: サービス内容,
        O: 開始時間, P: 終了時間, Q: 提供時間, R: 備考
    """
    staff1_name: str       # A: 職員名1
    staff1_type: str       # B: 職種1
    staff2_name: str       # C: 職員名2
    staff2_type: str       # D: 職種2
    staff2_accompany: str  # E: 同行2
    staff3_name: str       # F: 職員名3
    staff3_type: str       # G: 職種3
    staff3_accompany: str  # H: 同行3
    facility: str          # I: 事業所名
    date: str              # J: 日付
    weekday: str           # K: 曜日
    user_name: str         # L: 利用者
    work_type: str         # M: 業務種別
    service_content: str   # N: サービス内容
    time_start: str        # O: 開始時間
    time_end: str          # P: 終了時間
    duration: str          # Q: 提供時間
    remarks: str           # R: 備考


def load_csv(file_path: str) -> list[ScheduleEntry]:
    """
    CSVファイルを読み込んでScheduleEntryのリストを返す

    CSVの想定フォーマット（カイポケ出力）:
        職員名1, 職種1, 職員名2, 職種2, 同行2, 職員名3, 職種3, 同行3,
        事業所名, 日付, 曜日, 利用者, 業務種別, サービス内容,
        開始時間, 終了時間, 提供時間, 備考
    """
    entries = []
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"CSVファイルが見つかりません: {file_path}")

    # エンコーディングを自動判定（cp932 または utf-8）
    encodings = ["cp932", "utf-8-sig", "utf-8", "shift_jis"]
    content = None
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                content = f.read()
            break
        except UnicodeDecodeError:
            continue

    if content is None:
        raise ValueError(f"CSVファイルのエンコーディングを判定できません: {file_path}")

    import io
    with io.StringIO(content) as f:
        reader = csv.reader(f)
        header = next(reader, None)  # ヘッダーをスキップ

        for row in reader:
            if len(row) >= 18:
                entries.append(ScheduleEntry(
                    staff1_name=row[0].strip(),
                    staff1_type=row[1].strip(),
                    staff2_name=row[2].strip(),
                    staff2_type=row[3].strip(),
                    staff2_accompany=row[4].strip(),
                    staff3_name=row[5].strip(),
                    staff3_type=row[6].strip(),
                    staff3_accompany=row[7].strip(),
                    facility=row[8].strip(),
                    date=row[9].strip(),
                    weekday=row[10].strip(),
                    user_name=row[11].strip(),
                    work_type=row[12].strip(),
                    service_content=row[13].strip(),
                    time_start=row[14].strip(),
                    time_end=row[15].strip(),
                    duration=row[16].strip(),
                    remarks=row[17].strip() if len(row) > 17 else "",
                ))

    return entries


def compute_diff(
    current: list[ScheduleEntry],
    optimized: list[ScheduleEntry],
    week_start: str
) -> dict:
    """
    差分を計算

    Args:
        current: 現在のスケジュール
        optimized: 最適化後のスケジュール
        week_start: 対象週の開始日（YYYY-MM-DD）

    Returns:
        dict: {
            "add": [追加すべきエントリ],
            "remove": [削除すべきエントリ],
            "modify": [(現在, 最適化後) のペア]
        }
    """
    # 対象週の範囲を計算
    start_date = datetime.strptime(week_start, "%Y-%m-%d")
    end_date = start_date + timedelta(days=6)

    # 日付を正規化する関数（日のみの場合は月を補完）
    def normalize_date(date_str: str, year_month: str) -> str:
        """
        日付を YYYY-MM-DD 形式に正規化

        Args:
            date_str: 日付文字列（"21" や "2026-04-21" など）
            year_month: 対象月（"2026-04" 形式）

        Returns:
            str: YYYY-MM-DD 形式の日付
        """
        date_str = date_str.strip()
        if "-" in date_str and len(date_str) >= 8:
            # すでに YYYY-MM-DD 形式
            return date_str
        else:
            # 日のみの場合、月を補完
            try:
                day = int(date_str)
                return f"{year_month}-{day:02d}"
            except ValueError:
                return date_str

    # week_start から年月を取得
    year_month = week_start[:7]  # "2026-04"

    # 対象週のエントリのみ抽出
    def is_in_week(entry: ScheduleEntry) -> bool:
        try:
            normalized = normalize_date(entry.date, year_month)
            entry_date = datetime.strptime(normalized, "%Y-%m-%d")
            return start_date <= entry_date <= end_date
        except ValueError:
            return False

    current_week = [e for e in current if is_in_week(e)]
    optimized_week = [e for e in optimized if is_in_week(e)]

    # キーを作成（利用者名 + 日付 + 開始時刻）
    def make_key(e: ScheduleEntry) -> str:
        normalized_date = normalize_date(e.date, year_month)
        return f"{e.user_name}_{normalized_date}_{e.time_start}"

    current_dict = {make_key(e): e for e in current_week}
    optimized_dict = {make_key(e): e for e in optimized_week}

    current_keys = set(current_dict.keys())
    optimized_keys = set(optimized_dict.keys())

    # 追加: 最適化にあって現在にない
    add_keys = optimized_keys - current_keys
    add = [optimized_dict[k] for k in add_keys]

    # 削除: 現在にあって最適化にない
    remove_keys = current_keys - optimized_keys
    remove = [current_dict[k] for k in remove_keys]

    # 変更: 両方にあるが内容が異なる
    common_keys = current_keys & optimized_keys
    modify = []
    for k in common_keys:
        c = current_dict[k]
        o = optimized_dict[k]
        if c != o:
            modify.append((c, o))

    return {
        "add": add,
        "remove": remove,
        "modify": modify,
    }


def navigate_to_user(page, user_name: str) -> bool:
    """
    指定された利用者の画面に遷移

    Returns:
        bool: 成功したかどうか
    """
    try:
        # 検索ボックスに利用者名を入力
        search_box = page.locator("input[placeholder*='検索'], input[name*='search'], .search-input").first
        if search_box.is_visible():
            search_box.fill(user_name)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1000)
            return True

        # 検索ボックスがない場合は、利用者リストから探す
        user_link = page.locator(f"text={user_name}").first
        if user_link.is_visible():
            user_link.click()
            page.wait_for_timeout(1000)
            return True

        return False

    except Exception as e:
        print(f"利用者への遷移に失敗: {e}")
        return False


def add_schedule_entry(page, entry: ScheduleEntry) -> bool:
    """
    スケジュールエントリを追加

    Returns:
        bool: 成功したかどうか
    """
    try:
        # 日付セルをクリックして追加ダイアログを開く
        date_cell = page.locator(f"[data-date='{entry.date}'], td:has-text('{entry.date}')").first
        date_cell.click()
        page.wait_for_timeout(500)

        # 開始時刻を入力
        start_input = page.locator("input[name*='start'], input[placeholder*='開始']").first
        if start_input.is_visible():
            start_input.fill(entry.time_start)

        # 終了時刻を入力
        end_input = page.locator("input[name*='end'], input[placeholder*='終了']").first
        if end_input.is_visible():
            end_input.fill(entry.time_end)

        # サービス内容を選択
        service_select = page.locator("select[name*='service']").first
        if service_select.is_visible():
            service_select.select_option(label=entry.service_content)

        # 担当者1を選択
        staff_select = page.locator("select[name*='staff']").first
        if staff_select.is_visible() and entry.staff1_name:
            staff_select.select_option(label=entry.staff1_name)

        # 保存ボタンをクリック
        save_button = page.locator("button:has-text('保存'), button:has-text('登録')").first
        if save_button.is_visible():
            save_button.click()
            page.wait_for_timeout(1000)

        return True

    except Exception as e:
        print(f"エントリ追加に失敗: {e}")
        return False


def remove_schedule_entry(page, entry: ScheduleEntry) -> bool:
    """
    スケジュールエントリを削除

    Returns:
        bool: 成功したかどうか
    """
    try:
        # 該当のスケジュールエントリをクリック
        entry_cell = page.locator(f"[data-date='{entry.date}']:has-text('{entry.time_start}')").first
        if not entry_cell.is_visible():
            entry_cell = page.locator(f"td:has-text('{entry.time_start}')").first

        entry_cell.click()
        page.wait_for_timeout(500)

        # 削除ボタンをクリック
        delete_button = page.locator("button:has-text('削除'), button:has-text('取消')").first
        if delete_button.is_visible():
            delete_button.click()
            page.wait_for_timeout(500)

            # 確認ダイアログがあれば「OK」をクリック
            ok_button = page.locator("button:has-text('OK'), button:has-text('はい')").first
            if ok_button.is_visible():
                ok_button.click()
                page.wait_for_timeout(1000)

        return True

    except Exception as e:
        print(f"エントリ削除に失敗: {e}")
        return False


def run_apply(
    month: str = "2026-04",
    week_start: str = None,
    current_csv: str = None,
    optimized_csv: str = None,
    headless: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    Block 3: 差分を検出して適用

    Args:
        month: 対象月（"2026-04" 形式）
        week_start: 対象週の開始日（"2026-04-20" 形式）
        current_csv: 現在のスケジュールCSVパス
        optimized_csv: 最適化後のスケジュールCSVパス
        headless: ヘッドレスモードで実行するか
        dry_run: テスト実行（差分計算のみ、実際の適用は行わない）

    Returns:
        dict: 実行結果
    """
    if not current_csv or not optimized_csv:
        raise ValueError("--current と --optimized は必須です")

    if not week_start:
        raise ValueError("--week-start は必須です")

    result = {
        "success": False,
        "diff": {"add": 0, "remove": 0, "modify": 0},
        "applied": {"add": 0, "remove": 0, "modify": 0},
    }

    # CSVを読み込み
    print(f"CSVを読み込んでいます...")
    print(f"  現在: {current_csv}")
    print(f"  最適化後: {optimized_csv}")

    current = load_csv(current_csv)
    optimized = load_csv(optimized_csv)

    print(f"  現在のエントリ数: {len(current)}")
    print(f"  最適化後のエントリ数: {len(optimized)}")

    # 差分を計算
    print(f"\n差分を計算しています（対象週: {week_start}～）...")
    diff = compute_diff(current, optimized, week_start)

    result["diff"]["add"] = len(diff["add"])
    result["diff"]["remove"] = len(diff["remove"])
    result["diff"]["modify"] = len(diff["modify"])

    print(f"\n=== Block 3: 差分検出結果 ===")
    print(f"追加: {len(diff['add'])} 件")
    print(f"削除: {len(diff['remove'])} 件")
    print(f"変更: {len(diff['modify'])} 件")

    if dry_run:
        print("\n(dry-run: 差分計算のみ、適用はスキップ)")
        result["success"] = True
        return result

    if len(diff["add"]) == 0 and len(diff["remove"]) == 0 and len(diff["modify"]) == 0:
        print("\n差分がありません。処理を終了します。")
        result["success"] = True
        return result

    # 差分を適用
    print(f"\n=== Block 3: 差分適用開始 ===")

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

            # 月間スケジュール管理画面に遷移
            goto_monthly_schedule(page)

            # サービス提供月を設定（4月固定）
            set_service_month(page, month)
            page.wait_for_timeout(1000)

            # 削除を実行
            for entry in diff["remove"]:
                print(f"削除: {entry.user_name} {entry.date} {entry.time_start}")
                if navigate_to_user(page, entry.user_name):
                    if remove_schedule_entry(page, entry):
                        result["applied"]["remove"] += 1
                        print("  → 削除完了")
                    else:
                        print("  → 削除失敗")

            # 追加を実行
            for entry in diff["add"]:
                print(f"追加: {entry.user_name} {entry.date} {entry.time_start}")
                if navigate_to_user(page, entry.user_name):
                    if add_schedule_entry(page, entry):
                        result["applied"]["add"] += 1
                        print("  → 追加完了")
                    else:
                        print("  → 追加失敗")

            # 変更を実行（削除してから追加）
            for current_entry, optimized_entry in diff["modify"]:
                print(f"変更: {current_entry.user_name} {current_entry.date} {current_entry.time_start}")
                if navigate_to_user(page, current_entry.user_name):
                    if remove_schedule_entry(page, current_entry):
                        if add_schedule_entry(page, optimized_entry):
                            result["applied"]["modify"] += 1
                            print("  → 変更完了")
                        else:
                            print("  → 変更失敗（追加時）")
                    else:
                        print("  → 変更失敗（削除時）")

            result["success"] = True
            print(f"\n=== Block 3: 完了 ===")
            print(f"適用結果:")
            print(f"  追加: {result['applied']['add']} / {result['diff']['add']} 件")
            print(f"  削除: {result['applied']['remove']} / {result['diff']['remove']} 件")
            print(f"  変更: {result['applied']['modify']} / {result['diff']['modify']} 件")

        except Exception as e:
            print(f"エラーが発生しました: {e}")
            save_artifacts(page, Path("artifacts"), "apply_error")
            raise

        finally:
            context.close()
            browser.close()

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Block 3: 差分適用")
    parser.add_argument("--month", default="2026-04", help="対象月 (デフォルト: 2026-04)")
    parser.add_argument("--week-start", required=True, help="対象週の開始日 (例: 2026-04-20)")
    parser.add_argument("--current", required=True, help="現在のスケジュールCSVパス")
    parser.add_argument("--optimized", required=True, help="最適化後のスケジュールCSVパス")
    parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    parser.add_argument("--dry-run", action="store_true", help="テスト実行（差分計算のみ）")
    args = parser.parse_args()

    # 4月以外は警告
    if not args.month.endswith("-04"):
        print("警告: 4月以外の月が指定されています！")
        confirm = input("続行しますか？ (y/N): ")
        if confirm.lower() != "y":
            print("キャンセルしました")
            sys.exit(0)

    run_apply(
        month=args.month,
        week_start=args.week_start,
        current_csv=args.current,
        optimized_csv=args.optimized,
        headless=not args.headed,
        dry_run=args.dry_run,
    )
