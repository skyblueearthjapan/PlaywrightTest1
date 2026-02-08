# GAS差分検証機能 - 設計仕様書

## 1. 概要

差分適用（カイポケへの自動反映）を実行する前に、差分検出結果を検証する機能をGASアプリに追加する。

### 目的
- `/api/diff` のレスポンスに差分結果CSVの内容（`csv_content`）が含まれる
- GAS側でそのCSVをDriveにアップロードし、最適化スケジュール（`gas_optimized_*.csv`）と照合する
- 整合性が確認できた場合のみ「差分適用」ボタンを有効化する

### フロー
```
[差分確認プレビュー] ボタン押下
        ↓
Python API: /api/diff 実行
        ↓
GAS: APIレスポンスから csv_content + drive_file（推奨ファイル名）を取得
        ↓
GAS: csv_content を Google Drive にアップロード（GASのユーザー権限で実行）
        ↓
GAS: DriveのCSV + 最適化CSVと照合
        ↓
GAS: 検証結果をサイドバーに表示
        ↓
[OK] → 差分適用ボタンを有効化
[NG] → エラー詳細を表示、適用ボタンは無効のまま
```

**注意**: サーバー側（サービスアカウント）にはDriveストレージ割り当てがないため、
ファイルの新規作成はGAS側（ユーザー権限）で行う必要がある。

---

## 2. APIレスポンス仕様（Python側 - 実装済み）

### POST /api/diff

レスポンス（`result` 内）に以下が追加済み:

```json
{
  "success": true,
  "result": {
    "total_corrections": 123,
    "summary": {
      "time_changes": 114,
      "staff_changes": 116,
      "date_changes": 59,
      "additions": 15,
      "deletions": 35,
      "edits": 64,
      "date_change_actions": 9,
      "events": 5,
      "by_business_type": {
        "医療保険": 80,
        "介護保険": 35,
        "ミーティング": 3,
        "研修": 2
      },
      "summary_text": "編集: 64件, 時間変更: 114件, ..."
    },
    "corrections": [ ... ],
    "correction_sheet_json": "...",
    "output_files": {
      "json": "data/correction_sheet.json",
      "csv": "data/correction_sheet.csv"
    },
    "drive_file": {
      "filename": "diff_result_20260406_20260412.csv",
      "folder_id": "1tQJKZDjonFwiY6wYYgx1iVgu4cM98vRp"
    },
    "csv_content": "利用者,日付(前),日付(後),開始時間(前),..."
  }
}
```

**重要**:
- `csv_content`: 差分結果CSVの全内容（テキスト）。GAS側でDriveにアップロードすること。
- `drive_file.filename`: 推奨ファイル名。`drive_file.folder_id`: アップロード先フォルダID。
- GASは `DriveApp.getFolderById(folder_id).createFile(filename, csv_content, MimeType.CSV)` でアップロードする。

---

## 3. 差分結果CSVフォーマット（15列）

ファイル名: `diff_result_{week_start}_{week_end}.csv`
例: `diff_result_20260406_20260412.csv`

エンコーディング: UTF-8 BOM付き

| 列番号 | ヘッダー | 説明 | 例 |
|--------|----------|------|-----|
| 1 | 利用者 | 利用者名 | 山田太郎 |
| 2 | 日付(前) | 変更前の日付 | 6 |
| 3 | 日付(後) | 変更後の日付 | 7 |
| 4 | 開始時間(前) | 変更前の開始時間 | 09:00 |
| 5 | 開始時間(後) | 変更後の開始時間 | 10:00 |
| 6 | 終了時間(前) | 変更前の終了時間 | 10:00 |
| 7 | 終了時間(後) | 変更後の終了時間 | 11:00 |
| 8 | 職員1(前) | 変更前の担当職員1 | 佐藤花子 |
| 9 | 職員1(後) | 変更後の担当職員1 | 鈴木一郎 |
| 10 | 職員2(前) | 変更前の担当職員2 | |
| 11 | 職員2(後) | 変更後の担当職員2 | |
| 12 | サービス内容 | サービスの種別名 | 訪問看護 |
| 13 | アクション | add / edit / delete | edit |
| 14 | 業務種別 | 医療保険 / 介護保険 / イベント名 | 医療保険 |
| 15 | 備考 | イベント名等 | ミーティング |

### アクション定義
- `add`: 最適化CSVにあるがカイポケ現行CSVにない → 新規追加
- `delete`: カイポケ現行CSVにあるが最適化CSVにない → 削除
- `edit`: 両方にあるが内容が異なる → 編集（時間・職員・日付の変更）

### 業務種別の判定ルール
- `"医療保険"`: 通常のスケジュール（医療保険対象）
- `"介護保険"`: 通常のスケジュール（介護保険対象）
- その他の値（例: `"ミーティング"`, `"研修"`）: イベント扱い

---

## 4. 最適化CSVフォーマット（参照用・18列）

ファイル名: `gas_optimized_{week_start}_{week_end}.csv`
例: `gas_optimized_20260406_20260412.csv`

同じDriveフォルダ (`folder_id: 1tQJKZDjonFwiY6wYYgx1iVgu4cM98vRp`) に存在する。

| 列番号 | ヘッダー | 説明 |
|--------|----------|------|
| 1 | 利用者名 | |
| 2 | 性別 | |
| 3 | 要介護度 | |
| 4 | 保険種別 | |
| 5 | サービス内容 | |
| 6 | 日付 | 日のみ（例: 6, 7） |
| 7 | 曜日 | 月, 火, ... |
| 8 | 開始時間 | HH:MM |
| 9 | 終了時間 | HH:MM |
| 10 | 職員1 | |
| 11 | 職員2 | |
| 12 | 備考 | |
| 13 | 業務種別 | 医療保険/介護保険/イベント名 |
| 14-18 | (予備列) | |

---

## 5. GAS側で実装する機能

### 5-1. diffPreview() の改修

現在の `diffPreview()` 関数を以下のように改修する:

```javascript
function diffPreview() {
  const ui = SpreadsheetApp.getUi();

  // 1. 対象月・週の情報を取得
  const month = getTargetMonth();
  if (!month) return;
  const weekStart = getWeekStart();  // 例: "20260406" または "2026-04-06"
  const weekEnd = getWeekEnd();      // 例: "20260412" または "2026-04-12"

  // 2. /api/diff を呼び出し（use_drive=true でDriveからCSV読み込み）
  const response = UrlFetchApp.fetch(API_URL + '/api/diff', {
    method: 'POST',
    contentType: 'application/json',
    payload: JSON.stringify({
      use_drive: true,
      month: month,
      week_start: weekStart,
      week_end: weekEnd,
    }),
    muteHttpExceptions: true,
  });

  const data = JSON.parse(response.getContentText());

  if (!data.success) {
    ui.alert('エラー', data.error, ui.ButtonSet.OK);
    return;
  }

  const result = data.result;

  // 3. drive_file 情報を確認
  if (!result.drive_file) {
    ui.alert('警告', '差分結果CSVのDriveアップロードに失敗しました。', ui.ButtonSet.OK);
    return;
  }

  // 4. 差分結果をサイドバー/シートに表示
  displayDiffSummary(result);

  // 5. 検証処理を実行
  const verification = verifyDiffResult(result);

  // 6. 検証結果に基づいて差分適用ボタンの有効/無効を制御
  if (verification.ok) {
    // PropertiesServiceに検証OKフラグを保存
    PropertiesService.getScriptProperties().setProperty('diff_verified', 'true');
    PropertiesService.getScriptProperties().setProperty('diff_file_id', result.drive_file.file_id);
    ui.alert('検証完了',
      '差分検証OK - 差分適用を実行できます。\n\n' + verification.message,
      ui.ButtonSet.OK);
  } else {
    PropertiesService.getScriptProperties().setProperty('diff_verified', 'false');
    ui.alert('検証NG',
      '差分検証に問題があります。\n\n' + verification.message,
      ui.ButtonSet.OK);
  }
}
```

### 5-2. verifyDiffResult() - 検証ロジック（新規関数）

```javascript
/**
 * 差分結果を最適化CSVと照合し、整合性を検証する
 *
 * @param {Object} diffResult - /api/diff のレスポンス result
 * @returns {Object} { ok: boolean, message: string, details: Array }
 */
function verifyDiffResult(diffResult) {
  const errors = [];
  const warnings = [];

  // === チェック1: csv_contentが存在し、Driveにアップロードする ===
  const csvContentStr = diffResult.csv_content;
  const driveFile = diffResult.drive_file;
  if (!csvContentStr) {
    errors.push('APIレスポンスにcsv_contentが含まれていません');
    return { ok: false, message: errors.join('\n'), details: errors };
  }
  // GAS側でDriveにアップロード（サービスアカウントではなくユーザー権限で実行）
  let uploadedFileId = null;
  if (driveFile && driveFile.folder_id && driveFile.filename) {
    try {
      const folder = DriveApp.getFolderById(driveFile.folder_id);
      // 既存の同名ファイルを削除してから作成
      const existingFiles = folder.getFilesByName(driveFile.filename);
      while (existingFiles.hasNext()) {
        existingFiles.next().setTrashed(true);
      }
      const newFile = folder.createFile(driveFile.filename, csvContentStr, MimeType.CSV);
      uploadedFileId = newFile.getId();
    } catch (e) {
      warnings.push('差分結果CSVのDriveアップロードに失敗: ' + e.message);
    }
  }

  // === チェック2: 差分結果CSVの行数が一致するか ===
  const csvRows = Utilities.parseCsv(csvContentStr);
  const csvDataRows = csvRows.length - 1; // ヘッダー除く

  if (csvDataRows !== diffResult.total_corrections) {
    errors.push(
      'CSV行数とAPI件数が不一致: ' +
      'CSV=' + csvDataRows + '行, API=' + diffResult.total_corrections + '件'
    );
  }

  // === チェック3: 最適化CSVと照合 ===
  // 最適化CSVをDriveから読み込む
  const folderId = driveFile.folder_id;
  const folder = DriveApp.getFolderById(folderId);

  // 最適化CSVファイルを検索（naming convention: gas_optimized_YYYYMMDD_YYYYMMDD.csv）
  const optimizedFiles = folder.getFilesByName(
    'gas_optimized_' + getWeekStart().replace(/-/g, '') + '_' + getWeekEnd().replace(/-/g, '') + '.csv'
  );

  if (!optimizedFiles.hasNext()) {
    warnings.push('最適化CSVがDriveに見つかりません。照合をスキップします。');
  } else {
    const optimizedFile = optimizedFiles.next();
    const optimizedContent = optimizedFile.getBlob().getDataAsString('UTF-8');
    const optimizedRows = Utilities.parseCsv(optimizedContent);

    // 最適化CSVの利用者リストを取得（列1=利用者名）
    const optimizedUsers = new Set();
    for (let i = 1; i < optimizedRows.length; i++) {
      if (optimizedRows[i][0]) {
        optimizedUsers.add(optimizedRows[i][0].trim());
      }
    }

    // 差分結果の「追加」アクションの利用者が最適化CSVに存在するか
    const corrections = diffResult.corrections;
    for (const c of corrections) {
      if (c.action === 'add') {
        if (!optimizedUsers.has(c.user_name)) {
          errors.push(
            '追加予定の利用者「' + c.user_name + '」が最適化CSVに存在しません'
          );
        }
      }
    }

    // 業務種別チェック: 最適化CSVの業務種別と差分の業務種別が矛盾していないか
    const optimizedBusinessTypes = {};
    for (let i = 1; i < optimizedRows.length; i++) {
      const userName = (optimizedRows[i][0] || '').trim();
      const bt = (optimizedRows[i][12] || '').trim(); // 列13=業務種別
      if (userName && bt) {
        if (!optimizedBusinessTypes[userName]) {
          optimizedBusinessTypes[userName] = new Set();
        }
        optimizedBusinessTypes[userName].add(bt);
      }
    }

    // add アクションの業務種別が最適化CSVと一致するか
    for (const c of corrections) {
      if (c.action === 'add' && c.business_type) {
        const userBTs = optimizedBusinessTypes[c.user_name];
        if (userBTs && !userBTs.has(c.business_type)) {
          warnings.push(
            '「' + c.user_name + '」の業務種別「' + c.business_type +
            '」が最適化CSVと異なります（最適化CSV: ' +
            Array.from(userBTs).join(', ') + '）'
          );
        }
      }
    }
  }

  // === チェック4: サマリーの妥当性チェック ===
  const summary = diffResult.summary;
  const dateChangeActions = summary.date_change_actions || 0;
  const totalActions = summary.additions + summary.deletions + summary.edits + dateChangeActions;
  if (totalActions !== diffResult.total_corrections) {
    errors.push(
      'アクション合計が不一致: add(' + summary.additions +
      ')+delete(' + summary.deletions +
      ')+edit(' + summary.edits +
      ')+date_change(' + dateChangeActions +
      ')=' + totalActions +
      ' vs total=' + diffResult.total_corrections
    );
  }

  // === チェック5: 業務種別の分布チェック ===
  const byBT = summary.by_business_type || {};
  const btTotal = Object.values(byBT).reduce((a, b) => a + b, 0);
  if (btTotal !== diffResult.total_corrections) {
    warnings.push(
      '業務種別の合計が総修正数と不一致: ' + btTotal + ' vs ' + diffResult.total_corrections
    );
  }

  // === 結果まとめ ===
  const ok = errors.length === 0;
  let message = '';

  if (ok) {
    message = '全チェック合格\n';
    message += '合計: ' + diffResult.total_corrections + '件\n';
    message += '  追加: ' + summary.additions + '件\n';
    message += '  削除: ' + summary.deletions + '件\n';
    message += '  編集: ' + summary.edits + '件\n';
    message += '  イベント: ' + summary.events + '件\n';
    for (const [bt, count] of Object.entries(byBT)) {
      message += '  ' + bt + ': ' + count + '件\n';
    }
  }

  if (warnings.length > 0) {
    message += '\n[警告]\n' + warnings.map(w => '- ' + w).join('\n');
  }

  if (errors.length > 0) {
    message += '\n[エラー]\n' + errors.map(e => '- ' + e).join('\n');
  }

  return { ok, message, details: { errors, warnings } };
}
```

### 5-3. applyDiff() のガード追加

```javascript
function applyDiff() {
  const ui = SpreadsheetApp.getUi();

  // 検証済みかチェック
  const verified = PropertiesService.getScriptProperties().getProperty('diff_verified');
  if (verified !== 'true') {
    ui.alert(
      '実行不可',
      '差分検証が完了していません。\n先に「差分確認（プレビュー）」を実行してください。',
      ui.ButtonSet.OK
    );
    return;
  }

  // （以降は既存の applyDiff ロジック）
  // ...

  // 適用完了後、検証フラグをクリア
  PropertiesService.getScriptProperties().setProperty('diff_verified', 'false');
}
```

### 5-4. displayDiffSummary() - サイドバー表示（新規関数）

```javascript
/**
 * 差分結果のサマリーをサイドバーに表示する
 *
 * @param {Object} result - /api/diff レスポンスの result
 */
function displayDiffSummary(result) {
  const summary = result.summary;
  const byBT = summary.by_business_type || {};

  // 「差分結果」シートに書き込む（既存の writeDiffResultToSheet を拡張）
  let sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('差分結果');
  if (!sheet) {
    sheet = SpreadsheetApp.getActiveSpreadsheet().insertSheet('差分結果');
  }

  // シートクリア
  sheet.clear();

  // ヘッダー（15列に拡張）
  sheet.getRange('A1:O1').setValues([[
    '利用者', '日付(前)', '日付(後)',
    '開始時間(前)', '開始時間(後)', '終了時間(前)', '終了時間(後)',
    '職員1(前)', '職員1(後)', '職員2(前)', '職員2(後)',
    'サービス内容', 'アクション', '業務種別', '備考'
  ]]);

  // データ行
  const corrections = result.corrections;
  if (corrections.length > 0) {
    const data = corrections.map(c => [
      c.user_name,
      c.date_from, c.date_to,
      c.start_time_from, c.start_time_to,
      c.end_time_from, c.end_time_to,
      c.staff1_from, c.staff1_to,
      c.staff2_from, c.staff2_to,
      c.service_type, c.action,
      c.business_type, c.remarks
    ]);
    sheet.getRange(2, 1, data.length, 15).setValues(data);
  }

  // 色分け（アクション別）
  for (let i = 0; i < corrections.length; i++) {
    const row = i + 2;
    const action = corrections[i].action;
    const range = sheet.getRange(row, 1, 1, 15);
    if (action === 'add') {
      range.setBackground('#d4edda');  // 緑（追加）
    } else if (action === 'delete') {
      range.setBackground('#f8d7da');  // 赤（削除）
    } else if (action === 'edit') {
      range.setBackground('#fff3cd');  // 黄（編集）
    }
  }

  // Drive情報を最下部に表示
  if (result.drive_file) {
    const infoRow = corrections.length + 4;
    sheet.getRange(infoRow, 1).setValue('Drive File ID:');
    sheet.getRange(infoRow, 2).setValue(result.drive_file.file_id);
    sheet.getRange(infoRow + 1, 1).setValue('Drive Filename:');
    sheet.getRange(infoRow + 1, 2).setValue(result.drive_file.filename);
  }
}
```

### 5-5. getWeekStart() / getWeekEnd() - 週情報取得（新規関数）

```javascript
/**
 * 対象週の開始日を取得
 * シートの特定セル（例: B3）から読み取る、または入力を求める
 *
 * @returns {string} "YYYYMMDD" 形式 (例: "20260406")
 */
function getWeekStart() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();

  // B3セルから対象週開始を取得（例: "2026-04-06" or "20260406"）
  const cell = sheet.getRange('B3').getValue();
  if (cell) {
    const cleaned = String(cell).replace(/-/g, '');
    if (/^\d{8}$/.test(cleaned)) return cleaned;
  }

  // セルに値がない場合は入力を求める
  const ui = SpreadsheetApp.getUi();
  const response = ui.prompt(
    '対象週の開始日',
    '対象週の開始日を入力してください (例: 2026-04-06 or 20260406)',
    ui.ButtonSet.OK_CANCEL
  );

  if (response.getSelectedButton() === ui.Button.OK) {
    return response.getResponseText().replace(/-/g, '');
  }
  return null;
}

/**
 * 対象週の終了日を取得
 * シートの特定セル（例: B4）から読み取る、または入力を求める
 *
 * @returns {string} "YYYYMMDD" 形式 (例: "20260412")
 */
function getWeekEnd() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();

  // B4セルから対象週終了を取得
  const cell = sheet.getRange('B4').getValue();
  if (cell) {
    const cleaned = String(cell).replace(/-/g, '');
    if (/^\d{8}$/.test(cleaned)) return cleaned;
  }

  // セルに値がない場合は入力を求める
  const ui = SpreadsheetApp.getUi();
  const response = ui.prompt(
    '対象週の終了日',
    '対象週の終了日を入力してください (例: 2026-04-12 or 20260412)',
    ui.ButtonSet.OK_CANCEL
  );

  if (response.getSelectedButton() === ui.Button.OK) {
    return response.getResponseText().replace(/-/g, '');
  }
  return null;
}
```

---

## 6. Google Drive関連情報

| 項目 | 値 |
|------|-----|
| 共有フォルダID | `1tQJKZDjonFwiY6wYYgx1iVgu4cM98vRp` |
| カイポケ現行CSV | `kaipoke_current_{month}.csv` |
| 最適化CSV | `gas_optimized_{week_start}_{week_end}.csv` |
| 差分結果CSV (NEW) | `diff_result_{week_start}_{week_end}.csv` |

week_start/week_end はハイフンなしの `YYYYMMDD` 形式。

---

## 7. 検証チェック項目一覧

| # | チェック内容 | 種別 | 説明 |
|---|-------------|------|------|
| 1 | csv_content存在 + Driveアップロード | Error | `csv_content` が存在し、GAS側でDriveにアップロードできること |
| 2 | CSV行数一致 | Error | CSVのデータ行数 == `total_corrections` |
| 3 | 追加利用者の存在確認 | Error | `action=add` の利用者が最適化CSVに存在すること |
| 4 | アクション合計一致 | Error | `add + delete + edit + date_change_actions == total_corrections` |
| 5 | 業務種別の整合性 | Warning | `add` の業務種別が最適化CSVの業務種別と一致すること |
| 6 | 業務種別合計一致 | Warning | 業務種別別件数の合計 == total_corrections |

---

## 8. 実装手順（GASエージェント向け）

### Step 1: 週情報の取得機能を追加
- `getWeekStart()`, `getWeekEnd()` 関数を追加
- シートのセル（B3, B4）またはプロンプトから値を取得

### Step 2: diffPreview() を改修
- `use_drive: true` + `week_start` + `week_end` をリクエストに追加
- レスポンスの `csv_content` + `drive_file`（推奨ファイル名・フォルダID）を処理
- `verifyDiffResult()` を呼び出し（内部でGAS側Driveアップロード実行）
- 検証結果に応じて `PropertiesService` にフラグを保存

### Step 3: verifyDiffResult() を新規実装
- DriveApp を使って差分結果CSVと最適化CSVを読み込み
- 上記7つのチェック項目を実行

### Step 4: displayDiffSummary() を新規実装
- 「差分結果」シートに15列のデータを書き込み
- アクション別に色分け

### Step 5: applyDiff() にガードを追加
- `diff_verified !== 'true'` の場合は実行を拒否

### Step 6: writeDiffResultToSheet() を拡張
- 既存の12列 → 15列（業務種別、備考を追加）

---

## 9. テスト手順

1. GASアプリのサイドバーで「差分確認プレビュー」ボタンを押す
2. APIが実行され、DriveにCSVがアップロードされることを確認
3. 「差分結果」シートにデータが表示されることを確認
4. 検証結果のアラートが表示されることを確認
5. 検証OKの場合、「差分適用」ボタンが動作することを確認
6. 検証なしで「差分適用」を押すと拒否されることを確認

---

## 10. 注意事項

- API_URLは現在のGASアプリの設定値をそのまま使用すること
- `DriveApp.getFileById()` はサービスアカウントではなく、GASの実行ユーザーの権限で動作する。Driveフォルダがユーザーと共有されていること（既に共有済み）
- `Utilities.parseCsv()` はGAS組み込み関数でCSVパースが可能
- `PropertiesService.getScriptProperties()` はスクリプト全体で共有される設定ストア
- エラーが発生してもアラートで通知し、ユーザーが状況を把握できるようにする
