# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Kaipoke (カイポケ) RPA automation system** — a Playwright-based browser automation tool that manages monthly visit schedules for a home nursing care service (訪問看護). It automates the web application at `r.kaipoke.biz` through both a CLI (`main.py`) and a Flask REST API (`api_server.py`), with Google Apps Script (GAS) as the primary remote caller.

The system is designed to run in a Docker container on a VPS with VNC for remote browser viewing.

## Commands

### CLI Usage
```bash
# Activate venv first
.venv\Scripts\activate  # Windows

# Login and save session (required before other commands)
python main.py login --headed

# Expand weekly patterns to monthly schedule for all users
python main.py expand --month 2026-04 --headed --dry-run

# Export monthly schedule as CSV
python main.py export --month 2026-04 --out data/current_202604.csv

# Apply diff corrections from correction sheet
python commands/auto_apply.py --sheet data/correction_sheet.json --month 2026-04 --dry-run --headed

# Run diff engine standalone
python -m lib.diff_engine --current data/current.csv --optimized data/optimized.csv --output data/correction_sheet.json
```

### API Server
```bash
python api_server.py  # Starts Flask on 0.0.0.0:5000
```

### Docker
```bash
docker-compose build
docker-compose up -d
docker-compose logs -f
```

### DOM Catalog Tool (debugging)
```bash
python tools/dom_catalog.py --url "https://r.kaipoke.biz/..." --state state.json --headed
```

## Architecture

### Two Execution Modes

1. **CLI mode** (`main.py`): Subcommands `login`, `expand`, `export`, `apply` — each imports from `commands/` and runs Playwright synchronously.
2. **API mode** (`api_server.py`): Flask server called by GAS. Exposes REST endpoints (`/api/expand`, `/api/export`, `/api/diff`, `/api/apply`, `/api/kaipoke/*`). Jobs run in background threads. Bearer token auth on `/api/kaipoke/*` endpoints.

### Core Modules

- **`lib/common.py`** — Shared Playwright helpers: browser context creation, login flow, navigation (レセプト→訪問看護→月間スケジュール管理), month setting via Reiwa calendar, artifact saving. All commands use `create_browser_context()` → `login()` → navigation helpers.
- **`lib/diff_engine.py`** — CSV comparison engine. Parses Kaipoke 18-column CSV format. Produces `Correction` dataclass instances with actions: `edit`, `add`, `delete`, `date_change`. Multi-pass matching: exact service+time → service-only → cross-date → unmatched as add/delete.
- **`lib/stop_signal.py`** — Emergency stop mechanism (file `.stop_requested` + threading.Event). API sets it, command loops check `is_stop_requested()`.
- **`lib/google_drive.py`** — Service account-based Google Drive upload/download. Credentials at `credentials/service_account.json`.

### Command Modules (`commands/`)

- **`expand.py`** — Iterates all users via "次へ" (next) link, clicks "週間訪問パターンから展開" button for each. Handles native `confirm()` dialogs.
- **`export.py`** — Navigates to export page (各種情報出力→出力対象選択→スケジュール表), sets year/month selects, triggers CSV download.
- **`auto_apply.py`** — The main apply engine. **2-phase processing**:
  - Phase 1 (利用者別タブ): Schedule edits/adds/deletes grouped by user. Uses dropdown `div#user_search select` for O(1) user selection.
  - Phase 2 (職員別タブ): Event entries grouped by staff member. Uses `select#staffMemberInternalId`.
  - Handles medical insurance (医療保険) and nursing insurance (介護保険) with different form fields, plus custom events (個別業務).
- **`apply.py`** — Older/simpler apply command using week-based diff (used via `main.py apply`). `auto_apply.py` is the preferred engine used by the API.

### Data Flow (GAS Integration)

```
GAS → POST /api/export → Playwright scrapes CSV → response (or Drive upload)
GAS → POST /api/diff → diff_engine compares CSVs → correction_sheet.json
GAS → POST /api/apply → auto_apply reads correction_sheet → Playwright edits Kaipoke
```

The `/api/diff` endpoint accepts CSV data three ways: inline content, file paths, or Google Drive auto-download.

## Key Patterns

- **Browser**: Firefox (not Chromium) — chosen for stability on the target site.
- **Session persistence**: `state.json` stores Playwright storage_state (cookies). Created by `login` command, reused by all others.
- **Navigation path**: Every command follows Login → レセプト → 訪問看護/1260192047 → target page.
- **Month handling**: Dates use Reiwa era (令和). `parse_month("2026-04")` returns `(2026, 4)`, `to_reiwa(2026)` returns `8`. The `set_service_month()` function clicks 次月/前月 buttons to navigate to the target month.
- **Name matching**: `normalize_name()` in `auto_apply.py` handles kanji variants (栁→柳, 髙→高, etc.) and full-width/half-width space normalization. `name_matches()` uses substring matching after normalization.
- **Kaipoke CSS selectors**: Form fields use JSF-style IDs with colons (e.g., `#form\:corporation_id`). Schedule editing uses specific IDs like `#inPopupStartHour`, `#chargeStaff1Id1`, `#btnRegisPop`, `#inPopupBtnDel`.
- **Error artifacts**: On failure, screenshots and HTML are saved to `artifacts/` directory via `save_artifacts()`.
- **Environment variables**: `KAIPOKE_CORP_ID`, `KAIPOKE_USER_ID`, `KAIPOKE_PASSWORD` in `.env` file. `KAIPOKE_API_TOKEN` for API auth.

## Kaipoke CSV Format (18 columns)

```
職員名1, 職種1, 職員名2, 職種2, 同行2, 職員名3, 職種3, 同行3,
事業所名, 日付, 曜日, 利用者, 業務種別, サービス内容,
開始時間, 終了時間, 提供時間, 備考
```

Encodings: Kaipoke exports in cp932/Shift_JIS. The code tries `utf-8-sig`, `utf-8`, `cp932`, `shift_jis` in order.
