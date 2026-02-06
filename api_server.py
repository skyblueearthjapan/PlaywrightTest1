"""
カイポケ自動化 API サーバー

GASアプリケーションからHTTPリクエストを受け取り、
Playwright自動化スクリプトを実行します。
VNC/noVNC経由でブラウザ画面をリアルタイム配信します。

起動方法:
    python api_server.py

エンドポイント:
    POST /api/expand  - 月間スケジュール展開
    POST /api/export  - CSV出力
    POST /api/apply   - 差分適用
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
from pathlib import Path
from datetime import datetime
from functools import wraps
from collections import deque
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent))

from commands.expand import run_expand
from commands.export import run_export
from commands.auto_apply import run_auto_apply
from lib.diff_engine import compare_schedules, generate_correction_sheet, load_correction_sheet
from lib.google_drive import (
    load_drive_config,
    save_drive_config,
    upload_to_drive,
    download_from_drive,
    find_file_by_name,
)

app = Flask(__name__)
CORS(app, resources={
    r"/api/*": {
        "origins": ["https://script.google.com", "https://*.googleusercontent.com"],
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

# 実行中のプロセス
current_process = None
process_lock = threading.Lock()


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


@app.route('/api/status', methods=['GET'])
def api_status():
    """サーバー状態確認"""
    return jsonify({
        "status": "running",
        "current_task": current_task,
        "job": job_state,
        "timestamp": datetime.now().isoformat(),
    })


@app.route('/api/expand', methods=['POST'])
def api_expand():
    """月間スケジュール展開 API"""
    global current_task

    if current_task["running"]:
        return jsonify({
            "success": False,
            "error": "別のタスクが実行中です",
            "current_task": current_task,
        }), 409

    try:
        data = request.get_json() or {}
        month = data.get("month", "2026-04")

        current_task = {
            "running": True,
            "command": "expand",
            "started_at": datetime.now().isoformat(),
        }

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
        current_task = {"running": False, "command": None, "started_at": None}


@app.route('/api/export', methods=['POST'])
def api_export():
    """CSV出力 API"""
    global current_task

    if current_task["running"]:
        return jsonify({
            "success": False,
            "error": "別のタスクが実行中です",
            "current_task": current_task,
        }), 409

    try:
        data = request.get_json() or {}
        month = data.get("month", "2026-04")
        out_path = data.get("out_path")
        upload_to_drive_flag = data.get("upload_to_drive", False)
        drive_folder_id = data.get("drive_folder_id")

        if upload_to_drive_flag and not drive_folder_id:
            config = load_drive_config()
            drive_folder_id = config.get("folder_id")

        current_task = {
            "running": True,
            "command": "export",
            "started_at": datetime.now().isoformat(),
        }

        add_log(f"export 開始 (month={month})")
        print(f"\n=== API: export 開始 (month={month}) ===")

        headless = data.get("headless", False)
        result = run_export(
            month=month,
            out_path=out_path,
            headless=headless,
            upload_to_drive=upload_to_drive_flag,
            drive_folder_id=drive_folder_id,
        )

        add_log(f"export 完了: {result}")
        return jsonify({
            "success": True,
            "result": result,
        })

    except Exception as e:
        add_log(f"export エラー: {e}")
        print(f"エラー: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500

    finally:
        current_task = {"running": False, "command": None, "started_at": None}


@app.route('/api/apply', methods=['POST'])
def api_apply():
    """差分適用 API"""
    global current_task

    if current_task["running"]:
        return jsonify({
            "success": False,
            "error": "別のタスクが実行中です",
            "current_task": current_task,
        }), 409

    try:
        data = request.get_json() or {}
        month = data.get("month", "2026-04")
        correction_sheet = data.get("correction_sheet", "data/correction_sheet.json")
        dry_run = data.get("dry_run", True)
        headed = data.get("headed", True)  # デフォルトでVNC表示
        limit = data.get("limit")

        if not Path(correction_sheet).exists():
            return jsonify({
                "success": False,
                "error": f"修正シートが見つかりません: {correction_sheet}",
            }), 404

        current_task = {
            "running": True,
            "command": "apply",
            "started_at": datetime.now().isoformat(),
        }

        add_log(f"apply 開始 (month={month}, dry_run={dry_run})")
        print(f"\n=== API: apply 開始 (month={month}, dry_run={dry_run}) ===")
        result = run_auto_apply(
            correction_sheet=correction_sheet,
            month=month,
            headless=not headed,
            dry_run=dry_run,
            limit=limit,
        )

        add_log(f"apply 完了: {result}")
        return jsonify({
            "success": True,
            "result": result,
        })

    except Exception as e:
        add_log(f"apply エラー: {e}")
        print(f"エラー: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500

    finally:
        current_task = {"running": False, "command": None, "started_at": None}


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
    """差分確認 API"""
    try:
        data = request.get_json() or {}
        current_csv = data.get("current_csv")
        optimized_csv = data.get("optimized_csv")
        week_start = data.get("week_start")
        week_end = data.get("week_end")
        output_path = data.get("output_path", "data/correction_sheet.json")

        if not current_csv or not optimized_csv:
            return jsonify({
                "success": False,
                "error": "current_csv, optimized_csv は必須です",
            }), 400

        corrections = compare_schedules(
            current_csv=current_csv,
            optimized_csv=optimized_csv,
            target_week_start=week_start,
            target_week_end=week_end,
        )

        generate_correction_sheet(corrections, output_path, format="json")
        csv_path = output_path.replace(".json", ".csv")
        generate_correction_sheet(corrections, csv_path, format="csv")

        result = {
            "total_corrections": len(corrections),
            "summary": {
                "time_changes": sum(1 for c in corrections if c.has_time_change()),
                "staff_changes": sum(1 for c in corrections if c.has_staff_change()),
                "date_changes": sum(1 for c in corrections if c.has_date_change()),
                "additions": sum(1 for c in corrections if c.action == "add"),
                "deletions": sum(1 for c in corrections if c.action == "delete"),
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
                    "action": c.action,
                }
                for c in corrections
            ],
            "output_files": {
                "json": output_path,
                "csv": csv_path,
            }
        }

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
        print(f"エラー: {e}")
        import traceback
        traceback.print_exc()
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
            "GET  /api/status - サーバー状態確認",
            "GET  /api/test   - 接続テスト",
            "POST /api/expand - 月間スケジュール展開",
            "POST /api/export - CSV出力",
            "POST /api/diff   - 差分確認",
            "POST /api/apply  - 差分適用",
            "POST /api/kaipoke/run    - Playwright実行開始（VNC付き）",
            "POST /api/kaipoke/stop   - 実行停止",
            "GET  /api/kaipoke/status - 状態取得",
            "GET  /api/kaipoke/logs   - ログ取得",
        ]
    }

    if request.method == 'POST':
        data = request.get_json() or {}
        result["received_data"] = data
        result["message"] = "接続テスト成功 - POSTデータを受信しました"

    return jsonify(result)


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
    global job_state, current_process

    # 既に実行中なら現状を返す（冪等）
    if job_state["state"] == "running":
        vnc_token = generate_vnc_token()
        return jsonify({
            "ok": True,
            "job": job_state,
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

                job_state["state"] = "idle"
                job_state["progress"] = "done"
                job_state["ended_at"] = datetime.now().isoformat()
                add_log(f"ジョブ完了: {result}")

            except Exception as e:
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
            "job": job_state,
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

    if job_state["state"] != "running":
        return jsonify({
            "ok": True,
            "job": job_state,
            "message": "実行中のジョブはありません"
        })

    try:
        add_log("停止リクエスト受信")

        # 状態を更新
        job_state["state"] = "stopped"
        job_state["progress"] = "stopped"
        job_state["ended_at"] = datetime.now().isoformat()

        add_log("ジョブを停止しました")

        return jsonify({
            "ok": True,
            "job": job_state
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
        "job": job_state,
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
    print("  POST /api/export     - CSV出力")
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
