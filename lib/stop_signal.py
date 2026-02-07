"""
非常停止シグナル - 共通モジュール

GASの非常停止ボタン → API → このモジュール → 各コマンドのループで検出

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

_stop_event = threading.Event()


def request_stop():
    """停止を要求（API側から呼ぶ）"""
    _stop_event.set()


def clear_stop():
    """停止フラグをクリア（ジョブ開始時に呼ぶ）"""
    _stop_event.clear()


def is_stop_requested() -> bool:
    """停止が要求されているか（コマンドのループ内で呼ぶ）"""
    return _stop_event.is_set()
