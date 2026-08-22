"""S3 ドライラン: 新規追加ダイアログで service_type 別に3選択を行い、選択結果を読み戻す。登録しない。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from playwright.sync_api import sync_playwright
from lib.common import (create_browser_context, login, dismiss_popup, goto_receipt, goto_yoriyori,
                        goto_monthly_schedule, set_service_month)
from commands.auto_apply import (select_user, click_new_add_button, close_edit_dialog,
                                 fill_medical_insurance_fields, resolve_medical_selects)


def selected_text(page, sel):
    return page.locator(sel).evaluate("el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : null")


def main(month, user):
    cases = ["精神基本療養費Ⅰ・正看", "精神基本療養費Ⅰ・准看", "基本療養費Ⅰ・正看", "基本療養費Ⅰ・准看"]
    with sync_playwright() as p:
        browser, context, page = create_browser_context(p, headless=True)
        try:
            login(page); dismiss_popup(page); goto_receipt(page); goto_yoriyori(page)
            goto_monthly_schedule(page); set_service_month(page, month)
            assert select_user(page, user), "利用者選択失敗"
            for st in cases:
                print("\n=== case:", st)
                assert click_new_add_button(page), "新規追加ボタン"
                ok = fill_medical_insurance_fields(page, st)
                got = {k: selected_text(page, s) for k, s in [("区分", "select#inPopupEstimate1"), ("療養費", "select#inPopupEstimate2"), ("資格", "select#inPopupEstimate3")]}
                exp = resolve_medical_selects(st)["expected_content"]
                print("  fill ok:", ok, "| selected:", got, "| expected:", exp)
                close_edit_dialog(page)
                page.wait_for_timeout(1500)
        finally:
            context.close(); browser.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-08", sys.argv[2] if len(sys.argv) > 2 else "前川　心愛")
