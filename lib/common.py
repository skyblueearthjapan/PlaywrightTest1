"""
共通モジュール - ログイン、ナビゲーション、月設定などの共通処理

使い方:
    from lib.common import create_browser_context, login, goto_monthly_schedule, set_service_month
"""

import os
import unicodedata
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright, Page, BrowserContext
from dotenv import load_dotenv

load_dotenv()

LOGIN_URL = "https://r.kaipoke.biz/biztop/"
STATE_FILE = "state.json"


def normalize_text(text: str) -> str:
    """全角文字を半角に変換"""
    text = unicodedata.normalize('NFKC', text)
    text = text.translate(str.maketrans(
        'ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ０１２３４５６７８９',
        'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    ))
    return text


def create_browser_context(playwright, headless: bool = False, use_state: bool = True):
    """
    ブラウザとコンテキストを作成

    Returns:
        tuple: (browser, context, page)
    """
    browser = playwright.firefox.launch(headless=headless)

    if use_state and Path(STATE_FILE).exists():
        print(f"ログイン状態を復元しています: {STATE_FILE}")
        context = browser.new_context(storage_state=STATE_FILE)
    else:
        context = browser.new_context()

    page = context.new_page()
    return browser, context, page


def login(page: Page, save_state: bool = False, context: BrowserContext = None) -> bool:
    """
    カイポケにログイン

    Args:
        page: Playwrightのページオブジェクト
        save_state: ログイン状態を保存するか
        context: storage_state保存用のコンテキスト

    Returns:
        bool: ログイン成功かどうか
    """
    corp_id = normalize_text(os.environ.get("KAIPOKE_CORP_ID", "").strip())
    user_id = normalize_text(os.environ.get("KAIPOKE_USER_ID", "").strip())
    password = os.environ.get("KAIPOKE_PASSWORD", "").strip()

    if not corp_id or not user_id or not password:
        raise ValueError(".env ファイルに KAIPOKE_CORP_ID, KAIPOKE_USER_ID, KAIPOKE_PASSWORD が設定されていません")

    print(f"ログインページを開いています: {LOGIN_URL}")
    page.goto(LOGIN_URL)
    page.wait_for_load_state("networkidle")

    # すでにログイン済みかチェック
    if "biztop" not in page.url and "error" not in page.url:
        print("すでにログイン済みです")
        return True

    # エラーページの場合は「トップへ戻る」をクリック
    if "error" in page.url or "nonmember" in page.url:
        print("エラーページが表示されました。トップへ戻るをクリックします...")
        page.click("text=トップへ戻る")
        page.wait_for_load_state("networkidle")

    print("認証情報を入力しています...")

    # 法人ID を入力
    page.fill("#form\\:corporation_id", corp_id)
    page.wait_for_timeout(300)

    # ユーザーID を入力
    page.fill("#form\\:member_login_id", user_id)
    page.wait_for_timeout(300)

    # パスワード を入力
    password_field = page.locator("#form\\:password")
    password_field.click()
    page.wait_for_timeout(200)
    password_field.fill("")
    page.wait_for_timeout(200)
    password_field.fill(password)
    page.wait_for_timeout(500)

    # ログインボタンをクリック
    print("ログインボタンをクリックしています...")
    page.click("#form\\:logn_nochklogin")
    page.wait_for_load_state("networkidle")

    # ポップアップを閉じる
    try:
        page.wait_for_timeout(1000)
        page.mouse.click(400, 650)
        page.wait_for_timeout(500)
    except Exception:
        pass

    # storage_state を保存
    if save_state and context:
        context.storage_state(path=STATE_FILE)
        print(f"ログイン状態を保存しました: {STATE_FILE}")

    print("ログイン成功！")
    return True


def dismiss_popup(page: Page) -> None:
    """ポップアップを閉じる（表示されている場合）"""
    try:
        page.mouse.click(400, 650)
        page.wait_for_timeout(500)
    except Exception:
        pass


def goto_receipt(page: Page) -> None:
    """
    レセプト画面に遷移
    """
    print("レセプトボタンをクリックしています...")
    page.click("text=レセプト")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    print("レセプト画面を表示しました")


def goto_yoriyori(page: Page) -> None:
    """
    訪問看護/よりより（1260192047）画面に遷移
    """
    print("訪問看護のリンクをクリックしています...")
    page.click("text=訪問看護/1260192047")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    print("訪問看護の詳細画面を表示しました")


def goto_monthly_schedule(page: Page) -> None:
    """
    月間スケジュール管理画面に遷移

    ナビゲーション: ホーム → スケジュール → 月間スケジュール管理
    """
    print("月間スケジュール管理画面に遷移しています...")

    # スケジュールメニューをクリック
    page.click("text=スケジュール")
    page.wait_for_timeout(500)

    # 月間スケジュール管理をクリック
    page.click("text=月間スケジュール管理")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    print("月間スケジュール管理画面を表示しました")


def goto_export_page(page: Page) -> None:
    """
    各種情報出力画面に遷移

    ナビゲーション: ホーム → 各種情報出力
    """
    print("各種情報出力画面に遷移しています...")

    # 各種情報出力メニューをクリック
    page.click("text=各種情報出力")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    print("各種情報出力画面を表示しました")


def parse_month(month_str: str) -> tuple[int, int]:
    """
    月文字列をパース

    Args:
        month_str: "2026-04" または "R8-04" 形式

    Returns:
        tuple: (西暦年, 月)
    """
    if month_str.upper().startswith("R"):
        # 令和形式 (R8-04)
        parts = month_str[1:].split("-")
        reiwa_year = int(parts[0])
        month = int(parts[1])
        year = 2018 + reiwa_year  # 令和1年 = 2019年
    else:
        # 西暦形式 (2026-04)
        parts = month_str.split("-")
        year = int(parts[0])
        month = int(parts[1])

    return year, month


def to_reiwa(year: int) -> int:
    """西暦年を令和年に変換"""
    return year - 2018


def set_service_month(page: Page, month_str: str) -> None:
    """
    サービス提供月を設定

    Args:
        page: Playwrightのページオブジェクト
        month_str: "2026-04" 形式の月文字列
    """
    year, month = parse_month(month_str)
    reiwa_year = to_reiwa(year)

    print(f"サービス提供月を設定しています: 令和{reiwa_year}年{month}月 ({year}-{month:02d})")

    # 年のプルダウンを選択（令和8年 = 2026年）
    # セレクタは画面によって異なる可能性があるため、複数パターンを試す
    year_selectors = [
        "select[name*='year']",
        "select[id*='year']",
        "#year",
        ".year-select",
    ]

    for selector in year_selectors:
        try:
            year_select = page.locator(selector).first
            if year_select.is_visible():
                year_select.select_option(str(reiwa_year))
                break
        except Exception:
            continue

    page.wait_for_timeout(300)

    # 月のプルダウンを選択
    month_selectors = [
        "select[name*='month']",
        "select[id*='month']",
        "#month",
        ".month-select",
    ]

    for selector in month_selectors:
        try:
            month_select = page.locator(selector).first
            if month_select.is_visible():
                month_select.select_option(str(month))
                break
        except Exception:
            continue

    page.wait_for_timeout(500)
    print(f"サービス提供月を設定しました")


def save_artifacts(page: Page, out_dir: Path, prefix: str = "") -> None:
    """失敗時の証拠を保存（スクリーンショット、HTML）"""
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix_str = f"{prefix}_" if prefix else ""

    page.screenshot(path=str(out_dir / f"{prefix_str}screenshot_{ts}.png"), full_page=True)
    html = page.content()
    (out_dir / f"{prefix_str}page_{ts}.html").write_text(html, encoding="utf-8")

    print(f"アーティファクトを保存しました: {out_dir}")
