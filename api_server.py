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
        "current_task": current_task,
        "job": job_state,
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
        current_task = {"running": False, "command": None, "started_at": None}


@app.route('/api/export', methods=['POST'])
def api_export():
    """CSV出力 API（Drive自動保存対応）"""
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
        auto_drive_upload = data.get("auto_drive_upload", False)

        current_task = {
            "running": True,
            "command": "export",
            "started_at": datetime.now().isoformat(),
        }

        clear_stop()  # 非常停止フラグをクリア
        add_log(f"export 開始 (month={month})")
        print(f"\n=== API: export 開始 (month={month}) ===")

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

        add_log(f"export 完了: success={result.get('success')}")
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
    """差分適用 API（インラインcorrectionデータ対応）"""
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
        correction_data = data.get("correction_data")  # インラインデータ
        dry_run = data.get("dry_run", True)
        headed = data.get("headed", True)  # デフォルトでVNC表示
        limit = data.get("limit")

        # インラインデータが提供された場合、ファイルに保存
        if correction_data:
            import json
            correction_sheet = "data/correction_sheet.json"
            Path("data").mkdir(parents=True, exist_ok=True)
            with open(correction_sheet, "w", encoding="utf-8") as f:
                json.dump(correction_data, f, ensure_ascii=False, indent=2)
            add_log("インラインcorrectionデータをファイルに保存")

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

        clear_stop()  # 非常停止フラグをクリア
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
            import os
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

        # サマリーテキスト生成
        time_changes = sum(1 for c in corrections if c.has_time_change())
        staff_changes = sum(1 for c in corrections if c.has_staff_change())
        date_changes = sum(1 for c in corrections if c.has_date_change())
        additions = sum(1 for c in corrections if c.action == "add")
        deletions = sum(1 for c in corrections if c.action == "delete")
        edits = sum(1 for c in corrections if c.action == "edit")

        summary_text = f"編集: {edits}件, 時間変更: {time_changes}件, スタッフ変更: {staff_changes}件, 日付変更: {date_changes}件, 追加: {additions}件, 削除: {deletions}件"

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
                "summary_text": summary_text,
            },
            "corrections": correction_sheet_data["corrections"],
            "correction_sheet_json": json_module.dumps(correction_sheet_data, ensure_ascii=False),
            "output_files": {
                "json": output_path,
                "csv": csv_path,
            },
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
            # インラインデータから読み込み
            items = correction_data.get("corrections", [])
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
            "POST /api/export        - CSV出力（Drive自動保存）",
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
