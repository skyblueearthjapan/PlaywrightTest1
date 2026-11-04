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
| `headed` | | 既定 `true`（VNC で見える）。`true`/`false` の真偽値のみ（それ以外は 400） |
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

## 記録書Ⅱの時刻も同じ時刻にそろえる（2026-11-04 追加）

設計: CareFlow02 `docs/plans/kaipoke-kiroku2-time-design-2026-11-04.md`。実装: `commands/care_record_time_edit.py`。

### 1 件の手順（記録書Ⅱ）

1. 実績の行を **日付・実績の開始/終了・職員1** で特定し、同じ行の `div.record-time a[onclick^=submitForm123]` を押して記録書Ⅱの編集画面（`/bizhnc/careRecordEdit/`）を開く。マークが無い行は `no_care_record`（記録書Ⅱは作らない）
2. 一覧の記録書Ⅱの時刻（`span.time-difference-care-record-document2`）がすでに新しい時刻なら `already_applied`、旧の時刻と違えば `record_time_mismatch`（開かない）
3. 編集画面で照らす: 記録書の番号（`select#visitDateItemList` の値＝マークの 2 番目の引数。空なら `doc_unreadable`）・利用者（隠し値 `userSurname`＋`userName` を空白・全角空白を除いてつないだ名前が、項目の `patient`（空白を除く）と完全一致。姓だけ・異体字は一致にしない）・訪問日（隠し値 `serviceDate`）・職員1（`careDocument2Dto.staffMemberInternalId` の選ばれた文字）・旧の時刻（4 つの select）。違えば保存せずに戻る
4. フォーム `#careRecordUpdateForm` 全体（`new FormData` の全項目・同名は配列）を読み、時刻だけを選ぶ（`visitYmdStartHour`・`visitYmdStartMin`・`visitYmdEndHour`・`visitYmdEndMin`）。選んだ後にもう一度読み比べ、時刻の 4 つ以外が違えば時刻を元へ戻して保存せずに戻る（`other_field_changed`。dry_run でも同じ）
5. dry_run: 時刻を元へ戻してから `#backButton`。本番: `#editButton` → confirm「入力した内容で変更しますか？」を OK → 月間スケジュールへ戻り、同じ行の記録書Ⅱの時刻が新しい時刻か読み直す
6. 本番: もう一度その記録書Ⅱを開き、番号・利用者・訪問日・職員1 が合い、4 つの時刻が新しい値で、フォームのほかの欄が保存前（4 で読んだもの）と同じか確かめてから `#backButton`。保存で変わって当然の隠し値は比べない（`updateDateAndTimeDocument2`・`updateUserFullNameDocument2`・`org.apache.struts.taglib.html.TOKEN`・`_csrf`・`csrfToken`・`token`）
7. 編集画面から戻れなかったら、その回の残りは `edit_screen_left_open` で止める（`stopped: true`）
8. 戻るときの confirm は「保存せず」側の文言のときだけ OK する。「保存しますか」「変更しますか」「登録しますか」を含む文は OK せず（キャンセル）失敗にする

### 実績と続けて（POST /api/actual-time/apply）

`items[].care_record: {"old_start": "14:33", "old_end": "15:33"}`（いまの記録書Ⅱの時刻）を付けると、実績が
`success`／`dry_run_ok`／`already_applied`／`no_change` のときに続けて記録書Ⅱを `new_start`〜`new_end` へそろえる。
実績が失敗した行の記録書Ⅱは触らない。結果の 1 件に `care_record_status`・`care_record_reason`
（・`care_record_detail`）を別に持ち、`result.care_record` に記録書Ⅱの件数（success/dry_run_ok/skipped/failed/error）が付く。
実績が停止・`user_not_found` などで飛んだ行の記録書Ⅱは `care_record_reason: "actual_skipped:<実績の理由>"`（例 `actual_skipped:stopped`）。

### 記録書Ⅱだけ（POST /api/care-record-time/apply・GET /api/care-record-time/result）

常に非同期（202）。409・400・`dry_run`（既定 true）・`job_id`・`headed`・`credentials`・ポーリングの形は actual-time と同じ。

```json
{ "month": "2026-10", "dry_run": true, "job_id": "...",
  "items": [{ "item_id": "visit-1", "date": "2026-10-01", "patient": "青栁○○", "staff1": "熊澤妙子",
              "actual_start": "14:53", "actual_end": "15:33",
              "old_start": "14:33", "old_end": "15:33",
              "new_start": "14:53", "new_end": "15:33" }] }
```

`actual_*` はいまのカイポケの実績（行の鍵）、`old_*` はいまの記録書Ⅱ、`new_*` はそろえる時刻。全部必須。
`actual_start < actual_end`・`old_start < old_end`・`new_start < new_end` でなければ 400。
`patient` は記録書Ⅱの画面の利用者名（姓＋名）と、空白を除いて完全一致する名前で渡す。
`details[]` は `item_id, user, date, staff1, actual_start, actual_end, old_start, old_end, new_start, new_end, status, reason`
と、あれば `observed`・`observed_record`（一覧で読めた記録書Ⅱの旧の時刻）・`observed_after`・`screen`・`messages`・`back_reason`・`back_messages`・
`fields`（変わった欄の名前）・`form_diff`（`{欄の名前: [前, 後]}`。`success`／`dry_run_ok` では時刻の 4 つだけが入るはず）。

### 記録書Ⅱの reason

| reason | status | 説明 |
|--------|--------|------|
| `no_care_record` | skipped | その実績の行に記録書Ⅱが無い（作らない） |
| `already_applied` | skipped | 記録書Ⅱはすでに新しい時刻 |
| `no_change` | skipped | 旧と新が同じ |
| `actual_not_applied` | skipped | （実績と続けてのとき）実績が書けていない（失敗・例外）ので触らない |
| `actual_skipped:<理由>` | skipped | （実績と続けてのとき）実績が `stopped`・`user_not_found`・`dialog_left_open` などで飛んだので触らない |
| `edit_screen_left_open` | failed／skipped | 編集画面が開いたまま。その回の残りは全部これで未処理 |
| `entry_not_found` / `end_mismatch` / `ambiguous` | failed | 実績の行を 1 件に決められない |
| `record_link_mismatch` | failed | 記録書のマークの利用者番号が実績の行と違う |
| `record_time_mismatch` | failed | 一覧の記録書Ⅱの時刻が旧の時刻と違う（開かない） |
| `open_failed` | failed | 編集画面が開かない |
| `doc_unreadable` | failed | 記録書の番号（`select#visitDateItemList` の値）が読めない（保存せず戻った） |
| `patient_unreadable` / `patient_mismatch` | failed | 画面の利用者名（`userSurname`＋`userName`）が読めない／`patient` と一致しない（保存せず戻った） |
| `doc_mismatch` / `date_mismatch` / `staff_mismatch` / `time_mismatch` / `time_unreadable` | failed | 編集画面が項目と違う（保存せず戻った） |
| `locked` | failed | 時刻の select が無い・disabled |
| `form_unreadable` | failed | フォーム `#careRecordUpdateForm` を読めない（保存せず戻った） |
| `time_set_failed` | failed | 時刻を選べない |
| `other_field_changed` | failed | 時刻を選んだら時刻の 4 つ以外の欄も変わった（`fields`・`form_diff` 付き。時刻を戻し保存せず戻った。dry_run でも出る） |
| `back_dialog_unknown` / `back_failed` / `user_lost` / `month_lost` | failed | 保存せずに戻れない・戻った後の利用者や月が合わない（`back_messages` 付き） |
| `save_dialog_unknown` / `save_not_confirmed` / `save_failed` | failed | 保存の確認の前に分からないダイアログ・確認が出ない・保存でエラー（保存せず戻った） |
| `save_maybe_saved` | failed | 保存の確認を OK した後に分からないダイアログが出た（**保存済みの可能性あり**・要目視） |
| `verify_failed` | failed | 保存後に一覧で新しい時刻を確認できない／開き直せない・開き直した画面が合わない・確かめた後に戻れない（**保存済みの可能性あり**） |
| `verify_other_field_changed` | failed | 保存後に開き直すと、時刻のほかの欄が保存前と違う（**保存済み**・`fields`・`form_diff` 付き・要目視） |
| `exception` | error | 予期しない例外 |
