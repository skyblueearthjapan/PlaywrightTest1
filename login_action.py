import os
from pathlib import Path
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# .env ファイルを読み込む
load_dotenv()

LOGIN_URL = "https://r.kaipoke.biz/biztop/"

def run_login():
    """
    .env ファイルから認証情報を読み込んでログインを実行
    """
    corp_id = os.environ.get("KAIPOKE_CORP_ID")
    user_id = os.environ.get("KAIPOKE_USER_ID")
    password = os.environ.get("KAIPOKE_PASSWORD")

    if not corp_id or not user_id or not password:
        raise ValueError(".env ファイルに KAIPOKE_CORP_ID, KAIPOKE_USER_ID, KAIPOKE_PASSWORD が設定されていません")

    with sync_playwright() as p:
        # Firefox を使用（Windows環境でより安定）
        browser = p.firefox.launch(headless=False)
        page = browser.new_page()

        print(f"ログインページを開いています: {LOGIN_URL}")
        page.goto(LOGIN_URL)

        # ページが読み込まれるまで待機
        page.wait_for_load_state("networkidle")

        # エラーページの場合は「トップへ戻る」をクリック
        if "error" in page.url or "nonmember" in page.url:
            print("エラーページが表示されました。トップへ戻るをクリックします...")
            page.click("text=トップへ戻る")
            page.wait_for_load_state("networkidle")

        print("認証情報を入力しています...")
        # 法人ID を入力
        page.fill("#form\\:corporation_id", corp_id)

        # ユーザーID を入力
        page.fill("#form\\:member_login_id", user_id)

        # パスワード を入力
        page.fill("#form\\:password", password)

        print("ログインボタンをクリックしています...")
        # ログインボタンをクリック（image型のinput要素）
        page.click("#form\\:logn_nochklogin")

        # ログイン後の画面が落ち着くまで待つ
        print("ログイン処理を待機しています...")
        page.wait_for_load_state("networkidle")

        # 目視確認用に5秒待機
        print("ログイン成功！5秒後にブラウザを閉じます...")
        page.wait_for_timeout(5000)

        browser.close()
        print("完了しました。")


if __name__ == "__main__":
    run_login()
