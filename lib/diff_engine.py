"""
差分比較エンジン - 2つのCSVを比較して修正シートを生成

使い方:
    from lib.diff_engine import compare_schedules, generate_correction_sheet

    corrections = compare_schedules(
        current_csv="data/current_202604.csv",
        optimized_csv="data/optimized_202604.csv"
    )
    generate_correction_sheet(corrections, "data/correction_sheet.json")
"""

import csv
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class ScheduleEntry:
    """スケジュールエントリ（1行分のデータ）"""
    user_name: str          # 利用者名
    date: str               # 日付（"1", "3" など）
    weekday: str            # 曜日
    business_type: str      # 業務種別（"医療保険", "介護保険", or イベント名）
    service_type: str       # サービス内容
    start_time: str         # 開始時間 (HH:MM)
    end_time: str           # 終了時間 (HH:MM)
    staff1_name: str        # 職員1名
    staff1_type: str        # 職員1職種
    staff2_name: str = ""   # 職員2名
    staff2_type: str = ""   # 職員2職種
    staff3_name: str = ""   # 職員3名
    staff3_type: str = ""   # 職員3職種
    remarks: str = ""       # 備考

    def get_key(self) -> str:
        """利用者+日付+業務種別+サービス種別でユニークキーを生成"""
        return f"{self.user_name}|{self.date}|{self.business_type}|{self.service_type}"

    def get_time_key(self) -> str:
        """利用者+日付+開始時間でユニークキーを生成（時間変更の検出用）"""
        return f"{self.user_name}|{self.date}|{self.start_time}"

    def is_medical_insurance(self) -> bool:
        """医療保険かどうか"""
        return self.business_type == "医療保険"

    def is_nursing_insurance(self) -> bool:
        """介護保険かどうか"""
        return self.business_type == "介護保険"

    def is_event(self) -> bool:
        """イベント（業務種別が保険以外）かどうか"""
        return self.business_type not in ("医療保険", "介護保険", "")


@dataclass
class Correction:
    """修正1件分のデータ"""
    user_name: str          # 利用者名
    date_from: str          # 変更前の日付
    date_to: str            # 変更後の日付
    start_time_from: str    # 変更前の開始時間
    start_time_to: str      # 変更後の開始時間
    end_time_from: str      # 変更前の終了時間
    end_time_to: str        # 変更後の終了時間
    staff1_from: str        # 変更前の職員1
    staff1_to: str          # 変更後の職員1
    staff2_from: str        # 変更前の職員2
    staff2_to: str          # 変更後の職員2（削除の場合は空文字）
    service_type: str       # サービス内容
    action: str             # "edit" or "delete" or "add" or "date_change"
    business_type: str = "" # 業務種別（"医療保険", "介護保険", or イベント名）
    remarks: str = ""       # 備考（イベント名等）

    def has_date_change(self) -> bool:
        return self.date_from != self.date_to

    def has_time_change(self) -> bool:
        return (self.start_time_from != self.start_time_to or
                self.end_time_from != self.end_time_to)

    def has_staff_change(self) -> bool:
        return (self.staff1_from != self.staff1_to or
                self.staff2_from != self.staff2_to)

    def is_medical_insurance(self) -> bool:
        """医療保険かどうか"""
        return self.business_type == "医療保険"

    def is_nursing_insurance(self) -> bool:
        """介護保険かどうか"""
        return self.business_type == "介護保険"

    def is_event(self) -> bool:
        """イベント（業務種別が保険以外）かどうか"""
        return self.business_type not in ("医療保険", "介護保険", "")

    def is_schedule(self) -> bool:
        """通常のスケジュール（利用者別タブで操作）かどうか"""
        return self.business_type in ("医療保険", "介護保険", "")


def _extract_day_of_month(value: str) -> Optional[int]:
    """Return day-of-month (1-31) from a date string, or None on failure.

    Accepts plain day numbers ("1", "03"), zero-padded days, ``MM/dd``,
    ``yyyy/MM/dd``, ``yyyy-MM-dd``, ``yyyy.MM.dd`` and any whitespace
    surrounding. Used by ``compare_schedules`` to filter by week range
    (C-10) without crashing on month-spanning data.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Direct integer day (e.g. "1", "03")
    if s.isdigit():
        try:
            d = int(s)
            return d if 1 <= d <= 31 else None
        except ValueError:
            return None
    # Split by common separators
    for sep in ("/", "-", ".", " "):
        if sep in s:
            parts = [p for p in s.split(sep) if p.strip()]
            if not parts:
                continue
            # Last numeric part is the day in yyyy/MM/dd, MM/dd, or dd alone
            for candidate in reversed(parts):
                c = candidate.strip()
                if c.isdigit():
                    try:
                        d = int(c)
                    except ValueError:
                        continue
                    if 1 <= d <= 31:
                        return d
            return None
    return None


def parse_time(time_str: str) -> tuple[int, int]:
    """時間文字列をパース ("HH:MM" or "H:MM" -> (hour, minute))"""
    if not time_str or time_str == "-":
        return (0, 0)
    parts = time_str.replace("：", ":").split(":")
    if len(parts) >= 2:
        return (int(parts[0]), int(parts[1]))
    return (0, 0)


def format_time(hour: int, minute: int) -> str:
    """時間をフォーマット"""
    return f"{hour:02d}:{minute:02d}"


def read_csv_auto_encoding(file_path: str) -> list[list[str]]:
    """CSVファイルを自動エンコーディング検出で読み込む"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"CSVファイルが見つかりません: {file_path}")

    encodings = ["utf-8-sig", "utf-8", "cp932", "shift_jis"]
    content = None
    used_encoding = None

    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                content = f.read()
            used_encoding = enc
            break
        except UnicodeDecodeError:
            continue

    if content is None:
        raise ValueError(f"CSVファイルのエンコーディングを判定できません: {file_path}")

    print(f"  [DEBUG] ファイル読み込み: {path.name} (encoding={used_encoding}, size={len(content)}文字)")

    import io
    rows = []
    with io.StringIO(content) as f:
        reader = csv.reader(f)
        for row in reader:
            rows.append(row)

    print(f"  [DEBUG] 行数: {len(rows)} (ヘッダー含む)")
    if rows:
        print(f"  [DEBUG] ヘッダー列数: {len(rows[0])}")
        if rows[0]:
            print(f"  [DEBUG] ヘッダー先頭: '{rows[0][0]}'")
    if len(rows) > 1:
        print(f"  [DEBUG] データ1行目列数: {len(rows[1])}")

    return rows


def parse_kaipoke_csv(file_path: str) -> list[ScheduleEntry]:
    """
    カイポケ出力CSVをパース

    CSVフォーマット（18列）:
    職員名1, 職種1, 職員名2, 職種2, 同行2, 職員名3, 職種3, 同行3,
    事業所名, 日付, 曜日, 利用者, 業務種別, サービス内容,
    開始時間, 終了時間, 提供時間, 備考
    """
    rows = read_csv_auto_encoding(file_path)
    entries = []
    skipped_rows = 0

    # ヘッダーをスキップ
    for row in rows[1:]:
        if len(row) >= 18:
            entries.append(ScheduleEntry(
                staff1_name=row[0].strip(),
                staff1_type=row[1].strip(),
                staff2_name=row[2].strip(),
                staff2_type=row[3].strip(),
                staff3_name=row[5].strip(),
                staff3_type=row[6].strip(),
                user_name=row[11].strip(),
                date=row[9].strip(),
                weekday=row[10].strip(),
                business_type=row[12].strip(),
                service_type=row[13].strip(),
                start_time=row[14].strip(),
                end_time=row[15].strip(),
                remarks=row[17].strip() if len(row) > 17 else "",
            ))
        else:
            skipped_rows += 1

    print(f"  [DEBUG] parse_kaipoke_csv: {len(entries)}件パース, {skipped_rows}行スキップ（列数不足）")
    if entries:
        e = entries[0]
        print(f"  [DEBUG]   先頭エントリ: 利用者='{e.user_name}', 日付='{e.date}', "
              f"業務種別='{e.business_type}', サービス='{e.service_type}', "
              f"時間='{e.start_time}-{e.end_time}', "
              f"職員1='{e.staff1_name}', 職員2='{e.staff2_name}'")
    if len(entries) > 1:
        e = entries[1]
        print(f"  [DEBUG]   2番目エントリ: 利用者='{e.user_name}', 日付='{e.date}', "
              f"業務種別='{e.business_type}', サービス='{e.service_type}', "
              f"時間='{e.start_time}-{e.end_time}', "
              f"職員1='{e.staff1_name}', 職員2='{e.staff2_name}'")

    return entries


def parse_optimized_csv(file_path: str) -> list[ScheduleEntry]:
    """
    最適化CSVをパース

    CSVフォーマット（想定）:
    利用者名, 日付, 曜日, サービス内容, 開始時間, 終了時間, 職員1, 職員2, 備考
    または カイポケと同じフォーマット
    """
    rows = read_csv_auto_encoding(file_path)
    entries = []

    if not rows:
        print(f"  [DEBUG] parse_optimized_csv: ファイルが空です")
        return entries

    # ヘッダーを確認してフォーマットを判定
    header = rows[0] if rows else []
    print(f"  [DEBUG] parse_optimized_csv: ヘッダー列数={len(header)}, header[0]='{header[0] if header else 'N/A'}'")

    # カイポケフォーマット（18列）の場合
    if len(header) >= 18 and "職員名" in str(header[0]):
        print(f"  [DEBUG] parse_optimized_csv: カイポケフォーマットとして検出 → parse_kaipoke_csvに委譲")
        return parse_kaipoke_csv(file_path)

    print(f"  [DEBUG] parse_optimized_csv: 簡易フォーマットとしてパース")
    # 簡易フォーマット（利用者, 日付, 曜日, サービス, 開始, 終了, 職員1, 職員2, 備考）
    for row in rows[1:]:
        if len(row) >= 6:
            entries.append(ScheduleEntry(
                user_name=row[0].strip(),
                date=row[1].strip(),
                weekday=row[2].strip() if len(row) > 2 else "",
                business_type="",
                service_type=row[3].strip() if len(row) > 3 else "",
                start_time=row[4].strip() if len(row) > 4 else "",
                end_time=row[5].strip() if len(row) > 5 else "",
                staff1_name=row[6].strip() if len(row) > 6 else "",
                staff1_type="",
                staff2_name=row[7].strip() if len(row) > 7 else "",
                staff2_type="",
                remarks=row[8].strip() if len(row) > 8 else "",
            ))

    print(f"  [DEBUG] parse_optimized_csv: {len(entries)}件パース（簡易フォーマット）")
    return entries


def compare_schedules(
    current_csv: str,
    optimized_csv: str,
    target_users: list[str] = None,
    target_week_start: int = None,
    target_week_end: int = None,
) -> list[Correction]:
    """
    2つのCSVを比較して差分（修正リスト）を生成

    マッチングロジック:
    - CSVの行順は関係ない（順不同OK）
    - 利用者名 + 日付 + サービス内容 でグループ化
    - 同じ利用者・日付のエントリは開始時間でマッチング

    Args:
        current_csv: 現在のカイポケスケジュールCSV
        optimized_csv: 最適化後のスケジュールCSV
        target_users: 対象利用者のリスト（Noneの場合は全員）
        target_week_start: 対象週の開始日（1-31）
        target_week_end: 対象週の終了日（1-31）

    Returns:
        list[Correction]: 修正リスト
    """
    print(f"CSVを読み込んでいます...")
    print(f"  現在のCSV: {current_csv}")
    print(f"  最適化CSV: {optimized_csv}")

    current_entries = parse_kaipoke_csv(current_csv)
    optimized_entries = parse_optimized_csv(optimized_csv)

    print(f"  現在のエントリ数: {len(current_entries)}")
    print(f"  最適化エントリ数: {len(optimized_entries)}")

    # 対象をフィルタリング
    if target_users:
        current_entries = [e for e in current_entries if e.user_name in target_users]
        optimized_entries = [e for e in optimized_entries if e.user_name in target_users]
        print(f"  [DEBUG] ユーザーフィルタ後: current={len(current_entries)}, optimized={len(optimized_entries)}")

    if target_week_start and target_week_end:
        print(f"  [DEBUG] 日付フィルタ: {target_week_start} ～ {target_week_end}")
        # フィルタ前のdate値をデバッグ出力
        current_dates_before = set(e.date for e in current_entries)
        optimized_dates_before = set(e.date for e in optimized_entries)
        print(f"  [DEBUG] フィルタ前 current の日付一覧: {sorted(current_dates_before)}")
        print(f"  [DEBUG] フィルタ前 optimized の日付一覧: {sorted(optimized_dates_before)}")

        def in_range(entry):
            # Bug fix (C-10): ``int(entry.date)`` blew up when the CSV
            # contained ``yyyy/MM/dd`` or ``MM/dd`` strings, and silently
            # filtered out month-spanning entries. Normalise by extracting
            # only the day component.
            day = _extract_day_of_month(entry.date)
            if day is None:
                print(f"  [DEBUG] 日付パース失敗: '{entry.date}' (利用者={entry.user_name})")
                return False
            return target_week_start <= day <= target_week_end
        current_entries = [e for e in current_entries if in_range(e)]
        optimized_entries = [e for e in optimized_entries if in_range(e)]
        print(f"  [DEBUG] 日付フィルタ後: current={len(current_entries)}, optimized={len(optimized_entries)}")
    else:
        print(f"  [DEBUG] 日付フィルタなし (week_start={target_week_start}, week_end={target_week_end})")

    # 現在のエントリをキーでインデックス化
    current_by_key = {}
    for entry in current_entries:
        key = entry.get_key()
        if key not in current_by_key:
            current_by_key[key] = []
        current_by_key[key].append(entry)

    # 最適化エントリをキーでインデックス化
    optimized_by_key = {}
    for entry in optimized_entries:
        key = entry.get_key()
        if key not in optimized_by_key:
            optimized_by_key[key] = []
        optimized_by_key[key].append(entry)

    corrections = []

    # 利用者ごとにグループ化して比較
    all_users = set(e.user_name for e in current_entries + optimized_entries)
    print(f"\n対象利用者数: {len(all_users)}")

    for user in sorted(all_users):
        user_current = [e for e in current_entries if e.user_name == user]
        user_optimized = [e for e in optimized_entries if e.user_name == user]

        print(f"\n  [DEBUG] === 利用者: '{user}' ===")
        print(f"  [DEBUG]   current: {len(user_current)}件, optimized: {len(user_optimized)}件")
        if user_current:
            for i, e in enumerate(user_current[:3]):
                print(f"  [DEBUG]   current[{i}]: 日付={e.date}, サービス='{e.service_type}', "
                      f"時間={e.start_time}-{e.end_time}, 職員1='{e.staff1_name}', 職員2='{e.staff2_name}'")
        if user_optimized:
            for i, e in enumerate(user_optimized[:3]):
                print(f"  [DEBUG]   optimized[{i}]: 日付={e.date}, サービス='{e.service_type}', "
                      f"時間={e.start_time}-{e.end_time}, 職員1='{e.staff1_name}', 職員2='{e.staff2_name}'")

        # 片方にしか存在しない利用者の処理
        if not user_current and user_optimized:
            # 最適化CSVにのみ存在 → 全て追加
            for opt_entry in user_optimized:
                corrections.append(Correction(
                    user_name=user,
                    date_from="",
                    date_to=opt_entry.date,
                    start_time_from="",
                    start_time_to=opt_entry.start_time,
                    end_time_from="",
                    end_time_to=opt_entry.end_time,
                    staff1_from="",
                    staff1_to=opt_entry.staff1_name,
                    staff2_from="",
                    staff2_to=opt_entry.staff2_name,
                    service_type=opt_entry.service_type,
                    action="add",
                    business_type=opt_entry.business_type,
                    remarks=opt_entry.remarks,
                ))
            continue

        if user_current and not user_optimized:
            # 現在CSVにのみ存在 → 全て削除
            for cur_entry in user_current:
                corrections.append(Correction(
                    user_name=user,
                    date_from=cur_entry.date,
                    date_to="",
                    start_time_from=cur_entry.start_time,
                    start_time_to="",
                    end_time_from=cur_entry.end_time,
                    end_time_to="",
                    staff1_from=cur_entry.staff1_name,
                    staff1_to="",
                    staff2_from=cur_entry.staff2_name,
                    staff2_to="",
                    service_type=cur_entry.service_type,
                    action="delete",
                    business_type=cur_entry.business_type,
                    remarks=cur_entry.remarks,
                ))
            continue

        # 日付変更検出のために、全体でのマッチング状態を追跡
        all_matched_current = set()  # マッチ済みのcurrentエントリのインデックス
        all_matched_optimized = set()  # マッチ済みのoptimizedエントリのインデックス

        # まず、日付変更（3日→4日など）を検出
        # 現在のエントリの中で、最適化後に日付が変わるものを探す
        current_dates = set(e.date for e in user_current)
        optimized_dates = set(e.date for e in user_optimized)

        # 日付ごとに比較
        all_dates = current_dates | optimized_dates

        # Bug fix (C-10): use day-of-month extraction so yyyy/MM/dd dates
        # sort correctly alongside plain day numbers.
        for date in sorted(
            all_dates,
            key=lambda x: _extract_day_of_month(x) if _extract_day_of_month(x) is not None else 0,
        ):
            current_on_date = [(i, e) for i, e in enumerate(user_current) if e.date == date]
            optimized_on_date = [(i, e) for i, e in enumerate(user_optimized) if e.date == date]

            # 同じ日付のエントリをマッチング
            # マッチング優先順位:
            # 1. サービス内容 + 開始時間が完全一致
            # 2. サービス内容が一致（時間は異なる）
            # 3. サービス内容が類似
            matched_current_local = set()
            matched_optimized_local = set()

            # Pass 1: サービス内容 + 開始時間が完全一致
            for opt_idx, opt_entry in optimized_on_date:
                for cur_idx, cur_entry in current_on_date:
                    if cur_idx in matched_current_local or opt_idx in matched_optimized_local:
                        continue
                    if (cur_entry.service_type == opt_entry.service_type and
                        cur_entry.start_time == opt_entry.start_time):
                        # 差分があるかチェック
                        has_diff = (
                            cur_entry.end_time != opt_entry.end_time or
                            cur_entry.staff1_name != opt_entry.staff1_name or
                            cur_entry.staff2_name != opt_entry.staff2_name
                        )
                        if has_diff:
                            corrections.append(Correction(
                                user_name=user,
                                date_from=cur_entry.date,
                                date_to=opt_entry.date,
                                start_time_from=cur_entry.start_time,
                                start_time_to=opt_entry.start_time,
                                end_time_from=cur_entry.end_time,
                                end_time_to=opt_entry.end_time,
                                staff1_from=cur_entry.staff1_name,
                                staff1_to=opt_entry.staff1_name,
                                staff2_from=cur_entry.staff2_name,
                                staff2_to=opt_entry.staff2_name,
                                service_type=cur_entry.service_type,
                                action="edit",
                                business_type=cur_entry.business_type,
                                remarks=opt_entry.remarks,
                            ))
                        matched_current_local.add(cur_idx)
                        matched_optimized_local.add(opt_idx)
                        all_matched_current.add(cur_idx)
                        all_matched_optimized.add(opt_idx)

            # Pass 1 結果
            if current_on_date or optimized_on_date:
                print(f"  [DEBUG]   日付{date}: current={len(current_on_date)}件, optimized={len(optimized_on_date)}件, "
                      f"Pass1マッチ: {len(matched_current_local)}件")

            # Pass 2: サービス内容が一致（時間は異なる）
            pass2_matched = 0
            for opt_idx, opt_entry in optimized_on_date:
                if opt_idx in matched_optimized_local:
                    continue
                for cur_idx, cur_entry in current_on_date:
                    if cur_idx in matched_current_local:
                        continue
                    # Bug fix (C-9): when either service_type is empty
                    # string, ``"" in other`` is always True in Python,
                    # which forced spurious substring matches between
                    # unrelated services. Require both sides non-empty
                    # for substring fallback matching.
                    cur_svc = cur_entry.service_type or ""
                    opt_svc = opt_entry.service_type or ""
                    if cur_svc == opt_svc:
                        svc_match = True
                    elif cur_svc and opt_svc and (cur_svc in opt_svc or opt_svc in cur_svc):
                        svc_match = True
                    else:
                        svc_match = False
                    if svc_match:
                        # 差分があるかチェック
                        has_diff = (
                            cur_entry.start_time != opt_entry.start_time or
                            cur_entry.end_time != opt_entry.end_time or
                            cur_entry.staff1_name != opt_entry.staff1_name or
                            cur_entry.staff2_name != opt_entry.staff2_name
                        )
                        print(f"  [DEBUG]     Pass2マッチ: cur_svc='{cur_entry.service_type}' <-> opt_svc='{opt_entry.service_type}', "
                              f"has_diff={has_diff}")
                        if has_diff:
                            print(f"  [DEBUG]       差分詳細: 時間 {cur_entry.start_time}-{cur_entry.end_time} → {opt_entry.start_time}-{opt_entry.end_time}, "
                                  f"職員1 '{cur_entry.staff1_name}' → '{opt_entry.staff1_name}', "
                                  f"職員2 '{cur_entry.staff2_name}' → '{opt_entry.staff2_name}'")
                        pass2_matched += 1
                        if has_diff:
                            corrections.append(Correction(
                                user_name=user,
                                date_from=cur_entry.date,
                                date_to=opt_entry.date,
                                start_time_from=cur_entry.start_time,
                                start_time_to=opt_entry.start_time,
                                end_time_from=cur_entry.end_time,
                                end_time_to=opt_entry.end_time,
                                staff1_from=cur_entry.staff1_name,
                                staff1_to=opt_entry.staff1_name,
                                staff2_from=cur_entry.staff2_name,
                                staff2_to=opt_entry.staff2_name,
                                service_type=cur_entry.service_type,
                                action="edit",
                                business_type=cur_entry.business_type,
                                remarks=opt_entry.remarks,
                            ))
                        matched_current_local.add(cur_idx)
                        matched_optimized_local.add(opt_idx)
                        all_matched_current.add(cur_idx)
                        all_matched_optimized.add(opt_idx)
                        break

        # Pass 3: 日付変更の検出（異なる日付間でのマッチング）
        # マッチしなかったcurrentエントリと、マッチしなかったoptimizedエントリを比較
        unmatched_current = [(i, e) for i, e in enumerate(user_current) if i not in all_matched_current]
        unmatched_optimized = [(i, e) for i, e in enumerate(user_optimized) if i not in all_matched_optimized]

        print(f"  [DEBUG]   Pass1+2後の未マッチ: current={len(unmatched_current)}件, optimized={len(unmatched_optimized)}件")
        for i, e in unmatched_current:
            print(f"  [DEBUG]     未マッチcurrent[{i}]: 日付={e.date}, サービス='{e.service_type}', "
                  f"時間={e.start_time}-{e.end_time}, 職員1='{e.staff1_name}'")
        for i, e in unmatched_optimized:
            print(f"  [DEBUG]     未マッチoptimized[{i}]: 日付={e.date}, サービス='{e.service_type}', "
                  f"時間={e.start_time}-{e.end_time}, 職員1='{e.staff1_name}'")

        for cur_idx, cur_entry in unmatched_current:
            for opt_idx, opt_entry in unmatched_optimized:
                if opt_idx in all_matched_optimized:
                    continue
                # Bug fix (C-9): guard substring fallback against empty
                # strings (``"" in any_string`` is True in Python).
                cur_svc = cur_entry.service_type or ""
                opt_svc = opt_entry.service_type or ""
                if cur_svc == opt_svc:
                    svc_match = True
                elif cur_svc and opt_svc and (cur_svc in opt_svc or opt_svc in cur_svc):
                    svc_match = True
                else:
                    svc_match = False
                # サービス内容が一致し、日付が異なる場合は日付変更
                if svc_match:
                    # 日付が異なることを確認
                    if cur_entry.date != opt_entry.date:
                        corrections.append(Correction(
                            user_name=user,
                            date_from=cur_entry.date,
                            date_to=opt_entry.date,
                            start_time_from=cur_entry.start_time,
                            start_time_to=opt_entry.start_time,
                            end_time_from=cur_entry.end_time,
                            end_time_to=opt_entry.end_time,
                            staff1_from=cur_entry.staff1_name,
                            staff1_to=opt_entry.staff1_name,
                            staff2_from=cur_entry.staff2_name,
                            staff2_to=opt_entry.staff2_name,
                            service_type=cur_entry.service_type,
                            action="date_change",
                            business_type=cur_entry.business_type,
                            remarks=opt_entry.remarks,
                        ))
                        all_matched_current.add(cur_idx)
                        all_matched_optimized.add(opt_idx)
                        break

        # Pass 4: 削除の検出（currentにのみ存在するエントリ）
        final_unmatched_current = [(i, e) for i, e in enumerate(user_current) if i not in all_matched_current]
        final_unmatched_optimized_pre = [(i, e) for i, e in enumerate(user_optimized) if i not in all_matched_optimized]
        print(f"  [DEBUG]   Pass3後の最終未マッチ: current={len(final_unmatched_current)}件 (→削除), "
              f"optimized={len(final_unmatched_optimized_pre)}件 (→追加)")
        for cur_idx, cur_entry in final_unmatched_current:
            corrections.append(Correction(
                user_name=user,
                date_from=cur_entry.date,
                date_to="",  # 削除先はなし
                start_time_from=cur_entry.start_time,
                start_time_to="",
                end_time_from=cur_entry.end_time,
                end_time_to="",
                staff1_from=cur_entry.staff1_name,
                staff1_to="",
                staff2_from=cur_entry.staff2_name,
                staff2_to="",
                service_type=cur_entry.service_type,
                action="delete",
                business_type=cur_entry.business_type,
                remarks=cur_entry.remarks,
            ))

        # Pass 5: 追加の検出（optimizedにのみ存在するエントリ）
        final_unmatched_optimized = [(i, e) for i, e in enumerate(user_optimized) if i not in all_matched_optimized]
        for opt_idx, opt_entry in final_unmatched_optimized:
            corrections.append(Correction(
                user_name=user,
                date_from="",  # 追加元はなし
                date_to=opt_entry.date,
                start_time_from="",
                start_time_to=opt_entry.start_time,
                end_time_from="",
                end_time_to=opt_entry.end_time,
                staff1_from="",
                staff1_to=opt_entry.staff1_name,
                staff2_from="",
                staff2_to=opt_entry.staff2_name,
                service_type=opt_entry.service_type,
                action="add",
                business_type=opt_entry.business_type,
                remarks=opt_entry.remarks,
            ))

    print(f"\n  [DEBUG] ========== 比較結果サマリー ==========")
    print(f"  [DEBUG] 総修正件数: {len(corrections)}")
    actions = {}
    for c in corrections:
        actions[c.action] = actions.get(c.action, 0) + 1
    for action, count in sorted(actions.items()):
        print(f"  [DEBUG]   {action}: {count}件")
    if not corrections:
        print(f"  [DEBUG] ★★★ 修正が0件です。全エントリがマッチして差分なしか、パース/フィルタに問題がある可能性があります ★★★")

    return corrections


def generate_correction_sheet(
    corrections: list[Correction],
    output_path: str,
    format: str = "json"
) -> str:
    """
    修正シートを生成

    Args:
        corrections: 修正リスト
        output_path: 出力ファイルパス
        format: "json" or "csv"

    Returns:
        str: 出力ファイルパス
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if format == "json":
        data = {
            "total_corrections": len(corrections),
            "summary": {
                "time_changes": sum(1 for c in corrections if c.has_time_change()),
                "staff_changes": sum(1 for c in corrections if c.has_staff_change()),
                "date_changes": sum(1 for c in corrections if c.has_date_change()),
                "additions": sum(1 for c in corrections if c.action == "add"),
                "deletions": sum(1 for c in corrections if c.action == "delete"),
            },
            "corrections": [asdict(c) for c in corrections],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    elif format == "csv":
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "利用者", "日付(前)", "日付(後)",
                "開始時間(前)", "開始時間(後)",
                "終了時間(前)", "終了時間(後)",
                "職員1(前)", "職員1(後)",
                "職員2(前)", "職員2(後)",
                "サービス内容", "アクション",
                "業務種別", "備考"
            ])
            for c in corrections:
                writer.writerow([
                    c.user_name, c.date_from, c.date_to,
                    c.start_time_from, c.start_time_to,
                    c.end_time_from, c.end_time_to,
                    c.staff1_from, c.staff1_to,
                    c.staff2_from, c.staff2_to,
                    c.service_type, c.action,
                    c.business_type, c.remarks
                ])

    # 業務種別ごとの集計
    by_business_type = {}
    for c in corrections:
        bt = c.business_type or "(未設定)"
        by_business_type[bt] = by_business_type.get(bt, 0) + 1

    print(f"修正シートを生成しました: {path}")
    print(f"  合計: {len(corrections)} 件")
    print(f"  時間変更: {sum(1 for c in corrections if c.has_time_change())} 件")
    print(f"  職員変更: {sum(1 for c in corrections if c.has_staff_change())} 件")
    print(f"  日付変更: {sum(1 for c in corrections if c.has_date_change())} 件")
    print(f"  追加: {sum(1 for c in corrections if c.action == 'add')} 件")
    print(f"  削除: {sum(1 for c in corrections if c.action == 'delete')} 件")
    for bt, count in sorted(by_business_type.items()):
        print(f"  {bt}: {count} 件")

    return str(path)


def load_correction_sheet(file_path: str) -> list[Correction]:
    """
    修正シートを読み込む

    Args:
        file_path: 修正シートのパス（JSON）

    Returns:
        list[Correction]: 修正リスト
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    corrections = []
    # data が配列の場合（GASからインライン送信時）と dict の場合に対応
    items = data if isinstance(data, list) else data.get("corrections", [])
    for item in items:
        corrections.append(Correction(**item))

    return corrections


def parse_csv_from_content(content: str, csv_type: str = "kaipoke") -> list[ScheduleEntry]:
    """
    CSVテキスト文字列からScheduleEntryリストを生成

    Args:
        content: CSV文字列
        csv_type: "kaipoke" or "optimized"

    Returns:
        list[ScheduleEntry]: パース結果
    """
    import tempfile
    import os

    # BOM除去
    if content and content[0] == '\ufeff':
        content = content[1:]

    # 一時ファイルに書き込んで既存パーサーを再利用
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv',
                                      encoding='utf-8', delete=False) as f:
        f.write(content)
        tmp_path = f.name

    try:
        if csv_type == "kaipoke":
            return parse_kaipoke_csv(tmp_path)
        else:
            return parse_optimized_csv(tmp_path)
    finally:
        os.unlink(tmp_path)


def compare_schedules_from_content(
    current_content: str,
    optimized_content: str,
    target_users: list[str] = None,
    target_week_start: int = None,
    target_week_end: int = None,
) -> list[Correction]:
    """
    CSVテキスト文字列を直接比較して差分を生成

    Args:
        current_content: 現在のCSVテキスト
        optimized_content: 最適化CSVテキスト
        target_users: 対象利用者
        target_week_start: 対象週の開始日
        target_week_end: 対象週の終了日

    Returns:
        list[Correction]: 修正リスト
    """
    import tempfile
    import os

    # BOM除去
    if current_content and current_content[0] == '\ufeff':
        current_content = current_content[1:]
    if optimized_content and optimized_content[0] == '\ufeff':
        optimized_content = optimized_content[1:]

    # 一時ファイルに書き込み
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv',
                                      encoding='utf-8', delete=False) as f:
        f.write(current_content)
        current_tmp = f.name

    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv',
                                      encoding='utf-8', delete=False) as f:
        f.write(optimized_content)
        optimized_tmp = f.name

    try:
        return compare_schedules(
            current_csv=current_tmp,
            optimized_csv=optimized_tmp,
            target_users=target_users,
            target_week_start=target_week_start,
            target_week_end=target_week_end,
        )
    finally:
        os.unlink(current_tmp)
        os.unlink(optimized_tmp)


def validate_correction_data(corrections: list[Correction]) -> dict:
    """
    修正データがPlaywright適用に十分かを検証

    Args:
        corrections: 修正リスト

    Returns:
        dict: 検証結果
    """
    warnings = []
    errors = []
    invalid_items = []
    by_action = {}

    for i, c in enumerate(corrections):
        item_errors = []

        # アクション別カウント初期化
        if c.action not in by_action:
            by_action[c.action] = {"count": 0, "valid": 0, "invalid": 0}
        by_action[c.action]["count"] += 1

        # 全アクション共通: user_name必須
        if not c.user_name or not c.user_name.strip():
            item_errors.append("user_name が未設定です")

        # アクション別バリデーション
        if c.action == "edit":
            if not c.date_from:
                item_errors.append("date_from が未設定です")
            if not c.start_time_from:
                item_errors.append("start_time_from が未設定です（既存予定を特定できません）")
            if not c.has_time_change() and not c.has_staff_change():
                warnings.append(f"[{i}] {c.user_name} {c.date_from}日: 変更内容がありません")

        elif c.action == "delete":
            if not c.date_from:
                item_errors.append("date_from が未設定です")
            if not c.start_time_from:
                item_errors.append("start_time_from が未設定です（削除対象を特定できません）")

        elif c.action == "add":
            if not c.date_to:
                item_errors.append("date_to が未設定です")
            if not c.start_time_to:
                item_errors.append("start_time_to が未設定です")
            if not c.end_time_to:
                item_errors.append("end_time_to が未設定です")
            if not c.business_type:
                warnings.append(f"[{i}] {c.user_name} {c.date_to}日: business_type が未設定です（デフォルト動作になります）")
            if c.is_event() and not c.remarks:
                warnings.append(f"[{i}] {c.user_name} {c.date_to}日: イベントですが備考（イベント名）が未設定です")
            if not c.is_event():
                if not c.staff1_to:
                    warnings.append(f"[{i}] {c.user_name} {c.date_to}日: 職員1が未設定です")
                if not c.staff2_to:
                    warnings.append(f"[{i}] {c.user_name} {c.date_to}日: 職員2が未設定です（1人訪問）")

        elif c.action == "date_change":
            if not c.date_from:
                item_errors.append("date_from が未設定です")
            if not c.date_to:
                item_errors.append("date_to が未設定です")
            if not c.start_time_from:
                item_errors.append("start_time_from が未設定です")
            if c.date_from and c.date_to and c.date_from == c.date_to:
                warnings.append(f"[{i}] {c.user_name}: date_changeですが日付が同じです")

        else:
            item_errors.append(f"不明なaction: {c.action}")

        # 時間フォーマット検証
        for field_name, value in [
            ("start_time_from", c.start_time_from),
            ("start_time_to", c.start_time_to),
            ("end_time_from", c.end_time_from),
            ("end_time_to", c.end_time_to),
        ]:
            if value and value.strip() and not re.match(r'^\d{1,2}:\d{2}$', value):
                item_errors.append(f"{field_name} の形式が不正: '{value}' (HH:MM形式が必要)")

        if item_errors:
            errors.extend([f"[{i}] {c.user_name}: {e}" for e in item_errors])
            invalid_items.append({
                "index": i,
                "user_name": c.user_name,
                "action": c.action,
                "reasons": item_errors,
            })
            by_action[c.action]["invalid"] += 1
        else:
            by_action[c.action]["valid"] += 1

    by_business_type = {}
    for c in corrections:
        bt = c.business_type or "(未設定)"
        by_business_type[bt] = by_business_type.get(bt, 0) + 1

    return {
        "valid": len(errors) == 0,
        "total_corrections": len(corrections),
        "warnings": warnings,
        "errors": errors,
        "invalid_corrections": invalid_items,
        "by_action": by_action,
        "by_business_type": by_business_type,
    }


if __name__ == "__main__":
    # テスト実行
    import argparse

    parser = argparse.ArgumentParser(description="CSV差分比較エンジン")
    parser.add_argument("--current", required=True, help="現在のCSVファイル")
    parser.add_argument("--optimized", required=True, help="最適化CSVファイル")
    parser.add_argument("--output", default="data/correction_sheet.json", help="出力ファイル")
    parser.add_argument("--format", choices=["json", "csv"], default="json", help="出力形式")
    parser.add_argument("--user", help="対象利用者（カンマ区切り）")
    parser.add_argument("--week-start", type=int, help="対象週の開始日")
    parser.add_argument("--week-end", type=int, help="対象週の終了日")
    args = parser.parse_args()

    target_users = args.user.split(",") if args.user else None

    corrections = compare_schedules(
        current_csv=args.current,
        optimized_csv=args.optimized,
        target_users=target_users,
        target_week_start=args.week_start,
        target_week_end=args.week_end,
    )

    generate_correction_sheet(corrections, args.output, args.format)
