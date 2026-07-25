# -*- coding: utf-8 -*-
"""職員スケジュール画面 (staffSchedule) HTML から個別業務(イベント)を抽出する純パーサ.

CareFlow「イベント取り込み」(kaipoke-event-inbound-design.md E-0) の心臓部。
Playwright に依存せず HTML 文字列だけで動く (フィクスチャテスト可能)。

抽出対象:
  * ヘッダ:  #weeklyShifts thead の <a href="...day=YYYYMMDD">  → 列⇔日付対応
  * 本体:    #scheduleShifts の <tr ... staff-id="...">
             各 td 内の <a class="... btnIndividual" onclick="showIndividualEdit(
               yyyymm, '区分', 個別業務ID, 日, 職員内部ID)">
               <span class="targetTime">HH:MM～HH:MM</span>
               <span class="targetPerson">タイトル</span>

冪等キー external_key = "{個別業務ID}:{職員内部ID}:{YYYY-MM-DD}"。
個別業務IDは週パターン由来の予定だと複数日で同一になるため (2026-07-25 実測)、
単独では一意にならない。複合キーで一意 (実データ23件で検証済み)。

患者訪問 (showHNC097807Edit 系アンカー) は class が異なるため混入しない。
"""

from __future__ import annotations

import re
from datetime import date as Date

# ヘッダの日付リンク (列順)
_RE_HEADER_DAY = re.compile(r'day=(\d{8})')
# 職員行の開始
_RE_ROW_SPLIT = re.compile(r'<tr[^>]*\bstaff-id="(\d+)"[^>]*>')
# th 内の表示 span (display:none の読み仮名 span は除外)
_RE_HIDDEN_SPAN = re.compile(r'<span style="display: none;">.*?</span>', re.S)
_RE_SPAN_TEXT = re.compile(r'<span>([^<]*)</span>')
# td 境界
_RE_TD_SPLIT = re.compile(r'<td\b')
# 個別業務アンカー (class に btnIndividual トークンを含む <a> 全体・属性込み)
_RE_INDIVIDUAL_FULL = re.compile(
    r'<a\b(?=[^>]*class="[^"]*\bbtnIndividual\b[^"]*")[^>]*>.*?</a>', re.S
)
_RE_EDIT_ARGS = re.compile(
    r"showIndividualEdit\(\s*(\d+)\s*,\s*'(\d+)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
    re.S,
)
_RE_TIME = re.compile(
    r'class="targetTime[^"]*"\s*>\s*(\d{1,2}:\d{2})\s*～\s*(\d{1,2}:\d{2})', re.S
)
_RE_TITLE = re.compile(r'class="targetPerson[^"]*"[^>]*>\s*(.*?)\s*</span>', re.S)


def _clean_text(raw: str) -> str:
    """タグ除去 + 空白正規化 (タイトル用)。"""
    text = re.sub(r'<[^>]+>', '', raw)
    return re.sub(r'\s+', ' ', text).strip()


def _fmt_hhmm(value: str) -> str:
    hh, mm = value.split(':')
    return f"{int(hh):02d}:{mm}"


def parse_header_dates(html: str) -> list[Date]:
    """#weeklyShifts のヘッダから列順の日付リストを返す。"""
    m = re.search(r'id="weeklyShifts".*?</table>', html, re.S)
    if not m:
        raise ValueError("weeklyShifts header table not found")
    days = _RE_HEADER_DAY.findall(m.group(0))
    if not days:
        raise ValueError("no date columns in weeklyShifts header")
    return [Date(int(d[:4]), int(d[4:6]), int(d[6:8])) for d in days]


def parse_individual_tasks(html: str) -> list[dict]:
    """HTML 全体から個別業務レコードを抽出する。

    Returns:
        [{staff_kaipoke_id, staff_name, date, start, end, title,
          kaipoke_task_id, external_key}]

    Raises:
        ValueError: 画面構造が想定と異なる場合 (無言の空振り/ズレ取り込み防止)。
            - ヘッダ/本体テーブルが見つからない
            - onclick 引数の day と列日付が食い違う (列⇔日付対応のパース事故検知)
    """
    dates = parse_header_dates(html)

    m = re.search(r'id="scheduleShifts".*?</table>', html, re.S)
    if not m:
        raise ValueError("scheduleShifts table not found")
    body = m.group(0)

    tasks: list[dict] = []
    # staff-id で行分割 → [prefix, staff_id1, row1, staff_id2, row2, ...]
    parts = _RE_ROW_SPLIT.split(body)
    for idx in range(1, len(parts) - 1, 2):
        staff_id = parts[idx]
        row_html = parts[idx + 1]

        # 職員名: th 内の表示 span を結合 (姓 + 全角空白 + 名 = CareFlow マスタと同形式)
        th_end = row_html.find('</th>')
        th_html = row_html[:th_end] if th_end >= 0 else row_html
        th_visible = _RE_HIDDEN_SPAN.sub('', th_html)
        ul_pos = th_visible.find('<ul')
        name_zone = th_visible[:ul_pos] if ul_pos >= 0 else th_visible
        name_parts = [s.strip() for s in _RE_SPAN_TEXT.findall(name_zone) if s.strip()]
        staff_name = '　'.join(name_parts)

        rest = row_html[th_end + len('</th>'):] if th_end >= 0 else row_html
        cells = _RE_TD_SPLIT.split(rest)[1:]
        for col, cell in enumerate(cells):
            if col >= len(dates):
                break
            col_date = dates[col]
            for a_html in _RE_INDIVIDUAL_FULL.findall(cell):
                args = _RE_EDIT_ARGS.search(a_html)
                t = _RE_TIME.search(a_html)
                title_m = _RE_TITLE.search(a_html)
                title = _clean_text(title_m.group(1)) if title_m else ''
                day_from_args = int(args.group(4)) if args else None
                if day_from_args is not None and day_from_args != col_date.day:
                    raise ValueError(
                        f"day mismatch: onclick day={day_from_args} vs column {col_date} "
                        f"(staff={staff_name} title={title})"
                    )
                task_id = args.group(3) if args else None
                tasks.append({
                    'staff_kaipoke_id': staff_id,
                    'staff_name': staff_name,
                    'date': col_date.isoformat(),
                    'start': _fmt_hhmm(t.group(1)) if t else None,
                    'end': _fmt_hhmm(t.group(2)) if t else None,
                    'title': title,
                    'kaipoke_task_id': task_id,
                    'external_key': (
                        f"{task_id}:{staff_id}:{col_date.isoformat()}" if task_id else None
                    ),
                })
    return tasks
