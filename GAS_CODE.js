/**
 * カイポケ自動化 - GAS (Google Apps Script) コード
 *
 * このコードをGoogle Spreadsheetのスクリプトエディタに貼り付けてください。
 *
 * 使い方:
 * 1. Google Spreadsheetを開く
 * 2. 拡張機能 → Apps Script
 * 3. このコードを貼り付け
 * 4. API_URLを自分のサーバーのURLに変更
 * 5. 保存して実行
 */

// ====== 設定 ======
const API_URL = 'http://localhost:5000';  // Playwright APIサーバーのURL
// ngrokを使う場合: const API_URL = 'https://xxxx.ngrok.io';

/**
 * カスタムメニューを追加
 */
function onOpen() {
  const ui = SpreadsheetApp.getUi();
  ui.createMenu('カイポケ自動化')
    .addItem('1. 月間スケジュール展開', 'expandSchedule')
    .addItem('2. CSV出力（Driveに保存）', 'exportCSV')
    .addItem('3. 差分確認（プレビュー）', 'diffPreview')
    .addItem('4. 差分適用（カイポケに反映）', 'applyDiff')
    .addSeparator()
    .addItem('サーバー状態確認', 'checkStatus')
    .addSeparator()
    .addItem('*** 非常停止 ***', 'emergencyStop')
    .addToUi();
}

/**
 * サーバー状態確認
 */
function checkStatus() {
  try {
    const response = UrlFetchApp.fetch(API_URL + '/api/status', {
      method: 'GET',
      muteHttpExceptions: true,
    });

    const result = JSON.parse(response.getContentText());

    if (result.status === 'running') {
      SpreadsheetApp.getUi().alert(
        'サーバー状態',
        'サーバーは正常に動作しています。\n\n' +
        'タスク状態: ' + (result.current_task.running ? '実行中' : '待機中'),
        SpreadsheetApp.getUi().ButtonSet.OK
      );
    } else {
      SpreadsheetApp.getUi().alert('エラー', 'サーバーに接続できません', SpreadsheetApp.getUi().ButtonSet.OK);
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert(
      'エラー',
      'サーバーに接続できません。\n\n' +
      'APIサーバーが起動しているか確認してください。\n' +
      'URL: ' + API_URL + '\n\n' +
      'エラー: ' + e.message,
      SpreadsheetApp.getUi().ButtonSet.OK
    );
  }
}

/**
 * 非常停止
 * 実行中のPlaywright処理を緊急停止します。
 */
function emergencyStop() {
  const ui = SpreadsheetApp.getUi();

  const confirm = ui.alert(
    '*** 非常停止 ***',
    '実行中の処理を緊急停止します。\n\n' +
    '現在処理中の利用者の操作が完了した後に停止します。\n\n' +
    '本当に停止しますか？',
    ui.ButtonSet.YES_NO
  );

  if (confirm !== ui.Button.YES) {
    return;
  }

  try {
    const response = UrlFetchApp.fetch(API_URL + '/api/stop', {
      method: 'POST',
      contentType: 'application/json',
      payload: JSON.stringify({}),
      muteHttpExceptions: true,
    });

    const status = response.getResponseCode();
    const result = JSON.parse(response.getContentText());

    if (status === 200 && result.success) {
      ui.alert(
        '非常停止',
        '停止を要求しました。\n\n' +
        result.message + '\n\n' +
        'タスク: ' + (result.current_task ? result.current_task.command || 'なし' : 'なし'),
        ui.ButtonSet.OK
      );
    } else {
      ui.alert('エラー', '停止に失敗しました: ' + (result.error || '不明なエラー'), ui.ButtonSet.OK);
    }
  } catch (e) {
    ui.alert(
      'エラー',
      '非常停止リクエストに失敗しました。\n\n' +
      'サーバーに接続できません。\n' +
      'エラー: ' + e.message,
      ui.ButtonSet.OK
    );
  }
}

/**
 * 1. 月間スケジュール展開
 */
function expandSchedule() {
  const ui = SpreadsheetApp.getUi();

  // 対象月を取得（シートから、または入力）
  const month = getTargetMonth();
  if (!month) return;

  ui.alert('実行開始', '月間スケジュール展開を開始します。\n対象月: ' + month, ui.ButtonSet.OK);

  try {
    const response = UrlFetchApp.fetch(API_URL + '/api/expand', {
      method: 'POST',
      contentType: 'application/json',
      payload: JSON.stringify({
        month: month,
      }),
      muteHttpExceptions: true,
    });

    const result = JSON.parse(response.getContentText());

    if (result.success) {
      ui.alert(
        '完了',
        '月間スケジュール展開が完了しました。\n\n' +
        '成功: ' + result.result.success + '件\n' +
        'スキップ: ' + result.result.skipped + '件\n' +
        '失敗: ' + result.result.failed + '件',
        ui.ButtonSet.OK
      );
    } else {
      ui.alert('エラー', result.error || '不明なエラー', ui.ButtonSet.OK);
    }
  } catch (e) {
    ui.alert('エラー', 'APIリクエストに失敗しました: ' + e.message, ui.ButtonSet.OK);
  }
}

/**
 * 2. CSV出力（Driveに保存）
 */
function exportCSV() {
  const ui = SpreadsheetApp.getUi();

  const month = getTargetMonth();
  if (!month) return;

  ui.alert('実行開始', 'CSV出力を開始します。\n対象月: ' + month, ui.ButtonSet.OK);

  try {
    const response = UrlFetchApp.fetch(API_URL + '/api/export', {
      method: 'POST',
      contentType: 'application/json',
      payload: JSON.stringify({
        month: month,
        upload_to_drive: true,
      }),
      muteHttpExceptions: true,
    });

    const result = JSON.parse(response.getContentText());

    if (result.success) {
      ui.alert(
        '完了',
        'CSV出力が完了しました。\n\n' +
        'ファイル: ' + result.result.file_path,
        ui.ButtonSet.OK
      );
    } else {
      ui.alert('エラー', result.error || '不明なエラー', ui.ButtonSet.OK);
    }
  } catch (e) {
    ui.alert('エラー', 'APIリクエストに失敗しました: ' + e.message, ui.ButtonSet.OK);
  }
}

/**
 * 3. 差分確認（プレビュー）
 */
function diffPreview() {
  const ui = SpreadsheetApp.getUi();

  // CSVファイルパスを設定から取得、またはデフォルト
  const currentCsv = 'data/kaipoke_current.csv';
  const optimizedCsv = 'data/optimized.csv';

  ui.alert('実行開始', '差分確認を開始します。', ui.ButtonSet.OK);

  try {
    const response = UrlFetchApp.fetch(API_URL + '/api/diff', {
      method: 'POST',
      contentType: 'application/json',
      payload: JSON.stringify({
        current_csv: currentCsv,
        optimized_csv: optimizedCsv,
      }),
      muteHttpExceptions: true,
    });

    const result = JSON.parse(response.getContentText());

    if (result.success) {
      const summary = result.result.summary;
      const message =
        '差分確認が完了しました。\n\n' +
        '=== 修正件数 ===\n' +
        '合計: ' + result.result.total_corrections + '件\n' +
        '時間変更: ' + summary.time_changes + '件\n' +
        '職員変更: ' + summary.staff_changes + '件\n' +
        '日付変更: ' + summary.date_changes + '件\n' +
        '追加: ' + summary.additions + '件\n' +
        '削除: ' + summary.deletions + '件\n\n' +
        '修正シート: ' + result.result.output_files.csv;

      ui.alert('差分確認結果', message, ui.ButtonSet.OK);

      // 結果をシートに書き込む（オプション）
      writeDiffResultToSheet(result.result);
    } else {
      ui.alert('エラー', result.error || '不明なエラー', ui.ButtonSet.OK);
    }
  } catch (e) {
    ui.alert('エラー', 'APIリクエストに失敗しました: ' + e.message, ui.ButtonSet.OK);
  }
}

/**
 * 4. 差分適用（カイポケに反映）
 */
function applyDiff() {
  const ui = SpreadsheetApp.getUi();

  const month = getTargetMonth();
  if (!month) return;

  // 確認ダイアログ
  const confirm = ui.alert(
    '確認',
    '差分をカイポケに適用します。\n\n' +
    '対象月: ' + month + '\n\n' +
    '本当に実行しますか？',
    ui.ButtonSet.YES_NO
  );

  if (confirm !== ui.Button.YES) {
    return;
  }

  // dry-runで実行するか確認
  const dryRunConfirm = ui.alert(
    'テスト実行',
    'まずテスト実行（dry-run）を行いますか？\n\n' +
    'はい: テスト実行（実際には保存しない）\n' +
    'いいえ: 本番実行（実際に保存する）',
    ui.ButtonSet.YES_NO
  );

  const dryRun = (dryRunConfirm === ui.Button.YES);

  ui.alert('実行開始', '差分適用を開始します。\n\ndry-run: ' + dryRun, ui.ButtonSet.OK);

  try {
    const response = UrlFetchApp.fetch(API_URL + '/api/apply', {
      method: 'POST',
      contentType: 'application/json',
      payload: JSON.stringify({
        month: month,
        correction_sheet: 'data/correction_sheet.json',
        dry_run: dryRun,
      }),
      muteHttpExceptions: true,
    });

    const result = JSON.parse(response.getContentText());

    if (result.success) {
      const r = result.result;
      ui.alert(
        '完了',
        '差分適用が完了しました。\n\n' +
        '成功: ' + r.success + '件\n' +
        '失敗: ' + r.failed + '件\n' +
        'スキップ: ' + r.skipped + '件\n' +
        '合計: ' + r.total + '件\n\n' +
        (dryRun ? '※テスト実行のため、実際には保存されていません' : ''),
        ui.ButtonSet.OK
      );
    } else {
      ui.alert('エラー', result.error || '不明なエラー', ui.ButtonSet.OK);
    }
  } catch (e) {
    ui.alert('エラー', 'APIリクエストに失敗しました: ' + e.message, ui.ButtonSet.OK);
  }
}

/**
 * 対象月を取得
 */
function getTargetMonth() {
  // シートの「対象月」セルから取得、またはダイアログで入力
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();

  // B2セルから対象月を取得（例: "2026年4月" → "2026-04"）
  const monthCell = sheet.getRange('B2').getValue();

  if (monthCell) {
    // "2026年4月" 形式を "2026-04" に変換
    const match = String(monthCell).match(/(\d{4})年(\d{1,2})月/);
    if (match) {
      const year = match[1];
      const month = match[2].padStart(2, '0');
      return year + '-' + month;
    }
    // すでに "2026-04" 形式の場合
    if (/^\d{4}-\d{2}$/.test(monthCell)) {
      return monthCell;
    }
  }

  // セルに値がない場合は入力を求める
  const ui = SpreadsheetApp.getUi();
  const response = ui.prompt(
    '対象月',
    '対象月を入力してください (例: 2026-04)',
    ui.ButtonSet.OK_CANCEL
  );

  if (response.getSelectedButton() === ui.Button.OK) {
    return response.getResponseText();
  }

  return null;
}

/**
 * 差分結果をシートに書き込む
 */
function writeDiffResultToSheet(result) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('差分結果');
  if (!sheet) return;

  // ヘッダー
  sheet.getRange('A1:L1').setValues([[
    '利用者', '日付(前)', '日付(後)',
    '開始(前)', '開始(後)', '終了(前)', '終了(後)',
    '職員1(前)', '職員1(後)', '職員2(前)', '職員2(後)',
    'アクション'
  ]]);

  // データ
  const data = result.corrections.map(c => [
    c.user_name,
    c.date_from,
    c.date_to,
    c.start_time_from,
    c.start_time_to,
    c.end_time_from,
    c.end_time_to,
    c.staff1_from,
    c.staff1_to,
    c.staff2_from,
    c.staff2_to,
    c.action,
  ]);

  if (data.length > 0) {
    sheet.getRange(2, 1, data.length, 12).setValues(data);
  }
}
