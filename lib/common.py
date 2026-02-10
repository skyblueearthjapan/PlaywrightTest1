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

    # エラーページチェック（最優先）
    # エラーページのURLには "kaipokebiz" や "nonmember" が含まれることがある
    current_url = page.url
    page_content = page.content()

    # エラーページの特徴的なテキストをチェック
    is_error_page = (
        "error" in current_url.lower() or
        "nonmember" in current_url.lower() or
        "システムエラー" in page_content or
        "処理を続行できませんでした" in page_content or
        "トップへ戻る" in page_content and "ブラウザの「戻る」" in page_content
    )

    if is_error_page:
        print("エラーページが検出されました。ログインページに戻ります...")
        # トップへ戻るボタンをクリックするか、直接ログインURLへ
        try:
            page.click("text=トップへ戻る", timeout=5000)
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
            page.wait_for_timeout(1000)
        except Exception:
            # ボタンがない場合は直接ログインURLへ
            page.goto(LOGIN_URL, timeout=timeout)
            page.wait_for_load_state("domcontentloaded", timeout=timeout)

    # すでにログイン済みかチェック（エラーページでないことを確認後）
    current_url = page.url
    if "biztop" not in current_url and "error" not in current_url.lower() and "nonmember" not in current_url.lower():
        # ダッシュボードの特徴的な要素をチェック
        try:
            dashboard_element = page.locator("text=レセプト").first
            if dashboard_element.is_visible(timeout=3000):
                print(f"すでにログイン済みです（URL: {current_url}）")
                return True
        except Exception:
            pass

    # ログインフォームが表示されているか確認
    corp_id_field = page.locator("#form\\:corporation_id")
    if not corp_id_field.is_visible(timeout=5000):
        # フォームがない場合、ログインURLへ再度遷移
        print("ログインフォームが見つかりません。ログインURLへ再遷移します...")
        page.goto(LOGIN_URL, timeout=timeout)
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
        page.wait_for_timeout(1000)

    # 再度フォームを確認
    corp_id_field = page.locator("#form\\:corporation_id")
    if not corp_id_field.is_visible(timeout=5000):
        print("警告: ログインフォームが見つかりません")
        return False

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
    """ポップアップや通知バーを閉じる（表示されている場合）

    カイポケはKARTE（マーケティングツール）の広告ポップアップを表示することがある。
    z-index: 2147483642 でサイドバーを覆い隠すため、確実に除去する必要がある。
    """
    try:
        # 1. KARTE広告ポップアップをJavaScriptで強制除去
        #    （閉じるボタンのクリックより確実）
        removed = page.evaluate("""() => {
            let count = 0;
            // KARTEのルート要素をすべて除去
            document.querySelectorAll('.karte-r, .karte-g, [id^="karte-"]').forEach(el => {
                el.remove();
                count++;
            });
            // 高z-indexのオーバーレイも除去（z-index > 2147483000）
            document.querySelectorAll('*').forEach(el => {
                const z = parseInt(window.getComputedStyle(el).zIndex);
                if (z > 2147483000) {
                    el.remove();
                    count++;
                }
            });
            return count;
        }""")
        if removed > 0:
            print(f"  KARTE広告ポップアップを除去しました（{removed}要素）")
            page.wait_for_timeout(500)

        # 2. 通知バーの閉じるボタン（KARTE以外）
        close_buttons = [
            ".karte-close",
            "[aria-label='閉じる']",
            "button.close",
            "a.close",
            ".notification-close",
            "[aria-label='Close']",
        ]
        for selector in close_buttons:
            try:
                close_btn = page.locator(selector).first
                if close_btn.is_visible(timeout=500):
                    close_btn.click()
                    page.wait_for_timeout(300)
            except Exception:
                continue
    except Exception:
        pass


def goto_receipt(page: Page, timeout: int = 60000) -> None:
    """
    レセプト画面に遷移

    ポップアップがレセプトボタンを覆っている場合はdismiss_popupを再実行してリトライする。

    Args:
        page: Playwrightのページオブジェクト
        timeout: タイムアウト（ミリ秒、デフォルト60秒）
    """
    # サイドバー内の「レセプト」リンクを特定するセレクタ（優先度順）
    # "text=レセプト" は通知メール件名等にもマッチするため使用しない
    receipt_selectors = [
        "#gNavBiz-wrap a:has(i.sprite-rezept)",       # アイコンクラスで特定（最も確実）
        "a[onclick*=\"pushClickIconEvent('レセプト')\"]",  # onclick属性で特定
        "#gNavBiz-wrap a:has-text('レセプト')",        # サイドバー内にスコープ
    ]

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            print("レセプトボタンをクリックしています...")
            clicked = False
            for selector in receipt_selectors:
                try:
                    btn = page.locator(selector).first
                    btn.wait_for(state="visible", timeout=5000)
                    btn.click(timeout=10000)
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                raise RuntimeError("レセプトリンクが見つかりません")

            try:
                page.wait_for_load_state("networkidle", timeout=timeout)
            except Exception:
                print("  (networkidle待機がタイムアウト、domcontentloadedで続行)")
                page.wait_for_load_state("domcontentloaded", timeout=timeout)
            page.wait_for_timeout(1000)
            print("レセプト画面を表示しました")
            return
        except Exception as e:
            if attempt < max_attempts - 1:
                print(f"  レセプトボタンのクリック失敗（{e}）")
                print(f"  ポップアップ除去を再試行します... ({attempt + 2}/{max_attempts})")
                dismiss_popup(page)
                page.wait_for_timeout(1000)
            else:
                raise RuntimeError(
                    f"レセプトボタンをクリックできませんでした（{max_attempts}回試行）: {e}"
                )


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
        # 方法1: <select>ドロップダウンから読み取り（サービス提供年月）
        try:
            selects = page.locator("select")
            for i in range(selects.count()):
                select = selects.nth(i)
                if select.is_visible(timeout=1000):
                    text = select.evaluate(
                        "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''"
                    )
                    if text and "令和" in text and "月" in text:
                        return text.strip()
        except Exception:
            pass

        # 方法2: ページ内の「令和X年Y月」テキスト要素を探す
        try:
            month_elem = page.locator("text=/令和\\d+年\\d+月/").first
            if month_elem.is_visible(timeout=1000):
                return month_elem.text_content().strip()
        except Exception:
            pass

        return ""

    # 現在の月をチェック
    current_text = get_current_month_text()
    print(f"  現在の月: '{current_text}'")

    if target_text in current_text:
        print(f"  すでに目標の月です: {target_text}")
        return

    # 目標月を超えていたら「前月」で戻る
    import re
    def extract_month_number(text):
        m = re.search(r'(\d+)月', text)
        return int(m.group(1)) if m else 0

    def extract_year_number(text):
        m = re.search(r'令和(\d+)年', text)
        return int(m.group(1)) if m else 0

    current_month_num = extract_month_number(current_text)
    current_year_num = extract_year_number(current_text)
    target_month_num = month
    target_year_num = reiwa_year

    # 前月/次月どちらに進むべきか判定
    current_total = current_year_num * 12 + current_month_num
    target_total = target_year_num * 12 + target_month_num
    go_forward = target_total >= current_total

    btn_text = "次月" if go_forward else "前月"
    btn_selector = "a.next, a:has-text('次月')" if go_forward else "a:has-text('前月')"
    print(f"  方向: {'次月→' if go_forward else '←前月'}（差: {abs(target_total - current_total)}ヶ月）")

    # ボタンで移動（最大12回）
    for i in range(12):
        nav_btn = page.locator(btn_selector).first
        if not nav_btn.is_visible():
            nav_btn = page.locator(f"text={btn_text}").first

        if not nav_btn.is_visible():
            print(f"  「{btn_text}」ボタンが見つかりません")
            break

        nav_btn.click()
        page.wait_for_timeout(1500)

        current_text = get_current_month_text()
        print(f"  → {current_text}")

        if target_text in current_text:
            print(f"サービス提供月を設定しました: {current_text}")
            return

    print(f"警告: 目標の月({target_text})に到達できませんでした")


def verify_service_month(page: Page, month_str: str) -> None:
    """
    サービス提供月が正しく設定されているか検証

    Args:
        page: Playwrightのページオブジェクト
        month_str: "2026-04" 形式の月文字列

    Raises:
        RuntimeError: 月の設定が不正な場合
    """
    year, month = parse_month(month_str)
    reiwa_year = to_reiwa(year)
    expected = f"令和{reiwa_year}年{month}月"

    actual = ""
    try:
        selects = page.locator("select")
        for i in range(selects.count()):
            select = selects.nth(i)
            if select.is_visible(timeout=1000):
                text = select.evaluate(
                    "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''"
                )
                if text and "令和" in text and "月" in text:
                    actual = text.strip()
                    break
    except Exception:
        pass

    if actual and expected not in actual:
        error_msg = f"月の設定が不正です！期待: {expected}, 実際: {actual}"
        print(f"エラー: {error_msg}")
        save_artifacts(page, Path("artifacts"), "wrong_month")
        raise RuntimeError(error_msg)

    print(f"月の検証OK: {actual or expected}")


def setup_monthly_schedule_page(
    page: Page,
    context: BrowserContext,
    month_str: str,
) -> None:
    """
    ログイン → レセプト → 訪問看護 → 月間スケジュール → 月設定 → 月検証
    を一括で行う共通セットアップ関数

    Args:
        page: Playwrightのページオブジェクト
        context: ブラウザコンテキスト（state保存用）
        month_str: "2026-04" 形式の月文字列
    """
    if not login(page, save_state=True, context=context):
        raise RuntimeError("ログインに失敗しました")
    page.wait_for_timeout(1000)
    dismiss_popup(page)
    goto_receipt(page)
    goto_yoriyori(page)
    goto_monthly_schedule(page)
    set_service_month(page, month_str)
    page.wait_for_timeout(1000)
    verify_service_month(page, month_str)


def check_session(page: Page) -> bool:
    """
    セッションが有効かどうかを確認

    ログインフォーム（#form\\:corporation_id）が表示されていたらセッション切れ。
    エラーページ（システムエラー等）もセッション無効とみなす。

    Returns:
        bool: セッションが有効ならTrue
    """
    try:
        current_url = page.url
        # ログインページにリダイレクトされている
        if "biztop" in current_url:
            login_form = page.locator("#form\\:corporation_id")
            if login_form.is_visible(timeout=2000):
                return False
        # エラーページ
        if "error" in current_url.lower() or "nonmember" in current_url.lower():
            return False
        # ページ内容でエラー検出
        content = page.content()
        if "システムエラー" in content or "処理を続行できませんでした" in content:
            return False
        return True
    except Exception:
        return False


def ensure_session(page: Page, context: BrowserContext, month_str: str = None) -> bool:
    """
    セッションが切れていたら再ログイン＋ナビゲーションを復旧

    Args:
        page: Playwrightのページオブジェクト
        context: ブラウザコンテキスト
        month_str: 復旧後に月間スケジュール画面まで戻る場合の月（Noneなら再ログインのみ）

    Returns:
        bool: 復旧に成功したらTrue
    """
    if check_session(page):
        return True

    print("セッション切れを検出。再ログインします...")
    try:
        login(page, save_state=True, context=context)
        page.wait_for_timeout(1000)
        dismiss_popup(page)

        if month_str:
            goto_receipt(page)
            goto_yoriyori(page)
            goto_monthly_schedule(page)
            set_service_month(page, month_str)
            page.wait_for_timeout(1000)

        print("セッション復旧完了")
        return True
    except Exception as e:
        print(f"セッション復旧に失敗: {e}")
        return False


def save_artifacts(page: Page, out_dir: Path, prefix: str = "") -> None:
    """失敗時の証拠を保存（スクリーンショット、HTML）"""
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix_str = f"{prefix}_" if prefix else ""

    page.screenshot(path=str(out_dir / f"{prefix_str}screenshot_{ts}.png"), full_page=True)
    html = page.content()
    (out_dir / f"{prefix_str}page_{ts}.html").write_text(html, encoding="utf-8")

    print(f"アーティファクトを保存しました: {out_dir}")
