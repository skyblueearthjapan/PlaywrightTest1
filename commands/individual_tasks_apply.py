# -*- coding: utf-8 -*-
"""個別業務(イベント)の書き込み — 職員スケジュール画面への新規登録 (Phase 3).

CareFlow「イベントをカイポケへ送る」(kaipoke-event-two-way-design.md §3-①/§7) の書込側。
読取 (individual_tasks.py) と対になる。**登録のみ** (更新・削除はしない)。

フロー (1 アイテムずつ):
  1. ログイン → 職員スケジュール (週間) → 全職員表示 → 対象日の週へ切替
  2. 重複チェック: 現在の盤面をパースし、同 staff×日×時刻×名称 が既にあれば skip
  3. showIndividualAdd(ym, day, staffInternalId) でポップアップを開く
  4. 方式選択: 蓄積マスタに同名があれば「登録データから選択」/ 無ければ「新しく登録」
     (マスタ肥大の抑制・設計 §7-b)
  5. 時刻 (Hour + Min 十の位/一の位) と 区分=予定 を設定 → 「登録する」
  6. 盤面を再読込してパースし、登録された個別業務IDを回収 →
     external_key = "{個別業務ID}:{職員内部ID}:{YYYY-MM-DD}" を返す (楽スケ側の昇格用)

Usage:
    python main.py individual-tasks-apply --items-json '[{...}]'
items: [{"external_ref": "<楽スケevent UUID>", "staff_internal_id": "4553818",
         "date": "2026-11-18", "start": "09:00", "end": "09:15", "title": "朝会"}]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from commands.individual_tasks import _select_all_staff, _set_week, _wait_reload
from lib.common import create_browser_context, save_artifacts, setup_yoriyori_page
from lib.individual_tasks_parser import parse_individual_tasks

POPUP_CONTAINER = "#idRenderPopupIndividual"
BTN_ADD = "#popupIndividualButtonAdd"


def _hhmm_tuple(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def _select_by_int(page, selector: str, value: int) -> None:
    """select の option 値が '9' / '09' どちらでも通るように両方試す。"""
    for cand in (str(value), f"{value:02d}"):
        try:
            page.select_option(selector, value=cand)
            return
        except Exception:
            continue
    raise RuntimeError(f"select_option 失敗: {selector} value={value}")


def _set_minute(page, name_prefix: str, minute: int) -> None:
    """分は Left/Right の 2 select。十の位/一の位の割当を実測して設定する。

    どちらが十の位かは option 値域で判定 (0-5 のみ = 十の位)。判定不能なら
    Left=十の位 と仮定して試し、失敗したら入れ替える。
    """
    left = f"select[name='{name_prefix}MinLeft']"
    right = f"select[name='{name_prefix}MinRight']"
    tens, ones = minute // 10, minute % 10

    def opts(sel: str) -> list[str]:
        return page.eval_on_selector(
            sel, "el => Array.from(el.options).map(o => o.value)"
        )

    left_vals = [v for v in opts(left) if v.strip() != ""]
    left_max = max((int(v) for v in left_vals if v.isdigit()), default=9)
    if left_max <= 5:
        _select_by_int(page, left, tens)
        _select_by_int(page, right, ones)
    else:
        _select_by_int(page, left, ones)
        _select_by_int(page, right, tens)


def _find_master_option(page, title: str) -> str | None:
    """蓄積マスタ (popupIndividualInternalId) から完全一致の名称を探す。"""
    options = page.eval_on_selector(
        "#popupIndividualInternalId",
        "el => Array.from(el.options).map(o => ({v: o.value, t: o.textContent.trim()}))",
    )
    for o in options:
        if o["t"] == title and o["v"] and o["v"] != "0":
            return o["v"]
    return None


def _fill_and_save_popup(page, item: dict) -> None:
    """開いたポップアップに入力して保存する (呼び出し側で検証)。"""
    title = str(item["title"]).strip()
    sh, sm = _hhmm_tuple(item["start"])
    eh, em = _hhmm_tuple(item["end"])

    page.wait_for_selector(BTN_ADD, timeout=15000)
    page.wait_for_timeout(500)

    master = _find_master_option(page, title)
    if master is not None:
        page.check("input[name='popupSelectedIndividual'][value='1']")
        page.select_option("#popupIndividualInternalId", value=master)
        print(f"    マスタ選択: {title} (id={master})")
    else:
        page.check("input[name='popupSelectedIndividual'][value='2']")
        page.fill("#popupIndividualName", title)
        print(f"    新規名称: {title}")

    _select_by_int(page, "select[name='popupIndividualStartHour']", sh)
    _set_minute(page, "popupIndividualStart", sm)
    _select_by_int(page, "select[name='popupIndividualEndHour']", eh)
    _set_minute(page, "popupIndividualEnd", em)

    # 区分 = 予定 (01)。value 表記ゆれに備え '01' → '1' → 先頭radio の順で試す。
    for v in ("01", "1"):
        try:
            page.check(f"input[name='popupIndividualPlanActDivision'][value='{v}']")
            break
        except Exception:
            continue
    else:
        page.eval_on_selector(
            "input[name='popupIndividualPlanActDivision']", "el => el.click()"
        )

    # ボタンが disabled のままなら保存不能 (actDate 未設定など) — 明示エラー
    page.wait_for_timeout(300)
    if page.eval_on_selector(BTN_ADD, "el => el.disabled"):
        raise RuntimeError("登録ボタンが disabled のままです (actDate 未設定の疑い)")
    page.click(BTN_ADD)
    _wait_reload(page)


def _reload_board(page) -> str:
    """盤面を再描画して最新 HTML を返す (submitDate 再送 = 同週リフレッシュ)。"""
    page.evaluate("submitDate(0, 'staffScheduleForm')")
    _wait_reload(page)
    return page.content()


def _existing_key(tasks: list[dict], item: dict) -> str | None:
    """盤面パース結果から item と同一の予定を探し external_key を返す。"""
    for t in tasks:
        if (
            t["staff_kaipoke_id"] == str(item["staff_internal_id"])
            and t["date"] == item["date"]
            and t["start"] == item["start"]
            and t["end"] == item["end"]
            and t["title"].strip() == str(item["title"]).strip()
        ):
            return t["external_key"]
    return None


def run_individual_tasks_apply(items: list[dict], headless: bool = True) -> dict:
    """個別業務を職員スケジュールへ登録する。

    Returns: {success, results: [{external_ref, outcome, external_key?, error?}]}
      outcome: 'added' | 'skipped_duplicate' | 'failed'
    """
    results: list[dict] = []

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)
        # 想定外の confirm/alert はダイアログで固まらないよう受理する
        page.on("dialog", lambda d: d.accept())
        try:
            setup_yoriyori_page(page, context)
            page.wait_for_timeout(1000)
            _select_all_staff(page)

            for item in items:
                ref = item.get("external_ref")
                try:
                    date_str = str(item["date"])
                    ym = date_str[:7].replace("-", "")
                    day = int(date_str[8:10])
                    staff_internal = str(item["staff_internal_id"])

                    _set_week(page, date_str)
                    _select_all_staff(page)  # 週切替後の再検証 (既に「－」なら no-op)

                    tasks = parse_individual_tasks(page.content())
                    dup = _existing_key(tasks, item)
                    if dup is not None:
                        print(f"  skip (重複): {item['title']} {date_str}")
                        results.append(
                            {"external_ref": ref, "outcome": "skipped_duplicate",
                             "external_key": dup}
                        )
                        continue

                    print(f"  登録: {item['title']} {date_str} "
                          f"{item['start']}-{item['end']} staff={staff_internal}")
                    page.evaluate(
                        f"showIndividualAdd({ym}, {day}, {staff_internal})"
                    )
                    _fill_and_save_popup(page, item)

                    # 保存後検証: 盤面再読込 → 同一予定の出現と採番IDを確認
                    html = _reload_board(page)
                    key = _existing_key(parse_individual_tasks(html), item)
                    if key is None:
                        raise RuntimeError("保存後の盤面に登録内容が見つかりません")
                    print(f"    OK external_key={key}")
                    results.append(
                        {"external_ref": ref, "outcome": "added", "external_key": key}
                    )
                except Exception as e:  # アイテム単位で失敗を隔離
                    print(f"  失敗: {item.get('title')} {item.get('date')}: {e}")
                    save_artifacts(page, Path("artifacts"), "individual_tasks_apply_error")
                    results.append(
                        {"external_ref": ref, "outcome": "failed", "error": str(e)}
                    )
                    # ポップアップが開きっぱなしなら ESC で閉じて次へ
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
        finally:
            context.close()
            browser.close()

    ok = sum(1 for r in results if r["outcome"] in ("added", "skipped_duplicate"))
    return {"success": True, "total": len(items), "ok": ok, "results": results}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="個別業務(イベント)書き込み")
    parser.add_argument("--items-json", required=True, help="items の JSON 配列")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    res = run_individual_tasks_apply(json.loads(args.items_json), headless=not args.headed)
    print(json.dumps(res, ensure_ascii=False, indent=1))
