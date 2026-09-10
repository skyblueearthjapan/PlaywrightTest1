# -*- coding: utf-8 -*-
"""予定/実績(division) 対応の単体テスト（ブラウザ不要）

対象:
  - commands.export.default_export_path / drive_filename / validate_division
  - api_server._run_export_core（run_export を monkeypatch して stale 判定を検証）

    python -m pytest tests/test_export_division.py -q
"""

import hashlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import api_server  # noqa: E402
import commands.export as export_mod  # noqa: E402
from commands.export import (  # noqa: E402
    ACTUAL_RADIO_ERROR,
    ACTUAL_RADIO_SELECTOR,
    PLAN_RADIO_SELECTOR,
    default_export_path,
    drive_filename,
    is_division_selected,
    run_export,
    select_division_radio,
    validate_division,
)


# =============================================================================
# パスヘルパー（純粋関数）
# =============================================================================

def test_default_export_path_plan():
    assert default_export_path("2026-08", "plan") == Path("data/current_202608.csv")


def test_default_export_path_actual():
    assert default_export_path("2026-08", "actual") == Path("data/current_202608_actual.csv")


def test_default_export_path_defaults_to_plan():
    assert default_export_path("2026-08") == Path("data/current_202608.csv")


def test_default_export_path_invalid_division():
    with pytest.raises(ValueError):
        default_export_path("2026-08", "jisseki")


def test_drive_filename():
    assert drive_filename("2026-08", "plan") == "current_202608.csv"
    assert drive_filename("2026-08", "actual") == "current_202608_actual.csv"
    with pytest.raises(ValueError):
        drive_filename("2026-08", "")


def test_validate_division():
    assert validate_division("plan") == "plan"
    assert validate_division("actual") == "actual"
    with pytest.raises(ValueError):
        validate_division("ACTUAL")


# =============================================================================
# 予定/実績ラジオ（極小 page スタブ）
# =============================================================================

class FakePage:
    """page.check / is_checked / locator(...).count() だけを持つ極小スタブ"""

    def __init__(self, existing=(ACTUAL_RADIO_SELECTOR, PLAN_RADIO_SELECTOR),
                 check_succeeds=True):
        self.existing = set(existing)
        self.check_succeeds = check_succeeds
        self.checked = set()
        self.check_calls = []

    def check(self, selector, timeout=None):
        self.check_calls.append(selector)
        if selector not in self.existing:
            raise RuntimeError(f"element not found: {selector}")
        if self.check_succeeds:
            self.checked.add(selector)

    def is_checked(self, selector):
        return selector in self.checked

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        page = self

        class _Loc:
            def count(self):
                return 1 if selector in page.existing else 0

        return _Loc()


@pytest.fixture(autouse=True)
def no_artifacts(monkeypatch):
    """save_artifacts（スクショ保存）はテストでは無効化"""
    monkeypatch.setattr(export_mod, "save_artifacts", lambda *a, **k: None)


def test_select_division_radio_actual_ok():
    page = FakePage()
    assert select_division_radio(page, "actual") is True
    assert page.check_calls == [ACTUAL_RADIO_SELECTOR]


def test_select_division_radio_actual_missing():
    """実績ラジオが無い → False（呼び出し側はCSV出力を押さない）"""
    page = FakePage(existing=(PLAN_RADIO_SELECTOR,))
    assert select_division_radio(page, "actual") is False


def test_select_division_radio_actual_not_checked():
    """check しても選択状態にならない → False"""
    page = FakePage(check_succeeds=False)
    assert select_division_radio(page, "actual") is False


def test_select_division_radio_plan_never_touches_page():
    """予定は画面既定。ラジオが有っても触らない（待ち時間を発生させない）"""
    page = FakePage()
    assert select_division_radio(page, "plan") is True
    assert page.check_calls == []

    page_without_radio = FakePage(existing=())
    assert select_division_radio(page_without_radio, "plan") is True
    assert page_without_radio.check_calls == []


def test_is_division_selected():
    page = FakePage()
    select_division_radio(page, "actual")
    assert is_division_selected(page, "actual") is True

    # postback で戻された想定
    page.checked.discard(ACTUAL_RADIO_SELECTOR)
    assert is_division_selected(page, "actual") is False

    # 予定は検査対象外
    assert is_division_selected(page, "plan") is True


# =============================================================================
# run_export のフロー（Playwright をまるごとスタブ化）
# =============================================================================

class FlipPage(FakePage):
    """settle 待ちの後に実績ラジオが外れる（遅延postbackの再現）"""

    def __init__(self, flip_after=1):
        super().__init__()
        self.flip_after = flip_after
        self.is_checked_calls = 0

    def is_checked(self, selector):
        self.is_checked_calls += 1
        if self.is_checked_calls > self.flip_after:
            self.checked.discard(selector)
        return selector in self.checked


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeClosable:
    def close(self):
        pass


def _stub_playwright(monkeypatch, page, download_result=None):
    """run_export が使う外部依存をすべてスタブ化し、記録用 dict を返す"""
    seen = {"download_dirs": [], "clicked": 0}

    monkeypatch.setattr(export_mod, "sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(
        export_mod, "create_browser_context",
        lambda p, headless=True: (_FakeClosable(), _FakeClosable(), page),
    )
    monkeypatch.setattr(export_mod, "setup_yoriyori_page", lambda page_, ctx: None)
    monkeypatch.setattr(export_mod, "goto_export_page", lambda page_: None)
    monkeypatch.setattr(export_mod, "click_schedule_table", lambda page_: True)
    monkeypatch.setattr(export_mod, "set_export_month", lambda page_, month: True)

    def fake_click_csv_export_button(page_, download_dir, timeout=30000):
        seen["clicked"] += 1
        seen["download_dirs"].append(Path(download_dir))
        if download_result is None:
            return None
        download_dir = Path(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
        downloaded = download_dir / "schedule.csv"
        downloaded.write_bytes(CSV_BYTES)
        return downloaded

    monkeypatch.setattr(export_mod, "click_csv_export_button", fake_click_csv_export_button)
    return seen


def test_run_export_reasserts_division_before_download(monkeypatch, tmp_path):
    """(e) settle 待ちの後に実績が外れたら、ダウンロードせずエラーを返す"""
    page = FlipPage(flip_after=1)  # select_division_radio 内の検証だけ True
    seen = _stub_playwright(monkeypatch, page, download_result="ok")
    out_file = tmp_path / "current_202608_actual.csv"

    result = run_export(
        month="2026-08", out_path=str(out_file), headless=True, division="actual"
    )

    assert result["success"] is False
    assert result["error"] == ACTUAL_RADIO_ERROR
    assert seen["clicked"] == 0          # CSV出力ボタンを押していない
    assert not out_file.exists()         # 実績名のファイルを作っていない


def test_run_export_plan_does_not_touch_radio_and_uses_temp_dir(monkeypatch, tmp_path):
    """予定はラジオに触れない。ダウンロードは .download 経由で out_file へ"""
    page = FakePage()
    seen = _stub_playwright(monkeypatch, page, download_result="ok")
    out_file = tmp_path / "current_202608.csv"

    result = run_export(
        month="2026-08", out_path=str(out_file), headless=True, division="plan"
    )

    assert result["success"] is True
    assert result["division"] == "plan"
    assert page.check_calls == []
    assert seen["download_dirs"][0] == out_file.parent / ".download"
    assert out_file.read_bytes() == CSV_BYTES
    assert not (out_file.parent / ".download").exists()  # 空になったら片付ける


def test_run_export_actual_missing_radio_aborts(monkeypatch, tmp_path):
    """実績ラジオが無い画面ではダウンロードしない"""
    page = FakePage(existing=(PLAN_RADIO_SELECTOR,))
    seen = _stub_playwright(monkeypatch, page, download_result="ok")
    out_file = tmp_path / "current_202608_actual.csv"

    result = run_export(
        month="2026-08", out_path=str(out_file), headless=True, division="actual"
    )

    assert result["success"] is False
    assert result["error"] == ACTUAL_RADIO_ERROR
    assert seen["clicked"] == 0
    assert not out_file.exists()


def test_run_export_invalid_division_raises(monkeypatch, tmp_path):
    page = FakePage()
    _stub_playwright(monkeypatch, page, download_result="ok")
    with pytest.raises(ValueError):
        run_export(month="2026-08", out_path=str(tmp_path / "x.csv"), division="jisseki")


# =============================================================================
# _run_export_core
# =============================================================================

CSV_BODY = "利用者名,曜日\n山田太郎,月\n"
CSV_BYTES = CSV_BODY.encode("cp932")  # 改行変換を避けるため bytes で書き込む


@pytest.fixture
def stub_run_export(monkeypatch):
    """run_export を差し替えるフィクスチャ。calls に呼び出し引数を記録する。"""
    calls = []

    def _install(*, success=True, write_file=True, mtime_offset=0.0, error=None,
                 out_dir: Path = None):
        def fake_run_export(month, out_path, headless, division, **kwargs):
            calls.append(
                {"month": month, "out_path": out_path, "headless": headless,
                 "division": division}
            )
            target = Path(out_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if write_file:
                target.write_bytes(CSV_BYTES)
                if mtime_offset:
                    ts = time.time() + mtime_offset
                    os.utime(target, (ts, ts))
            result = {
                "success": success,
                "file_path": str(target),
                "drive_file_id": None,
                "division": division,
            }
            if error:
                result["error"] = error
            return result

        monkeypatch.setattr(api_server, "run_export", fake_run_export)
        return calls

    _install.calls = calls
    return _install


@pytest.fixture(autouse=True)
def no_drive(monkeypatch):
    """Drive 設定読み込みを無効化（テストが環境設定に依存しないように）"""
    monkeypatch.setattr(api_server, "load_drive_config", lambda: {})


def _patch_default_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        api_server,
        "default_export_path",
        lambda month, division="plan": tmp_path / (
            f"current_{month.replace('-', '')}"
            + ("_actual" if division == "actual" else "")
            + ".csv"
        ),
    )


def test_core_success_fresh_actual(monkeypatch, tmp_path, stub_run_export):
    """(a) 成功かつファイルが新しい → csv_content あり・division エコー・sha256 一致"""
    _patch_default_path(monkeypatch, tmp_path)
    calls = stub_run_export(success=True)

    result = api_server._run_export_core({"month": "2026-08", "division": "actual"})

    assert result["success"] is True
    assert result["division"] == "actual"
    assert result["csv_content"] == CSV_BODY
    assert result["row_count"] == 2
    assert result["file_size_bytes"] == len(CSV_BYTES)
    expected_sha = hashlib.sha256(CSV_BYTES).hexdigest()
    assert result["csv_sha256"] == expected_sha
    assert result["csv_mtime"] is not None
    assert result["csv_mtime"].endswith("+00:00")
    # 実績は予定ファイルを上書きしないパスへ
    assert calls[0]["out_path"] == str(tmp_path / "current_202608_actual.csv")
    assert calls[0]["division"] == "actual"


def test_core_plan_uses_plan_path(monkeypatch, tmp_path, stub_run_export):
    """(c) division 省略時は plan・予定パス。csv_content/row_count も従来どおり返る"""
    _patch_default_path(monkeypatch, tmp_path)
    calls = stub_run_export(success=True)

    result = api_server._run_export_core({"month": "2026-08"})

    assert result["success"] is True
    assert result["division"] == "plan"
    assert calls[0]["out_path"] == str(tmp_path / "current_202608.csv")
    assert result["csv_content"] == CSV_BODY
    assert result["row_count"] == 2
    assert result["file_size_bytes"] == len(CSV_BYTES)


@pytest.mark.parametrize(
    "offset, expect_fresh",
    [(-1.0, True), (-2.5, False)],
)
def test_core_mtime_tolerance_boundary(monkeypatch, tmp_path, stub_run_export,
                                       offset, expect_fresh):
    """(b) MTIME_TOLERANCE_SEC(=2.0) の境界: -1.0秒は新しい / -2.5秒は stale"""
    assert api_server.MTIME_TOLERANCE_SEC == 2.0
    _patch_default_path(monkeypatch, tmp_path)
    stub_run_export(success=True, mtime_offset=offset)

    result = api_server._run_export_core({"month": "2026-08", "division": "actual"})

    assert result["success"] is expect_fresh
    if expect_fresh:
        assert result["csv_content"] == CSV_BODY
    else:
        assert result["csv_content"] is None
        assert result["error"] == api_server.STALE_CSV_ERROR


def test_core_failure_without_error_key(monkeypatch, tmp_path, stub_run_export):
    """run_export が error 無しで失敗 → stale ではなく汎用エラー文言"""
    _patch_default_path(monkeypatch, tmp_path)
    stub_run_export(success=False)

    result = api_server._run_export_core({"month": "2026-08", "division": "actual"})

    assert result["success"] is False
    assert result["error"] == api_server.EXPORT_FAILED_ERROR


def test_core_undecodable_csv(monkeypatch, tmp_path, stub_run_export):
    """新しいファイルだがデコードできない → success False・全フィールド None"""
    _patch_default_path(monkeypatch, tmp_path)
    stub_run_export(success=True)
    monkeypatch.setattr(api_server, "_read_csv_text", lambda path: None)

    result = api_server._run_export_core({"month": "2026-08", "division": "actual"})

    assert result["success"] is False
    assert result["error"] == api_server.UNREADABLE_CSV_ERROR
    assert result["csv_content"] is None
    assert result["row_count"] is None
    assert result["file_size_bytes"] is None


def test_core_export_timestamp_is_utc_aware(monkeypatch, tmp_path, stub_run_export):
    """export_timestamp は tz-aware UTC (ISO 8601)"""
    _patch_default_path(monkeypatch, tmp_path)
    stub_run_export(success=True)

    result = api_server._run_export_core({"month": "2026-08"})

    parsed = datetime.fromisoformat(result["export_timestamp"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_core_stale_file(monkeypatch, tmp_path, stub_run_export):
    """(b) 成功を返すが、ファイルが started_at より古い → stale 扱い"""
    _patch_default_path(monkeypatch, tmp_path)
    stub_run_export(success=True, mtime_offset=-3600)

    result = api_server._run_export_core({"month": "2026-08", "division": "actual"})

    assert result["success"] is False
    assert result["csv_content"] is None
    assert "stale" in result["error"]
    # 調査用に sha256 / mtime は残す
    assert result["csv_sha256"] is not None
    assert result["csv_mtime"] is not None


def test_core_run_export_failure(monkeypatch, tmp_path, stub_run_export):
    """(c) run_export が success False → csv_content は None・元のエラーを保持"""
    _patch_default_path(monkeypatch, tmp_path)
    stub_run_export(success=False, error="実績ラジオが見つかりません/選択できません")

    result = api_server._run_export_core({"month": "2026-08", "division": "actual"})

    assert result["success"] is False
    assert result["csv_content"] is None
    assert result["error"] == "実績ラジオが見つかりません/選択できません"


def test_core_failure_with_old_plan_file_does_not_leak(monkeypatch, tmp_path, stub_run_export):
    """既存の古いCSVが残っていても、失敗時にその中身を返さない（STALE事故の再発防止）"""
    _patch_default_path(monkeypatch, tmp_path)
    stale = tmp_path / "current_202608_actual.csv"
    stale.write_text("古い予定データ\n", encoding="cp932")
    old = time.time() - 86400
    os.utime(stale, (old, old))
    stub_run_export(success=False, write_file=False, error="ダウンロード失敗")

    result = api_server._run_export_core({"month": "2026-08", "division": "actual"})

    assert result["success"] is False
    assert result["csv_content"] is None


def test_core_invalid_division(monkeypatch, tmp_path, stub_run_export):
    """(d) division が不正 → 400 相当のエラーdict・run_export は呼ばれない"""
    _patch_default_path(monkeypatch, tmp_path)
    calls = stub_run_export(success=True)

    result = api_server._run_export_core({"month": "2026-08", "division": "yotei"})

    assert result["success"] is False
    assert result["status_code"] == 400
    assert result["csv_content"] is None
    assert "division" in result["error"]
    assert calls == []


# =============================================================================
# POST /api/export（Flask テストクライアント）
# =============================================================================

class _SyncThread:
    """スレッドを立てずに target をその場で実行するスタブ"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_server, "apply_request_credentials", lambda data: None)
    with api_server.job_state_lock:
        api_server.current_task = {"running": False, "command": None, "started_at": None}
        api_server.export_result_store = {"result": None, "completed_at": None, "error": None}
    api_server.app.config["TESTING"] = True
    with api_server.app.test_client() as c:
        yield c


def _auth():
    return {"Authorization": f"Bearer {api_server.API_TOKEN}"}


def test_api_export_async_passes_division(monkeypatch, tmp_path, stub_run_export, client):
    """(a) 非同期モードでも division がスレッドに渡り、result store の形は不変"""
    _patch_default_path(monkeypatch, tmp_path)
    calls = stub_run_export(success=True)
    monkeypatch.setattr(api_server.threading, "Thread", _SyncThread)

    res = client.post(
        "/api/export",
        json={"month": "2026-08", "division": "actual", "async": True},
        headers=_auth(),
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body["success"] is True
    assert body["async"] is True

    # スレッド（同期実行）に division が渡っている
    assert calls[0]["division"] == "actual"
    assert calls[0]["out_path"] == str(tmp_path / "current_202608_actual.csv")

    store = dict(api_server.export_result_store)
    assert set(store.keys()) == {"result", "completed_at", "error"}
    assert store["error"] is None
    assert store["completed_at"] is not None
    assert store["result"]["division"] == "actual"
    assert store["result"]["csv_content"] == CSV_BODY

    with api_server.job_state_lock:
        assert api_server.current_task["running"] is False


def test_api_export_sync_passes_division(monkeypatch, tmp_path, stub_run_export, client):
    """同期モード: division が渡り、結果がそのまま返る"""
    _patch_default_path(monkeypatch, tmp_path)
    calls = stub_run_export(success=True)

    res = client.post(
        "/api/export",
        json={"month": "2026-08", "division": "actual"},
        headers=_auth(),
    )

    assert res.status_code == 200
    assert res.get_json()["result"]["division"] == "actual"
    assert calls[0]["division"] == "actual"


@pytest.mark.parametrize("async_mode", [False, True])
def test_api_export_invalid_division_returns_400(monkeypatch, tmp_path, stub_run_export,
                                                 client, async_mode):
    """(d) division 不正 → HTTP 400・run_export は呼ばれない・タスクは解放される"""
    _patch_default_path(monkeypatch, tmp_path)
    calls = stub_run_export(success=True)
    monkeypatch.setattr(api_server.threading, "Thread", _SyncThread)

    res = client.post(
        "/api/export",
        json={"month": "2026-08", "division": "yotei", "async": async_mode},
        headers=_auth(),
    )

    assert res.status_code == 400
    body = res.get_json()
    assert body["success"] is False
    assert body["division"] == "yotei"
    assert calls == []
    with api_server.job_state_lock:
        assert api_server.current_task["running"] is False


def test_drive_name_for_division():
    assert api_server._drive_name_for_division("kaipoke_export_202608.csv", "plan") == \
        "kaipoke_export_202608.csv"
    assert api_server._drive_name_for_division("kaipoke_export_202608.csv", "actual") == \
        "kaipoke_export_202608_actual.csv"
