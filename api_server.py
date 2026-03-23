"""
カイポケ自動化 API サーバー

GASアプリケーションからHTTPリクエストを受け取り、
Playwright自動化スクリプトを実行します。
VNC/noVNC経由でブラウザ画面をリアルタイム配信します。

起動方法:
    python api_server.py

エンドポイント:
    POST /api/expand  - 月間スケジュール展開
    POST /api/export  - CSV出力（async対応）
    GET  /api/export/result - CSV出力結果取得（ポーリング用）
    POST /api/apply   - 差分適用
    GET  /api/apply/result  - 差分適用結果取得（ポーリング用）
    POST /api/allocate - 配置エンジン実行
    GET  /api/status  - サーバー状態確認

    # VNC/ジョブ管理API
    POST /api/kaipoke/run    - Playwright実行開始
    POST /api/kaipoke/stop   - 実行停止
    GET  /api/kaipoke/status - 状態取得（VNC URL含む）
    GET  /api/kaipoke/logs   - ログ取得
"""

import sys
import os
import secrets
import subprocess
import threading
import time
import logging
from pathlib import Path
from datetime import datetime
from functools import wraps
from collections import deque
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# エンジンログをstdoutに出力（docker logsで確認可能に）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logging.getLogger("lib.allocation_engine").setLevel(logging.INFO)

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent))

from commands.expand import run_expand
from commands.export import run_export
from commands.auto_apply import run_auto_apply
from lib.stop_signal import request_stop, clear_stop, is_stop_requested
from lib.diff_engine import (
    compare_schedules,
    compare_schedules_from_content,
    generate_correction_sheet,
    load_correction_sheet,
    validate_correction_data,
    Correction,
)
from lib.google_drive import (
    load_drive_config,
    save_drive_config,
    upload_to_drive,
    download_from_drive,
    find_file_by_name,
    list_files_in_folder,
)

app = Flask(__name__)
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# ====== 設定 ======
API_TOKEN = os.environ.get("KAIPOKE_API_TOKEN", "default-dev-token")
VPS_HOST = os.environ.get("VPS_HOST", "kaipoke-api.net")
NOVNC_PORT = os.environ.get("NOVNC_PORT", "6080")

# ====== ジョブ管理 ======
job_state = {
    "id": None,
    "state": "idle",  # idle, running, failed, stopped
    "progress": None,
    "started_at": None,
    "ended_at": None,
    "last_error": None,
    "mode": None,
}

# ログリングバッファ（5000行）
log_buffer = deque(maxlen=5000)
log_lock = threading.Lock()

# noVNCトークン管理
vnc_tokens = {}  # {token: expiry_time}

# job_state / current_task の排他ロック
job_state_lock = threading.Lock()


def add_log(message: str):
    """ログを追加"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    with log_lock:
        log_buffer.append(f"{timestamp} {message}")


def generate_vnc_token(ttl_minutes: int = 30) -> str:
    """VNCトークンを生成"""
    token = secrets.token_urlsafe(16)
    expiry = time.time() + (ttl_minutes * 60)
    vnc_tokens[token] = expiry
    # 期限切れトークンをクリーンアップ
    current_time = time.time()
    expired = [t for t, exp in vnc_tokens.items() if exp < current_time]
    for t in expired:
        del vnc_tokens[t]
    return token


def validate_vnc_token(token: str) -> bool:
    """VNCトークンを検証"""
    if token not in vnc_tokens:
        return False
    if vnc_tokens[token] < time.time():
        del vnc_tokens[token]
        return False
    return True


def require_auth(f):
    """Bearer認証デコレータ"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"ok": False, "error": "Missing Authorization header"}), 401
        token = auth_header[7:]
        if token != API_TOKEN:
            return jsonify({"ok": False, "error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated


def add_security_headers(response: Response) -> Response:
    """セキュリティヘッダーを追加（iframe埋め込み許可）"""
    # X-Frame-Options は設定しない（iframe許可のため）
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors https://script.google.com https://*.googleusercontent.com"
    )
    return response


@app.after_request
def after_request(response):
    return add_security_headers(response)


# ====== 既存のAPIエンドポイント ======

# 実行中のタスクを追跡（レガシー互換）
current_task = {
    "running": False,
    "command": None,
    "started_at": None,
}

# apply結果を保存（非同期完了後にGASが取得可能）
apply_result_store = {
    "result": None,
    "completed_at": None,
    "error": None,
}

# apply進捗を保存（実行中にGASがポーリングで取得）
apply_progress = {
    "processed": 0,
    "total": 0,
    "phase": "",
    "current_name": "",
    "success": 0,
    "failed": 0,
    "skipped": 0,
    "updated_at": None,
}

# export結果を保存（非同期完了後にGASが取得可能）
export_result_store = {
    "result": None,
    "completed_at": None,
    "error": None,
}


@app.route('/api/status', methods=['GET'])
def api_status():
    """サーバー状態確認"""
    with job_state_lock:
        return jsonify({
            "status": "running",
            "current_task": dict(current_task),
            "job": dict(job_state),
            "stop_requested": is_stop_requested(),
            "timestamp": datetime.now().isoformat(),
        })


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """
    非常停止 API

    実行中のPlaywright処理を安全に停止します。
    現在処理中の利用者の操作が完了した後に停止します。
    """
    global current_task, job_state

    request_stop()
    add_log("非常停止が要求されました")

    with job_state_lock:
        if current_task["running"]:
            current_task["command"] = f"{current_task['command']}(停止中)"
            add_log(f"実行中のタスクを停止します: {current_task['command']}")

        if job_state["state"] == "running":
            job_state["state"] = "stopped"
            job_state["progress"] = "stopped"
            job_state["ended_at"] = datetime.now().isoformat()

        return jsonify({
            "success": True,
            "message": "非常停止を要求しました。現在の利用者の処理完了後に停止します。",
            "current_task": dict(current_task),
            "job": dict(job_state),
        })


@app.route('/api/expand', methods=['POST'])
def api_expand():
    """月間スケジュール展開 API"""
    global current_task

    with job_state_lock:
        if current_task["running"]:
            return jsonify({
                "success": False,
                "error": "別のタスクが実行中です",
                "current_task": dict(current_task),
            }), 409

        current_task = {
            "running": True,
            "command": "expand",
            "started_at": datetime.now().isoformat(),
        }

    try:
        data = request.get_json() or {}
        month = data.get("month", "2026-04")

        clear_stop()  # 非常停止フラグをクリア
        add_log(f"expand 開始 (month={month})")
        print(f"\n=== API: expand 開始 (month={month}) ===")

        # headed=Trueで実行（VNCで見えるように）
        headless = data.get("headless", False)
        result = run_expand(month=month, headless=headless)

        add_log(f"expand 完了: {result}")
        return jsonify({
            "success": True,
            "result": result,
        })

    except Exception as e:
        add_log(f"expand エラー: {e}")
        print(f"エラー: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500

    finally:
        with job_state_lock:
            current_task = {"running": False, "command": None, "started_at": None}


def _run_export_core(data):
    """export処理の共通ロジック。結果dictを返す。"""
    month = data.get("month", "2026-04")
    out_path = data.get("out_path")
    auto_drive_upload = data.get("auto_drive_upload", False)
    week_start = data.get("week_start")  # "2026-04-06" or "20260406"
    week_end = data.get("week_end")       # "2026-04-12" or "20260412"

    headless = data.get("headless", False)
    result = run_export(
        month=month,
        out_path=out_path,
        headless=headless,
    )

    # CSV内容をレスポンスに含める
    csv_path = Path(result.get("file_path", ""))
    if csv_path.exists():
        csv_content = None
        for enc in ["cp932", "utf-8-sig", "utf-8"]:
            try:
                csv_content = csv_path.read_text(encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        if csv_content:
            result["csv_content"] = csv_content
            result["row_count"] = csv_content.count("\n")
            result["file_size_bytes"] = csv_path.stat().st_size

    # export_timestamp を追加
    result["export_timestamp"] = datetime.now().isoformat()

    # drive_file 情報を追加（GAS側でDriveアップロードする際に使用）
    try:
        config = load_drive_config()
        folder_id = config.get("folder_id")
        if folder_id:
            month_str = month.replace("-", "")
            drive_filename = config.get("current_csv_name", "kaipoke_export_{month}.csv")
            drive_filename = drive_filename.replace("{month}", month_str)
            result["drive_file"] = {
                "filename": drive_filename,
                "folder_id": folder_id,
            }
            # week_start/week_end が指定された場合、検証用ファイル名も追加
            if week_start and week_end:
                ws = week_start.replace("-", "")
                we = week_end.replace("-", "")
                result["drive_file"]["verification_filename"] = f"kaipoke_current_{ws}_{we}_post_apply.csv"
    except Exception:
        pass

    # Google Driveに自動アップロード（Shared Drive使用時のみ有効）
    if auto_drive_upload and result.get("success"):
        try:
            config = load_drive_config()
            folder_id = config.get("folder_id")
            if folder_id:
                month_str = month.replace("-", "")
                drive_filename = config.get("current_csv_name", "kaipoke_export_{month}.csv")
                drive_filename = drive_filename.replace("{month}", month_str)
                file_id = upload_to_drive(
                    str(csv_path), folder_id, filename=drive_filename
                )
                if file_id:
                    result["drive_file_id"] = file_id
                    result["drive_filename"] = drive_filename
                    add_log(f"Driveアップロード完了: {drive_filename}")
                else:
                    add_log("Driveアップロードに失敗（サービスアカウント未設定の可能性）")
            else:
                add_log("Drive folder_idが未設定です")
        except Exception as e:
            add_log(f"Driveアップロードエラー: {e}")
            result["drive_error"] = str(e)

    return result


@app.route('/api/export', methods=['POST'])
def api_export():
    """CSV出力 API（Drive自動保存対応・非同期モード対応）"""
    global current_task, export_result_store

    with job_state_lock:
        if current_task["running"]:
            return jsonify({
                "success": False,
                "error": "別のタスクが実行中です",
                "current_task": dict(current_task),
            }), 409
        current_task = {
            "running": True,
            "command": "export",
            "started_at": datetime.now().isoformat(),
        }

    async_mode = False
    try:
        data = request.get_json() or {}
        month = data.get("month", "2026-04")
        async_mode = data.get("async", False)

        clear_stop()  # 非常停止フラグをクリア
        add_log(f"export 開始 (month={month}, async={async_mode})")
        print(f"\n=== API: export 開始 (month={month}, async={async_mode}) ===")

        if async_mode:
            # 非同期モード: バックグラウンドスレッドで実行し即座にレスポンス
            export_result_store = {"result": None, "completed_at": None, "error": None}

            # リクエストデータをコピー（Flaskリクエストコンテキスト外で使用するため）
            request_data = dict(data)

            def run_export_async():
                global current_task, export_result_store
                try:
                    result = _run_export_core(request_data)
                    add_log(f"export 完了: success={result.get('success')}")
                    with job_state_lock:
                        export_result_store = {
                            "result": result,
                            "completed_at": datetime.now().isoformat(),
                            "error": None,
                        }
                except Exception as e:
                    add_log(f"export エラー: {e}")
                    print(f"エラー: {e}")
                    import traceback
                    traceback.print_exc()
                    with job_state_lock:
                        export_result_store = {
                            "result": None,
                            "completed_at": datetime.now().isoformat(),
                            "error": str(e),
                        }
                finally:
                    with job_state_lock:
                        current_task = {"running": False, "command": None, "started_at": None}

            thread = threading.Thread(target=run_export_async, daemon=True)
            thread.start()

            return jsonify({
                "success": True,
                "async": True,
                "message": "CSV出力を開始しました。/api/export/result でポーリングしてください。",
            })

        else:
            # 同期モード: 従来どおり結果を直接返す
            result = _run_export_core(data)
            add_log(f"export 完了: success={result.get('success')}")
            return jsonify({
                "success": True,
                "result": result,
            })

    except Exception as e:
        add_log(f"export エラー: {e}")
        print(f"エラー: {e}")
        with job_state_lock:
            current_task = {"running": False, "command": None, "started_at": None}
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500

    finally:
        # 同期モードの場合のみここでリセット（非同期はスレッド内でリセット）
        if not async_mode:
            with job_state_lock:
                current_task = {"running": False, "command": None, "started_at": None}


@app.route('/api/export/result', methods=['GET'])
def api_export_result():
    """CSV出力の結果を取得（ポーリング用）

    GASから定期的に呼び出し、exportが完了したかを確認する。
    - status=running: まだ実行中
    - status=completed: 完了（結果あり、csv_content含む）
    - status=error: エラーで終了
    - status=no_result: まだ一度も実行されていない
    """
    with job_state_lock:
        running = current_task.get("running", False) and current_task.get("command") == "export"

    if running:
        return jsonify({
            "success": True,
            "status": "running",
            "message": "CSV出力中...",
            "started_at": current_task.get("started_at"),
        })

    # 完了済み
    with job_state_lock:
        store = dict(export_result_store)

    if store.get("error"):
        return jsonify({
            "success": False,
            "status": "error",
            "error": store["error"],
            "completed_at": store.get("completed_at"),
        })

    if store.get("result"):
        return jsonify({
            "success": True,
            "status": "completed",
            "result": store["result"],
            "completed_at": store.get("completed_at"),
        })

    # まだ一度も実行されていない
    return jsonify({
        "success": False,
        "status": "no_result",
        "message": "export結果がありません。先に /api/export を呼び出してください。",
    })


@app.route('/api/apply', methods=['POST'])
def api_apply():
    """差分適用 API（非同期実行 — Cloudflare 524タイムアウト対策）

    即座にレスポンスを返し、バックグラウンドで適用処理を実行する。
    GASは /api/apply/result でポーリングして結果を取得する。
    """
    global current_task, apply_result_store

    with job_state_lock:
        if current_task["running"]:
            return jsonify({
                "success": False,
                "error": "別のタスクが実行中です",
                "current_task": dict(current_task),
            }), 409

        current_task = {
            "running": True,
            "command": "apply",
            "started_at": datetime.now().isoformat(),
        }

    try:
        data = request.get_json() or {}
        month = data.get("month", "2026-04")
        correction_sheet = data.get("correction_sheet", "data/correction_sheet.json")
        correction_data = data.get("correction_data")  # インラインデータ
        dry_run = data.get("dry_run", True)
        headed = data.get("headed", True)  # デフォルトでVNC表示
        limit = data.get("limit")
        action_filter = data.get("action_filter")  # "edit", "add", "delete", "date_change"
        business_type_filter = data.get("business_type_filter")  # "医療保険", "介護保険", "イベント"
        target_users = data.get("target_users")  # ["山田太郎", "佐藤花子"]

        # インラインデータが提供された場合、ファイルに保存
        if correction_data:
            import json
            # correction_data が配列の場合は dict でラップ
            if isinstance(correction_data, list):
                correction_data = {"corrections": correction_data}
            correction_sheet = "data/correction_sheet.json"
            Path("data").mkdir(parents=True, exist_ok=True)
            with open(correction_sheet, "w", encoding="utf-8") as f:
                json.dump(correction_data, f, ensure_ascii=False, indent=2)
            add_log("インラインcorrectionデータをファイルに保存")

        if not Path(correction_sheet).exists():
            with job_state_lock:
                current_task = {"running": False, "command": None, "started_at": None}
            return jsonify({
                "success": False,
                "error": f"修正シートが見つかりません: {correction_sheet}",
            }), 404

        clear_stop()  # 非常停止フラグをクリア
        filter_info = ""
        if action_filter:
            filter_info += f", action={action_filter}"
        if business_type_filter:
            filter_info += f", business_type={business_type_filter}"
        if target_users:
            filter_info += f", users={target_users}"
        if limit:
            filter_info += f", limit={limit}"
        add_log(f"apply 開始 (month={month}, dry_run={dry_run}{filter_info})")
        print(f"\n=== API: apply 開始 (month={month}, dry_run={dry_run}{filter_info}) ===")

        # 結果ストア・進捗をリセット
        apply_result_store = {"result": None, "completed_at": None, "error": None}
        apply_progress = {
            "processed": 0, "total": 0, "phase": "starting",
            "current_name": "", "success": 0, "failed": 0, "skipped": 0,
            "updated_at": datetime.now().isoformat(),
        }

        # バックグラウンドスレッドで実行
        def run_apply_async():
            global current_task, apply_result_store, apply_progress

            # 進捗コールバック
            def on_progress(progress_data):
                with job_state_lock:
                    apply_progress.update(progress_data)
                    apply_progress["updated_at"] = datetime.now().isoformat()

            try:
                result = run_auto_apply(
                    correction_sheet=correction_sheet,
                    month=month,
                    headless=not headed,
                    dry_run=dry_run,
                    limit=limit,
                    action_filter=action_filter,
                    business_type_filter=business_type_filter,
                    target_users=target_users,
                    progress_callback=on_progress,
                )
                add_log(f"apply 完了: success={result.get('success', 0)}, failed={result.get('failed', 0)}")
                with job_state_lock:
                    apply_result_store = {
                        "result": result,
                        "completed_at": datetime.now().isoformat(),
                        "error": None,
                    }
            except Exception as e:
                add_log(f"apply エラー: {e}")
                print(f"エラー: {e}")
                import traceback
                traceback.print_exc()
                with job_state_lock:
                    apply_result_store = {
                        "result": None,
                        "completed_at": datetime.now().isoformat(),
                        "error": str(e),
                    }
            finally:
                with job_state_lock:
                    current_task = {"running": False, "command": None, "started_at": None}

        thread = threading.Thread(target=run_apply_async, daemon=True)
        thread.start()

        return jsonify({
            "success": True,
            "async": True,
            "message": "差分適用を開始しました。/api/apply/result でポーリングしてください。",
        })

    except Exception as e:
        add_log(f"apply 開始エラー: {e}")
        print(f"エラー: {e}")
        import traceback
        traceback.print_exc()
        with job_state_lock:
            current_task = {"running": False, "command": None, "started_at": None}
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


@app.route('/api/apply/result', methods=['GET'])
def api_apply_result():
    """差分適用の結果を取得（ポーリング用）

    GASから定期的に呼び出し、適用が完了したかを確認する。
    - running=true: まだ実行中
    - running=false + result: 完了（結果あり）
    - running=false + error: エラーで終了
    """
    with job_state_lock:
        running = current_task.get("running", False) and current_task.get("command") == "apply"

    if running:
        with job_state_lock:
            progress = dict(apply_progress)
        return jsonify({
            "success": True,
            "status": "running",
            "message": "適用処理を実行中です...",
            "started_at": current_task.get("started_at"),
            "progress": progress,
        })

    # 完了済み
    with job_state_lock:
        store = dict(apply_result_store)

    if store.get("error"):
        return jsonify({
            "success": False,
            "status": "error",
            "error": store["error"],
            "completed_at": store.get("completed_at"),
        })

    if store.get("result"):
        return jsonify({
            "success": True,
            "status": "completed",
            "result": store["result"],
            "completed_at": store.get("completed_at"),
        })

    # まだ一度も実行されていない
    return jsonify({
        "success": False,
        "status": "no_result",
        "message": "適用結果がありません。先に /api/apply を呼び出してください。",
    })


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """設定 API"""
    if request.method == 'GET':
        config = load_drive_config()
        return jsonify({
            "success": True,
            "config": config,
        })

    else:  # POST
        try:
            data = request.get_json() or {}
            config = load_drive_config()

            if "folder_id" in data:
                config["folder_id"] = data["folder_id"]

            save_drive_config(config)

            return jsonify({
                "success": True,
                "config": config,
            })

        except Exception as e:
            return jsonify({
                "success": False,
                "error": str(e),
            }), 500


@app.route('/api/diff', methods=['POST'])
def api_diff():
    """差分確認 API（CSV内容受信 / Drive自動読み込み対応）"""
    try:
        import json as json_module
        data = request.get_json() or {}

        # パターンA: Driveから自動読み込み
        use_drive = data.get("use_drive", False)
        # パターンB: CSVコンテンツ直接送信
        current_csv_content = data.get("current_csv_content")
        optimized_csv_content = data.get("optimized_csv_content")
        # パターンC: 既存のファイルパス指定
        current_csv = data.get("current_csv")
        optimized_csv = data.get("optimized_csv")

        month = data.get("month", "2026-04")
        week_start = data.get("week_start")
        week_end = data.get("week_end")
        output_path = data.get("output_path", "data/correction_sheet.json")

        # week_start/week_end を日の数値(int)に変換
        # YYYYMMDD形式 (例: "20260202") → 日のみ (例: 2)
        # YYYY-MM-DD形式 (例: "2026-02-02") → 日のみ (例: 2)
        # 数値のみ (例: "2") → そのまま
        week_start_day = None
        week_end_day = None
        if week_start:
            ws_clean = str(week_start).replace("-", "")
            if len(ws_clean) == 8:  # YYYYMMDD
                week_start_day = int(ws_clean[6:8])
            else:
                week_start_day = int(ws_clean)
        if week_end:
            we_clean = str(week_end).replace("-", "")
            if len(we_clean) == 8:  # YYYYMMDD
                week_end_day = int(we_clean[6:8])
            else:
                week_end_day = int(we_clean)

        add_log(f"diff 開始 (month={month}, week={week_start_day}-{week_end_day})")
        print(f"[DEBUG /api/diff] リクエストパラメータ:")
        print(f"  use_drive={use_drive}, month={month}")
        print(f"  week_start={week_start} → day={week_start_day}")
        print(f"  week_end={week_end} → day={week_end_day}")
        print(f"  current_csv={current_csv}, optimized_csv={optimized_csv}")
        print(f"  current_csv_content: {'あり (' + str(len(current_csv_content)) + '文字)' if current_csv_content else 'なし'}")
        print(f"  optimized_csv_content: {'あり (' + str(len(optimized_csv_content)) + '文字)' if optimized_csv_content else 'なし'}")

        corrections = None

        # パターンA: Driveから自動ダウンロード
        if use_drive:
            config = load_drive_config()
            folder_id = config.get("folder_id")
            if not folder_id:
                return jsonify({"success": False, "error": "Drive folder_idが未設定です"}), 400

            month_str = month.replace("-", "")
            current_name = config.get("current_csv_name", "kaipoke_current_{month}.csv").replace("{month}", month_str)

            # 最適化CSVは週単位: week_start, week_end (YYYYMMDD) が必要
            if not week_start or not week_end:
                return jsonify({"success": False, "error": "use_drive=true の場合、week_start と week_end (YYYYMMDD形式) が必要です"}), 400
            ws = week_start.replace("-", "")
            we = week_end.replace("-", "")
            optimized_name = config.get("optimized_csv_name", "gas_optimized_{week_start}_{week_end}.csv")
            optimized_name = optimized_name.replace("{week_start}", ws).replace("{week_end}", we)

            print(f"[DEBUG /api/diff] パターンA: Drive自動ダウンロード")
            print(f"  current_name: {current_name}")
            print(f"  optimized_name: {optimized_name}")
            print(f"  folder_id: {folder_id}")

            # Driveからダウンロード
            current_file_id = find_file_by_name(folder_id, current_name)
            optimized_file_id = find_file_by_name(folder_id, optimized_name)
            print(f"  current_file_id: {current_file_id}")
            print(f"  optimized_file_id: {optimized_file_id}")

            if not current_file_id:
                return jsonify({"success": False, "error": f"Driveに {current_name} が見つかりません"}), 404
            if not optimized_file_id:
                return jsonify({"success": False, "error": f"Driveに {optimized_name} が見つかりません"}), 404

            Path("data").mkdir(parents=True, exist_ok=True)
            current_local = f"data/{current_name}"
            optimized_local = f"data/{optimized_name}"

            if not download_from_drive(current_file_id, current_local):
                return jsonify({"success": False, "error": f"{current_name} のダウンロードに失敗"}), 500
            if not download_from_drive(optimized_file_id, optimized_local):
                return jsonify({"success": False, "error": f"{optimized_name} のダウンロードに失敗"}), 500

            # ダウンロードしたファイルのサイズを確認
            cur_size = os.path.getsize(current_local) if os.path.exists(current_local) else 0
            opt_size = os.path.getsize(optimized_local) if os.path.exists(optimized_local) else 0
            print(f"  [DEBUG] ダウンロード完了: current={cur_size}bytes, optimized={opt_size}bytes")

            corrections = compare_schedules(
                current_csv=current_local,
                optimized_csv=optimized_local,
                target_week_start=week_start_day,
                target_week_end=week_end_day,
            )

        # パターンB: CSVコンテンツ直接送信
        elif current_csv_content and optimized_csv_content:
            print(f"[DEBUG /api/diff] パターンB: CSVコンテンツ直接送信")
            corrections = compare_schedules_from_content(
                current_content=current_csv_content,
                optimized_content=optimized_csv_content,
                target_week_start=week_start_day,
                target_week_end=week_end_day,
            )

        # パターンC: ファイルパス指定
        elif current_csv and optimized_csv:
            print(f"[DEBUG /api/diff] パターンC: ファイルパス指定 current={current_csv}, optimized={optimized_csv}")
            corrections = compare_schedules(
                current_csv=current_csv,
                optimized_csv=optimized_csv,
                target_week_start=week_start_day,
                target_week_end=week_end_day,
            )

        else:
            return jsonify({
                "success": False,
                "error": "use_drive=true、current_csv_content+optimized_csv_content、または current_csv+optimized_csv のいずれかが必要です",
            }), 400

        # 修正シートを生成・保存
        generate_correction_sheet(corrections, output_path, format="json")
        csv_path = output_path.replace(".json", ".csv")
        generate_correction_sheet(corrections, csv_path, format="csv")

        # 差分結果CSVの内容を読み込み（GAS側でDriveアップロード用）
        csv_content = ""
        drive_upload_info = None
        try:
            csv_abs_path = str(Path(csv_path).resolve())
            if os.path.exists(csv_abs_path):
                with open(csv_abs_path, "r", encoding="utf-8-sig") as f:
                    csv_content = f.read()

            # GAS側でアップロードする際の推奨ファイル名を生成
            config = load_drive_config()
            folder_id = config.get("folder_id", "")
            ws = str(week_start).replace("-", "") if week_start else "unknown"
            we = str(week_end).replace("-", "") if week_end else "unknown"
            name_template = config.get("diff_result_csv_name", "diff_result_{week_start}_{week_end}.csv")
            drive_filename = name_template.format(week_start=ws, week_end=we)
            drive_upload_info = {
                "filename": drive_filename,
                "folder_id": folder_id,
            }
            add_log(f"[Drive] CSV準備完了: {drive_filename} ({len(csv_content)}bytes), GAS側でアップロード予定")
        except Exception as e:
            add_log(f"警告: CSV読み込み中にエラー: {e}")
            print(f"[WARN] CSV read error: {e}", flush=True)

        # サマリーテキスト生成
        time_changes = sum(1 for c in corrections if c.has_time_change())
        staff_changes = sum(1 for c in corrections if c.has_staff_change())
        date_changes = sum(1 for c in corrections if c.has_date_change())
        additions = sum(1 for c in corrections if c.action == "add")
        deletions = sum(1 for c in corrections if c.action == "delete")
        edits = sum(1 for c in corrections if c.action == "edit")
        date_change_actions = sum(1 for c in corrections if c.action == "date_change")

        events = sum(1 for c in corrections if c.is_event())
        by_business_type = {}
        for c in corrections:
            bt = c.business_type or "(未設定)"
            by_business_type[bt] = by_business_type.get(bt, 0) + 1

        summary_text = f"編集: {edits}件, 日付移動: {date_change_actions}件, 時間変更: {time_changes}件, スタッフ変更: {staff_changes}件, 日付変更: {date_changes}件, 追加: {additions}件, 削除: {deletions}件, イベント: {events}件"

        # correction_sheet JSONを文字列として含める
        correction_sheet_data = {
            "total_corrections": len(corrections),
            "summary": {
                "time_changes": time_changes,
                "staff_changes": staff_changes,
                "date_changes": date_changes,
                "additions": additions,
                "deletions": deletions,
                "edits": edits,
                "date_change_actions": date_change_actions,
                "events": events,
                "by_business_type": by_business_type,
            },
            "corrections": [
                {
                    "user_name": c.user_name,
                    "date_from": c.date_from,
                    "date_to": c.date_to,
                    "start_time_from": c.start_time_from,
                    "start_time_to": c.start_time_to,
                    "end_time_from": c.end_time_from,
                    "end_time_to": c.end_time_to,
                    "staff1_from": c.staff1_from,
                    "staff1_to": c.staff1_to,
                    "staff2_from": c.staff2_from,
                    "staff2_to": c.staff2_to,
                    "service_type": c.service_type,
                    "action": c.action,
                    "business_type": c.business_type,
                    "remarks": c.remarks,
                }
                for c in corrections
            ],
        }

        result = {
            "total_corrections": len(corrections),
            "summary": {
                "time_changes": time_changes,
                "staff_changes": staff_changes,
                "date_changes": date_changes,
                "additions": additions,
                "deletions": deletions,
                "edits": edits,
                "date_change_actions": date_change_actions,
                "events": events,
                "by_business_type": by_business_type,
                "summary_text": summary_text,
            },
            "corrections": correction_sheet_data["corrections"],
            "correction_sheet_json": json_module.dumps(correction_sheet_data, ensure_ascii=False),
            "output_files": {
                "json": output_path,
                "csv": csv_path,
            },
            "drive_file": drive_upload_info,
            "csv_content": csv_content,
        }

        add_log(f"diff 完了: {summary_text}")
        return jsonify({
            "success": True,
            "result": result,
        })

    except FileNotFoundError as e:
        return jsonify({
            "success": False,
            "error": f"ファイルが見つかりません: {str(e)}",
        }), 404

    except Exception as e:
        add_log(f"diff エラー: {e}")
        print(f"エラー: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


@app.route('/api/diff/validate', methods=['POST'])
def api_diff_validate():
    """差分データ検証 API - Playwright適用に十分なデータかを検証"""
    try:
        import json as json_module
        data = request.get_json() or {}
        correction_data = data.get("correction_data")
        correction_sheet = data.get("correction_sheet")

        corrections = []

        if correction_data:
            # インラインデータから読み込み（配列/dict両対応）
            items = correction_data if isinstance(correction_data, list) else correction_data.get("corrections", [])
            for item in items:
                corrections.append(Correction(**item))
        elif correction_sheet and Path(correction_sheet).exists():
            # ファイルから読み込み
            corrections = load_correction_sheet(correction_sheet)
        else:
            # デフォルトファイル
            default_path = "data/correction_sheet.json"
            if Path(default_path).exists():
                corrections = load_correction_sheet(default_path)
            else:
                return jsonify({
                    "success": False,
                    "error": "correction_data または correction_sheet が必要です",
                }), 400

        result = validate_correction_data(corrections)
        add_log(f"diff/validate: valid={result['valid']}, total={result['total_corrections']}")

        return jsonify({
            "success": True,
            "result": result,
        })

    except Exception as e:
        print(f"エラー: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


@app.route('/api/drive/files', methods=['GET'])
def api_drive_files():
    """Driveフォルダ内のファイル一覧を取得"""
    try:
        config = load_drive_config()
        folder_id = config.get("folder_id")
        if not folder_id:
            return jsonify({"success": False, "error": "Drive folder_idが未設定です"}), 400

        files = list_files_in_folder(folder_id)
        return jsonify({
            "success": True,
            "files": files,
            "folder_id": folder_id,
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


@app.route('/api/test', methods=['GET', 'POST'])
def api_test():
    """接続テスト API"""
    result = {
        "success": True,
        "message": "接続テスト成功 - Playwrightサーバーは正常に動作しています",
        "server_time": datetime.now().isoformat(),
        "api_version": "2.0.0",
        "vnc_enabled": True,
        "endpoints": [
            "GET  /api/status        - サーバー状態確認",
            "GET  /api/test          - 接続テスト",
            "POST /api/expand        - 月間スケジュール展開",
            "POST /api/export        - CSV出力（async対応・Drive自動保存）",
            "GET  /api/export/result - CSV出力結果取得（ポーリング用）",
            "POST /api/diff          - 差分確認（CSV内容/Drive/ファイルパス対応）",
            "POST /api/diff/validate - 差分データ検証",
            "POST /api/apply         - 差分適用（インラインデータ対応）",
            "GET  /api/drive/files   - Driveファイル一覧",
            "GET/POST /api/config    - 設定取得/更新",
        ]
    }

    if request.method == 'POST':
        data = request.get_json() or {}
        result["received_data"] = data
        result["message"] = "接続テスト成功 - POSTデータを受信しました"

    return jsonify(result)


# ====== 配置エンジンAPI ======

@app.route('/api/allocate', methods=['POST'])
def api_allocate():
    """Run the allocation engine with provided data."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data"}), 400

        # Lazy imports to avoid startup issues
        from lib.allocation_models import (
            Patient, Staff, VisitRequest, Event, StaffChange,
            PatientChange, SpecialWeekHeader, SpecialWeekDetail, MentorPair,
            WeeklyPattern, ConfirmedHistory
        )
        from lib.allocation_engine import AllocationEngine

        add_log("allocate 開始")
        print("\n=== API: allocate 開始 ===")

        # Parse staff masters
        staff_list = []
        for s in data.get("staff_masters", []):
            staff_list.append(Staff(
                sid=s["staff_id"],
                name=s.get("staff_name", ""),
                gender=s.get("gender", ""),
                lat=s.get("latitude"),
                lng=s.get("longitude"),
                shift_start_min=s.get("shift_start_minutes", 540),
                shift_end_min=s.get("shift_end_minutes", 1080),
                work_days=s.get("work_days", []),
                areas=s.get("areas", []),
                max_per_day=s.get("max_per_day", 999),
                alloc_pref=s.get("alloc_pref", "均等"),
            ))

        # Parse patient masters
        patient_map = {}
        for p in data.get("patient_masters", []):
            patient_map[p["patient_id"]] = Patient(
                pid=p["patient_id"],
                name=p.get("patient_name", ""),
                area=p.get("area", ""),
                lat=p.get("latitude"),
                lng=p.get("longitude"),
                service_minutes=p.get("service_minutes", 60),
                day_priority=p.get("day_priority", "低"),
            )

        # Parse events
        events = []
        for e in data.get("events", []):
            events.append(Event(
                event_id=e["event_id"],
                staff_id=e["staff_id"],
                date_str=e["date"],
                event_type=e.get("event_type", ""),
                title=e.get("title", ""),
                start_min=e.get("start_time_minutes"),
                end_min=e.get("end_time_minutes"),
                duration_min=e.get("duration_minutes", 60),
                fixed_slot=e.get("fixed_slot", True),
            ))

        # Parse staff changes
        staff_changes = []
        for sc in data.get("staff_changes", []):
            staff_changes.append(StaffChange(
                staff_id=sc["staff_id"],
                date_str=sc["date"],
                restriction_type=sc["restriction_type"],
                start_min=sc.get("start_time_minutes"),
                end_min=sc.get("end_time_minutes"),
            ))

        # Parse weekly patterns (optional)
        weekly_patterns = []
        for wp in data.get("weekly_patterns", []):
            weekly_patterns.append(WeeklyPattern(
                pid=wp["patient_id"],
                pname=wp.get("patient_name", ""),
                day_code=wp.get("day_code", ""),
                start_min=wp.get("start_time_minutes"),
                end_min=wp.get("end_time_minutes"),
                service_min=wp.get("service_minutes", 60),
                need_staff=wp.get("need_staff", 1),
                note=wp.get("note", ""),
            ))

        # Parse confirmed history (optional)
        confirmed_history = []
        for ch in data.get("confirmed_history", []):
            confirmed_history.append(ConfirmedHistory(
                week_start=ch.get("week_start", ""),
                date_str=ch.get("date", ""),
                pid=ch.get("patient_id", ""),
                staff_id=ch.get("staff_id", ""),
                staff_name=ch.get("staff_name", ""),
            ))

        # Parse weekly requests
        requests = []
        for r in data.get("weekly_requests", []):
            requests.append(VisitRequest(
                request_id=r.get("request_id", ""),
                date_str=r["date"],
                weekday=r.get("weekday", ""),
                pid=r["patient_id"],
                pname=r.get("patient_name", ""),
                area=r.get("area", ""),
                start_min=r.get("start_time_minutes"),
                end_min=r.get("end_time_minutes"),
                service_min=r.get("service_minutes", 60),
                need_staff=r.get("need_staff", 1),
                specified_staff_ids=r.get("specified_staff_ids", []),
                specified_type=r.get("specified_type", ""),
                ng_staff_ids=r.get("ng_staff_ids", []),
                sex_limit=r.get("sex_limit", ""),
                cont_pref=r.get("continuation_pref", ""),
                time_type=r.get("time_type", ""),
                earliest_min=r.get("earliest_time_minutes"),
                latest_min=r.get("latest_time_minutes"),
                prev_staff_id=r.get("prev_staff_id", ""),
                prev_staff_name=r.get("prev_staff_name", ""),
                change_type=r.get("change_type", "通常"),
                note=r.get("notes", ""),
            ))

        # Parse patient changes (個別変更リクエスト)
        patient_changes = []
        for pc in data.get("patient_changes", []):
            patient_changes.append(PatientChange(
                pid=pc["patient_id"],
                date_str=pc["date"],
                operation=pc.get("operation", ""),
                time_type=pc.get("time_type", ""),
                start_min=pc.get("start_time_minutes"),
                end_min=pc.get("end_time_minutes"),
                earliest_min=pc.get("earliest_time_minutes"),
                latest_min=pc.get("latest_time_minutes"),
                service_min=pc.get("service_minutes"),
                need_staff=pc.get("need_staff"),
                specified_staff_ids=pc.get("specified_staff_ids", []),
                specified_type=pc.get("specified_type", ""),
                ng_staff_ids=pc.get("ng_staff_ids", []),
                note=pc.get("note", ""),
            ))

        # Parse special week (特別訪問週間)
        special_week_data = data.get("special_week", {})
        special_week_headers = []
        for sh in special_week_data.get("headers", []):
            special_week_headers.append(SpecialWeekHeader(
                special_week_id=sh["special_week_id"],
                pid=sh.get("patient_id", ""),
                pname=sh.get("patient_name", ""),
                week_start=sh.get("week_start", ""),
                mode=sh.get("mode", "ADD"),
                reason=sh.get("reason", ""),
            ))
        special_week_details = []
        for sd in special_week_data.get("details", []):
            special_week_details.append(SpecialWeekDetail(
                special_week_id=sd["special_week_id"],
                pid=sd.get("patient_id", ""),
                date_str=sd.get("date", ""),
                time_type=sd.get("time_type", ""),
                start_min=sd.get("start_time_minutes"),
                end_min=sd.get("end_time_minutes"),
                earliest_min=sd.get("earliest_time_minutes"),
                latest_min=sd.get("latest_time_minutes"),
                service_min=sd.get("service_minutes", 60),
                need_staff=sd.get("need_staff", 1),
                note=sd.get("note", ""),
                change_policy=sd.get("change_policy", ""),
            ))

        # Parse mentor pairs (スタッフ同行割付)
        mentor_pairs = []
        for mp in data.get("mentor_pairs", []):
            mentor_pairs.append(MentorPair(
                trainee_staff_id=mp["trainee_staff_id"],
                mentor_staff_id=mp.get("mentor_staff_id", ""),
                start_date=mp.get("start_date", ""),
                end_date=mp.get("end_date", ""),
                band=mp.get("band", ""),
                start_min=mp.get("start_time_minutes"),
                end_min=mp.get("end_time_minutes"),
                day_condition=mp.get("day_condition", ""),
                priority=mp.get("priority", ""),
            ))

        # Run engine
        engine = AllocationEngine(
            staff_list=staff_list,
            patient_map=patient_map,
            events=events,
            staff_changes=staff_changes,
            weekly_patterns=weekly_patterns,
            confirmed_history=confirmed_history,
            patient_changes=patient_changes,
            special_week_headers=special_week_headers,
            special_week_details=special_week_details,
            mentor_pairs=mentor_pairs,
        )
        result = engine.allocate(requests)

        # Convert results to JSON-serializable format
        assignment_results = []
        for r in result["results"]:
            assignment_results.append({
                "visit_id": r.visit_id,
                "date": r.date_str,
                "weekday": r.weekday,
                "staff_id": r.staff_id,
                "staff_name": r.staff_name,
                "patient_id": r.pid,
                "patient_name": r.pname,
                "area": r.area,
                "start_time_minutes": r.start_min,
                "end_time_minutes": r.end_min,
                "service_minutes": r.service_min,
                "time_type": r.time_type,
                "earliest_time_minutes": r.earliest_min,
                "latest_time_minutes": r.latest_min,
                "notes": r.note,
                "is_event": r.is_event,
                "movement_km": round(r.movement_km, 2) if r.movement_km else None,
            })

        add_log(f"allocate 完了: {len(assignment_results)}件割当")
        return jsonify({
            "success": True,
            "result": {
                "assignment_results": assignment_results,
                "unassigned": result["unassigned"],
                "summary": result["summary"],
            }
        })

    except Exception as e:
        add_log(f"allocate エラー: {e}")
        print(f"エラー: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/api/allocate/debug", methods=["POST"])
def api_allocate_debug():
    """受信データの件数・サンプルを返す検証用エンドポイント"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data"}), 400

        sw = data.get("special_week", {})
        summary = {
            "week_start": data.get("week_start", ""),
            "staff_masters": len(data.get("staff_masters", [])),
            "patient_masters": len(data.get("patient_masters", [])),
            "weekly_requests": len(data.get("weekly_requests", [])),
            "events": len(data.get("events", [])),
            "staff_changes": len(data.get("staff_changes", [])),
            "weekly_patterns": len(data.get("weekly_patterns", [])),
            "confirmed_history": len(data.get("confirmed_history", [])),
            "patient_changes": len(data.get("patient_changes", [])),
            "special_week_headers": len(sw.get("headers", [])),
            "special_week_details": len(sw.get("details", [])),
            "mentor_pairs": len(data.get("mentor_pairs", [])),
        }

        samples = {}
        for key in ["staff_masters", "patient_masters", "weekly_requests",
                    "events", "staff_changes", "weekly_patterns",
                    "confirmed_history", "patient_changes", "mentor_pairs"]:
            arr = data.get(key, [])
            if arr:
                samples[key + "_sample"] = arr[0]
        if sw.get("headers"):
            samples["special_week_headers_sample"] = sw["headers"][0]
        if sw.get("details"):
            samples["special_week_details_sample"] = sw["details"][0]

        add_log("allocate/debug: " + str(summary))
        return jsonify({"success": True, "counts": summary, "samples": samples})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

# ====== VNC/ジョブ管理API ======

@app.route('/api/kaipoke/run', methods=['POST'])
@require_auth
def kaipoke_run():
    """
    Playwright実行開始 API

    リクエスト:
        Authorization: Bearer <TOKEN>
        {
            "mode": "expand" | "export" | "apply",
            "params": { ... }
        }

    レスポンス:
        {
            "ok": true,
            "job": { "id": "...", "state": "running", ... },
            "vnc": { "ready": true, "url": "https://.../novnc/?token=..." }
        }
    """
    global job_state

    # 既に実行中なら現状を返す（冪等）
    with job_state_lock:
        if job_state["state"] == "running":
            vnc_token = generate_vnc_token()
            return jsonify({
                "ok": True,
                "job": dict(job_state),
                "vnc": {
                    "ready": True,
                    "url": f"https://{VPS_HOST}/novnc/vnc.html?token={vnc_token}"
                },
                "message": "既に実行中です"
            })

    try:
        data = request.get_json() or {}
        mode = data.get("mode", "default")
        params = data.get("params", {})

        # ジョブ状態を更新
        job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        with job_state_lock:
            job_state.update({
                "id": job_id,
                "state": "running",
                "progress": "starting",
                "started_at": datetime.now().isoformat(),
                "ended_at": None,
                "last_error": None,
                "mode": mode,
            })

        add_log(f"ジョブ開始: {job_id} (mode={mode})")

        # VNCトークンを生成
        vnc_token = generate_vnc_token()

        # バックグラウンドでPlaywrightを実行
        def run_playwright():
            global job_state
            try:
                with job_state_lock:
                    job_state["progress"] = "running"
                add_log(f"Playwright実行中... (mode={mode})")

                if mode == "expand":
                    month = params.get("month", "2026-04")
                    result = run_expand(month=month, headless=False)
                elif mode == "export":
                    month = params.get("month", "2026-04")
                    result = run_export(month=month, headless=False)
                elif mode == "apply":
                    month = params.get("month", "2026-04")
                    correction_sheet = params.get("correction_sheet", "data/correction_sheet.json")
                    dry_run = params.get("dry_run", True)
                    result = run_auto_apply(
                        correction_sheet=correction_sheet,
                        month=month,
                        headless=False,
                        dry_run=dry_run
                    )
                else:
                    # デフォルト: 待機状態（VNC確認用）
                    add_log("待機モード - VNC接続を確認できます")
                    time.sleep(5)
                    result = {"message": "待機モード完了"}

                with job_state_lock:
                    job_state["state"] = "idle"
                    job_state["progress"] = "done"
                    job_state["ended_at"] = datetime.now().isoformat()
                add_log(f"ジョブ完了: {result}")

            except Exception as e:
                with job_state_lock:
                    job_state["state"] = "failed"
                    job_state["progress"] = "error"
                    job_state["last_error"] = str(e)
                    job_state["ended_at"] = datetime.now().isoformat()
                add_log(f"ジョブエラー: {e}")

        # スレッドで実行
        thread = threading.Thread(target=run_playwright, daemon=True)
        thread.start()

        return jsonify({
            "ok": True,
            "job": dict(job_state),
            "vnc": {
                "ready": True,
                "url": f"https://{VPS_HOST}/novnc/vnc.html?token={vnc_token}"
            }
        })

    except Exception as e:
        add_log(f"run エラー: {e}")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.route('/api/kaipoke/stop', methods=['POST'])
@require_auth
def kaipoke_stop():
    """
    Playwright実行停止 API
    """
    global job_state

    with job_state_lock:
        if job_state["state"] != "running":
            return jsonify({
                "ok": True,
                "job": dict(job_state),
                "message": "実行中のジョブはありません"
            })

        job_state["state"] = "stopped"
        job_state["progress"] = "stopped"
        job_state["ended_at"] = datetime.now().isoformat()

    try:
        add_log("停止リクエスト受信")
        add_log("ジョブを停止しました")

        with job_state_lock:
            return jsonify({
                "ok": True,
                "job": dict(job_state)
            })

    except Exception as e:
        add_log(f"stop エラー: {e}")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.route('/api/kaipoke/status', methods=['GET'])
@require_auth
def kaipoke_status():
    """
    状態取得 API
    """
    with job_state_lock:
        vnc_url = None
        if job_state["state"] == "running":
            vnc_token = generate_vnc_token()
            vnc_url = f"https://{VPS_HOST}/novnc/vnc.html?token={vnc_token}"

        return jsonify({
            "ok": True,
            "server": {
                "name": "kaipoke-rpa",
                "time": datetime.now().isoformat(),
                "host": VPS_HOST
            },
            "job": dict(job_state),
            "vnc": {
                "ready": job_state["state"] == "running",
                "url": vnc_url
            }
        })


@app.route('/api/kaipoke/logs', methods=['GET'])
@require_auth
def kaipoke_logs():
    """
    ログ取得 API

    クエリパラメータ:
        tail: 末尾N行を取得（デフォルト: 200）
    """
    tail = request.args.get("tail", 200, type=int)

    with log_lock:
        lines = list(log_buffer)[-tail:]

    return jsonify({
        "ok": True,
        "lines": lines,
        "total": len(log_buffer)
    })


@app.route('/api/kaipoke/vnc-url', methods=['GET'])
@require_auth
def kaipoke_vnc_url():
    """
    VNC URL取得 API
    """
    vnc_token = generate_vnc_token()
    return jsonify({
        "ok": True,
        "url": f"https://{VPS_HOST}/novnc/vnc.html?token={vnc_token}"
    })


# noVNC用のトークン検証エンドポイント
@app.route('/novnc/verify', methods=['GET'])
def verify_vnc_token():
    """noVNCトークン検証"""
    token = request.args.get("token", "")
    if validate_vnc_token(token):
        return jsonify({"valid": True})
    return jsonify({"valid": False}), 401


if __name__ == '__main__':
    print("=" * 50)
    print("カイポケ自動化 API サーバー v2.0")
    print("VNC/noVNC対応版")
    print("=" * 50)
    print("")
    print("エンドポイント:")
    print("  GET/POST /api/test   - 接続テスト")
    print("  GET  /api/status     - サーバー状態確認")
    print("  POST /api/expand     - 月間スケジュール展開")
    print("  POST /api/export     - CSV出力（async対応）")
    print("  GET  /api/export/result - CSV出力結果取得")
    print("  POST /api/diff       - 差分確認")
    print("  POST /api/apply      - 差分適用")
    print("  GET/POST /api/config - 設定取得/更新")
    print("")
    print("VNC/ジョブ管理API（Bearer認証必須）:")
    print("  POST /api/kaipoke/run    - 実行開始")
    print("  POST /api/kaipoke/stop   - 実行停止")
    print("  GET  /api/kaipoke/status - 状態取得")
    print("  GET  /api/kaipoke/logs   - ログ取得")
    print("")
    print(f"VPS Host: {VPS_HOST}")
    print(f"noVNC Port: {NOVNC_PORT}")
    print("")
    print("サーバーを起動しています...")
    print("")

    add_log("APIサーバー起動")
    app.run(host='0.0.0.0', port=5000, debug=False)
