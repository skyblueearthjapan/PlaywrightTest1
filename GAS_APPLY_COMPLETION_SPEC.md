# GAS側エージェント向け 差分適用・完了レポート・検証 設計仕様書

## 概要

カイポケ自動化システムにおいて、差分適用（`/api/apply`）の本番運用に必要な以下4つの機能を実装する。

1. **未割当の事前警告**: 適用前に「未割当」職員を含む修正をユーザーに通知
2. **本番適用の実行**: dry_run=false での適用呼び出し
3. **完了レポートの表示**: 適用結果をスプレッドシートに書き込み・アラート表示
4. **完了後検証**: カイポケCSVを再エクスポートし修正シートと照合

---

## 1. 未割当の事前警告（GAS側で実装）

### 背景

修正シート内に `staff1_to = "未割当"` の修正が含まれる場合がある。
VPS側では `"未割当"` を `'-'`（職員未選択）として処理するが、
これは意図的でない場合があるため、**GAS側で適用前にユーザーに警告する**。

### 実装タイミング

`applyDiff()` 関数内、APIを呼び出す**前**に実行する。

### ロジック

```javascript
function checkUnassignedStaff(corrections) {
  // corrections = diffPreview時にPropertiesServiceまたはシートに保存済みの修正データ
  const unassigned = corrections.filter(c => c.staff1_to === "未割当");

  if (unassigned.length === 0) return true; // 問題なし → 続行

  // 警告メッセージ作成
  const lines = unassigned.map(c =>
    `  ・${c.user_name} ${c.date_to}日 ${c.start_time_to}-${c.end_time_to} (${c.action})`
  );

  const msg = `以下の ${unassigned.length} 件は職員が「未割当」です。\n` +
    `カイポケ上では職員未選択（'-'）として登録されます。\n\n` +
    lines.join('\n') + '\n\n' +
    `このまま適用しますか？`;

  const ui = SpreadsheetApp.getUi();
  const response = ui.alert('未割当の職員あり', msg, ui.ButtonSet.YES_NO);
  return response === ui.Button.YES;
}
```

### 表示例（アラート）

```
⚠️ 未割当の職員あり

以下の 2 件は職員が「未割当」です。
カイポケ上では職員未選択（'-'）として登録されます。

  ・松田 美保 8日 14:00-14:35 (edit)
  ・後藤 治子 11日 11:00-11:35 (edit)

このまま適用しますか？
[はい] [いいえ]
```

---

## 2. 本番適用の実行

### `/api/apply` リクエスト仕様

```javascript
// GAS側から呼び出し
const payload = {
  correction_data: corrections,   // diffPreview時に取得済みの修正データ配列
  month: "2026-04",               // 対象月
  dry_run: false,                 // ★ 本番: false
  headed: true,                   // VNC表示（デバッグ用、本番でもtrue推奨）
  // limit: null,                 // 全件適用（省略可）
  // action_filter: null,         // フィルタなし（省略可）
  // business_type_filter: null,  // フィルタなし（省略可）
  // target_users: null,          // 全利用者（省略可）
};

const options = {
  method: 'post',
  contentType: 'application/json',
  payload: JSON.stringify(payload),
  muteHttpExceptions: true,
};

const response = UrlFetchApp.fetch(API_URL + '/api/apply', options);
```

### `/api/apply` レスポンス仕様（VPS側 現行）

```json
{
  "success": true,
  "result": {
    "total": 123,
    "schedule_total": 120,
    "event_total": 3,
    "success": 120,
    "failed": 0,
    "skipped": 3,
    "warnings": [
      {
        "type": "unassigned_staff",
        "user": "松田 美保",
        "date": "8",
        "action": "edit",
        "message": "職員1を未割当（'-'）で登録しました"
      }
    ],
    "execution_time_sec": 245.3,
    "completed_at": "2026-04-15T14:30:00",
    "details": [
      {
        "user": "山田 太郎",
        "date": "6",
        "action": "edit",
        "business_type": "医療保険",
        "status": "success"
      },
      {
        "user": "山田 太郎",
        "date": "8",
        "action": "delete",
        "business_type": "医療保険",
        "status": "success"
      },
      {
        "user": "川田 春奈",
        "date": "10",
        "action": "add",
        "business_type": "医療保険",
        "status": "skipped",
        "reason": "user_not_found"
      },
      {
        "staff": "佐藤 憲二",
        "date": "7",
        "action": "event_add",
        "business_type": "イベント",
        "event_name": "[EV] 会議:地域アド会議 / さいたま市サンプル会議室",
        "status": "success"
      }
    ]
  }
}
```

### details配列の各エントリ仕様

| フィールド | 型 | 説明 | 必須 |
|-----------|-----|------|------|
| `user` | string | 利用者名（Phase 1のみ） | Phase 1 |
| `staff` | string | 職員名（Phase 2のみ） | Phase 2 |
| `date` | string | 日付（日のみ、例: "6"） | Yes |
| `action` | string | `"edit"` / `"add"` / `"delete"` / `"date_change"` / `"event_add"` | Yes |
| `business_type` | string | `"医療保険"` / `"介護保険"` / `"イベント"` | Yes |
| `status` | string | `"success"` / `"failed"` / `"skipped"` / `"error"` | Yes |
| `reason` | string | 失敗・スキップ時の理由コード | No |
| `event_name` | string | イベント名（Phase 2成功時のみ） | No |

### reason コード一覧

| reason | 説明 |
|--------|------|
| `user_not_found` | 利用者がカイポケのドロップダウンに見つからない |
| `staff_not_found` | 職員がカイポケのドロップダウンに見つからない |
| `staff_tab_navigation_failed` | 職員別タブへの遷移失敗 |
| `entry_not_found` | 対象の予定（日付・時間・職員）が盤面に見つからない |
| `invalid_date_from` | 変更前の日付が空・数値でない（何も操作せず中止） |
| `delete_button_not_found` | 削除ボタンが表示されない（モーダルが開かない） |
| `delete_not_accepted` | 削除後もモーダルが閉じない（削除が受理されていない） |
| `delete_not_verified` | 削除後も同じ予定が残っている |
| `delete_error` | 削除中に例外が発生 |
| `staff_select_not_shown` | 職員選択セレクトが表示されない |
| `register_failed` | 「登録する」が通らない（エラー表示・モーダルが閉じない） |
| `add_failed_nothing_deleted` | 追加に失敗。**元の予定は消していない**（消失なし） |
| `old_row_remains_duplicate` | 追加は成功したが旧行の削除に失敗。**新旧2件が残る**（要手動削除） |
| `add_may_have_registered` | 追加は失敗扱いだが盤面に新しい行あり。復元操作はしていない（要目視） |
| `add_failed_old_row_intact` | 追加は失敗、元の行は残存（削除が効いていない）。復元操作なし |
| `add_failed_rolled_back` | 追加に失敗したので**元の予定を再追加して復元済み** |
| `add_failed_row_lost` | 追加に失敗し復元もできなかった。**要手動復元** |
| `unknown` | 失敗したが理由コードが記録されなかった |
| （エラーメッセージ文字列） | 予期しないエラーの場合、例外メッセージが入る |

---

## 3. 完了レポートの表示（GAS側で実装）

### 3-1. アラート表示（即時フィードバック）

適用完了直後にアラートダイアログで概要を表示する。

```javascript
function showApplyResult(result) {
  const ui = SpreadsheetApp.getUi();

  if (result.failed === 0 && result.skipped === 0) {
    // 全件成功
    ui.alert(
      '適用完了',
      `全 ${result.total} 件の適用が完了しました。\n\n` +
      `  成功: ${result.success} 件\n` +
      `  （スケジュール: ${result.schedule_total} 件、イベント: ${result.event_total} 件）`,
      ui.ButtonSet.OK
    );
  } else {
    // 失敗・スキップあり
    const failedDetails = result.details
      .filter(d => d.status === 'failed' || d.status === 'error')
      .map(d => `  ・${d.user || d.staff} ${d.date}日 ${d.action} [${d.reason || '不明'}]`);

    const skippedDetails = result.details
      .filter(d => d.status === 'skipped')
      .map(d => `  ・${d.user || d.staff} ${d.date}日 ${d.action} [${d.reason || '不明'}]`);

    let msg = `適用結果:\n` +
      `  成功: ${result.success} / 失敗: ${result.failed} / スキップ: ${result.skipped} / 合計: ${result.total}\n`;

    if (failedDetails.length > 0) {
      msg += `\n--- 失敗 ---\n${failedDetails.join('\n')}\n`;
    }
    if (skippedDetails.length > 0) {
      msg += `\n--- スキップ ---\n${skippedDetails.join('\n')}\n`;
    }

    msg += `\n詳細は「適用結果」シートを確認してください。`;

    ui.alert('適用完了（要確認）', msg, ui.ButtonSet.OK);
  }
}
```

### 3-2. スプレッドシートへの書き込み

**シート名**: `適用結果`（存在しなければ作成）

**ヘッダー（9列）**:
```
利用者/職員, 日付, アクション, 業務種別, ステータス, 理由, イベント名, Phase, タイムスタンプ
```

**色分けルール**:

| ステータス | 背景色 | 色名 |
|-----------|--------|------|
| `success` | `#d4edda` | 薄緑 |
| `failed` | `#f8d7da` | 薄赤 |
| `error` | `#f8d7da` | 薄赤 |
| `skipped` | `#fff3cd` | 薄黄 |

**書き込みロジック**:

```javascript
function writeApplyResultToSheet(result) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName('適用結果');

  if (!sheet) {
    sheet = ss.insertSheet('適用結果');
  } else {
    sheet.clear();
  }

  // ヘッダー
  const headers = ['利用者/職員', '日付', 'アクション', '業務種別',
                    'ステータス', '理由', 'イベント名', 'Phase', 'タイムスタンプ'];
  sheet.getRange(1, 1, 1, headers.length).setValues([headers])
    .setFontWeight('bold')
    .setBackground('#4a86c8')
    .setFontColor('#ffffff');

  // サマリー行（2行目）
  const timestamp = new Date().toLocaleString('ja-JP');
  const summaryRow = [
    `合計: ${result.total}件`, '',
    `成功: ${result.success}`, '',
    `失敗: ${result.failed}`,
    `スキップ: ${result.skipped}`, '', '', timestamp
  ];
  sheet.getRange(2, 1, 1, headers.length).setValues([summaryRow])
    .setFontWeight('bold')
    .setBackground('#e2e3e5');

  // 詳細データ（3行目～）
  const rows = result.details.map(d => [
    d.user || d.staff || '',
    d.date || '',
    d.action || '',
    d.business_type || '',
    d.status || '',
    d.reason || '',
    d.event_name || '',
    (d.action === 'event_add') ? 'Phase 2' : 'Phase 1',
    timestamp,
  ]);

  if (rows.length > 0) {
    const dataRange = sheet.getRange(3, 1, rows.length, headers.length);
    dataRange.setValues(rows);

    // 色分け
    const colorMap = {
      'success': '#d4edda',
      'failed': '#f8d7da',
      'error': '#f8d7da',
      'skipped': '#fff3cd',
    };

    for (let i = 0; i < rows.length; i++) {
      const status = rows[i][4]; // ステータス列
      const color = colorMap[status] || '#ffffff';
      sheet.getRange(3 + i, 1, 1, headers.length).setBackground(color);
    }
  }

  // 列幅自動調整
  for (let col = 1; col <= headers.length; col++) {
    sheet.autoResizeColumn(col);
  }
}
```

---

## 4. 完了後検証（GAS側で実装、VPS APIを利用）

### 4-1. 検証フロー

```
適用完了
  ↓
GAS: ユーザーに「検証を実行しますか？」と確認
  ↓ [はい]
GAS: VPS API /api/export を呼び出し（適用後のカイポケCSVを再エクスポート）
  ↓
VPS: カイポケにログインし、現行CSVをエクスポート → csv_content をレスポンスに含める
  ↓
GAS: エクスポートされたCSVを受け取り、Driveに保存
  ↓
GAS: 修正シートの内容と適用後CSVを照合
  ↓
GAS: 検証結果を「検証結果」シートに表示 + アラート
```

### 4-2. VPS API `/api/export`（実装済み・単独呼び出し対応済み）

`/api/export` は既に単独で呼び出し可能。月単位でカイポケCSVをエクスポートし、`csv_content` をレスポンスに含める。`week_start`/`week_end` パラメータも受け付ける（検証用ファイル名の生成に使用）。

**リクエスト**:
```json
{
  "month": "2026-04",
  "week_start": "2026-04-06",
  "week_end": "2026-04-12"
}
```

**レスポンス**:
```json
{
  "success": true,
  "result": {
    "success": true,
    "file_path": "data/kaipoke_202604.csv",
    "csv_content": "利用者,日付,曜日,開始時間,終了時間,...\n山田 太郎,6,月,10:00,11:00,...",
    "row_count": 850,
    "file_size_bytes": 125000,
    "drive_file": {
      "filename": "kaipoke_export_202604.csv",
      "folder_id": "1tQJKZDjonFwiY6wYYgx1iVgu4cM98vRp",
      "verification_filename": "kaipoke_current_20260406_20260412_post_apply.csv"
    },
    "export_timestamp": "2026-04-15T14:30:00"
  }
}
```

**重要フィールド**:
| フィールド | 説明 |
|-----------|------|
| `csv_content` | カイポケCSVの全文テキスト（月全体のデータ） |
| `drive_file.filename` | 標準のファイル名 |
| `drive_file.folder_id` | DriveフォルダID |
| `drive_file.verification_filename` | 検証用推奨ファイル名（week指定時のみ） |
| `export_timestamp` | エクスポート日時（ISO 8601） |

> **注意**: エクスポートは月全体のCSVです。GAS側で検証時に日付でフィルタしてください。

### 4-3. 検証ロジック（GAS側）

修正シートの各修正が正しく適用されたかを、適用後CSVと照合する。

```javascript
function verifyApplyResult(corrections, postApplyCsvContent, applyResult) {
  const postData = Utilities.parseCsv(postApplyCsvContent);
  // postData[0] = ヘッダー行
  // postData[1..] = データ行

  const results = [];

  for (const correction of corrections) {
    // applyResultで失敗/スキップだったものは検証スキップ
    const detail = applyResult.details.find(d =>
      (d.user === correction.user_name || d.staff === correction.staff1_to) &&
      d.date === (correction.date_to || correction.date_from) &&
      d.action === correction.action
    );

    if (detail && detail.status !== 'success') {
      results.push({
        correction: correction,
        verification: 'skipped',
        reason: `適用時 ${detail.status}: ${detail.reason || ''}`,
      });
      continue;
    }

    // CSVからこの利用者・日付・時間のエントリを検索
    const verified = verifySingleCorrection(correction, postData);
    results.push(verified);
  }

  return results;
}

function verifySingleCorrection(correction, postData) {
  // CSVのヘッダーからカラムインデックスを取得
  const headers = postData[0];
  const colIdx = {
    user: headers.indexOf('利用者'),
    date: headers.indexOf('日付'),
    startTime: headers.indexOf('開始時間'),
    endTime: headers.indexOf('終了時間'),
    staff1: headers.indexOf('職員1'),
    staff2: headers.indexOf('職員2'),
    serviceType: headers.indexOf('サービス内容'),
  };

  const userName = correction.user_name;
  const dateTo = correction.date_to;

  switch (correction.action) {
    case 'delete': {
      // 削除: 該当エントリがCSVに存在しないことを確認
      const found = postData.slice(1).find(row =>
        row[colIdx.user] === userName &&
        row[colIdx.date] === correction.date_from &&
        row[colIdx.startTime] === correction.start_time_from
      );
      return {
        correction: correction,
        verification: found ? 'FAIL' : 'OK',
        reason: found ? '削除されたはずのエントリがまだ存在する' : '',
      };
    }

    case 'add': {
      // 追加: 該当エントリがCSVに存在することを確認
      const found = postData.slice(1).find(row =>
        row[colIdx.user] === userName &&
        row[colIdx.date] === dateTo &&
        row[colIdx.startTime] === correction.start_time_to &&
        row[colIdx.endTime] === correction.end_time_to
      );
      return {
        correction: correction,
        verification: found ? 'OK' : 'FAIL',
        reason: found ? '' : '追加されたはずのエントリが見つからない',
      };
    }

    case 'edit': {
      // 編集: 変更後の値でエントリが存在し、変更前の値では存在しないことを確認
      const foundNew = postData.slice(1).find(row =>
        row[colIdx.user] === userName &&
        row[colIdx.date] === dateTo &&
        row[colIdx.startTime] === correction.start_time_to &&
        row[colIdx.endTime] === correction.end_time_to
      );
      return {
        correction: correction,
        verification: foundNew ? 'OK' : 'FAIL',
        reason: foundNew ? '' : '編集後のエントリが見つからない',
      };
    }

    case 'date_change': {
      // 日付変更: 新しい日付にエントリが存在し、元の日付からは消えていること
      const foundNew = postData.slice(1).find(row =>
        row[colIdx.user] === userName &&
        row[colIdx.date] === dateTo &&
        row[colIdx.startTime] === correction.start_time_to
      );
      const foundOld = postData.slice(1).find(row =>
        row[colIdx.user] === userName &&
        row[colIdx.date] === correction.date_from &&
        row[colIdx.startTime] === correction.start_time_from
      );

      if (foundNew && !foundOld) {
        return { correction, verification: 'OK', reason: '' };
      } else if (!foundNew) {
        return { correction, verification: 'FAIL', reason: '移動先にエントリが見つからない' };
      } else {
        return { correction, verification: 'FAIL', reason: '移動元のエントリがまだ残っている' };
      }
    }

    default:
      return { correction, verification: 'skipped', reason: `未対応アクション: ${correction.action}` };
  }
}
```

### 4-4. 検証結果のスプレッドシート表示

**シート名**: `検証結果`

**ヘッダー（10列）**:
```
利用者, 日付(前), 日付(後), アクション, 業務種別, サービス内容, 検証結果, 理由, 適用ステータス, タイムスタンプ
```

**色分け**:

| 検証結果 | 背景色 | 意味 |
|---------|--------|------|
| `OK` | `#d4edda` | 検証成功 - 期待通りに反映済み |
| `FAIL` | `#f8d7da` | 検証失敗 - 反映されていない |
| `skipped` | `#fff3cd` | 検証スキップ（適用失敗/スキップだったもの） |

### 4-5. 検証結果のアラート表示

```javascript
function showVerificationResult(verifyResults) {
  const ui = SpreadsheetApp.getUi();

  const ok = verifyResults.filter(r => r.verification === 'OK').length;
  const fail = verifyResults.filter(r => r.verification === 'FAIL').length;
  const skip = verifyResults.filter(r => r.verification === 'skipped').length;
  const total = verifyResults.length;

  if (fail === 0) {
    ui.alert(
      '検証完了 - 全件OK',
      `全 ${total} 件中 ${ok} 件が正常に反映されていることを確認しました。\n` +
      `（スキップ: ${skip} 件）\n\n` +
      `詳細は「検証結果」シートを確認してください。`,
      ui.ButtonSet.OK
    );
  } else {
    const failItems = verifyResults
      .filter(r => r.verification === 'FAIL')
      .map(r => `  ・${r.correction.user_name} ${r.correction.date_to}日 ${r.correction.action}: ${r.reason}`)
      .join('\n');

    ui.alert(
      '⚠ 検証完了 - 不一致あり',
      `検証結果: OK ${ok} / 不一致 ${fail} / スキップ ${skip} / 合計 ${total}\n\n` +
      `--- 不一致 ---\n${failItems}\n\n` +
      `手動での確認が必要です。\n` +
      `詳細は「検証結果」シートを確認してください。`,
      ui.ButtonSet.OK
    );
  }
}
```

---

## 5. 全体フロー（applyDiff 関数の改修）

```javascript
function applyDiff() {
  const ui = SpreadsheetApp.getUi();

  // ===== Step 0: 検証済みチェック =====
  const verified = PropertiesService.getScriptProperties().getProperty('diff_verified');
  if (verified !== 'true') {
    ui.alert('実行不可', '差分検証が完了していません。\n先に差分確認プレビューを実行してください。', ui.ButtonSet.OK);
    return;
  }

  // ===== Step 1: 修正データの取得 =====
  // PropertiesServiceまたはスプレッドシートから修正データを取得
  const corrections = getStoredCorrections();

  // ===== Step 2: 未割当チェック =====
  if (!checkUnassignedStaff(corrections)) {
    return; // ユーザーがキャンセル
  }

  // ===== Step 3: 最終確認 =====
  const confirmMsg = `以下の内容で差分適用を実行します。\n\n` +
    `  対象件数: ${corrections.length} 件\n` +
    `  対象月: ${getTargetMonth()}\n\n` +
    `⚠ この操作はカイポケのスケジュールを直接変更します。\n` +
    `本当に実行しますか？`;

  const confirm = ui.alert('差分適用の確認', confirmMsg, ui.ButtonSet.YES_NO);
  if (confirm !== ui.Button.YES) return;

  // ===== Step 4: 適用実行 =====
  ui.alert('実行開始', '差分適用を開始します。\n完了までお待ちください...', ui.ButtonSet.OK);

  const payload = {
    correction_data: corrections,
    month: getTargetMonth(),
    dry_run: false,       // ★ 本番適用
    headed: true,
  };

  const response = UrlFetchApp.fetch(API_URL + '/api/apply', {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });

  const json = JSON.parse(response.getContentText());

  if (!json.success) {
    ui.alert('適用エラー', `エラーが発生しました:\n${json.error}`, ui.ButtonSet.OK);
    return;
  }

  const result = json.result;

  // ===== Step 5: 完了レポート =====
  writeApplyResultToSheet(result);
  showApplyResult(result);

  // ===== Step 6: 検証フラグリセット =====
  PropertiesService.getScriptProperties().setProperty('diff_verified', 'false');

  // ===== Step 7: 完了後検証 =====
  const verifyConfirm = ui.alert(
    '検証実行',
    '適用が完了しました。\nカイポケからCSVを再エクスポートして検証を実行しますか？\n\n' +
    '（検証にはカイポケへの再ログインが必要なため、数分かかります）',
    ui.ButtonSet.YES_NO
  );

  if (verifyConfirm === ui.Button.YES) {
    runPostApplyVerification(corrections, result);
  }
}
```

---

## 6. 完了後検証の詳細フロー（GAS側）

```javascript
function runPostApplyVerification(corrections, applyResult) {
  const ui = SpreadsheetApp.getUi();

  try {
    // Step 1: カイポケCSV再エクスポート
    const exportPayload = {
      month: getTargetMonth(),
      week_start: getWeekStart(),
      week_end: getWeekEnd(),
      use_drive: true,
    };

    const exportResponse = UrlFetchApp.fetch(API_URL + '/api/export', {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(exportPayload),
      muteHttpExceptions: true,
    });

    const exportJson = JSON.parse(exportResponse.getContentText());

    if (!exportJson.success) {
      ui.alert('エクスポートエラー', `CSVエクスポートに失敗しました:\n${exportJson.error}`, ui.ButtonSet.OK);
      return;
    }

    const csvContent = exportJson.result.csv_content;

    // Step 2: CSVをDriveに保存
    const driveFile = exportJson.result.drive_file;
    if (csvContent && driveFile && driveFile.folder_id && driveFile.filename) {
      const folder = DriveApp.getFolderById(driveFile.folder_id);

      // 既存同名ファイルを削除
      const existing = folder.getFilesByName(driveFile.filename);
      while (existing.hasNext()) {
        existing.next().setTrashed(true);
      }

      // 適用後CSVとして保存（ファイル名に _post_apply を付ける）
      const postApplyFilename = driveFile.filename.replace('.csv', '_post_apply.csv');
      folder.createFile(postApplyFilename, csvContent, MimeType.CSV);
    }

    // Step 3: 検証実行
    const verifyResults = verifyApplyResult(corrections, csvContent, applyResult);

    // Step 4: 検証結果をシートに書き込み
    writeVerificationResultToSheet(verifyResults);

    // Step 5: 検証結果をアラート表示
    showVerificationResult(verifyResults);

  } catch (e) {
    ui.alert('検証エラー', `検証中にエラーが発生しました:\n${e.message}`, ui.ButtonSet.OK);
  }
}
```

---

## 7. データの永続化（修正データの保存方法）

### 問題
GASの `PropertiesService` はプロパティ値あたり9KB制限があるため、
123件の修正データ（JSON）は保存できない可能性がある。

### 解決方法

**方法A**: スプレッドシートの隠しシートに保存（推奨）

```javascript
function storeCorrections(corrections) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName('_corrections_cache');
  if (!sheet) {
    sheet = ss.insertSheet('_corrections_cache');
    sheet.hideSheet();
  }
  sheet.clear();
  sheet.getRange(1, 1).setValue(JSON.stringify(corrections));
}

function getStoredCorrections() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName('_corrections_cache');
  if (!sheet) return [];
  const json = sheet.getRange(1, 1).getValue();
  return json ? JSON.parse(json) : [];
}
```

**方法B**: CacheService（6時間有効、100KB制限）

```javascript
function storeCorrections(corrections) {
  const cache = CacheService.getScriptCache();
  cache.put('corrections', JSON.stringify(corrections), 21600); // 6時間
}
```

> **推奨**: 方法Aの隠しシート方式。サイズ制限がなく、永続的。

---

## 8. イベント追加の検証に関する注意

Phase 2（イベント追加）は**職員別タブ**で行われるため、
利用者別のCSVエクスポートでは直接検証できない場合がある。

イベント追加の検証は以下のいずれかで対応:
- **方法1**: 適用結果の `status: "success"` を信頼する（VPS側のdry-runテストで検証済み）
- **方法2**: カイポケの職員別タブから別途エクスポート（追加開発が必要）

> **推奨**: まずは方法1で運用し、必要に応じて方法2を検討。

---

## 9. VPS側の変更事項（実装済み）

### 9-1. `/api/apply` レスポンス拡張 - 実装済み

以下のフィールドが追加済み:
- `warnings[]` - 未割当で登録した場合などの警告配列
- `execution_time_sec` - 実行時間（秒）
- `completed_at` - 完了日時（ISO 8601）

GAS側は `result.warnings` を参照して警告を表示できます。

### 9-2. `/api/export` の単独呼び出し - 実装済み

- 単独で呼び出し可能（`/api/apply` とは独立）
- `week_start`/`week_end` パラメータ追加済み
- `drive_file` 構造を追加（filename, folder_id, verification_filename）
- `export_timestamp` を追加

### 9-3. `/api/apply` は `correction_data` を直接受付済み

`/api/apply` は既に `correction_data` パラメータを受け付けます。
GAS側から修正データ配列を直接送信できます:

```javascript
const payload = {
  correction_data: corrections,  // 修正データ配列をそのまま送信
  month: "2026-04",
  dry_run: false,
  headed: true,
};
```

VPS側で `correction_data` をファイルに保存してから `run_auto_apply()` を呼び出します。

---

## 10. 実装優先順位

| 順番 | 機能 | 担当 | 難易度 |
|------|------|------|--------|
| 1 | 未割当事前警告 | GAS | 低 |
| 2 | 本番適用呼び出し (dry_run=false) | GAS | 低 |
| 3 | 完了レポート（アラート + シート書き込み） | GAS | 中 |
| 4 | 完了後検証（CSVエクスポート + 照合） | GAS + VPS | 高 |

---

## 11. テスト手順

### Step 1: dry_run=true でのテスト
1. GASアプリから `applyDiff()` を呼び出し（dry_run=true に一時変更）
2. 未割当警告が表示されることを確認
3. 適用結果がシートに書き込まれることを確認
4. アラートが表示されることを確認

### Step 2: 検証フローのテスト
1. dry_runで得られた結果で検証ロジックをテスト
2. 検証結果がシートに書き込まれることを確認

### Step 3: 本番適用
1. dry_run=false で適用
2. 完了レポートを確認
3. 検証を実行し全件OKを確認
