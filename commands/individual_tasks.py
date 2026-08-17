# -*- coding: utf-8 -*-
"""個別業務(イベント)取得 — 職員スケジュール画面(週間)からの read-only 抽出.

CareFlow「イベント取り込み」(kaipoke-event-inbound-design.md) の取得側。
一切のクリック編集をしない (閲覧とパースのみ)。

フロー:
  1. ログイン → レセプト → 訪問看護 (setup_yoriyori_page)
     → 着地画面 = 職員スケジュール (staffSchedule・週間表示)
  2. 職員名セレクトを「－」(全職員) に変更 → 全職員グリッド
  3. currentDate に対象日を設定 → submitDate(0) で週を切替
  4. HTML を lib.individual_tasks_parser でパース → JSON 返却

Usage:
    python main.py individual-tasks --date 2026-07-20
"""

from __future__ import annotations

import sys
from datetime import date as Date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from lib.common import create_browser_context, save_artifacts, setup_yoriyori_page
from lib.individual_tasks_parser import parse_header_dates, parse_individual_tasks

# 全職員表示のラベル (全角ハイフンマイナス U+FF0D)
_ALL_STAFF_LABEL = "－"


def _wait_reload(page) -> None:
    """onchange/submitDate による画面再読込を待つ。

    networkidle はページに常駐通信があると永遠にアイドルにならず
    TimeoutError で全体が落ちる (2026-08-17 本番事故)。lib/common.py と同じく
    タイムアウト時は domcontentloaded で妥協して続行する。正しさは呼び出し側の
    値検証 (currentDate/「－」再検証/ヘッダ日付レンジ/0件×btnIndividual) が担保する。
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        print("  (networkidle待機がタイムアウト、domcontentloadedで続行)")
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    page.wait_for_timeout(1500)


def _select_all_staff(page) -> None:
    """職員名セレクトを「－」(全職員) にする。onchange で画面が再読込される。"""
    current = page.eval_on_selector(
        "#staffSelected",
        "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].textContent.trim() : ''",
    )
    if current == _ALL_STAFF_LABEL:
        return
    page.locator("select#staffSelected").select_option(label=_ALL_STAFF_LABEL)
    _wait_reload(page)


def _set_week(page, date_str: str) -> None:
    """currentDate に対象日を設定して submitDate で週を切替える。"""
    target = date_str.replace("-", "/")
    current = page.eval_on_selector("#currentDate", "el => el.value")
    if current == target:
        return
    page.evaluate(
        """(d) => {
            const el = document.getElementById('currentDate');
            el.value = d;
            submitDate(0, 'staffScheduleForm');
        }""",
        target,
    )
    _wait_reload(page)
    after = page.eval_on_selector("#currentDate", "el => el.value")
    if after != target:
        raise RuntimeError(f"週の切替に失敗: currentDate={after!r} (期待 {target!r})")


def run_individual_tasks(date_str: str, headless: bool = True) -> dict:
    """個別業務を取得する。

    Args:
        date_str: 対象日 "YYYY-MM-DD"。画面はこの日を含む週 (この日起点の7日) を表示する。
        headless: ヘッドレス実行。

    Returns:
        {success, week_start, week_end, week_dates, staff_count, tasks: [...]}
        tasks の形式は lib.individual_tasks_parser.parse_individual_tasks 参照。
    """
    # 早期バリデーション (Playwright 起動前)
    target = Date(*[int(x) for x in date_str.split("-")])

    result: dict = {"success": False, "tasks": []}

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)
        try:
            # ログイン → レセプト → 訪問看護 (着地 = 職員スケジュール週間)
            setup_yoriyori_page(page, context)
            page.wait_for_timeout(1000)

            # 全職員表示 → 週切替 (submitDate はフォーム値を引き継ぐが、
            # 切替後にも全職員表示を検証して事故を防ぐ)
            _select_all_staff(page)
            _set_week(page, date_str)
            _select_all_staff(page)  # 再検証 (既に「－」なら no-op)

            html = page.content()
            dates = parse_header_dates(html)
            if not (dates[0] <= target <= dates[-1]):
                raise RuntimeError(
                    f"指定日 {date_str} が表示週 {dates[0]}〜{dates[-1]} に含まれていません"
                )

            tasks = parse_individual_tasks(html)

            # 0件時の防御: パーサ空振りと本当の0件を区別する
            if not tasks and "btnIndividual" in html:
                raise RuntimeError(
                    "btnIndividual が存在するのにパース結果が0件です (画面構造変更の疑い)"
                )

            staff_count = html.count('staff-id="')
            print(f"個別業務 取得完了: {len(tasks)}件 (職員 {staff_count}行, "
                  f"週 {dates[0]}〜{dates[-1]})")
            result = {
                "success": True,
                "week_start": dates[0].isoformat(),
                "week_end": dates[-1].isoformat(),
                "week_dates": [d.isoformat() for d in dates],
                "staff_count": staff_count,
                "tasks": tasks,
            }
        except Exception:
            save_artifacts(page, Path("artifacts"), "individual_tasks_error")
            raise
        finally:
            context.close()
            browser.close()

    return result


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="個別業務(イベント)取得")
    parser.add_argument("--date", required=True, help="対象日 (YYYY-MM-DD)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを表示")
    args = parser.parse_args()

    res = run_individual_tasks(args.date, headless=not args.headed)
    print(json.dumps(res, ensure_ascii=False, indent=1))
