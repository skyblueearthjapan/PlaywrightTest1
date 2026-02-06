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

    # state.jsonが有効かどうか確認
    state_valid = False
    if use_state and Path(STATE_FILE).exists():
        try:
            import json
            with open(STATE_FILE, 'r') as f:
                state_data = json.load(f)
            # cookiesが存在し、空でないことを確認
            if state_data.get("cookies") and len(state_data["cookies"]) > 0:
                print(f"ログイン状態を復元しています: {STATE_FILE}")
                context = browser.new_context(storage_state=STATE_FILE)
                state_valid = True
            else:
                print("state.jsonにセッション情報がありません。新規ログインします。")
        except (json.JSONDecodeError, Exception) as e:
            print(f"state.jsonの読み込みに失敗: {e}。新規ログインします。")

    if not state_valid:
        context = browser.new_context()

    page = context.new_page()
    return browser, context, page


def login(page: Page, save_state: bool = False, context: BrowserContext = None, timeout: int = 60000) -> bool:
    """
    カイポケにログイン

    Args:
        page: Playwrightのページオブジェクト
        save_state: ログイン状態を保存するか
        context: storage_state保存用のコンテキスト
        timeout: タイムアウト（ミリ秒、デフォルト60秒）

    Returns:
        bool: ログイン成功かどうか
    """
    corp_id = normalize_text(os.environ.get("KAIPOKE_CORP_ID", "").strip())
    user_id = normalize_text(os.environ.get("KAIPOKE_USER_ID", "").strip())
    password = os.environ.get("KAIPOKE_PASSWORD", "").strip()

    if not corp_id or not user_id or not password:
        raise ValueError(".env ファイルに KAIPOKE_CORP_ID, KAIPOKE_USER_ID, KAIPOKE_PASSWORD が設定されていません")

    print(f"ログインページを開いています: {LOGIN_URL}")
    page.goto(LOGIN_URL, timeout=timeout)

    # ページの読み込みを待つ
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        print("  (networkidle待機がタイムアウト、続行)")
        page.wait_for_load_state("domcontentloaded", timeout=timeout)

    # すでにログイン済みかチェック
    # biztopはログインページ、ログイン後はリダイレクトされる
    current_url = page.url
    if "biztop" not in current_url and "error" not in current_url:
        print(f"すでにログイン済みです（URL: {current_url}）")
        return True

    # ログインフォームが表示されているか確認
    corp_id_field = page.locator("#form\\:corporation_id")
    if not corp_id_field.is_visible(timeout=3000):
        # フォームがない場合、ダッシュボードにいる可能性
        if "kaipoke" in page.url or "common" in page.url:
            print("すでにログイン済みです（ダッシュボード検出）")
            return True

    # エラーページの場合は「トップへ戻る」をクリック
    if "error" in page.url or "nonmember" in page.url:
        print("エラーページが表示されました。トップへ戻るをクリックします...")
        try:
            page.click("text=トップへ戻る", timeout=5000)
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            pass

    # ログインフォームが必要かどうか再確認
    corp_id_field = page.locator("#form\\:corporation_id")
    if not corp_id_field.is_visible(timeout=5000):
        # フォームがないので、ログイン済みと判断
        print("ログインフォームが見つかりません。ログイン済みと判断します。")
        return True

    print("認証情報を入力しています...")

    # 法人ID を入力
    corp_id_field.fill(corp_id)
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
    """ポップアップや通知バーを閉じる（表示されている場合）"""
    try:
        # 黄色い通知バー「ログアウト発生をともなうリリースについて」のXボタンを閉じる
        close_buttons = [
            "button.close",
            "a.close",
            ".notification-close",
            "[aria-label='Close']",
            "text=×",
        ]
        for selector in close_buttons:
            try:
                close_btn = page.locator(selector).first
                if close_btn.is_visible(timeout=1000):
                    close_btn.click()
                    page.wait_for_timeout(300)
            except Exception:
                continue

        # 画面中央のポップアップを閉じるためにクリック
        page.mouse.click(400, 650)
        page.wait_for_timeout(500)
    except Exception:
        pass


def goto_receipt(page: Page, timeout: int = 60000) -> None:
    """
    レセプト画面に遷移

    Args:
        page: Playwrightのページオブジェクト
        timeout: タイムアウト（ミリ秒、デフォルト60秒）
    """
    print("レセプトボタンをクリックしています...")
    page.click("text=レセプト")
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        print("  (networkidle待機がタイムアウト、domcontentloadedで続行)")
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    page.wait_for_timeout(1000)
    print("レセプト画面を表示しました")


def goto_yoriyori(page: Page, timeout: int = 60000) -> None:
    """
    訪問看護/よりより（1260192047）画面に遷移

    Args:
        page: Playwrightのページオブジェクト
        timeout: タイムアウト（ミリ秒、デフォルト60秒）
    """
    print("訪問看護のリンクをクリックしています...")
    page.click("text=訪問看護/1260192047")
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        # networkidleがタイムアウトした場合はdomcontentloadedで代替
        print("  (networkidle待機がタイムアウト、domcontentloadedで続行)")
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    page.wait_for_timeout(1000)
    print("訪問看護の詳細画面を表示しました")


def goto_monthly_schedule(page: Page, timeout: int = 60000) -> None:
    """
    月間スケジュール管理画面に遷移

    ナビゲーション: スケジュール管理（ドロップダウン）→ 月間スケジュール管理

    Args:
        page: Playwrightのページオブジェクト
        timeout: タイムアウト（ミリ秒、デフォルト60秒）
    """
    print("月間スケジュール管理画面に遷移しています...")

    # 複数の方法を試す
    monthly_link = page.locator("text=月間スケジュール管理").first

    # 方法1: すでに見えている場合はそのままクリック
    if monthly_link.is_visible(timeout=2000):
        monthly_link.click()
    else:
        # 方法2: スケジュール管理メニューをクリックしてドロップダウンを開く
        schedule_menu = page.locator("a:has-text('スケジュール管理')").first
        if schedule_menu.is_visible():
            # ホバーでドロップダウンを開く
            schedule_menu.hover()
            page.wait_for_timeout(800)

            # まだ見えない場合はクリック
            if not monthly_link.is_visible(timeout=1000):
                schedule_menu.click()
                page.wait_for_timeout(800)

        # 月間スケジュール管理をクリック
        if monthly_link.is_visible(timeout=3000):
            monthly_link.click()
        else:
            # 方法3: JavaScriptでナビゲート
            print("  ドロップダウンが開かないため、直接URLに移動します")
            page.goto("https://r.kaipoke.biz/bizhnc/monthlyShiftsList?isFromMenuBizhnc=true", timeout=timeout)

    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        print("  (networkidle待機がタイムアウト、domcontentloadedで続行)")
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    page.wait_for_timeout(1000)

    print("月間スケジュール管理画面を表示しました")


def goto_export_page(page: Page) -> None:
    """
    出力対象選択画面に遷移

    ナビゲーション: 上部ナビゲーションバー「各種情報出力▼」→「出力対象選択」
    """
    print("出力対象選択画面に遷移しています...")

    # 上部ナビゲーションバーの「各種情報出力▼」プルダウンをホバー/クリック
    # ※サイドメニューではなく、上部のナビゲーションバーにあるプルダウン
    export_menu = page.locator("a:has-text('各種情報出力')").first
    export_menu.hover()
    page.wait_for_timeout(500)

    # プルダウンが開かない場合はクリック
    output_select = page.locator("text=出力対象選択")
    if not output_select.is_visible():
        export_menu.click()
        page.wait_for_timeout(500)

    # 「出力対象選択」をクリック
    print("出力対象選択をクリックしています...")
    page.click("text=出力対象選択")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    print("出力対象選択画面を表示しました")


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

    月間スケジュール一覧画面で「次月」ボタンをクリックして目標の月に移動する。
    スクリーンショットによると、令和8年1月から4月へは「次月」ボタン3回クリック。

    Args:
        page: Playwrightのページオブジェクト
        month_str: "2026-04" 形式の月文字列
    """
    year, month = parse_month(month_str)
    reiwa_year = to_reiwa(year)
    target_text = f"令和{reiwa_year}年{month}月"

    print(f"サービス提供月を設定しています: {target_text} ({year}-{month:02d})")

    # 現在の月を確認する関数
    def get_current_month_text():
        # ページ内の「令和X年Y月」テキストを探す
        try:
            # DevToolsから見ると #tdNextServiceOffer 付近に年月がある
            month_elem = page.locator("text=/令和\\d+年\\d+月/").first
            if month_elem.is_visible():
                return month_elem.text_content().strip()
        except Exception:
            pass
        return ""

    # 現在の月をチェック
    current_text = get_current_month_text()
    print(f"  現在の月: {current_text}")

    if target_text in current_text:
        print(f"  すでに目標の月です: {target_text}")
        return

    # 「次月」ボタンで移動（最大12回）
    for i in range(12):
        next_btn = page.locator("a.next, a:has-text('次月')").first
        if not next_btn.is_visible():
            # 別のセレクタを試す
            next_btn = page.locator("text=次月").first

        if not next_btn.is_visible():
            print("  「次月」ボタンが見つかりません")
            break

        next_btn.click()
        page.wait_for_timeout(1500)

        current_text = get_current_month_text()
        print(f"  → {current_text}")

        if f"{month}月" in current_text:
            print(f"サービス提供月を設定しました: {current_text}")
            return

    print(f"警告: 目標の月({target_text})に到達できませんでした")


def save_artifacts(page: Page, out_dir: Path, prefix: str = "") -> None:
    """失敗時の証拠を保存（スクリーンショット、HTML）"""
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix_str = f"{prefix}_" if prefix else ""

    page.screenshot(path=str(out_dir / f"{prefix_str}screenshot_{ts}.png"), full_page=True)
    html = page.content()
    (out_dir / f"{prefix_str}page_{ts}.html").write_text(html, encoding="utf-8")

    print(f"アーティファクトを保存しました: {out_dir}")
