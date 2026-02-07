"""
非常停止シグナル - 共通モジュール

GASの非常停止ボタン → API → このモジュール → 各コマンドのループで検出

ファイルベース + threading.Event のハイブリッド方式:
  - ファイル (.stop_requested) でプロセス間通信
  - threading.Event で同一プロセス内の即時通知

使い方:
    from lib.stop_signal import is_stop_requested, clear_stop

    # ジョブ開始時
    clear_stop()

    # ループ内
    if is_stop_requested():
        print("非常停止が要求されました")
        break
"""

import threading
from pathlib import Path

_stop_event = threading.Event()
_STOP_FILE = Path(__file__).parent.parent / ".stop_requested"


def request_stop():
    """停止を要求（API側から呼ぶ）"""
    _stop_event.set()
    try:
        _STOP_FILE.write_text("stop", encoding="utf-8")
    except Exception:
        pass


def clear_stop():
    """停止フラグをクリア（ジョブ開始時に呼ぶ）"""
    _stop_event.clear()
    try:
        if _STOP_FILE.exists():
            _STOP_FILE.unlink()
    except Exception:
        pass


def is_stop_requested() -> bool:
    """停止が要求されているか（コマンドのループ内で呼ぶ）"""
    if _stop_event.is_set():
        return True
    try:
        if _STOP_FILE.exists():
            _stop_event.set()
            return True
    except Exception:
        pass
    return False
