"""
カイポケ 新規追加ダイアログ(医療保険)の選択肢採取 — 登録は一切しない (S3 Phase 0)。

  サービス区分 (#inPopupEstimate1) の全 option を列挙し、各 option を選んだ状態で
  連動する 基本療養費 (#inPopupEstimate2) / 職員資格 (#inPopupEstimate3) の option を採取。
  最後にダイアログを閉じる (登録ボタンは押さない)。

使い方:
    python commands/probe_service_options.py --month 2026-08 --user "青柳 あい" --out /tmp/probe.json
"""

import json
import sys
from pathlib import Path

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
)
from commands.auto_apply import (
    select_user,
    click_new_add_button,
    close_edit_dialog,
    _safe_click,
    _select_and_wait,
)


def _options(page, selector):
    loc = page.locator(selector)
    if not loc.is_visible(timeout=3000):
        return None
    for _ in range(10):
        opts = loc.locator("option").all()
        if len(opts) > 1:
            break
        page.wait_for_timeout(500)
    return loc.evaluate(
        "el => Array.from(el.options).map(o => ({value:o.value, text:o.text.trim(), selected:o.selected}))"
    )


def probe(page):
    out = {}
    if not click_new_add_button(page):
        raise RuntimeError("新規追加ボタンが押せません")
    radio = page.locator("input#inPopupInsuranceDivision02")
    _safe_click(page, radio, timeout=5000, description="医療保険ラジオ")
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    out["estimate1_options"] = _options(page, "select#inPopupEstimate1")
    out["by_estimate1"] = {}
    for opt in out["estimate1_options"] or []:
        text = opt["text"]
        if not text or not opt["value"]:
            continue
        _select_and_wait(page, page.locator("select#inPopupEstimate1"), text, "サービス区分")
        page.wait_for_timeout(1500)
        e2 = _options(page, "select#inPopupEstimate2")
        entry = {"estimate2_options": e2, "by_estimate2": {}}
        for o2 in e2 or []:
            if not o2["text"] or not o2["value"]:
                continue
            _select_and_wait(page, page.locator("select#inPopupEstimate2"), o2["text"], "基本療養費")
            page.wait_for_timeout(1500)
            entry["by_estimate2"][o2["text"]] = {
                "estimate3_options": _options(page, "select#inPopupEstimate3"),
            }
        out["by_estimate1"][text] = entry
    close_edit_dialog(page)
    return out


def main(month, user, out_path, headless=True):
    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=headless)
        try:
            login(page)
            dismiss_popup(page)
            goto_receipt(page)
            goto_yoriyori(page)
            goto_monthly_schedule(page)
            set_service_month(page, month)
            if not select_user(page, user):
                raise RuntimeError(f"利用者を選択できません: {user}")
            result = probe(page)
            Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-08")
    ap.add_argument("--user", default="青柳 あい")
    ap.add_argument("--out", default="/tmp/probe_service_options.json")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    main(a.month, a.user, a.out, headless=not a.headed)
