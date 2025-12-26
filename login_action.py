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
    # .env ファイルから読み込み（全角文字を除去）
    corp_id = os.environ.get("KAIPOKE_CORP_ID", "252650").strip()
    user_id = os.environ.get("KAIPOKE_USER_ID", "sYgRH").strip()
    password = os.environ.get("KAIPOKE_PASSWORD", "Yoriyori0401").strip()

    # 全角文字を半角に変換（念のため）
    import unicodedata
    user_id = unicodedata.normalize('NFKC', user_id)
    # さらに全角英数字を半角に変換
    user_id = user_id.translate(str.maketrans(
        'ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ０１２３４５６７８９',
        'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    ))

    if not corp_id or not user_id or not password:
        raise ValueError(".env ファイルに KAIPOKE_CORP_ID, KAIPOKE_USER_ID, KAIPOKE_PASSWORD が設定されていません")

    # デバッグ: 読み込んだ値を表示（パスワードは一部伏せる）
    print(f"法人ID: {corp_id}")
    print(f"ユーザーID: {user_id}")
    print(f"パスワード: {password[:2]}{'*' * (len(password)-2)}")

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
        print("  - 法人IDを入力中...")
        page.fill("#form\\:corporation_id", corp_id)
        page.wait_for_timeout(500)

        # ユーザーID を入力
        print("  - ユーザーIDを入力中...")
        page.fill("#form\\:member_login_id", user_id)
        page.wait_for_timeout(500)

        # パスワード を入力
        print("  - パスワードを入力中...")
        password_field = page.locator("#form\\:password")

        # パスワード欄にフォーカスを当てる
        password_field.click()
        page.wait_for_timeout(200)

        # 既存の値をクリア
        password_field.fill("")
        page.wait_for_timeout(200)

        # パスワードを入力
        password_field.fill(password)
        page.wait_for_timeout(500)

        # 入力されたか確認
        input_value = password_field.input_value()
        if input_value == password:
            print(f"  - パスワード入力確認: OK (長さ {len(input_value)} 文字)")
        else:
            print(f"  - 警告: パスワードが正しく入力されていません")

        print("  - すべての入力が完了しました")

        # ログインボタンをクリックする直前に、パスワードが消えていないか再確認
        page.wait_for_timeout(1000)
        check_value = password_field.input_value()
        if not check_value:
            print("  - 警告: パスワードが消されています。再入力します...")
            password_field.fill(password)
            page.wait_for_timeout(500)

        print("ログインボタンをクリックしています...")
        # ログインボタンをクリック
        page.click("#form\\:logn_nochklogin")

        # ログイン後の画面が落ち着くまで待つ
        print("ログイン処理を待機しています...")
        page.wait_for_load_state("networkidle")

        print("ログイン成功！")
        page.wait_for_timeout(2000)

        # ポップアップやモーダルが表示されている場合は閉じる
        try:
            print("ポップアップを閉じています...")
            # ポップアップの外側（暗い背景部分）をクリック
            # 左側の暗い部分をクリック（赤丸の位置）
            page.mouse.click(400, 650)
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"  ポップアップのクリックに失敗: {e}")

        # レセプトボタンをクリック
        print("レセプトボタンをクリックしています...")
        page.click("text=レセプト")
        page.wait_for_load_state("networkidle")

        print("レセプト画面を表示しました！")
        page.wait_for_timeout(2000)

        # 訪問看護のリンクをクリック
        print("訪問看護のリンクをクリックしています...")
        page.click("text=訪問看護/1260192047")
        page.wait_for_load_state("networkidle")

        print("訪問看護の詳細画面を表示しました！")
        # 目視確認用に5秒待機
        print("5秒後にブラウザを閉じます...")
        page.wait_for_timeout(5000)

        browser.close()
        print("完了しました。")


if __name__ == "__main__":
    run_login()
