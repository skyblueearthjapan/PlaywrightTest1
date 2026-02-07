"""
Google Drive連携モジュール

サービスアカウント認証を使用してGoogle Driveにファイルをアップロード/ダウンロードします。

セットアップ手順:
1. Google Cloud Consoleでプロジェクトを作成
2. Google Drive APIを有効化
3. サービスアカウントを作成し、JSONキーをダウンロード
4. credentials/service_account.json として保存
5. Google Driveで対象フォルダをサービスアカウントのメールアドレスと共有
"""

import os
from pathlib import Path
from typing import Optional

# Google API クライアント
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False
    print("警告: google-api-python-client がインストールされていません")
    print("pip install google-api-python-client google-auth でインストールしてください")

# プロジェクトルート
PROJECT_ROOT = Path(__file__).parent.parent

# サービスアカウント認証ファイルのパス
CREDENTIALS_PATH = PROJECT_ROOT / "credentials" / "service_account.json"

# スコープ（Driveへの読み書き権限）
SCOPES = ['https://www.googleapis.com/auth/drive']


def get_drive_service():
    """
    Google Drive APIサービスを取得

    Returns:
        Drive APIサービスオブジェクト、または None（認証失敗時）
    """
    if not GOOGLE_API_AVAILABLE:
        print("エラー: Google API クライアントが利用できません")
        return None

    if not CREDENTIALS_PATH.exists():
        print(f"エラー: 認証ファイルが見つかりません: {CREDENTIALS_PATH}")
        print("セットアップ手順:")
        print("1. Google Cloud Console でサービスアカウントを作成")
        print("2. JSONキーをダウンロード")
        print(f"3. {CREDENTIALS_PATH} として保存")
        return None

    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(CREDENTIALS_PATH),
            scopes=SCOPES
        )
        service = build('drive', 'v3', credentials=credentials)
        return service
    except Exception as e:
        print(f"Google Drive認証エラー: {e}")
        return None


def upload_to_drive(
    local_path: str,
    folder_id: str,
    filename: Optional[str] = None,
    overwrite: bool = True
) -> Optional[str]:
    """
    ファイルをGoogle Driveにアップロード

    Args:
        local_path: ローカルファイルのパス
        folder_id: アップロード先フォルダのID
        filename: Drive上でのファイル名（省略時はローカルファイル名を使用）
        overwrite: 同名ファイルがある場合に上書きするか

    Returns:
        str: アップロードされたファイルのID、または None（失敗時）
    """
    service = get_drive_service()
    if not service:
        return None

    local_file = Path(local_path)
    if not local_file.exists():
        print(f"エラー: ファイルが見つかりません: {local_path}")
        return None

    if filename is None:
        filename = local_file.name

    try:
        # 既存ファイルを検索（上書きの場合）
        existing_file_id = None
        if overwrite:
            query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
            results = service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            if files:
                existing_file_id = files[0]['id']

        # MIMEタイプを判定
        mime_type = 'text/csv' if local_file.suffix.lower() == '.csv' else 'application/octet-stream'

        # メディアオブジェクトを作成
        media = MediaFileUpload(str(local_file), mimetype=mime_type, resumable=True)

        if existing_file_id:
            # 既存ファイルを更新
            file = service.files().update(
                fileId=existing_file_id,
                media_body=media
            ).execute()
            print(f"ファイルを更新しました: {filename} (ID: {file['id']})")
        else:
            # 新規ファイルを作成
            file_metadata = {
                'name': filename,
                'parents': [folder_id]
            }
            file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id'
            ).execute()
            print(f"ファイルをアップロードしました: {filename} (ID: {file['id']})")

        return file['id']

    except Exception as e:
        print(f"アップロードエラー: {e}")
        return None


def download_from_drive(
    file_id: str,
    local_path: str
) -> bool:
    """
    Google Driveからファイルをダウンロード

    Args:
        file_id: ダウンロードするファイルのID
        local_path: 保存先のローカルパス

    Returns:
        bool: 成功したかどうか
    """
    service = get_drive_service()
    if not service:
        return False

    try:
        import io

        request = service.files().get_media(fileId=file_id)
        file_data = io.BytesIO()
        downloader = MediaIoBaseDownload(file_data, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"ダウンロード進捗: {int(status.progress() * 100)}%")

        # ファイルに保存
        local_file = Path(local_path)
        local_file.parent.mkdir(parents=True, exist_ok=True)

        with open(local_file, 'wb') as f:
            f.write(file_data.getvalue())

        print(f"ダウンロード完了: {local_path}")
        return True

    except Exception as e:
        print(f"ダウンロードエラー: {e}")
        return False


def list_files_in_folder(folder_id: str) -> list:
    """
    フォルダ内のファイル一覧を取得

    Args:
        folder_id: フォルダのID

    Returns:
        list: ファイル情報のリスト [{"id": "...", "name": "..."}, ...]
    """
    service = get_drive_service()
    if not service:
        return []

    try:
        query = f"'{folder_id}' in parents and trashed=false"
        results = service.files().list(
            q=query,
            fields="files(id, name, modifiedTime)"
        ).execute()

        files = results.get('files', [])
        return files

    except Exception as e:
        print(f"ファイル一覧取得エラー: {e}")
        return []


def find_file_by_name(folder_id: str, filename: str) -> Optional[str]:
    """
    フォルダ内のファイルを名前で検索

    Args:
        folder_id: フォルダのID
        filename: 検索するファイル名

    Returns:
        str: ファイルのID、または None（見つからない場合）
    """
    service = get_drive_service()
    if not service:
        return None

    try:
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])

        if files:
            return files[0]['id']
        return None

    except Exception as e:
        print(f"ファイル検索エラー: {e}")
        return None


# 設定ファイル管理
CONFIG_PATH = PROJECT_ROOT / "config" / "drive_config.json"


def load_drive_config() -> dict:
    """
    Google Drive設定を読み込む

    Returns:
        dict: 設定 {"folder_id": "...", ...}
    """
    import json

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)

    # デフォルト設定
    return {
        "folder_id": "",  # CSVを保存するフォルダID
        "current_csv_name": "current_{month}.csv",
        "optimized_csv_name": "optimized_{month}.csv",
    }


def save_drive_config(config: dict):
    """
    Google Drive設定を保存

    Args:
        config: 設定辞書
    """
    import json

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"設定を保存しました: {CONFIG_PATH}")
