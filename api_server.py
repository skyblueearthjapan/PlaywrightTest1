"""
カイポケ自動化 API サーバー

GASアプリケーションからHTTPリクエストを受け取り、
Playwright自動化スクリプトを実行します。

起動方法:
    python api_server.py

エンドポイント:
    POST /api/expand  - 月間スケジュール展開
    POST /api/export  - CSV出力
    POST /api/apply   - 差分適用
    GET  /api/status  - サーバー状態確認
"""

import sys
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify
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
CORS(app)  # GASからのクロスオリジンリクエストを許可

# 実行中のタスクを追跡
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
        "timestamp": datetime.now().isoformat(),
    })


@app.route('/api/expand', methods=['POST'])
def api_expand():
    """
    月間スケジュール展開 API

    リクエストボディ:
        {
            "month": "2026-04"  // 対象月（省略時は2026-04）
        }

    レスポンス:
        {
            "success": true,
            "result": {
                "success": 58,
                "skipped": 3,
                "failed": 0,
                "total": 61
            }
        }
    """
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

        # 実行中フラグを設定
        current_task = {
            "running": True,
            "command": "expand",
            "started_at": datetime.now().isoformat(),
        }

        print(f"\n=== API: expand 開始 (month={month}) ===")
        result = run_expand(month=month, headless=True)

        return jsonify({
            "success": True,
            "result": result,
        })

    except Exception as e:
        print(f"エラー: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500

    finally:
        current_task = {"running": False, "command": None, "started_at": None}


@app.route('/api/export', methods=['POST'])
def api_export():
    """
    CSV出力 API

    リクエストボディ:
        {
            "month": "2026-04",           // 対象月
            "out_path": "data/current.csv", // 出力先（省略可）
            "upload_to_drive": true,       // Google Driveにアップロード（省略可）
            "drive_folder_id": "xxx"       // DriveフォルダID（省略時は設定から取得）
        }

    レスポンス:
        {
            "success": true,
            "result": {
                "success": true,
                "file_path": "data/current_202604.csv",
                "drive_file_id": "xxx"
            }
        }
    """
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

        # フォルダIDが指定されていない場合は設定から取得
        if upload_to_drive_flag and not drive_folder_id:
            config = load_drive_config()
            drive_folder_id = config.get("folder_id")

        current_task = {
            "running": True,
            "command": "export",
            "started_at": datetime.now().isoformat(),
        }

        print(f"\n=== API: export 開始 (month={month}) ===")
        result = run_export(
            month=month,
            out_path=out_path,
            headless=True,
            upload_to_drive=upload_to_drive_flag,
            drive_folder_id=drive_folder_id,
        )

        return jsonify({
            "success": True,
            "result": result,
        })

    except Exception as e:
        print(f"エラー: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500

    finally:
        current_task = {"running": False, "command": None, "started_at": None}


@app.route('/api/apply', methods=['POST'])
def api_apply():
    """
    差分適用 API

    リクエストボディ:
        {
            "month": "2026-04",
            "correction_sheet": "data/correction_sheet.json",
            "dry_run": true,           // テスト実行（省略時: true）
            "headed": false,           // ブラウザ表示（省略時: false）
            "limit": 10                // 適用件数上限（省略可）
        }

    レスポンス:
        {
            "success": true,
            "result": {
                "total": 4,
                "success": 4,
                "failed": 0,
                "skipped": 0
            }
        }
    """
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
        headed = data.get("headed", False)
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

        print(f"\n=== API: apply 開始 (month={month}, dry_run={dry_run}) ===")
        result = run_auto_apply(
            correction_sheet=correction_sheet,
            month=month,
            headless=not headed,
            dry_run=dry_run,
            limit=limit,
        )

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

    finally:
        current_task = {"running": False, "command": None, "started_at": None}


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """
    設定 API

    GET: 現在の設定を取得
    POST: 設定を更新

    リクエストボディ (POST):
        {
            "folder_id": "Google DriveのフォルダID"
        }
    """
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

            # 更新可能な設定項目
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
    """
    差分確認 API（適用せずに差分のみ計算）

    リクエストボディ:
        {
            "current_csv": "data/current.csv",
            "optimized_csv": "data/optimized.csv",
            "week_start": 1,           // 対象週の開始日（省略可）
            "week_end": 7,             // 対象週の終了日（省略可）
            "output_path": "data/correction_sheet.json"  // 省略可
        }

    レスポンス:
        {
            "success": true,
            "result": {
                "total_corrections": 4,
                "summary": {
                    "time_changes": 2,
                    "staff_changes": 2,
                    "date_changes": 1,
                    "additions": 0,
                    "deletions": 0
                },
                "corrections": [...]
            }
        }
    """
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

        # 差分エンジンを使用して比較
        corrections = compare_schedules(
            current_csv=current_csv,
            optimized_csv=optimized_csv,
            target_week_start=week_start,
            target_week_end=week_end,
        )

        # 修正シートを生成
        generate_correction_sheet(corrections, output_path, format="json")
        csv_path = output_path.replace(".json", ".csv")
        generate_correction_sheet(corrections, csv_path, format="csv")

        # 結果をまとめる
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
    """
    接続テスト API

    GASからの接続テスト用エンドポイント

    レスポンス:
        {
            "success": true,
            "message": "接続テスト成功",
            "server_time": "2026-02-05T12:34:56",
            "received_data": {...}  // POSTの場合のみ
        }
    """
    result = {
        "success": True,
        "message": "接続テスト成功 - Playwrightサーバーは正常に動作しています",
        "server_time": datetime.now().isoformat(),
        "api_version": "1.0.0",
        "endpoints": [
            "GET  /api/status - サーバー状態確認",
            "GET  /api/test   - 接続テスト",
            "POST /api/expand - 月間スケジュール展開",
            "POST /api/export - CSV出力",
            "POST /api/diff   - 差分確認",
            "POST /api/apply  - 差分適用",
        ]
    }

    if request.method == 'POST':
        data = request.get_json() or {}
        result["received_data"] = data
        result["message"] = "接続テスト成功 - POSTデータを受信しました"

    return jsonify(result)


if __name__ == '__main__':
    print("=" * 50)
    print("カイポケ自動化 API サーバー")
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
    print("サーバーを起動しています...")
    print("")

    # 本番環境では host='0.0.0.0' でネットワーク全体に公開
    # 開発中は host='127.0.0.1' でローカルのみ
    app.run(host='0.0.0.0', port=5000, debug=False)
