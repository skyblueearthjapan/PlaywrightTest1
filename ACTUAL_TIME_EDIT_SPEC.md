# 実績時刻の書き換え API 仕様（らく助 → RPA）

## 概要

カイポケ「月間スケジュール一覧」の**実績側**の行（`showHNC097807Edit(...)` の 3 番目の引数が `'02'`）の
開始・終了時刻を、らく助の QR 実績の時刻へ**その場で**書き換える。削除→追加はしない。

実装: `commands/actual_time_edit.py`、口: `api_server.py` の `/api/actual-time/*`。
`/api/apply`（予定側）とは別の口。ジョブの排他（409）・非常停止（`/api/stop`）・ログ・認証（Bearer）は共用。

## 1 件の手順

1. 月・利用者を選ぶ（同じ利用者の行は続けて処理。利用者の中は日付→旧開始の順）
0. 1 件を始める前に編集ダイアログ（`input#btnRegisPop`）が見えていたら、その回の残りをすべて止める（`dialog_left_open`）
2. 実績側の行を **日付・旧の開始時刻・職員1** で特定。`old_end` があれば終了も一致が必要。0 件／終了違い／2 件以上なら触らない
3. 編集ダイアログを開き、ダイアログの時刻が特定した行の旧の開始・終了と同じになるまで待つ（職員1 の欄に値があれば職員1 も）。違えば閉じて失敗。「予定・実績」が実績（`#inPopupPlanAchievementsDivision02` が checked）か確かめる
4. 時刻の欄が disabled（確定・請求でロック）なら閉じて失敗（強制的に有効にはしない）。時刻を入れる（`#inPopupStartHour` ほか 6 つ）→ 入った値を読み直して確かめる
5. 「職員情報入力」を押す（時刻を変えると無効になる「登録する」を有効に戻すため）
6. 担当欄（`chargeStaff*`：職員1〜3・職種・同行）が押す前と同じか確かめる。違えば戻し、戻せなければ閉じて失敗。職員1 が空なら失敗。職員1〜3 の名前を一覧の行の職員1〜3 と照らし、違えば失敗
7. 「登録する」が無効なら閉じて失敗（強制的に有効にはしない）
8. dry_run: 登録せずに閉じる（× → 確認 OK）／本番: 登録する
9. 本番: 一覧を読み直し、その行が新しい時刻（職員1 も同じ）になり、旧の時刻の行が無いことを確かめる

## POST /api/actual-time/apply

常に非同期。受け付けたら **202** を返す。

```json
{
  "month": "2026-10",
  "dry_run": true,
  "job_id": "careflow-actual-20261101-01",
  "headed": true,
  "items": [
    {
      "item_id": "visit-12345",
      "user_name": "前川七海",
      "date": "2026-10-10",
      "old_start": "16:45",
      "old_end": "17:20",
      "staff1": "熊澤妙子",
      "new_start": "16:50",
      "new_end": "17:28"
    }
  ],
  "credentials": { "corp_id": "...", "user_id": "...", "password": "..." }
}
```

| 項目 | 必須 | 説明 |
|------|------|------|
| `month` | ○ | `YYYY-MM`。1 回の呼び出しは 1 か月分 |
| `dry_run` | | 既定 `true`。`true`/`false` の真偽値のみ（文字列は 400） |
| `job_id` | | 呼び出し側の相関 ID。結果にそのまま返る |
| `headed` | | 既定 `true`（VNC で見える） |
| `items[].item_id` | | 呼び出し側の ID。結果の各行にそのまま返る |
| `items[].user_name` | ○ | 利用者名（空白・異体字は吸収） |
| `items[].date` | ○ | 日（`"10"`）または `YYYY-MM-DD`（`month` と同じ月） |
| `items[].old_start` | ○ | いまのカイポケの実績の開始 `H:MM`/`HH:MM` |
| `items[].old_end` | | いまの実績の終了。与えたら一致が必要（違えば `end_mismatch`） |
| `items[].staff1` | ○ | 実績側の職員1 |
| `items[].new_start` / `new_end` | ○ | 新しい開始・終了（`new_start < new_end`） |

- 1 件でも不正なら全体を動かさず **400**（`errors` に `items[i]: 不正な項目 [...]`）。上限 3000 件
- 別のタスクが実行中なら **409**

202 の応答: `{"success": true, "async": true, "job_id": "...", "total": 1, "dry_run": true}`

## GET /api/actual-time/result

| status | 内容 |
|--------|------|
| `running` | `progress`: `{processed, total, current_name, success, dry_run_ok, skipped, failed, error}` |
| `completed` | `result`（下記） |
| `error` | ジョブ全体の失敗（ログイン・月の設定など）。`error` に文言 |
| `no_result` | まだ一度も実行していない |

```json
{
  "success": true, "status": "completed", "job_id": "...", "completed_at": "...",
  "result": {
    "month": "2026-10", "dry_run": false, "total": 1,
    "success": 1, "dry_run_ok": 0, "skipped": 0, "failed": 0, "error": 0,
    "stopped": true,
    "execution_time_sec": 42.0, "completed_at": "...",
    "details": [
      {
        "item_id": "visit-12345", "user": "前川七海", "date": "10",
        "old_start": "16:45", "old_end": "17:20", "staff1": "熊澤妙子",
        "new_start": "16:50", "new_end": "17:28",
        "status": "success",
        "reason": "（失敗・スキップのとき）",
        "observed_old": "16:45-17:20",
        "observed": ["その日の実績側の行 '開始-終了/職員1' の一覧（失敗のとき）"],
        "fields": ["変わった担当欄の id（staff_changed のとき）"],
        "messages": ["登録時に出たダイアログ・エラー文言"],
        "message": "補足"
      }
    ]
  }
}
```
`stopped` は非常停止したときだけ付く。

### details[].status

| status | カイポケを変えたか |
|--------|------------------|
| `success` | 変えた（一覧で新しい時刻を確認済み） |
| `dry_run_ok` | 変えていない（登録の直前まで進んで閉じた） |
| `skipped` | 変えていない |
| `failed` | 変えていない。**ただし `verify_failed` は登録済みの可能性あり（要目視）** |
| `error` | 不明（例外。要目視） |

### reason コード一覧

| reason | status | 説明 |
|--------|--------|------|
| `already_applied` | skipped | 旧の時刻の行が無く、新しい時刻の行が 1 件ある（再実行で二重に触らない） |
| `no_change` | skipped | 旧と新が同じ |
| `user_not_found` | skipped | 利用者を選べない |
| `stopped` | skipped | 非常停止で未処理 |
| `dialog_left_open` | failed／skipped | 編集ダイアログが閉じずに残っていた。その回の残りは全部これで未処理（`stopped: true`）。要目視 |
| `entry_not_found` | failed | 日付・旧開始・職員1 の実績行が無い（`observed` 付き）／クリック直前に一覧が変わった |
| `end_mismatch` | failed | 日付・旧開始・職員1 は一致したが、終了が `old_end` と違う |
| `ambiguous` | failed | 一致する実績行が 2 件以上 |
| `dialog_mismatch` | failed | 開いたダイアログの時刻（・職員1）が特定した行と違う（閉じた） |
| `locked` | failed | 時刻の欄が disabled（確定・請求でロック）（閉じた） |
| `staff_missing` | failed | 職員情報入力の後、職員1 が空（閉じた） |
| `open_failed` | failed | 編集ダイアログが開かない |
| `not_actual` | failed | ダイアログの「予定・実績」が実績ではない（閉じた） |
| `time_set_failed` | failed | 時刻を入れられない／「職員情報入力」の後で時刻が変わった |
| `staff_button_not_found` | failed | 「職員情報入力」が見つからない |
| `staff_changed` | failed | 「職員情報入力」で担当欄が変わり戻せなかった／職員1〜3 が一覧の行と違う（`fields` 付き） |
| `register_disabled` | failed | 「登録する」が無効のまま |
| `register_failed` | failed | 登録してもダイアログが閉じない・エラー表示（`messages` 付き） |
| `verify_failed` | failed | 登録後の一覧で新しい時刻を確認できない（**登録済みの可能性あり**） |
| `exception` | error | 予期しない例外（`message` 付き） |

## 呼び出し側（らく助）の流れ

1. `dry_run: true` で全件を投げ、`dry_run_ok` 以外を確認する
2. `dry_run: false` で投げ、`/api/actual-time/result` を数十秒おきにポーリングする
3. `failed` / `error` の行は人が確かめる。`already_applied` は成功と同じ扱いでよい
