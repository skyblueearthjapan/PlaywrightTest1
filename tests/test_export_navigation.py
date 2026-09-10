# -*- coding: utf-8 -*-
"""出力対象選択 →「スケジュール表」遷移の単体テスト（ブラウザ不要）

背景:
  2回連続で export すると（予定 → 実績）、カイポケが
  「訪問看護スケジュール表　出力条件　設定」画面へ直接着地することがある。
  従来の bare text ロケータ（text=スケジュール表）は画面見出しにマッチして
  is_visible() が True になり、click() が既定の30秒でタイムアウトしていた。

対象:
  - commands.export.is_on_schedule_settings_page
  - commands.export.click_schedule_table
  - commands.export.run_export の1回だけの再試行

    python -m pytest tests/test_export_navigation.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import commands.export as export_mod  # noqa: E402
from commands.export import (  # noqa: E402
    SCHEDULE_LINK_TIMEOUT_MS,
    click_schedule_table,
    is_on_schedule_settings_page,
    run_export,
)

DIVISION_INPUT = 'input[name="planAchievementsDivision"]'
SCHEDULE_LINK_TEXT = "スケジュール表"
CSV_BYTES = "利用者名,曜日\n山田太郎,月\n".encode("cp932")


class NavFakePage:
    """遷移まわりだけを持つ極小スタブ

    on_settings_page: 現在 設定画面に居るか
    link_exists: role=link の「スケジュール表」が押せるか
    settings_after_click: クリック後に設定画面へ遷移するか
    """

    def __init__(self, on_settings_page=False, link_exists=True,
                 settings_after_click=True, anchor_exists=True):
        self.on_settings_page = on_settings_page
        self.link_exists = link_exists
        self.settings_after_click = settings_after_click
        self.anchor_exists = anchor_exists
        self.role_clicks = []
        self.selector_clicks = []
        self.timeouts = []

    # --- ロケータ群 -------------------------------------------------------
    def locator(self, selector):
        page = self

        class _Loc:
            def count(self):
                if selector in (DIVISION_INPUT, "text=出力条件"):
                    return 1 if page.on_settings_page else 0
                return 0

        return _Loc()

    def get_by_role(self, role, name=None):
        page = self

        class _Loc:
            @property
            def first(self):
                return self

            def click(self, timeout=None):
                page.timeouts.append(timeout)
                if role != "link" or name != SCHEDULE_LINK_TEXT or not page.link_exists:
                    raise RuntimeError("link not found")
                page.role_clicks.append(name)
                page.on_settings_page = page.settings_after_click

        return _Loc()

    def click(self, selector, timeout=None):
        self.timeouts.append(timeout)
        if not self.anchor_exists:
            raise RuntimeError(f"element not found: {selector}")
        self.selector_clicks.append(selector)
        self.on_settings_page = self.settings_after_click

    # --- 待機/診断 --------------------------------------------------------
    def wait_for_load_state(self, state=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def title(self):
        return "訪問看護スケジュール表　出力条件　設定"


@pytest.fixture(autouse=True)
def no_artifacts(monkeypatch):
    monkeypatch.setattr(export_mod, "save_artifacts", lambda *a, **k: None)


# =============================================================================
# is_on_schedule_settings_page
# =============================================================================

def test_is_on_schedule_settings_page_detects_radio():
    assert is_on_schedule_settings_page(NavFakePage(on_settings_page=True)) is True
    assert is_on_schedule_settings_page(NavFakePage(on_settings_page=False)) is False


def test_is_on_schedule_settings_page_swallows_errors():
    class Boom:
        def locator(self, selector):
            raise RuntimeError("page closed")

    assert is_on_schedule_settings_page(Boom()) is False


# =============================================================================
# click_schedule_table
# =============================================================================

def test_click_schedule_table_skips_when_already_on_settings():
    """(2回目のexport) すでに設定画面 → クリックせずに True"""
    page = NavFakePage(on_settings_page=True)

    assert click_schedule_table(page) is True
    assert page.role_clicks == []
    assert page.selector_clicks == []


def test_click_schedule_table_clicks_link_and_verifies():
    """設定画面に居ない → role=link で押し、到達を検証して True"""
    page = NavFakePage(on_settings_page=False, settings_after_click=True)

    assert click_schedule_table(page) is True
    assert page.role_clicks == [SCHEDULE_LINK_TEXT]
    assert page.selector_clicks == []
    # 30秒の既定タイムアウトを使わない
    assert page.timeouts == [SCHEDULE_LINK_TIMEOUT_MS]


def test_click_schedule_table_falls_back_to_anchor_selector():
    """role=link が無ければ a:has-text にフォールバック"""
    page = NavFakePage(on_settings_page=False, link_exists=False,
                       settings_after_click=True)

    assert click_schedule_table(page) is True
    assert page.role_clicks == []
    assert page.selector_clicks == ["a:has-text('スケジュール表')"]
    assert page.timeouts == [SCHEDULE_LINK_TIMEOUT_MS, SCHEDULE_LINK_TIMEOUT_MS]


def test_click_schedule_table_returns_false_when_settings_never_appears():
    """クリックはできたが設定画面に到達しない → False"""
    page = NavFakePage(on_settings_page=False, settings_after_click=False)

    assert click_schedule_table(page) is False
    assert page.role_clicks == [SCHEDULE_LINK_TEXT]


def test_click_schedule_table_returns_false_when_no_link_at_all():
    """どのロケータでも押せない → False（例外を投げない）"""
    page = NavFakePage(on_settings_page=False, link_exists=False,
                       anchor_exists=False)

    assert click_schedule_table(page) is False


# =============================================================================
# run_export の1回だけの再試行
# =============================================================================

class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeClosable:
    def close(self):
        pass


def _stub_playwright_for_navigation(monkeypatch, page, click_results):
    """click_schedule_table の戻り値を順に差し替え、呼び出し回数を記録する"""
    seen = {"goto": 0, "click_schedule": 0, "download": 0}
    pending = list(click_results)

    monkeypatch.setattr(export_mod, "sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(
        export_mod, "create_browser_context",
        lambda p, headless=True: (_FakeClosable(), _FakeClosable(), page),
    )
    monkeypatch.setattr(export_mod, "setup_yoriyori_page", lambda page_, ctx: None)

    def fake_goto_export_page(page_):
        seen["goto"] += 1

    def fake_click_schedule_table(page_):
        seen["click_schedule"] += 1
        return pending.pop(0)

    def fake_click_csv_export_button(page_, download_dir, timeout=30000):
        seen["download"] += 1
        download_dir = Path(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
        downloaded = download_dir / "schedule.csv"
        downloaded.write_bytes(CSV_BYTES)
        return downloaded

    monkeypatch.setattr(export_mod, "goto_export_page", fake_goto_export_page)
    monkeypatch.setattr(export_mod, "click_schedule_table", fake_click_schedule_table)
    monkeypatch.setattr(export_mod, "set_export_month", lambda page_, month: True)
    monkeypatch.setattr(export_mod, "click_csv_export_button", fake_click_csv_export_button)
    return seen


def test_run_export_retries_schedule_table_once(monkeypatch, tmp_path):
    """1回目の失敗 → goto_export_page からやり直して成功 → export は続行"""
    page = NavFakePage()
    seen = _stub_playwright_for_navigation(monkeypatch, page, [False, True])
    out_file = tmp_path / "current_202608.csv"

    result = run_export(
        month="2026-08", out_path=str(out_file), headless=True, division="plan"
    )

    assert result["success"] is True
    assert seen["click_schedule"] == 2
    assert seen["goto"] == 2          # 初回 + 再試行
    assert seen["download"] == 1
    assert out_file.read_bytes() == CSV_BYTES


def test_run_export_gives_up_after_one_retry(monkeypatch, tmp_path):
    """再試行も失敗 → CSV出力ボタンを押さずに失敗を返す"""
    page = NavFakePage()
    seen = _stub_playwright_for_navigation(monkeypatch, page, [False, False])
    out_file = tmp_path / "current_202608.csv"

    result = run_export(
        month="2026-08", out_path=str(out_file), headless=True, division="plan"
    )

    assert result["success"] is False
    assert seen["click_schedule"] == 2
    assert seen["goto"] == 2
    assert seen["download"] == 0
    assert not out_file.exists()


def test_run_export_no_retry_when_first_click_succeeds(monkeypatch, tmp_path):
    """1回目で成功したら再試行しない（余計な遷移を増やさない）"""
    page = NavFakePage()
    seen = _stub_playwright_for_navigation(monkeypatch, page, [True])
    out_file = tmp_path / "current_202608.csv"

    result = run_export(
        month="2026-08", out_path=str(out_file), headless=True, division="plan"
    )

    assert result["success"] is True
    assert seen["click_schedule"] == 1
    assert seen["goto"] == 1
