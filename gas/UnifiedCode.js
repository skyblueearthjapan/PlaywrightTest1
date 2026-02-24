/**
 * 訪問看護 自動スケジューリング - 統合Webアプリ
 * URL1つで出力画面・入力画面を切り替え
 * 入力画面は管理者のみアクセス可能
 */

// ============================================================
// 定数（シート名・スプレッドシートID）
// ============================================================
const SPREADSHEET_ID = PropertiesService.getScriptProperties().getProperty('SS_ID');

// シート名（typo防止用に定数化）
const SHEETS = {
  // 出力系
  WEEK_VIEW: '週ビュー',
  WEEKLY_REQUEST: '週間リクエスト',
  ASSIGN_RESULT: '割当結果',
  ASSIGN_NG: '割当不可',
  ROUTE_SUMMARY: 'ルートサマリ',
  // 入力系
  PATIENT_MASTER: '患者マスタ',
  STAFF_MASTER: 'スタッフマスタ',
  CHANGE_REQUEST: '個別変更リクエスト',
  STAFF_CHANGE_REQUEST: 'スタッフ個別変更リクエスト',
  EVENT_REQUEST: 'イベントリクエスト',
  // 権限
  ADMIN: '管理者',
  // その他
  LOG: '実行ログ'
};

// 出力タブ一覧
const OUTPUT_TABS = [
  { key: 'weekView', name: '週ビュー', sheetName: SHEETS.WEEK_VIEW },
  { key: 'weeklyRequest', name: '週間リクエスト', sheetName: SHEETS.WEEKLY_REQUEST },
  { key: 'assignResult', name: '割当結果', sheetName: SHEETS.ASSIGN_RESULT },
  { key: 'assignNg', name: '割当不可', sheetName: SHEETS.ASSIGN_NG },
  { key: 'routeSummary', name: 'ルートサマリ', sheetName: SHEETS.ROUTE_SUMMARY }
];

// 入力タブ一覧
const INPUT_TABS = [
  { key: 'patient', name: '患者マスタ', sheetName: SHEETS.PATIENT_MASTER },
  { key: 'staff', name: 'スタッフマスタ', sheetName: SHEETS.STAFF_MASTER },
  { key: 'change', name: '個別変更リクエスト', sheetName: SHEETS.CHANGE_REQUEST },
  { key: 'staffChange', name: 'スタッフ個別変更', sheetName: SHEETS.STAFF_CHANGE_REQUEST },
  { key: 'event', name: 'イベントリクエスト', sheetName: SHEETS.EVENT_REQUEST }
];

// 実行ボタン→関数名のマッピング（ホワイトリスト）
const JOB_MAP = {
  'weeklyRequest': '週間リクエストを生成_',
  'assignResult': '割当結果を作成_',
  'updateWeekView': '週ビューを更新_',
  'routeSummary': 'ルートサマリを作成_',
  'updateGeo': '位置情報を更新_'
};

// ============================================================
// Webアプリ エントリポイント
// ============================================================

/**
 * Webアプリのエントリポイント
 * @param {Object} e - イベントパラメータ
 */
function doGet(e) {
  const page = (e && e.parameter && e.parameter.page) || 'output';

  try {
    // 設定チェック
    if (!SPREADSHEET_ID) {
      return createErrorPage('設定エラー', 'SS_ID が設定されていません。スクリプトプロパティを確認してください。');
    }

    if (page === 'input') {
      // 入力ページは管理者のみ
      const email = Session.getActiveUser().getEmail();
      if (!email || !isAdmin_(email)) {
        return HtmlService.createHtmlOutputFromFile('UnifiedNoAccess')
          .setTitle('アクセス権限エラー')
          .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
      }
      return HtmlService.createHtmlOutputFromFile('UnifiedInput')
        .setTitle('訪問看護 自動スケジューリング - 入力管理')
        .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
    }

    // 出力ページ（デフォルト）
    return HtmlService.createHtmlOutputFromFile('UnifiedOutput')
      .setTitle('訪問看護 自動スケジューリング')
      .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);

  } catch (err) {
    console.error('doGet error:', err);
    return createErrorPage('エラー', err.message);
  }
}

/**
 * エラーページを作成
 */
function createErrorPage(title, message) {
  return HtmlService.createHtmlOutput(
    '<html><body style="font-family:sans-serif;padding:40px;text-align:center;">' +
    '<h2 style="color:#E88B8B;">' + title + '</h2>' +
    '<p>' + message + '</p></body></html>'
  ).setTitle(title);
}

// ============================================================
// ユーティリティAPI（クライアントから呼び出し可能）
// ============================================================

/**
 * ベースURLを取得
 */
function getBaseUrl() {
  return ScriptApp.getService().getUrl();
}

/**
 * 現在のユーザーメールを取得
 */
function getCurrentUserEmail() {
  return Session.getActiveUser().getEmail() || '';
}

/**
 * 現在のユーザーが管理者かどうか
 */
function checkIsAdmin() {
  const email = Session.getActiveUser().getEmail();
  return {
    isAdmin: email ? isAdmin_(email) : false,
    email: email || '(取得不可)'
  };
}

/**
 * 出力タブ一覧を取得
 */
function listOutputTabs() {
  return OUTPUT_TABS;
}

/**
 * 入力タブ一覧を取得
 */
function listInputTabs() {
  return INPUT_TABS;
}

// ============================================================
// 管理者判定
// ============================================================

/**
 * 管理者かどうかを判定
 * @param {string} email - ユーザーのメールアドレス
 * @returns {boolean}
 */
function isAdmin_(email) {
  if (!email) return false;

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(SHEETS.ADMIN);
    if (!sheet) return false;

    const data = sheet.getDataRange().getValues();
    if (data.length < 2) return false;

    const headers = data[0].map(h => String(h).trim().toLowerCase());
    const emailIdx = headers.indexOf('email');
    const enabledIdx = headers.indexOf('enabled');
    const roleIdx = headers.indexOf('role');

    if (emailIdx < 0) return false;

    const emailLower = email.toLowerCase();

    for (let i = 1; i < data.length; i++) {
      const row = data[i];
      const rowEmail = String(row[emailIdx] || '').trim().toLowerCase();

      if (rowEmail === emailLower) {
        // enabled チェック
        if (enabledIdx >= 0) {
          const enabled = row[enabledIdx];
          if (enabled === false || String(enabled).toUpperCase() === 'FALSE') {
            return false;
          }
        }
        // role チェック（列がある場合のみ）
        if (roleIdx >= 0) {
          const role = String(row[roleIdx] || '').trim().toLowerCase();
          if (role !== 'admin') {
            return false;
          }
        }
        return true;
      }
    }
    return false;
  } catch (e) {
    console.error('isAdmin_ error:', e);
    return false;
  }
}

// ============================================================
// データ取得API（表示用）
// ============================================================

/**
 * 週ビューデータを取得
 */
function getWeekViewData() {
  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(SHEETS.WEEK_VIEW);
    if (!sheet) {
      throw new Error('シート「' + SHEETS.WEEK_VIEW + '」が見つかりません');
    }

    const lastRow = findLastDataRow_(sheet);
    const lastCol = 8; // A〜H列（職員名+7日分）

    if (lastRow < 1) {
      return { headerRow: [], bodyRows: [], rowCount: 0 };
    }

    const values = sheet.getRange(1, 1, lastRow, lastCol).getDisplayValues();
    const headerRow = values[0] || [];
    const bodyRows = values.slice(1).filter(row => row[0] && String(row[0]).trim() !== '');

    return {
      headerRow: headerRow,
      bodyRows: bodyRows,
      rowCount: bodyRows.length
    };
  } catch (e) {
    console.error('getWeekViewData error:', e);
    throw e;
  }
}

/**
 * 汎用シートテーブルデータを取得
 * @param {string} sheetName - シート名
 * @param {number} limitRows - 行数上限（null/undefined なら全件）
 */
function getSheetTableData(sheetName, limitRows) {
  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      return { header: [], rows: [], rowCount: 0, error: 'シートが見つかりません: ' + sheetName };
    }

    const data = sheet.getDataRange().getDisplayValues();
    if (data.length === 0) {
      return { header: [], rows: [], rowCount: 0 };
    }

    const header = data[0];
    let rows = data.slice(1).filter(row => row.some(cell => cell !== ''));

    // 行数制限
    const limit = limitRows || 500;
    if (rows.length > limit) {
      rows = rows.slice(rows.length - limit);
    }

    return {
      header: header,
      rows: rows,
      rowCount: rows.length,
      sheetName: sheetName
    };
  } catch (e) {
    console.error('getSheetTableData error:', e);
    return { header: [], rows: [], rowCount: 0, error: e.message };
  }
}

/**
 * 最終データ行を取得（A列基準）
 */
function findLastDataRow_(sheet) {
  const max = sheet.getMaxRows();
  if (max <= 1) return 1;
  const colA = sheet.getRange(1, 1, max, 1).getValues();
  let last = 1;
  for (let i = 0; i < colA.length; i++) {
    if (String(colA[i][0] || '').trim() !== '') {
      last = i + 1;
    }
  }
  return last;
}

// ============================================================
// 入力画面用API（CRUD操作）
// ============================================================

/**
 * 管理者チェック（APIで使用）
 * @returns {boolean}
 */
function requireAdmin_() {
  const email = Session.getActiveUser().getEmail();
  if (!email || !isAdmin_(email)) {
    throw new Error('権限がありません。管理者としてログインしてください。');
  }
  return true;
}

/**
 * スタッフマスタに「割当量」列を追加し、プルダウンを設定
 * スプレッドシートのメニュー「拡張機能」→「スクリプトエディタ」から実行可能
 */
function setupAllocPrefColumn() {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName(SHEETS.STAFF_MASTER);
  if (!sheet) {
    throw new Error('スタッフマスタシートが見つかりません');
  }

  var numCols = sheet.getLastColumn();
  var header = sheet.getRange(1, 1, 1, numCols).getValues()[0];
  var allocIdx = header.indexOf('割当量');

  // 列が存在しない場合は追加
  if (allocIdx < 0) {
    allocIdx = numCols;
    sheet.getRange(1, allocIdx + 1).setValue('割当量');
    numCols++;
  }

  // データバリデーション（プルダウン）を設定
  var lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    var range = sheet.getRange(2, allocIdx + 1, lastRow - 1, 1);
    var rule = SpreadsheetApp.newDataValidation()
      .requireValueInList(['均等', '多め', '少なめ'], true)
      .setAllowInvalid(false)
      .build();
    range.setDataValidation(rule);

    // 空セルにデフォルト値「均等」を設定
    var values = range.getValues();
    var newValues = values.map(function(row) {
      return [row[0] === '' ? '均等' : row[0]];
    });
    range.setValues(newValues);
  }

  return '「割当量」列のセットアップが完了しました（列: ' + (allocIdx + 1) + '）';
}

/**
 * 入力対象シート一覧を取得
 */
function input_listTables() {
  return INPUT_TABS;
}

/**
 * 入力用テーブルデータを取得（管理者チェック付き）
 * @param {string} sheetName - シート名
 */
function input_getTable(sheetName) {
  requireAdmin_();
  return getSheetTableData(sheetName, 1000);
}

/**
 * 行を末尾に追加
 * @param {string} sheetName - シート名
 * @param {Array<Array>} rows - 追加する行データ [[col1, col2, ...], ...]
 */
function input_appendRows(sheetName, rows) {
  requireAdmin_();

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error('別の処理が実行中です。少し待ってから再実行してください。');
  }

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      throw new Error('シートが見つかりません: ' + sheetName);
    }

    const headerRow = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    const numCols = headerRow.length;

    // 行データの列数を調整
    const normalizedRows = rows.map(row => {
      const newRow = new Array(numCols).fill('');
      for (let i = 0; i < Math.min(row.length, numCols); i++) {
        newRow[i] = row[i];
      }
      return newRow;
    });

    if (normalizedRows.length > 0) {
      const lastRow = sheet.getLastRow();
      sheet.getRange(lastRow + 1, 1, normalizedRows.length, numCols).setValues(normalizedRows);
    }

    return { success: true, message: normalizedRows.length + ' 行を追加しました', addedCount: normalizedRows.length };
  } finally {
    lock.releaseLock();
  }
}

/**
 * 行を削除（複数対応、行番号は1-based）
 * @param {string} sheetName - シート名
 * @param {Array<number>} rowNumbers - 削除する行番号（シートの行番号、1-based）
 */
function input_deleteRows(sheetName, rowNumbers) {
  requireAdmin_();

  if (!rowNumbers || rowNumbers.length === 0) {
    throw new Error('削除する行を指定してください');
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error('別の処理が実行中です。少し待ってから再実行してください。');
  }

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      throw new Error('シートが見つかりません: ' + sheetName);
    }

    // 降順にソート（行ずれ防止）
    const sortedRows = rowNumbers.slice().sort((a, b) => b - a);

    // ヘッダー行（1行目）は削除不可
    const validRows = sortedRows.filter(r => r > 1);

    let deletedCount = 0;
    validRows.forEach(rowNum => {
      if (rowNum <= sheet.getLastRow()) {
        sheet.deleteRow(rowNum);
        deletedCount++;
      }
    });

    return { success: true, message: deletedCount + ' 行を削除しました', deletedCount: deletedCount };
  } finally {
    lock.releaseLock();
  }
}

/**
 * セルを更新
 * @param {string} sheetName - シート名
 * @param {Array<Object>} updates - 更新データ [{row: number, col: number, value: any}, ...]
 *                                   row/col は 1-based（シートの実座標）
 */
function input_updateCells(sheetName, updates) {
  requireAdmin_();

  if (!updates || updates.length === 0) {
    return { success: true, message: '更新するデータがありません', updatedCount: 0 };
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error('別の処理が実行中です。少し待ってから再実行してください。');
  }

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      throw new Error('シートが見つかりません: ' + sheetName);
    }

    // ヘッダー行（1行目）は更新不可
    const validUpdates = updates.filter(u => u.row > 1);

    validUpdates.forEach(u => {
      sheet.getRange(u.row, u.col).setValue(u.value);
    });

    return { success: true, message: validUpdates.length + ' セルを更新しました', updatedCount: validUpdates.length };
  } finally {
    lock.releaseLock();
  }
}

/**
 * 行をコピー（選択行を複製して末尾に追加）
 * @param {string} sheetName - シート名
 * @param {Array<number>} rowNumbers - コピー元の行番号（シートの行番号、1-based）
 */
function input_copyRows(sheetName, rowNumbers) {
  requireAdmin_();

  if (!rowNumbers || rowNumbers.length === 0) {
    throw new Error('コピーする行を指定してください');
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error('別の処理が実行中です。少し待ってから再実行してください。');
  }

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      throw new Error('シートが見つかりません: ' + sheetName);
    }

    const lastCol = sheet.getLastColumn();
    const rowsToCopy = [];

    // 昇順にソート
    const sortedRows = rowNumbers.slice().sort((a, b) => a - b);

    sortedRows.forEach(rowNum => {
      if (rowNum > 1 && rowNum <= sheet.getLastRow()) {
        const rowData = sheet.getRange(rowNum, 1, 1, lastCol).getValues()[0];
        rowsToCopy.push(rowData);
      }
    });

    if (rowsToCopy.length > 0) {
      const lastRow = sheet.getLastRow();
      sheet.getRange(lastRow + 1, 1, rowsToCopy.length, lastCol).setValues(rowsToCopy);
    }

    return { success: true, message: rowsToCopy.length + ' 行をコピーしました', copiedCount: rowsToCopy.length };
  } finally {
    lock.releaseLock();
  }
}

/**
 * 指定行の下に空行を挿入（ID自動採番付き）
 * @param {string} sheetName - シート名
 * @param {number} baseRowIndex - 基準行のシート行番号（1-based）。この行の下に挿入
 * @returns {Object} { success, newRowIndex, newRowData }
 */
function input_insertRowBelow(sheetName, baseRowIndex) {
  requireAdmin_();

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error('別の処理が実行中です。少し待ってから再実行してください。');
  }

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      throw new Error('シートが見つかりません: ' + sheetName);
    }

    const numCols = sheet.getLastColumn();
    const lastRow = sheet.getLastRow();
    const header = sheet.getRange(1, 1, 1, numCols).getValues()[0];

    // 挿入位置を決定
    let insertAt;
    if (!baseRowIndex || baseRowIndex < 2) {
      // 行選択なし or ヘッダー行 → 末尾に追加
      insertAt = lastRow + 1;
    } else if (baseRowIndex >= lastRow) {
      // 最終行選択 → その下に追加
      insertAt = lastRow + 1;
    } else {
      // 中間行選択 → その下に挿入
      insertAt = baseRowIndex + 1;
      sheet.insertRowAfter(baseRowIndex);
    }

    // 空行データを作成
    const emptyRow = new Array(numCols).fill('');

    // シートに応じてIDを自動採番
    if (sheetName === SHEETS.PATIENT_MASTER) {
      const idxPid = header.indexOf('patient_id');
      if (idxPid >= 0) {
        emptyRow[idxPid] = generateNextId_(sheet, 'patient_id', 'P', 3);
      }
    } else if (sheetName === SHEETS.STAFF_MASTER) {
      const idxSid = header.indexOf('staff_id');
      if (idxSid >= 0) {
        emptyRow[idxSid] = generateNextId_(sheet, 'staff_id', 'S', 3);
      }
    } else if (sheetName === SHEETS.CHANGE_REQUEST) {
      const idxCid = header.indexOf('change_id');
      if (idxCid >= 0) {
        emptyRow[idxCid] = generateNextId_(sheet, 'change_id', 'C', 3);
      }
    } else if (sheetName === SHEETS.STAFF_CHANGE_REQUEST) {
      const idxScid = header.indexOf('staff_change_id');
      if (idxScid >= 0) {
        emptyRow[idxScid] = generateNextId_(sheet, 'staff_change_id', 'SC', 3);
      }
    } else if (sheetName === SHEETS.EVENT_REQUEST) {
      const idxEvid = header.indexOf('event_id');
      if (idxEvid >= 0) {
        emptyRow[idxEvid] = generateNextId_(sheet, 'event_id', 'EV', 3);
      }
    }

    sheet.getRange(insertAt, 1, 1, numCols).setValues([emptyRow]);

    return {
      success: true,
      message: '行を挿入しました',
      newRowIndex: insertAt,
      newRowData: emptyRow
    };
  } finally {
    lock.releaseLock();
  }
}

/**
 * 位置情報を更新（選択行のみ）
 * @param {string} sheetName - シート名（患者マスタ or スタッフマスタ）
 * @param {Array<number>} rowIndexes - 更新対象の行番号（1-based）
 * @returns {Object} { success, updatedCount, errors }
 */
function input_updateGeo(sheetName, rowIndexes) {
  requireAdmin_();

  if (!rowIndexes || rowIndexes.length === 0) {
    throw new Error('更新する行を選択してください');
  }

  // 件数制限（Geocoder API制限対策）
  const MAX_ROWS = 20;
  if (rowIndexes.length > MAX_ROWS) {
    throw new Error('一度に更新できるのは' + MAX_ROWS + '件までです。' + rowIndexes.length + '件選択されています。');
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    throw new Error('別の処理が実行中です。少し待ってから再実行してください。');
  }

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      throw new Error('シートが見つかりません: ' + sheetName);
    }

    const data = sheet.getDataRange().getValues();
    const header = data[0];

    // 住所列を特定
    let addrHeader = '住所';
    if (sheetName === SHEETS.STAFF_MASTER) {
      addrHeader = '拠点住所';
    }

    const idxAddr = header.indexOf(addrHeader);
    const idxLat = header.indexOf('緯度');
    const idxLng = header.indexOf('経度');

    if (idxAddr < 0 || idxLat < 0 || idxLng < 0) {
      throw new Error('住所/緯度/経度列が見つかりません');
    }

    const geocoder = Maps.newGeocoder();
    let updatedCount = 0;
    const errors = [];

    rowIndexes.forEach(rowIndex => {
      if (rowIndex < 2 || rowIndex > data.length) return;

      const rowData = data[rowIndex - 1]; // 0-based
      const addr = rowData[idxAddr];

      if (!addr) {
        errors.push('行' + rowIndex + ': 住所が空です');
        return;
      }

      try {
        const res = geocoder.geocode(addr);
        if (res.status === 'OK' && res.results && res.results.length > 0) {
          const loc = res.results[0].geometry.location;
          sheet.getRange(rowIndex, idxLat + 1).setValue(loc.lat);
          sheet.getRange(rowIndex, idxLng + 1).setValue(loc.lng);
          updatedCount++;
        } else {
          errors.push('行' + rowIndex + ': 住所「' + addr + '」の位置情報が取得できませんでした');
        }
        Utilities.sleep(200); // API制限対策
      } catch (e) {
        errors.push('行' + rowIndex + ': ' + e.message);
      }
    });

    return {
      success: true,
      message: updatedCount + '件の位置情報を更新しました',
      updatedCount: updatedCount,
      errors: errors
    };
  } finally {
    lock.releaseLock();
  }
}

/**
 * ID→名前の辞書を取得（自動補完用）
 * @returns {Object} { patients: {id: name}, staff: {id: name} }
 */
function input_getDictionaries() {
  requireAdmin_();

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const result = { patients: {}, staff: {} };

    // 患者マスタ
    const patientSheet = ss.getSheetByName(SHEETS.PATIENT_MASTER);
    if (patientSheet) {
      const pData = patientSheet.getDataRange().getValues();
      if (pData.length > 1) {
        const pHeader = pData[0];
        const idxId = pHeader.indexOf('patient_id');
        const idxName = pHeader.indexOf('患者名');
        if (idxId >= 0 && idxName >= 0) {
          for (let i = 1; i < pData.length; i++) {
            const id = pData[i][idxId];
            const name = pData[i][idxName];
            if (id) result.patients[id] = name || '';
          }
        }
      }
    }

    // スタッフマスタ
    const staffSheet = ss.getSheetByName(SHEETS.STAFF_MASTER);
    if (staffSheet) {
      const sData = staffSheet.getDataRange().getValues();
      if (sData.length > 1) {
        const sHeader = sData[0];
        const idxId = sHeader.indexOf('staff_id');
        const idxName = sHeader.indexOf('スタッフ名');
        if (idxId >= 0 && idxName >= 0) {
          for (let i = 1; i < sData.length; i++) {
            const id = sData[i][idxId];
            const name = sData[i][idxName];
            if (id) result.staff[id] = name || '';
          }
        }
      }
    }

    return { success: true, data: result };
  } catch (e) {
    console.error('input_getDictionaries error:', e);
    return { success: false, error: e.message, data: { patients: {}, staff: {} } };
  }
}

/**
 * エリア候補一覧を取得
 */
function input_getAreaOptions() {
  // 固定候補（実運用ではマスタ化も可）
  return ['A1', 'A2', 'A3', 'B1', 'B2', 'B3'];
}

/**
 * スタッフ選択肢一覧を取得（ID + 名前）
 * @returns {Array<{id: string, name: string, label: string}>}
 */
function input_getStaffOptions() {
  requireAdmin_();

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(SHEETS.STAFF_MASTER);
    if (!sheet) return [];

    const data = sheet.getDataRange().getValues();
    if (data.length <= 1) return [];

    const header = data[0];
    const idxId = header.indexOf('staff_id');
    const idxName = header.indexOf('スタッフ名');
    if (idxId < 0 || idxName < 0) return [];

    const options = [];
    for (let i = 1; i < data.length; i++) {
      const id = String(data[i][idxId] || '').trim();
      const name = String(data[i][idxName] || '').trim();
      if (id) {
        options.push({
          id: id,
          name: name,
          label: id + ' ' + name
        });
      }
    }
    return options;
  } catch (e) {
    console.error('input_getStaffOptions error:', e);
    return [];
  }
}

/**
 * 次のIDを生成（P001, S001, C001形式）
 * @param {Sheet} sheet - 対象シート
 * @param {string} idHeaderName - IDのヘッダー名（patient_id, staff_id, change_id）
 * @param {string} prefix - プレフィックス（P, S, C）
 * @param {number} padLen - ゼロ埋め桁数（デフォルト3）
 * @returns {string} 新しいID
 */
function generateNextId_(sheet, idHeaderName, prefix, padLen) {
  padLen = padLen || 3;

  if (!sheet) return prefix + '001';

  const data = sheet.getDataRange().getValues();
  if (data.length <= 1) return prefix + '001';

  const header = data[0];
  const idxId = header.indexOf(idHeaderName);
  if (idxId < 0) return prefix + '001';

  let maxNum = 0;
  const regex = new RegExp('^' + prefix + '(\\d+)$', 'i');

  for (let i = 1; i < data.length; i++) {
    const id = String(data[i][idxId] || '').trim();
    const match = id.match(regex);
    if (match) {
      const num = parseInt(match[1], 10);
      if (num > maxNum) maxNum = num;
    }
  }

  const nextNum = maxNum + 1;
  return prefix + String(nextNum).padStart(padLen, '0');
}

/**
 * ウィザードから行を作成
 * @param {string} formType - フォームタイプ（患者マスタ, スタッフマスタ, 個別変更リクエスト）
 * @param {Object} answers - 回答オブジェクト { key: value, ... }
 * @param {number} insertAfterRow - 挿入位置（1-based、省略時は末尾）
 * @returns {Object} { success, message, newRowIndex, newRowData }
 */
function input_createRowFromWizard(formType, answers, insertAfterRow) {
  var lock = null;
  try {
    // 権限チェック
    requireAdmin_();

    lock = LockService.getScriptLock();
    if (!lock.tryLock(10000)) {
      return { success: false, error: '別の処理が実行中です。少し待ってから再実行してください。' };
    }

    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);

    // フォームタイプからシート名を決定
    var sheetName;
    if (formType === '患者マスタ') {
      sheetName = SHEETS.PATIENT_MASTER;
    } else if (formType === 'スタッフマスタ') {
      sheetName = SHEETS.STAFF_MASTER;
    } else if (formType === '個別変更リクエスト') {
      sheetName = SHEETS.CHANGE_REQUEST;
    } else if (formType === 'スタッフ個別変更') {
      sheetName = SHEETS.STAFF_CHANGE_REQUEST;
    } else {
      return { success: false, error: '不明なフォームタイプ: ' + formType };
    }

    var sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      return { success: false, error: 'シートが見つかりません: ' + sheetName };
    }

    var numCols = sheet.getLastColumn();
    var lastRow = sheet.getLastRow();
    var header = sheet.getRange(1, 1, 1, numCols).getValues()[0];

    // ★必要な列が存在しない場合は自動追加（末尾に追加）
    var requiredColumns = {
      'スタッフマスタ': ['割当量'],
      '患者マスタ': ['訪問頻度', '訪問週', '保険区分'],
      '個別変更リクエスト': ['時間タイプ', '希望最早', '希望最遅']
    };
    var columnsToAdd = requiredColumns[formType] || [];
    columnsToAdd.forEach(function(colName) {
      if (header.indexOf(colName) < 0) {
        var newColIdx = numCols + 1;
        sheet.getRange(1, newColIdx).setValue(colName);
        header.push(colName);
        numCols++;
      }
    });

    // 挿入位置を決定
    var insertAt;
    if (!insertAfterRow || insertAfterRow < 2) {
      insertAt = lastRow + 1;
    } else if (insertAfterRow >= lastRow) {
      insertAt = lastRow + 1;
    } else {
      insertAt = insertAfterRow + 1;
      sheet.insertRowAfter(insertAfterRow);
    }

    // 空行データを作成
    var rowData = new Array(numCols).fill('');

    // IDを自動採番（autoIdタイプの場合）
    if (formType === '患者マスタ') {
      var idxPid = header.indexOf('patient_id');
      if (idxPid >= 0) {
        rowData[idxPid] = generateNextId_(sheet, 'patient_id', 'P', 3);
      }
    } else if (formType === 'スタッフマスタ') {
      var idxSid = header.indexOf('staff_id');
      if (idxSid >= 0) {
        rowData[idxSid] = generateNextId_(sheet, 'staff_id', 'S', 3);
      }
    } else if (formType === '個別変更リクエスト') {
      var idxCid = header.indexOf('change_id');
      if (idxCid >= 0) {
        rowData[idxCid] = generateNextId_(sheet, 'change_id', 'C', 3);
      }
    } else if (formType === 'スタッフ個別変更') {
      var idxScid = header.indexOf('staff_change_id');
      if (idxScid >= 0) {
        rowData[idxScid] = generateNextId_(sheet, 'staff_change_id', 'SC', 3);
      }
    } else if (formType === 'イベントリクエスト') {
      var idxEvid = header.indexOf('event_id');
      if (idxEvid >= 0) {
        rowData[idxEvid] = generateNextId_(sheet, 'event_id', 'EV', 3);
      }
    }

    // ヘッダー名とキーのマッピング定義
    var headerMapping = {
      // 患者マスタ用
      'name': '患者名',
      'sex': '性別',
      'address': '住所',
      'lat': '緯度',
      'lng': '経度',
      'area': 'エリア',
      'weeklyCount': '週訪問回数',
      'visitFrequency': '訪問頻度',
      'visitWeeks': '訪問週',
      'preferDays': '希望曜日（複数可）',
      'ngDays': '曜日NG',
      'timeType': '時間タイプ',
      'timeStart': '希望時間帯（開始）',
      'timeEnd': '希望時間帯（終了）',
      'serviceMin': 'サービス時間',
      'sexLimit': '性別制限',
      'needStaff': '必要スタッフ数',
      'fixedStaff': '指定スタッフID',
      'staffType': '指定タイプ',
      'ngStaff': 'NGスタッフID',
      'contPref': '継続希望',
      'insuranceType': '保険区分',
      'note': '備考',
      // スタッフマスタ用
      'staffName': 'スタッフ名',
      'baseAddress': '拠点住所',
      'shiftStart': 'シフト開始',
      'shiftEnd': 'シフト終了',
      'workDays': '勤務曜日',
      'areas': '得意エリア',
      'maxPerDay': '最大訪問件数/日',
      'allocPref': '割当量',
      'skill': 'スキル',
      // 個別変更リクエスト用
      'date': '日付',
      'operation': '操作',
      'start': '開始時刻(固定)',
      'end': '終了時刻(固定)',
      // スタッフ個別変更リクエスト用
      'restrictionType': '制限タイプ',
      'startTime': '開始時刻',
      'endTime': '終了時刻',
      'reason': '理由',
      // イベントリクエスト用
      'eventType': 'イベント種別',
      'title': 'タイトル',
      'timeMode': '時間指定方法',
      'durationMin': '所要時間(分)',
      'fixedSlot': '固定枠',
      'patientLinked': '患者紐づき',
      'patientAffect': '患者影響',
      'returnBefore': '事前事務所戻り',
      'returnAfter': '事後事務所戻り',
      'notes': '備考'
    };

    // 曜日の日本語→英語変換マップ
    var youbiJpToEn = {
      '日': 'Sun', '月': 'Mon', '火': 'Tue', '水': 'Wed',
      '木': 'Thu', '金': 'Fri', '土': 'Sat'
    };

    // 自動生成されたIDフィールドのリスト（上書き禁止）
    var autoIdFields = ['patient_id', 'staff_id', 'change_id', 'staff_change_id', 'event_id'];

    // answersをrowDataにマッピング
    for (var key in answers) {
      if (!answers.hasOwnProperty(key)) continue;
      var value = answers[key];

      // 空値はスキップ
      if (value === undefined || value === null || value === '') continue;

      // ヘッダー名を取得
      var headerName = headerMapping[key] || key;
      var idx = header.indexOf(headerName);
      if (idx < 0) continue;

      // 自動生成IDフィールドで既に値がある場合はスキップ（上書き防止）
      if (autoIdFields.indexOf(headerName) >= 0 && rowData[idx]) continue;

      // 配列の場合（multiSelect）はCSVに変換
      if (Array.isArray(value)) {
        // 曜日フィールドの場合は日本語→英語変換
        if (key === 'preferDays' || key === 'workDays') {
          value = value.map(function(d) {
            return youbiJpToEn[d] || d;
          });
        }
        rowData[idx] = value.join(',');
      } else {
        // 曜日NGフィールドの場合は日本語→英語変換
        if (key === 'ngDays' && value) {
          value = youbiJpToEn[value] || value;
        }
        rowData[idx] = value;
      }
    }

    // 個別変更リクエストの場合、患者IDから患者名を自動取得
    if (formType === '個別変更リクエスト') {
      var idxPatientId = header.indexOf('patient_id');
      var idxPatientName = header.indexOf('患者名');
      if (idxPatientId >= 0 && idxPatientName >= 0 && rowData[idxPatientId] && !rowData[idxPatientName]) {
        var patientSheet = ss.getSheetByName(SHEETS.PATIENT_MASTER);
        if (patientSheet) {
          var patientData = patientSheet.getDataRange().getValues();
          if (patientData.length > 1) {
            var pHeader = patientData[0];
            var pIdIdx = pHeader.indexOf('patient_id');
            var pNameIdx = pHeader.indexOf('患者名');
            if (pIdIdx >= 0 && pNameIdx >= 0) {
              var targetPid = rowData[idxPatientId];
              for (var p = 1; p < patientData.length; p++) {
                if (String(patientData[p][pIdIdx]).trim() === String(targetPid).trim()) {
                  rowData[idxPatientName] = patientData[p][pNameIdx] || '';
                  break;
                }
              }
            }
          }
        }
      }
    }

    // スタッフ個別変更の場合、スタッフIDからスタッフ名を自動取得 + 曜日を自動計算
    if (formType === 'スタッフ個別変更') {
      // スタッフ名の自動取得
      var idxStaffId = header.indexOf('staff_id');
      var idxStaffName = header.indexOf('スタッフ名');
      if (idxStaffId >= 0 && idxStaffName >= 0 && rowData[idxStaffId] && !rowData[idxStaffName]) {
        var staffSheet = ss.getSheetByName(SHEETS.STAFF_MASTER);
        if (staffSheet) {
          var staffData = staffSheet.getDataRange().getValues();
          if (staffData.length > 1) {
            var sHeader = staffData[0];
            var sIdIdx = sHeader.indexOf('staff_id');
            var sNameIdx = sHeader.indexOf('スタッフ名');
            if (sIdIdx >= 0 && sNameIdx >= 0) {
              var targetSid = rowData[idxStaffId];
              for (var s = 1; s < staffData.length; s++) {
                if (String(staffData[s][sIdIdx]).trim() === String(targetSid).trim()) {
                  rowData[idxStaffName] = staffData[s][sNameIdx] || '';
                  break;
                }
              }
            }
          }
        }
      }

      // 曜日の自動計算（日付から）
      var idxDate = header.indexOf('日付');
      var idxYoubi = header.indexOf('曜日');
      if (idxDate >= 0 && idxYoubi >= 0 && rowData[idxDate] && !rowData[idxYoubi]) {
        var dateVal = rowData[idxDate];
        var dateObj;
        if (dateVal instanceof Date) {
          dateObj = dateVal;
        } else {
          dateObj = new Date(dateVal);
        }
        if (!isNaN(dateObj.getTime())) {
          var youbiNames = ['日', '月', '火', '水', '木', '金', '土'];
          rowData[idxYoubi] = youbiNames[dateObj.getDay()];
        }
      }
    }

    // 行を挿入
    sheet.getRange(insertAt, 1, 1, numCols).setValues([rowData]);

    // 生成されたIDを取得
    var generatedId = '';
    if (formType === '患者マスタ') {
      generatedId = rowData[header.indexOf('patient_id')] || '';
    } else if (formType === 'スタッフマスタ') {
      generatedId = rowData[header.indexOf('staff_id')] || '';
    } else if (formType === '個別変更リクエスト') {
      generatedId = rowData[header.indexOf('change_id')] || '';
    } else if (formType === 'スタッフ個別変更') {
      generatedId = rowData[header.indexOf('staff_change_id')] || '';
    }

    return {
      success: true,
      message: formType + 'に新しいレコードを追加しました（ID: ' + generatedId + '）',
      newRowIndex: insertAt,
      newRowData: rowData,
      generatedId: generatedId
    };
  } catch (e) {
    console.error('input_createRowFromWizard error:', e);
    return { success: false, error: e.message || String(e) };
  } finally {
    if (lock) {
      try { lock.releaseLock(); } catch (ignore) {}
    }
  }
}

// ============================================================
// 同行割付ウィザード用API
// ============================================================

/**
 * 同行割付の既存データと休みマップを取得
 * @param {string} traineeId - 新人スタッフID
 * @param {string} from - 開始日 (yyyy/MM/dd)
 * @param {string} to - 終了日 (yyyy/MM/dd)
 */
function input_getMentorPairsForWeek(traineeId, from, to) {
  requireAdmin_();
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var tz = ss.getSpreadsheetTimeZone();

  var result = { pairs: [], dayOffMap: {} };

  // 日付パース
  function parseDate_(v) {
    if (!v) return null;
    if (v instanceof Date) return v;
    var m = String(v).match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})$/);
    if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return null;
  }

  var fromDate = parseDate_(from);
  var toDate = parseDate_(to);
  if (!fromDate || !toDate) return result;

  // 1) スタッフ同行割付シートから既存データを取得
  var pairSheet = ss.getSheetByName('スタッフ同行割付');
  if (pairSheet && pairSheet.getLastRow() > 1) {
    var pValues = pairSheet.getDataRange().getValues();
    var pHeader = pValues[0];
    var pData = pValues.slice(1);

    var idxTrainee = pHeader.indexOf('trainee_staff_id');
    var idxMentor = pHeader.indexOf('mentor_staff_id');
    var idxStartD = pHeader.indexOf('開始日');
    var idxEndD = pHeader.indexOf('終了日');
    var idxBand = pHeader.indexOf('時間帯');
    var idxPrio = pHeader.indexOf('優先度');

    pData.forEach(function(r) {
      if (r[idxTrainee] !== traineeId) return;
      var sd = parseDate_(r[idxStartD]);
      var ed = parseDate_(r[idxEndD]) || sd;
      if (!sd) return;

      // 日別に展開して対象週に含まれるものを抽出
      for (var d = new Date(sd); d <= ed; d.setDate(d.getDate() + 1)) {
        if (d >= fromDate && d <= toDate) {
          result.pairs.push({
            date: Utilities.formatDate(d, tz, 'yyyy/MM/dd'),
            mentor: r[idxMentor] || '',
            band: r[idxBand] || '終日',
            priority: r[idxPrio] || 1
          });
        }
      }
    });
  }

  // 2) スタッフ個別変更リクエストから休みマップを作成
  var staffChangeSheet = ss.getSheetByName('スタッフ個別変更リクエスト');
  if (staffChangeSheet && staffChangeSheet.getLastRow() > 1) {
    var scValues = staffChangeSheet.getDataRange().getValues();
    var scHeader = scValues[0];
    var scData = scValues.slice(1);

    var idxSid = scHeader.indexOf('staff_id');
    var idxDate = scHeader.indexOf('日付');
    var idxType = scHeader.indexOf('制限タイプ');

    scData.forEach(function(r) {
      var staffId = r[idxSid];
      var dateVal = parseDate_(r[idxDate]);
      var rType = String(r[idxType] || '').trim();
      if (!staffId || !dateVal) return;

      // 対象週かつ終日休みの場合
      if (dateVal >= fromDate && dateVal <= toDate) {
        if (rType === '休み' || rType === '終日不可' || rType === '終日') {
          var key = staffId + '|' + Utilities.formatDate(dateVal, tz, 'yyyy/MM/dd');
          result.dayOffMap[key] = true;
        }
      }
    });
  }

  return result;
}

/**
 * 同行割付をウィザードから保存（週単位で上書き）
 * @param {Object} payload - { traineeId, from, to, rows: [{date, mentor, band, priority}] }
 */
function input_saveMentorPairsWizard(payload) {
  var lock = null;
  try {
    requireAdmin_();

    lock = LockService.getScriptLock();
    if (!lock.tryLock(10000)) {
      return { success: false, error: '別の処理が実行中です。少し待ってから再実行してください。' };
    }

    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var tz = ss.getSpreadsheetTimeZone();

    var traineeId = payload.traineeId;
    var from = payload.from;
    var to = payload.to;
    var rows = payload.rows || [];

    if (!traineeId) {
      return { success: false, error: 'traineeIdが指定されていません' };
    }

    // 日付パース
    function parseDate_(v) {
      if (!v) return null;
      if (v instanceof Date) return v;
      var m = String(v).match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})$/);
      if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
      return null;
    }

    var fromDate = parseDate_(from);
    var toDate = parseDate_(to);
    if (!fromDate || !toDate) {
      return { success: false, error: '日付の形式が不正です' };
    }

    // シートを取得（なければ作成）
    var sheet = ss.getSheetByName('スタッフ同行割付');
    if (!sheet) {
      sheet = ss.insertSheet('スタッフ同行割付');
      var headers = ['trainee_staff_id', 'mentor_staff_id', '開始日', '終了日', '時間帯', '開始時刻', '終了時刻', '曜日条件', '優先度'];
      sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
      sheet.setFrozenRows(1);
    }

    var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    var idxTrainee = header.indexOf('trainee_staff_id');
    var idxMentor = header.indexOf('mentor_staff_id');
    var idxStartD = header.indexOf('開始日');
    var idxEndD = header.indexOf('終了日');
    var idxBand = header.indexOf('時間帯');
    var idxPrio = header.indexOf('優先度');

    // 1) 既存の同一trainee & 対象週に重なる行を削除（後ろから）
    var lastRow = sheet.getLastRow();
    if (lastRow > 1) {
      var allData = sheet.getRange(2, 1, lastRow - 1, header.length).getValues();

      for (var i = allData.length - 1; i >= 0; i--) {
        var r = allData[i];
        if (r[idxTrainee] !== traineeId) continue;

        var sd = parseDate_(r[idxStartD]);
        var ed = parseDate_(r[idxEndD]) || sd;
        if (!sd) continue;

        // 期間が重なるかチェック
        if (sd <= toDate && ed >= fromDate) {
          sheet.deleteRow(i + 2); // +2 because of header and 0-based index
        }
      }
    }

    // 2) 新しい行を追加
    var savedCount = 0;
    rows.forEach(function(row) {
      if (!row.mentor || !row.date) return;

      var rowDate = parseDate_(row.date);
      if (!rowDate) return;

      var newRow = new Array(header.length).fill('');
      newRow[idxTrainee] = traineeId;
      newRow[idxMentor] = row.mentor;
      newRow[idxStartD] = rowDate;
      newRow[idxEndD] = rowDate; // 日別なので開始=終了
      if (idxBand >= 0) newRow[idxBand] = row.band || '終日';
      if (idxPrio >= 0) newRow[idxPrio] = row.priority || 1;

      sheet.appendRow(newRow);
      savedCount++;
    });

    return { success: true, savedCount: savedCount };
  } catch (e) {
    console.error('input_saveMentorPairsWizard error:', e);
    return { success: false, error: e.message || String(e) };
  } finally {
    if (lock) {
      try { lock.releaseLock(); } catch (ignore) {}
    }
  }
}

/**
 * 特別訪問週間ウィザード用データ取得（Web UI用）
 * @param {string} weekStartStr - 週開始日（yyyy/MM/dd）
 * @param {string} patientId - 患者ID
 */
function input_getSpecialWeekWizardData(weekStartStr, patientId) {
  try {
    return api_getSpecialWeekWizardData(weekStartStr, patientId);
  } catch (e) {
    console.error('input_getSpecialWeekWizardData error:', e);
    return { error: e.message || String(e) };
  }
}

/**
 * 特別訪問週間ウィザード保存（Web UI用）
 * @param {Object} payload - { weekStartStr, patientId, patientName, mode, reason, items }
 */
function input_saveSpecialWeekWizard(payload) {
  try {
    requireAdmin_();
    return api_saveSpecialWeekWizard(payload);
  } catch (e) {
    console.error('input_saveSpecialWeekWizard error:', e);
    return { success: false, error: e.message || String(e) };
  }
}

// ============================================================
// 実行API（GAS実行ボタン用）
// ============================================================

/**
 * ジョブを実行（ホワイトリスト制御）
 * @param {string} jobKey - ジョブキー
 * @param {string} [weekStartStr] - 週開始日（yyyy-MM-dd）。未指定時は今週を自動算出
 */
function runJob(jobKey, weekStartStr) {
  const startTime = new Date();

  // ホワイトリストチェック
  const funcName = JOB_MAP[jobKey];
  if (!funcName) {
    return { ok: false, message: '不明なジョブキー: ' + jobKey };
  }

  // weekStartStr のバリデーション（指定時）
  if (weekStartStr) {
    var testDate = parseDateLoose_(weekStartStr);
    if (!testDate) {
      return { ok: false, message: '対象週の日付が不正です: ' + weekStartStr };
    }
    console.log('[runJob] jobKey=' + jobKey + ' weekStartStr=' + weekStartStr);
  } else {
    console.log('[runJob] jobKey=' + jobKey + ' weekStartStr=(auto/today)');
  }

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);

    // 関数を呼び出し（weekStartStr を渡す）
    let result;
    switch (jobKey) {
      case 'weeklyRequest':
        result = 週間リクエストを生成_(ss, weekStartStr);
        break;
      case 'assignResult':
        result = 割当結果を作成_(ss);
        break;
      case 'updateWeekView':
        result = 週ビューを更新_(ss, weekStartStr);
        break;
      case 'routeSummary':
        result = ルートサマリを作成_(ss, weekStartStr);
        break;
      default:
        throw new Error('未実装のジョブ: ' + jobKey);
    }

    const message = (result && result.message) || '処理が完了しました';
    logExecution_(jobKey, true, message, startTime);
    return { ok: true, message: message };

  } catch (e) {
    console.error('runJob error:', e);
    logExecution_(jobKey, false, e.message, startTime);
    return { ok: false, message: e.message };
  }
}

/**
 * 実行ログを記録
 */
function logExecution_(action, success, message, startTime) {
  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    let sheet = ss.getSheetByName(SHEETS.LOG);
    if (!sheet) {
      sheet = ss.insertSheet(SHEETS.LOG);
      sheet.appendRow(['タイムスタンプ', 'アクション', '成功', 'メッセージ', '実行時間(秒)', 'ユーザー']);
    }

    const endTime = new Date();
    const duration = ((endTime - startTime) / 1000).toFixed(2);
    const user = Session.getActiveUser().getEmail() || 'unknown';

    sheet.appendRow([
      endTime.toISOString(),
      action,
      success ? 'OK' : 'NG',
      message,
      duration,
      user
    ]);
  } catch (e) {
    console.error('logExecution_ error:', e);
  }
}

/**
 * 実行ログを取得
 * @param {number} limit - 取得件数
 */
function getExecutionLogs(limit) {
  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(SHEETS.LOG);
    if (!sheet) {
      return { success: true, logs: [] };
    }

    const data = sheet.getDataRange().getValues();
    if (data.length <= 1) {
      return { success: true, logs: [] };
    }

    const rows = data.slice(1).reverse().slice(0, limit || 10);
    const logs = rows.map(row => ({
      timestamp: row[0],
      action: row[1],
      success: row[2] === 'OK',
      message: row[3],
      duration: row[4],
      user: row[5]
    }));

    return { success: true, logs: logs };
  } catch (e) {
    console.error('getExecutionLogs error:', e);
    return { success: false, error: e.message, logs: [] };
  }
}

// ============================================================
// 設定チェック
// ============================================================

/**
 * 設定状態をチェック
 */
function checkConfiguration() {
  const issues = [];

  if (!SPREADSHEET_ID) {
    issues.push('SS_ID が設定されていません');
  }

  try {
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);

    // 主要シートチェック
    const requiredSheets = [SHEETS.WEEK_VIEW, SHEETS.PATIENT_MASTER, SHEETS.STAFF_MASTER];
    requiredSheets.forEach(name => {
      const sheet = ss.getSheetByName(name);
      if (!sheet) {
        issues.push('「' + name + '」シートが見つかりません');
      }
    });

  } catch (e) {
    issues.push('スプレッドシートにアクセスできません: ' + e.message);
  }

  return {
    success: issues.length === 0,
    issues: issues
  };
}

// ============================================================
// 共通ユーティリティ関数
// ============================================================

function normalizeYoubi(y) {
  if (!y) return null;
  y = String(y).trim();

  var en = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  if (en.indexOf(y) >= 0) return y;

  var enFull = {
    'Sunday':'Sun','Monday':'Mon','Tuesday':'Tue','Wednesday':'Wed',
    'Thursday':'Thu','Friday':'Fri','Saturday':'Sat'
  };
  if (enFull[y]) return enFull[y];

  var jp1 = { '日':'Sun','月':'Mon','火':'Tue','水':'Wed','木':'Thu','金':'Fri','土':'Sat' };
  if (jp1[y]) return jp1[y];

  var jp2 = { '日曜':'Sun','月曜':'Mon','火曜':'Tue','水曜':'Wed','木曜':'Thu','金曜':'Fri','土曜':'Sat' };
  if (jp2[y]) return jp2[y];

  var jp3 = { '日曜日':'Sun','月曜日':'Mon','火曜日':'Tue','水曜日':'Wed','木曜日':'Thu','金曜日':'Fri','土曜日':'Sat' };
  if (jp3[y]) return jp3[y];

  return null;
}

function toHalfWidthNumber_(v, def) {
  if (v === null || v === undefined || v === '') return def;
  var s = String(v).trim().replace(/[０-９]/g, function(c) {
    return String.fromCharCode(c.charCodeAt(0) - 0xFEE0);
  });
  var n = parseInt(s, 10);
  return isNaN(n) ? def : n;
}

/**
 * 指定日がその月の「何週目」かを計算（月曜基準）
 * @param {Date} date - 対象日
 * @returns {number} 1〜5の週番号
 */
function getWeekOfMonth_(date) {
  // その月の1日を取得
  const firstDay = new Date(date.getFullYear(), date.getMonth(), 1);
  // 1日の曜日（0=日曜, 1=月曜, ... 6=土曜）
  const firstDayOfWeek = firstDay.getDay();
  // 月曜基準に変換（月曜=0, 火曜=1, ... 日曜=6）
  const adjustedFirstDay = (firstDayOfWeek + 6) % 7;
  // 対象日が月の何日目か
  const dayOfMonth = date.getDate();
  // 週番号を計算（1始まり）
  const weekNumber = Math.ceil((dayOfMonth + adjustedFirstDay) / 7);
  return weekNumber;
}

/**
 * 患者の訪問頻度設定に基づき、指定週に訪問するかどうかを判定
 * @param {string} visitFrequency - 訪問頻度（'毎週' / '選択制'）
 * @param {string} visitWeeks - 訪問週（'1,3' / '2,4' / '1' など）
 * @param {number} weekOfMonth - 対象週番号（1〜5）
 * @returns {boolean} 訪問するならtrue
 */
function shouldVisitThisWeek_(visitFrequency, visitWeeks, weekOfMonth) {
  // 毎週の場合は常に訪問
  if (!visitFrequency || visitFrequency === '毎週' || visitFrequency === '') {
    return true;
  }
  // 選択制の場合、訪問週に含まれているか確認
  if (visitFrequency === '選択制') {
    if (!visitWeeks || visitWeeks === '') {
      // 訪問週が未設定の場合は訪問しない
      return false;
    }
    const weeks = String(visitWeeks).split(',').map(w => w.trim());
    return weeks.includes(String(weekOfMonth));
  }
  // その他の場合は訪問する
  return true;
}

function parseIdList(str) {
  if (!str) return [];
  return String(str).split(/[,\u3001\/・\s]+/).map(function(s){ return s.trim(); }).filter(Boolean);
}

function parseTypeList(str) {
  if (!str) return [];
  return String(str).split(/[,\u3001\/・\s]+/).map(function(s){ return s.trim(); }).filter(Boolean);
}

// ============================================================
// 週ビューを更新（ss引数版）
// ============================================================

function 週ビューを更新_(ss, weekStartStr) {
  const tz = ss.getSpreadsheetTimeZone();
  const resultSheet  = ss.getSheetByName('割当結果');
  const viewSheet    = ss.getSheetByName('週ビュー');
  const staffSheet   = ss.getSheetByName('スタッフマスタ');
  const patientSheet = ss.getSheetByName('患者マスタ');

  if (!resultSheet || !viewSheet) {
    throw new Error('「割当結果」シートと「週ビュー」シートを作ってから実行してください。');
  }

  const staffGenderMap = {};
  if (staffSheet) {
    const sValues = staffSheet.getDataRange().getValues();
    if (sValues.length > 1) {
      const sHeader = sValues[0];
      const sData   = sValues.slice(1);
      const sIdxId   = sHeader.indexOf('staff_id');
      const sIdxName = sHeader.indexOf('スタッフ名');
      const sIdxGen  = sHeader.indexOf('性別');
      sData.forEach(r => {
        const id = r[sIdxId];
        if (!id) return;
        staffGenderMap[id] = {
          gender: sIdxGen >= 0 ? (r[sIdxGen] || '') : '',
          name  : sIdxName >= 0 ? (r[sIdxName] || '') : ''
        };
      });
    }
  }

  const patientGenderMap = {};
  if (patientSheet) {
    const pValues = patientSheet.getDataRange().getValues();
    if (pValues.length > 1) {
      const pHeader = pValues[0];
      const pData   = pValues.slice(1);
      const pIdxId  = pHeader.indexOf('patient_id');
      const pIdxGen = pHeader.indexOf('性別');
      const pIdxName = pHeader.indexOf('患者名');
      pData.forEach(r => {
        const id = r[pIdxId];
        if (!id) return;
        patientGenderMap[id] = {
          gender: pIdxGen >= 0 ? (r[pIdxGen] || '') : '',
          name: pIdxName >= 0 ? (r[pIdxName] || '') : ''
        };
      });
    }
  }

  // ※イベントは割当結果のEV行から取得するため、イベントリクエストの読み込みは不要

  // weekStartStr が指定されていればその月曜を使用、なければ今週を自動算出
  let start;
  if (weekStartStr) {
    start = parseDateLoose_(weekStartStr);
    if (!start) throw new Error('対象週の日付が不正です: ' + weekStartStr);
    start.setHours(0,0,0,0);
    console.log('[週ビュー更新] 指定週=' + weekStartStr);
  } else {
    const today = new Date();
    today.setHours(0,0,0,0);
    const day = today.getDay();
    const diffToMonday = (day + 6) % 7;
    start = new Date(today);
    start.setDate(today.getDate() - diffToMonday);
    console.log('[週ビュー更新] 自動算出=今週');
  }
  const end = new Date(start);
  end.setDate(start.getDate() + 6);

  const values = resultSheet.getDataRange().getValues();
  if (values.length <= 1) {
    throw new Error('「割当結果」にデータがありません。');
  }
  const header = values[0];
  const data   = values.slice(1);

  const idxDate    = header.indexOf('日付');
  const idxStaff   = header.indexOf('スタッフ名');
  const idxStaffId = header.indexOf('staff_id');
  const idxStart   = header.indexOf('開始時刻');
  const idxEnd     = header.indexOf('終了時刻');
  const idxPatient = header.indexOf('患者名');
  const idxPid     = header.indexOf('patient_id');
  const idxVisitId = header.indexOf('visit_id');
  const idxNote    = header.indexOf('備考');

  if ([idxDate,idxStaff,idxStaffId,idxStart,idxEnd,idxPatient,idxPid].some(i => i === -1)) {
    throw new Error('「割当結果」のヘッダー名を確認してください。');
  }

  const startStr = Utilities.formatDate(start, tz, 'yyyy/MM/dd');
  const endStr   = Utilities.formatDate(end,   tz, 'yyyy/MM/dd');

  const weekData = data.filter(row => {
    const d = row[idxDate];
    if (!(d instanceof Date)) return false;
    const ds = Utilities.formatDate(d, tz, 'yyyy/MM/dd');
    return ds >= startStr && ds <= endStr;
  });

  const staffMap = new Map();
  // 訪問からスタッフを収集
  weekData.forEach(r => {
    const sid   = r[idxStaffId];
    const sname = r[idxStaff] || '';
    if (!sid && !sname) return;
    const key = sid || sname;
    if (!staffMap.has(key)) {
      let gender = '';
      if (sid && staffGenderMap[sid]) gender = staffGenderMap[sid].gender || '';
      staffMap.set(key, { id: sid || '', name: sname, gender: gender });
    }
  });

  // ※イベントは割当結果のEV行に含まれるため、staffMapには既に入っている

  let staffList = Array.from(staffMap.values());
  staffList.sort((a,b) => {
    if (a.name === '未割当' && b.name !== '未割当') return 1;
    if (b.name === '未割当' && a.name !== '未割当') return -1;
    // スタッフIDでソート（S001, S002, S003...の順）
    return (a.id || '').localeCompare(b.id || '', 'ja');
  });

  viewSheet.clear();
  viewSheet.getRange(1,1).setValue('職員名');

  for (let i = 0; i < 7; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    const label = Utilities.formatDate(d, tz, 'MM/dd(EEE)');
    viewSheet.getRange(1, 2 + i).setValue(label);
  }

  staffList.forEach((st, idx) => {
    let label = '';
    if (st.id) label += st.id + ' ';
    label += st.name || '';
    if (st.gender) label += '（' + st.gender + '）';
    viewSheet.getRange(2 + idx, 1).setValue(label);
  });

  // 時間値をHH:mm形式に変換するヘルパー関数
  function formatTimeVal(val) {
    if (!val) return '';
    if (val instanceof Date) return Utilities.formatDate(val, tz, 'HH:mm');
    if (typeof val === 'number') {
      const base = new Date(1899, 11, 30);
      const ms   = val * 24 * 60 * 60 * 1000;
      const dd   = new Date(base.getTime() + ms);
      return Utilities.formatDate(dd, tz, 'HH:mm');
    }
    return String(val);
  }

  // 時間値を分に変換するヘルパー関数（ソート用）
  function toSortMinutes(val) {
    if (!val) return 9999;
    if (val instanceof Date) return val.getHours() * 60 + val.getMinutes();
    if (typeof val === 'number') {
      // シリアル値から分に変換
      const totalMinutes = Math.round(val * 24 * 60);
      return totalMinutes % 1440;
    }
    // "HH:mm" 形式の文字列
    const match = String(val).match(/(\d{1,2}):(\d{2})/);
    if (match) return parseInt(match[1], 10) * 60 + parseInt(match[2], 10);
    return 9999;
  }

  // "その行がイベントか" 判定（visit_id が EV_ か、備考に [EV]）
  function isEventRowFromResult_(row) {
    const vid  = (idxVisitId >= 0) ? String(row[idxVisitId] || '') : '';
    const note = (idxNote >= 0) ? String(row[idxNote] || '') : '';
    return vid.indexOf('EV_') === 0 || note.indexOf('[EV]') >= 0;
  }

  let eventCount = 0;

  staffList.forEach((st, rIndex) => {
    for (let i = 0; i < 7; i++) {
      const d = new Date(start);
      d.setDate(start.getDate() + i);
      const targetDateStr = Utilities.formatDate(d, tz, 'yyyy/MM/dd');

      // 割当結果から、そのスタッフ×その日の行だけ取る（EV行もここに入る）
      const rows = weekData.filter(row => {
        const d2 = row[idxDate];
        const ds2 = Utilities.formatDate(d2, tz, 'yyyy/MM/dd');
        const sid   = row[idxStaffId] || '';
        const sname = row[idxStaff]   || '';
        const key   = sid || sname;
        return key === (st.id || st.name) && ds2 === targetDateStr;
      });

      const displayItems = [];

      rows.forEach(v => {
        const startVal = v[idxStart];
        const endVal   = v[idxEnd];
        const pid      = v[idxPid] || '';
        const pname    = v[idxPatient] || '';
        const noteVal  = (idxNote >= 0) ? String(v[idxNote] || '') : '';
        const vid      = (idxVisitId >= 0) ? String(v[idxVisitId] || '') : '';

        const stime = formatTimeVal(startVal);
        const etime = formatTimeVal(endVal);

        // EV行として表示（割当結果の備考に [EV] が入っている）
        if (isEventRowFromResult_(v)) {
          eventCount++;
          let evText = '';
          if (stime && etime) evText += stime + '〜' + etime + ' ';
          evText += noteVal ? noteVal : '[EV]';

          displayItems.push({
            sortKey: toSortMinutes(startVal),
            text: evText,
            isEvent: true
          });
          return;
        }

        // 通常訪問として表示
        const pInfo   = pid ? patientGenderMap[pid] : null;
        const pGender = pInfo ? (pInfo.gender || '') : '';
        const isTwo   = (vid.indexOf('-') >= 0) || (noteVal.indexOf('同時訪問') >= 0);
        const mark    = isTwo ? '👥 ' : '';

        let pidPart = '';
        if (pid) {
          pidPart = pid;
          if (pGender) pidPart += '（' + pGender + '）';
          pidPart += ' ';
        }

        let text;
        if (!stime && !etime) {
          text = mark + pidPart + pname;
        } else {
          text = mark + stime + '〜' + etime + ' ' + pidPart + pname;
        }

        displayItems.push({
          sortKey: toSortMinutes(startVal),
          text: text,
          isEvent: false
        });
      });

      // 時刻でソート
      displayItems.sort((a, b) => a.sortKey - b.sortKey);

      const cellText = displayItems.map(item => item.text).join('\n');
      if (cellText) {
        const cell = viewSheet.getRange(2 + rIndex, 2 + i);
        cell.setValue(cellText);
        cell.setWrap(true);
      }
    }
  });

  return { message: '週ビューを更新しました（' + staffList.length + '名、EV行' + eventCount + '件含む）' };
}

// ============================================================
// 割当結果を作成（ss引数版）
// ============================================================

function 割当結果を作成_(ss) {
  const tz = ss.getSpreadsheetTimeZone();

  const weeklySheet = ss.getSheetByName('週間リクエスト');
  const staffSheet  = ss.getSheetByName('スタッフマスタ');
  const resultSheet = ss.getSheetByName('割当結果');
  let historySheet = ss.getSheetByName('訪問履歴');
  const patientSheet = ss.getSheetByName('患者マスタ');

  if (!patientSheet) throw new Error('「患者マスタ」シートがありません。');
  if (!weeklySheet || !staffSheet || !resultSheet) {
    throw new Error('「週間リクエスト」「スタッフマスタ」「割当結果」シートがあるか確認してください。');
  }

  if (!historySheet) historySheet = ss.insertSheet('訪問履歴');
  if (historySheet.getLastRow() === 0) {
    var histHeader = ['visit_id','日付','曜日','staff_id','スタッフ名','patient_id','患者名','エリア','開始時刻','終了時刻','サービス時間','備考'];
    historySheet.getRange(1, 1, 1, histHeader.length).setValues([histHeader]);
  }

  function calcDistanceKm(lat1, lng1, lat2, lng2) {
    if (lat1 == null || lng1 == null || lat2 == null || lng2 == null) return null;
    const R = 6371;
    const toRad = Math.PI / 180;
    const dLat = (lat2 - lat1) * toRad;
    const dLng = (lng2 - lng1) * toRad;
    const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
              Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) *
              Math.sin(dLng/2) * Math.sin(dLng/2);
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    return R * c;
  }

  function distToScore(km) {
    if (km == null) return 99;
    if (km <= 2)  return 0;
    if (km <= 5)  return 1;
    if (km <= 10) return 2;
    return 5;
  }

  function applyStaffPreference(candidates, specifiedIdsArr, specifiedType, ngIdsArr) {
    var ngSet = {};
    ngIdsArr.forEach(function(id){ ngSet[id] = true; });
    candidates = candidates.filter(function(c){ return !ngSet[c.staff.id]; });
    if (!specifiedType || specifiedIdsArr.length === 0) return candidates;
    var specSet = {};
    specifiedIdsArr.forEach(function(id){ specSet[id] = true; });
    if (specifiedType === '必須') return candidates.filter(function(c){ return specSet[c.staff.id]; });
    if (specifiedType === '優先') {
      candidates.forEach(function(c){ c._pref = specSet[c.staff.id] ? 1 : 0; });
      candidates.sort(function(a,b){ if (a._pref !== b._pref) return b._pref - a._pref; return 0; });
    }
    return candidates;
  }

  var wValues = weeklySheet.getDataRange().getValues();
  if (wValues.length <= 1) throw new Error('「週間リクエスト」にデータがありません。');
  var wHeader = wValues[0];
  var wData   = wValues.slice(1);
  var wIdx = {
    date: wHeader.indexOf('日付'), youbi: wHeader.indexOf('曜日'), pid: wHeader.indexOf('patient_id'),
    pname: wHeader.indexOf('患者名'), area: wHeader.indexOf('エリア'), start: wHeader.indexOf('開始時刻'),
    end: wHeader.indexOf('終了時刻'), svcMin: wHeader.indexOf('サービス時間'), needStaff: wHeader.indexOf('必要スタッフ数'),
    specifiedIds: wHeader.indexOf('指定スタッフID'), specifiedType: wHeader.indexOf('指定タイプ'),
    ngStaffIds: wHeader.indexOf('NGスタッフID'), sexLimit: wHeader.indexOf('性別制限'),
    contPref: wHeader.indexOf('継続希望'), change: wHeader.indexOf('変更区分（通常/変更/追加/キャンセル）'),
    prevSid: wHeader.indexOf('前回担当スタッフID'), prevSname: wHeader.indexOf('前回担当スタッフ名'),
    timeType: wHeader.indexOf('時間タイプ'), earliest: wHeader.indexOf('希望最早時刻'),
    latest: wHeader.indexOf('希望最遅時刻'), note: wHeader.indexOf('備考')
  };

  var pValues = patientSheet.getDataRange().getValues();
  var pHeader = pValues[0];
  var pData   = pValues.slice(1);
  var pIdx = { id: pHeader.indexOf('patient_id'), name: pHeader.indexOf('患者名'), area: pHeader.indexOf('エリア'),
               lat: pHeader.indexOf('緯度'), lng: pHeader.indexOf('経度'), svcMin: pHeader.indexOf('サービス時間') };

  var patientMap = {};
  pData.forEach(function(row){
    var id = row[pIdx.id];
    if (!id) return;
    patientMap[id] = { name: row[pIdx.name], area: row[pIdx.area], lat: Number(row[pIdx.lat]) || null,
                       lng: Number(row[pIdx.lng]) || null, svcMin: Number(row[pIdx.svcMin]) || 0 };
  });

  var sValues = staffSheet.getDataRange().getValues();
  if (sValues.length <= 1) throw new Error('「スタッフマスタ」にスタッフが1人もいません。');
  var sHeader = sValues[0];
  var sData   = sValues.slice(1);

  // ヘッダー名の柔軟なマッチング（全角/半角スラッシュ対応）
  function findHeaderIndex(headerArr, targetName) {
    var idx = headerArr.indexOf(targetName);
    if (idx >= 0) return idx;
    // 全角スラッシュ版も試す
    var altName = targetName.replace(/\//g, '／');
    idx = headerArr.indexOf(altName);
    if (idx >= 0) return idx;
    // 半角スラッシュ版も試す（念のため）
    altName = targetName.replace(/／/g, '/');
    return headerArr.indexOf(altName);
  }

  var sIdx = { id: sHeader.indexOf('staff_id'), name: sHeader.indexOf('スタッフ名'), gender: sHeader.indexOf('性別'),
               lat: sHeader.indexOf('緯度'), lng: sHeader.indexOf('経度'), shiftS: sHeader.indexOf('シフト開始'),
               shiftE: sHeader.indexOf('シフト終了'), days: sHeader.indexOf('勤務曜日'), areas: sHeader.indexOf('得意エリア'),
               maxPer: findHeaderIndex(sHeader, '最大訪問件数/日'), alloc: sHeader.indexOf('割当量') };

  // ★デバッグログ：ヘッダーインデックスを確認
  console.log('[Header Debug] sIdx.maxPer=' + sIdx.maxPer + ' sHeader=' + JSON.stringify(sHeader));

  function parseDays(str) {
    if (!str) return [];
    var parts = String(str).split(/[,\u3001\/・\s]+/);
    var out = [];
    parts.forEach(function(p){ p = p.trim(); if (!p) return; var y = normalizeYoubi(p); if (y && out.indexOf(y) === -1) out.push(y); });
    return out;
  }

  // 文字列日付もDate化するパーサ
  function parseDate_(v) {
    if (!v) return null;
    if (v instanceof Date) return v;

    var s = String(v).trim();
    var m = s.match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})$/);
    if (m) {
      var dt = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
      return isNaN(dt.getTime()) ? null : dt;
    }
    var dt2 = new Date(s);
    return isNaN(dt2.getTime()) ? null : dt2;
  }

  // "10:00" / シリアル / Date すべて分にするパーサ
  function parseTimeToMinutes_(v) {
    if (v === null || v === undefined || v === '') return null;

    // 数値（シリアル値）の場合
    if (typeof v === 'number') {
      // 0〜1の範囲ならシリアル時刻として扱う
      if (v >= 0 && v < 1) {
        return Math.round(v * 24 * 60);
      }
      // 1以上なら分として解釈（誤入力対応）
      if (v >= 1 && v < 1440) {
        return Math.round(v);
      }
      // それ以外はシリアル値として扱う
      return Math.round(v * 24 * 60) % 1440;
    }

    // Date オブジェクトの場合
    if (v instanceof Date) {
      return v.getHours() * 60 + v.getMinutes();
    }

    // 文字列の場合
    var s = String(v).trim();

    // "14:00" や "9:30" 形式
    var m = s.match(/^(\d{1,2}):(\d{2})$/);
    if (m) return Number(m[1]) * 60 + Number(m[2]);

    // "14:00:00" 形式（秒付き）
    m = s.match(/^(\d{1,2}):(\d{2}):(\d{2})$/);
    if (m) return Number(m[1]) * 60 + Number(m[2]);

    // "午前10時30分" や "10時30分" 形式
    m = s.match(/(\d{1,2})時(\d{1,2})分/);
    if (m) return Number(m[1]) * 60 + Number(m[2]);

    // "14時" 形式（分なし）
    m = s.match(/^(\d{1,2})時$/);
    if (m) return Number(m[1]) * 60;

    // "HH:mm〜HH:mm" などにも保険で対応（先頭だけ）
    m = s.match(/(\d{1,2}):(\d{2})/);
    if (m) return Number(m[1]) * 60 + Number(m[2]);

    return null;
  }

  // 分→シリアル(スプレッドシート時刻) に変換
  function minutesToSerial_(min) {
    if (min == null) return '';
    return min / (24 * 60);
  }

  function toMinutes(v) {
    return parseTimeToMinutes_(v);
  }

  function normalizeContPref(v) {
    if (!v) return '';
    v = String(v).trim();
    if (v === '同じ人' || v === '同じ人希望') return '同じ人希望';
    // ★修正：「ローテーション」も「ローテーション優先」として認識
    if (v === 'ローテーション' || v === 'ローテーション優先') return 'ローテーション優先';
    if (v === 'どちらでも') return 'どちらでも';
    return v;
  }

  var EXTRA_BUFFER_MIN = 15;

  var staffList = [];
  sData.forEach(function(row){
    var id = row[sIdx.id];
    var name = row[sIdx.name];
    if (!id || !name) return;
    var workDays = parseDays(row[sIdx.days]);
    var shiftStartMin = toMinutes(row[sIdx.shiftS]);
    var shiftEndMin   = toMinutes(row[sIdx.shiftE]);
    var areasStr = row[sIdx.areas] || '';
    var areaList = String(areasStr).split(/[,\u3001\/・\s]+/).map(function(s){ return s.trim(); }).filter(function(s){ return s; });
    var maxPerDay = Number(row[sIdx.maxPer] || 0) || 999;
    // ★デバッグログ：maxPerDayの読み取り値を確認
    console.log('[maxPerDay Debug] staff=' + id + ' name=' + name + ' sIdx.maxPer=' + sIdx.maxPer + ' raw=' + row[sIdx.maxPer] + ' maxPerDay=' + maxPerDay);
    var allocPref = sIdx.alloc >= 0 ? (row[sIdx.alloc] || '均等') : '均等';
    if (allocPref !== '多め' && allocPref !== '少なめ') allocPref = '均等';
    staffList.push({ id: id, name: name, gender: row[sIdx.gender] || '', lat: row[sIdx.lat], lng: row[sIdx.lng],
                     shiftStartMin: shiftStartMin, shiftEndMin: shiftEndMin, workDays: workDays, areas: areaList, maxPerDay: maxPerDay, allocPref: allocPref });
  });

  if (staffList.length === 0) throw new Error('有効なスタッフ情報がありません。');

  // ============================================================
  // Task A: スタッフ個別変更リクエストの読み込みとMap化
  // ============================================================
  var staffChangeMap = {};  // key: staff_id|yyyy/MM/dd => [records...]
  var staffChangeSheet = ss.getSheetByName('スタッフ個別変更リクエスト');
  if (staffChangeSheet && staffChangeSheet.getLastRow() > 1) {
    var scValues = staffChangeSheet.getDataRange().getValues();
    var scHeader = scValues[0];
    var scIdx = {
      staffId: scHeader.indexOf('staff_id'),
      date: scHeader.indexOf('日付'),
      restrictionType: scHeader.indexOf('制限タイプ'),
      startTime: scHeader.indexOf('開始時刻'),
      endTime: scHeader.indexOf('終了時刻')
    };

    for (var sci = 1; sci < scValues.length; sci++) {
      var scRow = scValues[sci];
      var scStaffId = scRow[scIdx.staffId];
      // parseDate_ で文字列日付もパース
      var scDateObj = parseDate_(scRow[scIdx.date]);
      if (!scStaffId || !scDateObj) continue;

      var scDateStr = Utilities.formatDate(scDateObj, tz, 'yyyy/MM/dd');

      var scKey = scStaffId + '|' + scDateStr;
      if (!staffChangeMap[scKey]) staffChangeMap[scKey] = [];

      staffChangeMap[scKey].push({
        restrictionType: String(scRow[scIdx.restrictionType] || '').trim(),
        startTime: toMinutes(scRow[scIdx.startTime]),
        endTime: toMinutes(scRow[scIdx.endTime])
      });
    }
  }

  // ============================================================
  // Task A-2: イベントリクエストの読み込みとMap化
  // ============================================================
  var eventMap = {};  // key: staff_id|yyyy/MM/dd => [events...]
  var eventSheet = ss.getSheetByName('イベントリクエスト');
  if (eventSheet && eventSheet.getLastRow() > 1) {
    var evValues = eventSheet.getDataRange().getValues();
    var evHeader = evValues[0];

    // ヘッダー検索ヘルパー（空白や文字の違いに対応）
    function findEvHeaderIndex(headers, targetName) {
      var idx = headers.indexOf(targetName);
      if (idx >= 0) return idx;
      for (var i = 0; i < headers.length; i++) {
        if (String(headers[i]).trim() === targetName) return i;
      }
      for (var j = 0; j < headers.length; j++) {
        var h = String(headers[j]).trim();
        if (h.indexOf(targetName) >= 0 || targetName.indexOf(h) >= 0) return j;
      }
      return -1;
    }

    var evIdx = {
      eventId: findEvHeaderIndex(evHeader, 'event_id'),
      staffId: findEvHeaderIndex(evHeader, 'staff_id'),
      date: findEvHeaderIndex(evHeader, '日付'),
      eventType: findEvHeaderIndex(evHeader, 'イベント種別'),
      title: findEvHeaderIndex(evHeader, 'タイトル'),
      timeMode: findEvHeaderIndex(evHeader, '時間指定方法'),
      startTime: findEvHeaderIndex(evHeader, '開始時刻'),
      endTime: findEvHeaderIndex(evHeader, '終了時刻'),
      durationMin: findEvHeaderIndex(evHeader, '所要時間'),
      fixedSlot: findEvHeaderIndex(evHeader, '固定枠'),
      patientLinked: findEvHeaderIndex(evHeader, '患者紐づき'),
      patientId: findEvHeaderIndex(evHeader, 'patient_id'),
      patientAffect: findEvHeaderIndex(evHeader, '患者影響'),
      lat: findEvHeaderIndex(evHeader, '緯度'),
      lng: findEvHeaderIndex(evHeader, '経度')
    };

    for (var evi = 1; evi < evValues.length; evi++) {
      var evRow = evValues[evi];
      var evStaffId = evRow[evIdx.staffId];
      // parseDate_ で文字列日付もパース
      var evDateObj = parseDate_(evRow[evIdx.date]);
      if (!evStaffId || !evDateObj) continue;
      var evDateStr = Utilities.formatDate(evDateObj, tz, 'yyyy/MM/dd');

      var evKey = evStaffId + '|' + evDateStr;
      if (!eventMap[evKey]) eventMap[evKey] = [];

      // 時間をminに変換（toMinutes は parseTimeToMinutes_ をラップ）
      var evStartMin = toMinutes(evRow[evIdx.startTime]);
      var evEndMin = toMinutes(evRow[evIdx.endTime]);
      // duration: 値があれば使用、なければ開始/終了から計算、それもなければ60分
      var rawEvDuration = evRow[evIdx.durationMin];
      var evDuration;
      if (rawEvDuration && !isNaN(parseInt(rawEvDuration, 10))) {
        evDuration = parseInt(rawEvDuration, 10);
      } else if (evStartMin != null && evEndMin != null && evEndMin > evStartMin) {
        evDuration = evEndMin - evStartMin;  // 開始/終了から自動算出
      } else {
        evDuration = 60;  // デフォルト
      }
      var evTimeMode = String(evRow[evIdx.timeMode] || '').trim();

      eventMap[evKey].push({
        eventId: evRow[evIdx.eventId] || '',
        eventType: evRow[evIdx.eventType] || '',
        title: evRow[evIdx.title] || '',
        dateObj: evDateObj,
        timeMode: evTimeMode,
        startMin: evStartMin,
        endMin: evEndMin,
        durationMin: evDuration,
        fixedSlot: evRow[evIdx.fixedSlot] === true || evRow[evIdx.fixedSlot] === 'TRUE',
        patientLinked: evRow[evIdx.patientLinked] === true || evRow[evIdx.patientLinked] === 'TRUE',
        patientId: evRow[evIdx.patientId] || null,
        patientAffect: String(evRow[evIdx.patientAffect] || '').trim(),
        lat: evRow[evIdx.lat],
        lng: evRow[evIdx.lng]
      });
    }
  }

  // ============================================================
  // Task B & C: スタッフ制限の不可区間取得と衝突判定
  // ============================================================

  // スタッフの基本シフト情報を取得するヘルパー
  function getStaffShift_(staffId) {
    for (var i = 0; i < staffList.length; i++) {
      if (staffList[i].id === staffId) {
        return { shiftStartMin: staffList[i].shiftStartMin, shiftEndMin: staffList[i].shiftEndMin };
      }
    }
    return { shiftStartMin: 0, shiftEndMin: 1440 };
  }

  // スタッフの不可区間を取得（制限タイプ＋イベントに基づいて正規化）
  function getStaffBlockedIntervals_(staffId, dateStr) {
    var shift = getStaffShift_(staffId);
    var intervals = [];

    // スタッフ個別変更リクエストからの制限
    var records = staffChangeMap[staffId + '|' + dateStr];
    if (records && records.length > 0) {
      records.forEach(function(rec) {
        var rType = rec.restrictionType;

        if (rType === '休み' || rType === '終日不可' || rType === '終日') {
          // 終日不可: [0, 1440)
          intervals.push({ start: 0, end: 1440 });
        } else if (rType === '遅刻') {
          // 遅刻: [shiftStart, newStart) を不可
          var newStart = rec.startTime;
          if (newStart != null && shift.shiftStartMin != null) {
            intervals.push({ start: shift.shiftStartMin, end: newStart });
          }
        } else if (rType === '早退') {
          // 早退: [newEnd, shiftEnd) を不可
          var newEnd = rec.endTime;
          if (newEnd != null && shift.shiftEndMin != null) {
            intervals.push({ start: newEnd, end: shift.shiftEndMin });
          }
        } else if (rType === '時間指定') {
          // 時間指定: [start, end) を不可
          if (rec.startTime != null && rec.endTime != null) {
            intervals.push({ start: rec.startTime, end: rec.endTime });
          }
        } else if (rType === '午前休') {
          // 午前休: [shiftStart, 12:00) = 12:00まで不可
          var amStart = shift.shiftStartMin != null ? shift.shiftStartMin : 0;
          intervals.push({ start: amStart, end: 720 });
        } else if (rType === '午後休') {
          // 午後休: [12:00, shiftEnd) = 12:00以降不可
          var pmEnd = shift.shiftEndMin != null ? shift.shiftEndMin : 1440;
          intervals.push({ start: 720, end: pmEnd });
        }
      });
    }

    // ※イベントは不可区間に入れない（アンカー方式の後段で処理するため）
    // イベントがあるスタッフでも、その前後に訪問を詰められる可能性があるため
    // 割当段階で候補を潰さず、時刻自動調整フェーズでイベントを避けて配置する

    // 区間がなければ空配列
    if (intervals.length === 0) return [];

    // 区間をマージ（重複・連続区間の統合）
    if (intervals.length <= 1) return intervals;
    intervals.sort(function(a, b) { return a.start - b.start; });
    var merged = [intervals[0]];
    for (var i = 1; i < intervals.length; i++) {
      var last = merged[merged.length - 1];
      var curr = intervals[i];
      if (curr.start <= last.end) {
        // 重複または連続 → マージ
        last.end = Math.max(last.end, curr.end);
      } else {
        merged.push(curr);
      }
    }
    return merged;
  }

  // 2つの区間が重なるかチェック
  function intervalsOverlap_(a, b) {
    return a.start < b.end && b.start < a.end;
  }

  // 固定訪問: 訪問区間が不可区間と1分でも重なれば不可
  function isFixedVisitBlocked_(visitStart, visitEnd, blockedIntervals) {
    if (visitStart == null || visitEnd == null) return false;
    var visitInterval = { start: visitStart, end: visitEnd };
    for (var i = 0; i < blockedIntervals.length; i++) {
      if (intervalsOverlap_(visitInterval, blockedIntervals[i])) {
        return true;  // 衝突あり
      }
    }
    return false;  // 衝突なし
  }

  // 可動訪問: 許容範囲から不可区間を引いた空き区間にsvcMinを置けるか
  function isFlexibleVisitBlocked_(earliestMin, latestMin, svcMin, blockedIntervals) {
    if (earliestMin == null || latestMin == null) return false;
    if (svcMin <= 0) svcMin = 30;  // デフォルト30分

    // 許容範囲 [earliestMin, latestMin] から不可区間を除いた空き区間を計算
    var available = [{ start: earliestMin, end: latestMin }];

    blockedIntervals.forEach(function(blocked) {
      var newAvailable = [];
      available.forEach(function(avail) {
        if (blocked.end <= avail.start || blocked.start >= avail.end) {
          // 重ならない
          newAvailable.push(avail);
        } else {
          // 重なる → 分割
          if (avail.start < blocked.start) {
            newAvailable.push({ start: avail.start, end: blocked.start });
          }
          if (blocked.end < avail.end) {
            newAvailable.push({ start: blocked.end, end: avail.end });
          }
        }
      });
      available = newAvailable;
    });

    // 空き区間のどこかにsvcMinが収まるかチェック
    for (var i = 0; i < available.length; i++) {
      var gap = available[i].end - available[i].start;
      if (gap >= svcMin) {
        return false;  // 収まる → ブロックされていない
      }
    }
    return true;  // どこにも収まらない → ブロック
  }

  // スタッフがこの訪問に対応可能かチェック（スタッフ個別変更を考慮）
  function isStaffAvailableForVisit_(staffId, dateStr, timeType, startMin, endMin, earliestMin, latestMin, svcMin) {
    var blockedIntervals = getStaffBlockedIntervals_(staffId, dateStr);
    if (blockedIntervals.length === 0) return true;  // 制限なし

    // デバッグログ
    console.log('Staff restriction check:', staffId, dateStr, 'blocked:', JSON.stringify(blockedIntervals), 'timeType:', timeType, 'start:', startMin, 'end:', endMin);

    // 終日不可チェック（[0,1440)が含まれていれば完全除外）
    for (var i = 0; i < blockedIntervals.length; i++) {
      if (blockedIntervals[i].start === 0 && blockedIntervals[i].end >= 1440) {
        return false;  // 終日不可
      }
    }

    // 固定訪問または具体的な時間が指定されている場合
    if (timeType === '固定' || (startMin != null && endMin != null)) {
      // 訪問区間が不可区間と重なれば不可
      if (startMin != null && endMin != null) {
        var isBlocked = isFixedVisitBlocked_(startMin, endMin, blockedIntervals);
        console.log('Fixed visit check:', startMin, '-', endMin, 'blocked:', isBlocked);
        return !isBlocked;
      }
    }

    // 可動訪問（午前/午後/終日/時間帯）
    // 許容範囲を決定
    var effEarliest = earliestMin;
    var effLatest = latestMin;

    // timeTypeによるデフォルト許容範囲
    if (timeType === '午前') {
      if (effEarliest == null) effEarliest = 9 * 60;   // 09:00
      if (effLatest == null) effLatest = 12 * 60;      // 12:00
    } else if (timeType === '午後') {
      if (effEarliest == null) effEarliest = 13 * 60;  // 13:00
      if (effLatest == null) effLatest = 17 * 60;      // 17:00
    } else if (timeType === '終日') {
      if (effEarliest == null) effEarliest = 9 * 60;   // 09:00
      if (effLatest == null) effLatest = 18 * 60;      // 18:00
    } else {
      // 時間帯など: start/endから許容範囲を取得
      if (effEarliest == null) effEarliest = startMin;
      if (effLatest == null) effLatest = endMin;
    }

    // 判定不能の場合、スタッフのシフト全体で判定
    if (effEarliest == null || effLatest == null) {
      var shift = getStaffShift_(staffId);
      effEarliest = shift.shiftStartMin || 0;
      effLatest = shift.shiftEndMin || 1440;
    }

    var isBlocked = isFlexibleVisitBlocked_(effEarliest, effLatest, svcMin || 30, blockedIntervals);
    console.log('Flexible visit check:', effEarliest, '-', effLatest, 'svcMin:', svcMin, 'blocked:', isBlocked);
    return !isBlocked;
  }

  var assignCountMap = {};
  function getAssignCount(staffId, dateStr) { return assignCountMap[staffId + '|' + dateStr] || 0; }
  function incAssignCount(staffId, dateStr) { var k = staffId + '|' + dateStr; assignCountMap[k] = (assignCountMap[k] || 0) + 1; }

  function getSoftCap_(staff) {
    if (staff.allocPref === '多め') return staff.maxPerDay;
    if (staff.allocPref === '少なめ') return Math.max(1, staff.maxPerDay - 2);
    return Math.max(1, staff.maxPerDay - 1); // 均等
  }

  var patientWeekCount = {};
  function getPatientWeekCount(pid, staffId) { return patientWeekCount[pid + '|' + staffId] || 0; }
  function incPatientWeekCount(pid, staffId) { var k = pid + '|' + staffId; patientWeekCount[k] = (patientWeekCount[k] || 0) + 1; }

  var weeklyRequests = [];
  wData.forEach(function(row){
    var d = row[wIdx.date];
    if (!(d instanceof Date)) return;
    var changeType = row[wIdx.change];
    if (changeType === 'キャンセル') return;
    var contPrefVal = normalizeContPref(row[wIdx.contPref]);
    weeklyRequests.push({ row: row, date: d, dateStr: Utilities.formatDate(d, tz, 'yyyy/MM/dd'), start: row[wIdx.start], end: row[wIdx.end], contPref: contPrefVal });
  });

  weeklyRequests.sort(function(a,b){
    if (a.date.getTime() !== b.date.getTime()) return a.date - b.date;
    // ★ローテーション優先の患者を先に処理（スタッフ分散のため）
    var aIsRotation = (a.contPref === 'ローテーション優先') ? 0 : 1;
    var bIsRotation = (b.contPref === 'ローテーション優先') ? 0 : 1;
    if (aIsRotation !== bIsRotation) return aIsRotation - bIsRotation;
    return toMinutes(a.start) - toMinutes(b.start);
  });

  var resultRows = [];
  var unassignedList = [];

  // ============================================================
  // Task A-2.5: イベントを resultRows に先挿入（イベント優先の固定アンカー）
  // ============================================================

  // staff_id|dateStr ごとに、イベントの開始/終了を確定させる
  function resolveEventInterval_(ev, staffId) {
    var shift = getStaffShift_(staffId);
    var shiftStart = (shift.shiftStartMin != null ? shift.shiftStartMin : 540);
    var shiftEnd   = (shift.shiftEndMin   != null ? shift.shiftEndMin   : 1080);

    // 1) start/end が両方あるなら最優先
    if (ev.startMin != null && ev.endMin != null && ev.endMin > ev.startMin) {
      return { start: ev.startMin, end: ev.endMin };
    }

    // 2) timeMode + duration で決める
    var dur = Number(ev.durationMin || 60) || 60;
    var mode = String(ev.timeMode || '').trim();

    if (mode === '午前') {
      var s = shiftStart;
      var e = Math.min(s + dur, 720);
      return { start: s, end: e };
    }
    if (mode === '午後') {
      var s2 = 720;
      var e2 = Math.min(s2 + dur, shiftEnd);
      return { start: s2, end: e2 };
    }
    if (mode === '終日') {
      return { start: shiftStart, end: shiftEnd };
    }

    // 3) それでも決まらない → 仮置き（目印用：13:00開始）
    var s3 = Math.max(shiftStart, 13 * 60);
    var e3 = Math.min(s3 + dur, shiftEnd);
    return { start: s3, end: e3 };
  }

  // イベント行をresultRowsへ
  Object.keys(eventMap).forEach(function(key) {
    var parts = key.split('|');
    var staffId = parts[0];
    var dateStr = parts[1];

    var events = eventMap[key] || [];
    if (!staffId || !dateStr || events.length === 0) return;

    // dateStr -> Date化
    var dateObj = parseDate_(dateStr);
    if (!dateObj) return;

    events.forEach(function(ev, idxEv) {
      var itv = resolveEventInterval_(ev, staffId);
      if (!itv || itv.start == null || itv.end == null || itv.end <= itv.start) return;

      var evid = ev.eventId ? String(ev.eventId) : ('EVT' + ('000' + (idxEv + 1)).slice(-3));
      var visitId = 'EV_' + evid;

      var evLabel = '[EV] ' + (ev.eventType || '') + (ev.title ? ':' + ev.title : '');
      var note = evLabel + (ev.patientLinked && ev.patientId ? ('（' + ev.patientId + '）') : '');

      // スタッフ名を取得
      var staffName = '';
      for (var si = 0; si < staffList.length; si++) {
        if (staffList[si].id === staffId) { staffName = staffList[si].name; break; }
      }

      // resultRowsのフォーマット:
      // [visit_id, 日付, 曜日, staff_id, スタッフ名, patient_id, 患者名, エリア, 開始時刻, 終了時刻,
      //  サービス時間, 時間タイプ, 希望最早時刻, 希望最遅時刻, 備考]
      resultRows.push([
        visitId,
        dateObj,
        '',               // 曜日
        staffId,
        staffName,
        '',               // patient_id
        '',               // 患者名
        'EV',             // エリア（目印）
        minutesToSerial_(itv.start),
        minutesToSerial_(itv.end),
        (itv.end - itv.start), // サービス時間(分)
        '固定',
        minutesToSerial_(itv.start), // 希望最早
        minutesToSerial_(itv.end),   // 希望最遅
        note
      ]);

      // ★★★ イベントもassignCountMapにカウントしてmaxPerDay制限を正しく適用 ★★★
      incAssignCount(staffId, dateStr);
    });
  });

  // ============================================================
  // 割当時の重なりチェック用：staffDateMapを先に初期化（イベント含む）
  // ============================================================
  var staffDateMap = {};
  resultRows.forEach(function(r, i) {
    var d = r[1], staffId = r[3];
    if (!staffId || !(d instanceof Date)) return;
    var key = staffId + '|' + Utilities.formatDate(d, tz, 'yyyy/MM/dd');
    if (!staffDateMap[key]) staffDateMap[key] = [];
    staffDateMap[key].push(i);
  });

  // 固定訪問が既存予定と重なるかチェックするヘルパー
  function isOverlappedWithExisting_(staffId, dateObj, startMin, endMin) {
    if (!staffId || !(dateObj instanceof Date) || startMin == null || endMin == null) return false;

    var dateStr = Utilities.formatDate(dateObj, tz, 'yyyy/MM/dd');
    var key = staffId + '|' + dateStr;
    var idxList = staffDateMap[key] || [];

    for (var i = 0; i < idxList.length; i++) {
      var r = resultRows[idxList[i]];
      if (!r) continue;
      // 未割当は無視
      if (!r[3] || r[4] === '未割当') continue;

      var s = toMinutes(r[8]);
      var e = toMinutes(r[9]);
      if (s == null || e == null) continue;

      if (s < endMin && startMin < e) return true; // overlap
    }
    return false;
  }

  // ★ローテーション優先用：患者ごとに「処理中に最後に割り当てられたスタッフID」を動的に追跡
  var lastAssignedStaffByPatient = {};

  weeklyRequests.forEach(function(item, idx){
    var row = item.row;
    var dateObj = item.date;
    var dateStr = item.dateStr;
    var youbiRaw = row[wIdx.youbi];
    var youbi = normalizeYoubi(youbiRaw);
    var pid = row[wIdx.pid];
    var pname = row[wIdx.pname];
    var area = row[wIdx.area];
    var start = row[wIdx.start];
    var end = row[wIdx.end];

    var svcRaw = row[wIdx.svcMin];
    var svcMin = Number(svcRaw);
    if (!svcMin && patientMap[pid] && patientMap[pid].svcMin) svcMin = Number(patientMap[pid].svcMin) || 0;
    if (!svcMin && typeof svcRaw === 'string') { var m = svcRaw.match(/(\d+)/); if (m) svcMin = Number(m[1]); }
    if (!svcMin) svcMin = 0;

    var sexLimit = row[wIdx.sexLimit];
    var contPrefRaw = row[wIdx.contPref];
    var contPref = normalizeContPref(contPrefRaw);
    var prevSid = row[wIdx.prevSid];
    var prevSname = row[wIdx.prevSname];
    var note = row[wIdx.note];
    var timeType = row[wIdx.timeType];
    var earliest = row[wIdx.earliest];
    var latest = row[wIdx.latest];

    var specifiedIdsArr = wIdx.specifiedIds >= 0 ? parseIdList(row[wIdx.specifiedIds]) : [];
    var specifiedType = wIdx.specifiedType >= 0 ? String(row[wIdx.specifiedType] || '').trim() : '';
    var ngIdsArr = wIdx.ngStaffIds >= 0 ? parseIdList(row[wIdx.ngStaffIds]) : [];

    var startMin = toMinutes(start);
    var endMin = toMinutes(end);

    var pInfo = patientMap[pid] || {};
    var plat = pInfo.lat;
    var plng = pInfo.lng;

    var avoidPrev = (contPref === 'ローテーション優先' && prevSid);
    // ★ローテーション優先時：動的に追跡した「直前割当スタッフ」を取得
    var dynamicPrevSid = lastAssignedStaffByPatient[pid] || null;
    var earliestMin = earliest ? toMinutes(earliest) : null;
    var latestMin = latest ? toMinutes(latest) : null;

    function canStaffServe(st, preferAreaFlagObj) {
      if (sexLimit === '女性のみ' && st.gender !== '女性') return false;
      if (sexLimit === '男性のみ' && st.gender !== '男性') return false;
      if (youbi && st.workDays.length > 0 && st.workDays.indexOf(youbi) === -1) return false;
      if (st.shiftStartMin != null && st.shiftEndMin != null) {
        if (timeType === '固定') {
          if (startMin != null && startMin < st.shiftStartMin) return false;
          if (endMin != null && endMin > st.shiftEndMin) return false;
          // ★追加：同スタッフの既存予定と重なるなら不可
          if (startMin != null && endMin != null) {
            if (isOverlappedWithExisting_(st.id, dateObj, startMin, endMin)) return false;
          }
        } else {
          var reqStart = earliestMin != null ? earliestMin : startMin;
          var reqEnd = latestMin != null ? latestMin : endMin;
          if (reqStart == null) reqStart = st.shiftStartMin;
          if (reqEnd == null) reqEnd = st.shiftEndMin;
          var latestStart = Math.max(reqStart, st.shiftStartMin);
          var earliestEnd = Math.min(reqEnd, st.shiftEndMin);
          if (latestStart >= earliestEnd) return false;
        }
      }
      var count = getAssignCount(st.id, dateStr);
      if (count >= getSoftCap_(st)) return false;

      // スタッフ個別変更リクエストによる制限チェック
      if (!isStaffAvailableForVisit_(st.id, dateStr, timeType, startMin, endMin, earliestMin, latestMin, svcMin)) {
        return false;
      }

      preferAreaFlagObj.flag = false;
      return true;
    }

    var needStaff = wIdx.needStaff >= 0 ? toHalfWidthNumber_(row[wIdx.needStaff], 1) : 1;
    if (needStaff < 1) needStaff = 1;
    if (needStaff > 2) needStaff = 2;

    var usedStaffIds = {};

    for (var slot = 1; slot <= needStaff; slot++) {
      var chosenStaff = null;
      var rotationDebugNote = ''; // ★デバッグ用

      if (specifiedType === '必須' && specifiedIdsArr.length > 0) {
        for (var si = 0; si < specifiedIdsArr.length; si++) {
          var specId = specifiedIdsArr[si];
          if (usedStaffIds[specId]) continue;
          var stSpec = staffList.find(function(s){ return s.id === specId; });
          if (stSpec) {
            var objSpec = { flag: false };
            if (canStaffServe(stSpec, objSpec)) { chosenStaff = stSpec; break; }
          }
        }
        if (!chosenStaff) note = (note || '') + ' / 指定必須スタッフ割当不可';
      } else if (contPref === '同じ人希望' && prevSid && !usedStaffIds[prevSid]) {
        if (ngIdsArr.indexOf(prevSid) < 0) {
          var stPrev = staffList.find(function(s){ return s.id === prevSid; });
          if (stPrev) {
            var objPrev = { flag: false };
            if (canStaffServe(stPrev, objPrev)) chosenStaff = stPrev;
          }
        }
      }

      if (!chosenStaff) {
        var candidates = [];
        // ★デバッグ用：除外理由を追跡
        var debugExcludeReasons = {};
        staffList.forEach(function(st){
          if (usedStaffIds[st.id]) { debugExcludeReasons[st.id] = 'usedStaffIds'; return; }
          if (sexLimit === '女性のみ' && st.gender !== '女性') { debugExcludeReasons[st.id] = 'sexLimit(女性のみ)'; return; }
          if (sexLimit === '男性のみ' && st.gender !== '男性') { debugExcludeReasons[st.id] = 'sexLimit(男性のみ)'; return; }
          if (youbi && st.workDays.length > 0 && st.workDays.indexOf(youbi) === -1) { debugExcludeReasons[st.id] = 'workDays(' + youbi + ')'; return; }
          if (avoidPrev && prevSid && st.id === prevSid) { debugExcludeReasons[st.id] = 'avoidPrev'; return; }
          if (st.shiftStartMin != null && st.shiftEndMin != null) {
            if (timeType === '固定') {
              if (startMin != null && startMin < st.shiftStartMin) { debugExcludeReasons[st.id] = 'shiftStart'; return; }
              if (endMin != null && endMin > st.shiftEndMin) { debugExcludeReasons[st.id] = 'shiftEnd'; return; }
              // ★追加：同スタッフの既存予定と重なるなら不可
              if (startMin != null && endMin != null) {
                if (isOverlappedWithExisting_(st.id, dateObj, startMin, endMin)) { debugExcludeReasons[st.id] = 'overlap'; return; }
              }
            } else {
              var reqStart = earliestMin != null ? earliestMin : startMin;
              var reqEnd = latestMin != null ? latestMin : endMin;
              if (reqStart == null) reqStart = st.shiftStartMin;
              if (reqEnd == null) reqEnd = st.shiftEndMin;
              var latestStart = Math.max(reqStart, st.shiftStartMin);
              var earliestEnd = Math.min(reqEnd, st.shiftEndMin);
              if (latestStart >= earliestEnd) { debugExcludeReasons[st.id] = 'timeWindow'; return; }
            }
          }
          var dayCount = getAssignCount(st.id, dateStr);
          if (dayCount >= getSoftCap_(st)) { debugExcludeReasons[st.id] = 'softCap'; return; }

          // スタッフ個別変更リクエストによる制限チェック
          if (!isStaffAvailableForVisit_(st.id, dateStr, timeType, startMin, endMin, earliestMin, latestMin, svcMin)) { debugExcludeReasons[st.id] = 'staffAvailability'; return; }

          var distKm = calcDistanceKm(plat, plng, st.lat, st.lng);
          var thisPatientCount = getPatientWeekCount(pid, st.id);

          // ★ローテーション優先時: 今週既にこの患者に割り当てられたスタッフは候補から除外
          // ただし、除外すると候補がいなくなる場合に備えて、別リストにも保持
          // ★追加：動的に追跡した直前割当スタッフかどうかのフラグ
          var isDynamicPrev = (contPref === 'ローテーション優先' && dynamicPrevSid && st.id === dynamicPrevSid);
          candidates.push({ staff: st, dayCount: dayCount, adjustedDayCount: dayCount, patientCount: thisPatientCount,
                           distKm: distKm, distScore: distToScore(distKm), samePatientToday: false,
                           assignedThisWeek: thisPatientCount > 0, isDynamicPrev: isDynamicPrev });
        });

        candidates = applyStaffPreference(candidates, specifiedIdsArr, specifiedType, ngIdsArr);

        // ★デバッグログ：ローテーション優先の患者で候補数と除外理由を確認
        var rotationDebugNote = '';
        if (contPref === 'ローテーション優先') {
          var excludeInfo = Object.keys(debugExcludeReasons).map(function(sid){ return sid + ':' + debugExcludeReasons[sid]; }).join(', ');
          console.log('[Rotation Debug] pid=' + pid + ' date=' + dateStr + ' candidates=' + candidates.length +
            ' names=[' + candidates.map(function(c){ return c.staff.id + ':' + c.staff.name; }).join(', ') + ']' +
            ' excluded=[' + excludeInfo + ']' +
            ' dynamicPrevSid=' + (dynamicPrevSid || 'none'));
          // 備考欄にも出力（デバッグ用）
          rotationDebugNote = '候補' + candidates.length + '名[' + candidates.map(function(c){ return c.staff.id; }).join(',') + '] 除外[' + excludeInfo + ']';
        }

        // ★ローテーション優先時: 今週未割り当てスタッフを優先（週内分散）
        if (contPref === 'ローテーション優先' && candidates.length > 0) {
          var unassignedThisWeek = candidates.filter(function(c){ return !c.assignedThisWeek; });
          // 未割り当てスタッフがいる場合はそちらを使用、いない場合は全候補を使用
          if (unassignedThisWeek.length > 0) {
            candidates = unassignedThisWeek;
          }
          // 全員が既に割り当て済みの場合は、patientCount最小のスタッフを選ぶためそのまま続行

          // ★重要：直前割当スタッフを候補から除外（他の候補がいる場合のみ）
          // これにより、ソートに関係なく確実に別のスタッフが選ばれる
          if (dynamicPrevSid && candidates.length > 1) {
            var nonDynamicPrev = candidates.filter(function(c){ return !c.isDynamicPrev; });
            if (nonDynamicPrev.length > 0) {
              candidates = nonDynamicPrev;
            }
          }

          // ★追加: 候補が複数いる場合、曜日によってローテーション（日ごとに異なるスタッフを優先）
          if (candidates.length > 1) {
            // スタッフIDでソートして決定論的な順序を作る
            candidates.sort(function(a, b) {
              return a.staff.id.localeCompare(b.staff.id);
            });
            // 曜日インデックスで候補リストをシフト（月=1, 火=2, ... 日=0）
            var dayOfWeek = dateObj.getDay();
            var shift = dayOfWeek % candidates.length;
            candidates = candidates.slice(shift).concat(candidates.slice(0, shift));
            // シフト後の順位を記録（後のソートで使用）
            candidates.forEach(function(c, i) {
              c._rotationRank = i;
            });
          }
        }

        if (candidates.length > 0) {
          candidates.forEach(function(c){
            var samePatientToday = resultRows.some(function(rr){
              return rr[3] === c.staff.id && rr[5] === pid && Utilities.formatDate(rr[1], tz, 'yyyy/MM/dd') === dateStr;
            });
            c.samePatientToday = samePatientToday;
          });

          candidates.sort(function(a, b){
            if (a._pref !== undefined && b._pref !== undefined && a._pref !== b._pref) return b._pref - a._pref;
            // samePatientToday=falseのスタッフを優先（同じ患者に今日割当済みのスタッフは後ろ）
            if (a.samePatientToday !== b.samePatientToday) return a.samePatientToday ? 1 : -1;
            // ★ローテーション優先時: 動的に追跡した直前割当スタッフを後回し（連続割当を防止）
            if (contPref === 'ローテーション優先' && a.isDynamicPrev !== b.isDynamicPrev) {
              return a.isDynamicPrev ? 1 : -1;
            }
            if (contPref === 'ローテーション優先' && a.patientCount !== b.patientCount) return a.patientCount - b.patientCount;
            // ★ローテーション優先時: 曜日シフト順位を使用（日ごとに異なるスタッフを選択）
            if (contPref === 'ローテーション優先' && a._rotationRank !== undefined && b._rotationRank !== undefined) {
              return a._rotationRank - b._rotationRank;
            }
            // ★均等優先: 割当回数を距離より優先（スタッフ間の負荷バランスを重視）
            if (a.adjustedDayCount !== b.adjustedDayCount) return a.adjustedDayCount - b.adjustedDayCount;
            // 割当回数が同じ場合は距離で判定
            if (a.distScore !== b.distScore) return a.distScore - b.distScore;
            return 0;
          });
          chosenStaff = candidates[0].staff;
        }
      }

      if (!chosenStaff) {
        var fallback = [];
        staffList.forEach(function(st){
          if (usedStaffIds[st.id]) return;
          if (ngIdsArr.indexOf(st.id) >= 0) return;
          if (sexLimit === '女性のみ' && st.gender !== '女性') return;
          if (sexLimit === '男性のみ' && st.gender !== '男性') return;
          if (youbi && st.workDays.length > 0 && st.workDays.indexOf(youbi) === -1) return;
          if (startMin != null && st.shiftStartMin != null && startMin < st.shiftStartMin) return;
          if (endMin != null && st.shiftEndMin != null && endMin > st.shiftEndMin) return;

          // スタッフ個別変更リクエストによる制限チェック（fallback時も適用）
          if (!isStaffAvailableForVisit_(st.id, dateStr, timeType, startMin, endMin, earliestMin, latestMin, svcMin)) return;

          // ★追加：動的に追跡した直前割当スタッフかどうかのフラグ
          var isDynamicPrevFb = (contPref === 'ローテーション優先' && dynamicPrevSid && st.id === dynamicPrevSid);
          var dayCountFb = getAssignCount(st.id, dateStr);
          var candidateObj = { staff: st, dayCount: dayCountFb, adjustedDayCount: dayCountFb, patientCount: getPatientWeekCount(pid, st.id), isDynamicPrev: isDynamicPrevFb };

          // ★maxPerDay制限チェック：上限に達していなければfallbackに追加、達していれば除外
          if (dayCountFb < st.maxPerDay) {
            fallback.push(candidateObj);
          }
          // ★上限に達しているスタッフは割り当てない（未割当にする）
        });

        if (fallback.length > 0) {
          // ★ローテーション優先時: 直前割当スタッフを除外（他の候補がいる場合のみ）
          if (contPref === 'ローテーション優先' && dynamicPrevSid && fallback.length > 1) {
            var nonDynamicPrevFb = fallback.filter(function(c){ return !c.isDynamicPrev; });
            if (nonDynamicPrevFb.length > 0) {
              fallback = nonDynamicPrevFb;
            }
          }
          // ★ローテーション優先時: 曜日シフトを適用（日ごとに異なるスタッフを選択）
          if (contPref === 'ローテーション優先' && fallback.length > 1) {
            fallback.sort(function(a, b) {
              return a.staff.id.localeCompare(b.staff.id);
            });
            var dayOfWeekFb = dateObj.getDay();
            var shiftFb = dayOfWeekFb % fallback.length;
            fallback = fallback.slice(shiftFb).concat(fallback.slice(0, shiftFb));
            fallback.forEach(function(c, i) {
              c._rotationRank = i;
            });
          }
          // ★ローテーション優先時: patientCount（週内割当回数）を最優先でソート
          fallback.sort(function(a,b){
            // ★追加：動的に追跡した直前割当スタッフを後回し
            if (contPref === 'ローテーション優先' && a.isDynamicPrev !== b.isDynamicPrev) {
              return a.isDynamicPrev ? 1 : -1;
            }
            if (contPref === 'ローテーション優先' && a.patientCount !== b.patientCount) {
              return a.patientCount - b.patientCount;
            }
            // ★追加：曜日シフト順位を使用
            if (contPref === 'ローテーション優先' && a._rotationRank !== undefined && b._rotationRank !== undefined) {
              return a._rotationRank - b._rotationRank;
            }
            // ★均等優先: 割当回数でソート（スタッフ間の負荷バランスを重視）
            return a.adjustedDayCount - b.adjustedDayCount;
          });
          chosenStaff = fallback[0].staff;
        }
      }

      var staffId = '', staffName = '';
      if (chosenStaff) {
        var tempStaffId = chosenStaff.id;
        var tempStaffName = chosenStaff.name;
        var tempMaxPerDay = chosenStaff.maxPerDay;

        // ★最終安全策：割当前にmaxPerDayを再チェック（二重チェック）
        var countBeforeAssign = getAssignCount(tempStaffId, dateStr);
        console.log('[Assign Debug] date=' + dateStr + ' staff=' + tempStaffId + ' countBefore=' + countBeforeAssign + ' maxPerDay=' + tempMaxPerDay + ' (assigning pid=' + pid + ')');

        // ★重要：maxPerDayを超えている場合は割当てを行わない（未割当にする）
        if (countBeforeAssign >= tempMaxPerDay) {
          console.log('[BLOCKED] maxPerDay exceeded! staff=' + tempStaffId + ' date=' + dateStr + ' count=' + countBeforeAssign + ' maxPerDay=' + tempMaxPerDay + ' -> marking as unassigned');
          staffName = '未割当';
          note = (note || '') + ' / maxPerDay上限超過(' + tempStaffId + ')';
          unassignedList.push({ date: dateObj, youbi: youbiRaw, pid: pid, pname: pname, needStaff: needStaff, slot: slot, reason: 'maxPerDay上限超過(' + tempStaffId + ' ' + countBeforeAssign + '/' + tempMaxPerDay + ')' });
        } else {
          staffId = tempStaffId;
          staffName = tempStaffName;
          usedStaffIds[staffId] = true;
          incAssignCount(staffId, dateStr);
          incPatientWeekCount(pid, staffId);
          // ★ローテーション優先用：この患者に最後に割り当てたスタッフを記録
          lastAssignedStaffByPatient[pid] = staffId;
        }
      } else {
        staffName = '未割当';
        unassignedList.push({ date: dateObj, youbi: youbiRaw, pid: pid, pname: pname, needStaff: needStaff, slot: slot, reason: note || '条件を満たすスタッフなし' });
      }

      var visitId = 'V' + Utilities.formatString('%03d', idx + 1);
      if (needStaff > 1) visitId = visitId + '-' + slot;

      var note2 = note || '';
      if (needStaff > 1) note2 = (note2 ? note2 + ' / ' : '') + '同時訪問(' + slot + '/' + needStaff + ')';
      // ★デバッグ用：ローテーション優先時の候補情報を備考に追加
      if (rotationDebugNote) note2 = (note2 ? note2 + ' / ' : '') + rotationDebugNote;

      resultRows.push([visitId, dateObj, youbiRaw, staffId, staffName, pid, pname, area, start, end, svcMin, timeType, earliest, latest, note2]);

      // ★追加：この行を staffDateMap に登録（後続の重なり判定に使う）
      if (staffId && dateObj instanceof Date) {
        var k = staffId + '|' + dateStr;
        if (!staffDateMap[k]) staffDateMap[k] = [];
        staffDateMap[k].push(resultRows.length - 1);
      }
    }
  });

  // ルート最適化（簡略版）
  var staffLocMap = {};
  staffList.forEach(function(st){ staffLocMap[st.id] = { lat: Number(st.lat) || null, lng: Number(st.lng) || null }; });

  var dayGroupMap = {};
  resultRows.forEach(function(r, i){
    var d = r[1], sId = r[3];
    if (!sId || !(d instanceof Date)) return;
    var key = sId + '|' + Utilities.formatDate(d, tz, 'yyyy/MM/dd');
    if (!dayGroupMap[key]) dayGroupMap[key] = [];
    dayGroupMap[key].push(i);
  });

  Object.keys(dayGroupMap).forEach(function(key){
    var idxList = dayGroupMap[key];
    idxList.sort(function(aIdx, bIdx){ return toMinutes(resultRows[aIdx][8]) - toMinutes(resultRows[bIdx][8]); });
  });

  // 移動距離計算
  var prevVisitIdArr = new Array(resultRows.length).fill('');
  var moveKmArr = new Array(resultRows.length).fill('');
  var moveMinArr = new Array(resultRows.length).fill('');

  // staffDateMapを再構築（時刻調整・Level1再挿入用：イベント含む）
  staffDateMap = {};
  resultRows.forEach(function(r, i){
    var d = r[1], staffId = r[3];
    // イベント行も含める（時刻調整のアンカー、Level1再挿入の隙間判定に必要）
    if (!staffId || !(d instanceof Date)) return;
    var key = staffId + '|' + Utilities.formatDate(d, tz, 'yyyy/MM/dd');
    if (!staffDateMap[key]) staffDateMap[key] = [];
    staffDateMap[key].push(i);
  });

  // --- 2名体制ペアマップ構築 ---
  var coupledVisitMap = {};  // { "V001": [rowIdx1, rowIdx2] }
  resultRows.forEach(function(r, i) {
    var vid = r[0];
    if (!vid || typeof vid !== 'string') return;
    var m = vid.match(/^(V\d+)-(\d+)$/);
    if (m) {
      var baseId = m[1];
      if (!coupledVisitMap[baseId]) coupledVisitMap[baseId] = [];
      coupledVisitMap[baseId].push(i);
    }
  });

  // ★移動距離計算専用：患者行のみ（イベントはpid空なので除外）
  var staffDateMapForMove = {};
  resultRows.forEach(function(r, i){
    var d = r[1], staffId = r[3], pid = r[5];
    if (!staffId || !(d instanceof Date) || !pid) return;
    var key = staffId + '|' + Utilities.formatDate(d, tz, 'yyyy/MM/dd');
    if (!staffDateMapForMove[key]) staffDateMapForMove[key] = [];
    staffDateMapForMove[key].push(i);
  });

  Object.keys(staffDateMapForMove).forEach(function(key){
    var indexList = staffDateMapForMove[key];
    indexList.sort(function(aIdx, bIdx){ return toMinutes(resultRows[aIdx][8]) - toMinutes(resultRows[bIdx][8]); });
    for (var j = 0; j < indexList.length; j++) {
      var currIndex = indexList[j];
      if (j === 0) continue;
      var prevIndex = indexList[j - 1];
      var prevPid = resultRows[prevIndex][5];
      var currPid = resultRows[currIndex][5];
      var prevP = patientMap[prevPid] || {};
      var currP = patientMap[currPid] || {};
      if (prevP.lat != null && prevP.lng != null && currP.lat != null && currP.lng != null) {
        var distKm = calcDistanceKm(prevP.lat, prevP.lng, currP.lat, currP.lng);
        prevVisitIdArr[currIndex] = resultRows[prevIndex][0];
        moveKmArr[currIndex] = distKm;
        moveMinArr[currIndex] = Math.round(distKm / 20 * 60);
      }
    }
  });

  // ============================================================
  // 時刻自動調整（イベント/固定訪問をアンカーとして可動訪問を隙間に詰める）
  // ============================================================

  // "その行がイベントか" 判定（visit_idがEV_ か 備考に[EV]）
  function isEventRow_(row) {
    var vid = String(row[0] || '');
    var note = String(row[14] || '');
    return vid.indexOf('EV_') === 0 || note.indexOf('[EV]') >= 0;
  }

  // 分ベースのstart/end取得
  function rowStartMin_(row) { return toMinutes(row[8]); }
  function rowEndMin_(row)   { return toMinutes(row[9]); }

  function setRowTimeByMinutes_(row, startMin, endMin) {
    row[8] = minutesToSerial_(startMin);
    row[9] = minutesToSerial_(endMin);
  }

  // 2区間が重なるか
  function overlap_(s1,e1,s2,e2) {
    return s1 < e2 && s2 < e1;
  }

  // 固定区間を避けて次の開始時刻を見つける
  function findNextNonOverlappingStart_(candidateStart, durationMin, fixedIntervals, latestMin) {
    var s = candidateStart;
    var e = s + durationMin;

    // ガード: latestMin制約チェック
    if (latestMin != null && s > latestMin) return null;

    // 固定区間と重なる限り、固定区間の end までジャンプ
    var safety = 0;
    while (safety < 200) {
      safety++;

      var hit = null;
      for (var i = 0; i < fixedIntervals.length; i++) {
        var f = fixedIntervals[i];
        if (overlap_(s, e, f.start, f.end)) { hit = f; break; }
      }
      if (!hit) return s; // 重ならない → この開始時刻でOK

      s = hit.end; // 固定区間の終了後に移動
      e = s + durationMin;

      if (latestMin != null && e > latestMin) return null; // 時間切れ
    }
    return null; // ループ防止（見つからない）
  }

  // --- 2名体制ペアの時刻同期関数 ---
  function syncCoupledVisitTimes_(resultRows, coupledVisitMap, staffDateMap, tz) {
    var EXTRA_BUFFER = 15; // 分

    Object.keys(coupledVisitMap).forEach(function(baseId) {
      var indices = coupledVisitMap[baseId];
      if (indices.length !== 2) return;

      var r1 = resultRows[indices[0]];
      var r2 = resultRows[indices[1]];

      // 両方にスタッフが割当済みか確認
      if (!r1[3] || !r2[3]) return;

      var s1 = rowStartMin_(r1), e1 = rowEndMin_(r1);
      var s2 = rowStartMin_(r2), e2 = rowEndMin_(r2);

      if (s1 === s2 && e1 === e2) return; // 既に同期済み

      // 同期先時刻を決定: 遅い方の開始時刻に合わせる
      var targetStart = Math.max(s1, s2);
      var svcMin = (e1 - s1) || (e2 - s2) || 60;
      var targetEnd = targetStart + svcMin;

      // 時間窓制約チェック
      var earliest1 = toMinutes(r1[12]) || 0;
      var latest1 = toMinutes(r1[13]) || 1440;
      var earliest2 = toMinutes(r2[12]) || 0;
      var latest2 = toMinutes(r2[13]) || 1440;
      var commonEarliest = Math.max(earliest1, earliest2);
      var commonLatest = Math.min(latest1, latest2);

      if (targetStart < commonEarliest) targetStart = commonEarliest;
      if (targetStart > commonLatest) targetStart = commonLatest;
      targetEnd = targetStart + svcMin;

      // 両スタッフの当日スケジュールで衝突チェック
      var canPlace = true;
      [indices[0], indices[1]].forEach(function(rIdx) {
        var row = resultRows[rIdx];
        var staffId = row[3];
        var dateStr = Utilities.formatDate(row[1], tz, 'yyyy/MM/dd');
        var key = staffId + '|' + dateStr;
        var dayIndices = staffDateMap[key] || [];

        dayIndices.forEach(function(otherIdx) {
          if (otherIdx === indices[0] || otherIdx === indices[1]) return;
          var otherS = rowStartMin_(resultRows[otherIdx]);
          var otherE = rowEndMin_(resultRows[otherIdx]);
          if (overlap_(targetStart, targetEnd + EXTRA_BUFFER, otherS, otherE + EXTRA_BUFFER)) {
            canPlace = false;
          }
        });
      });

      if (canPlace) {
        setRowTimeByMinutes_(r1, targetStart, targetEnd);
        setRowTimeByMinutes_(r2, targetStart, targetEnd);
      } else {
        // フォールバック: 備考に警告を追加
        r1[14] = (r1[14] || '') + ' [時刻同期不可:要手動調整]';
        r2[14] = (r2[14] || '') + ' [時刻同期不可:要手動調整]';
      }
    });
  }

  Object.keys(staffDateMap).forEach(function(key){
    var idxList = staffDateMap[key];
    if (!idxList || idxList.length === 0) return;

    // 時刻未確定が混ざるので、まずアンカー/可動で分ける
    var anchors = []; // {idx, s, e, kind}
    var flexes  = []; // idx

    idxList.forEach(function(rIdx){
      var row = resultRows[rIdx];
      var timeType = row[11];
      var s = rowStartMin_(row);
      var e = rowEndMin_(row);

      var isEv = isEventRow_(row);

      // アンカー条件：
      // - イベント（常に固定）
      // - timeType固定 かつ start/end がある（固定訪問）
      if (isEv) {
        if (s != null && e != null && e > s) anchors.push({ idx: rIdx, s: s, e: e, kind: 'EV' });
        else {
          // イベントなのに時間が取れない → とりあえず何もしない（要データ）
          row[14] = (row[14] || '') + ' / EV時間未確定';
          flexes.push(rIdx);
        }
        return;
      }

      if (timeType === '固定' && s != null && e != null && e > s) {
        anchors.push({ idx: rIdx, s: s, e: e, kind: 'FIX' });
        return;
      }

      // それ以外は可動として扱う
      flexes.push(rIdx);
    });

    anchors.sort(function(a,b){ return a.s - b.s; });

    // ============================================================
    // アンカー同士（EV/FIX）の衝突も解消：後勝ちを未割当に落とす
    // ルール：EVは最優先。EVと衝突したFIXは落とす。FIX同士なら後の方を落とす。
    // ============================================================
    (function resolveAnchorConflicts_(){
      anchors.sort(function(a,b){ return a.s - b.s; });

      var cleaned = [];
      for (var i = 0; i < anchors.length; i++) {
        var cur = anchors[i];
        if (cleaned.length === 0) { cleaned.push(cur); continue; }

        var prev = cleaned[cleaned.length - 1];

        // 重なり判定
        if (overlap_(prev.s, prev.e, cur.s, cur.e)) {
          // EV優先：FIXを落とす
          if (prev.kind === 'EV' && cur.kind === 'FIX') {
            // cur を落とす
            var rowC = resultRows[cur.idx];
            rowC[3] = '';
            rowC[4] = '未割当';
            rowC[14] = (rowC[14] || '') + ' / アンカー衝突(EV優先で未割当)';
            unassignedList.push({ date: rowC[1], youbi: rowC[2], pid: rowC[5], pname: rowC[6], needStaff: 1, slot: 1, reason: 'アンカー衝突(EV優先)' });
            continue;
          }
          if (prev.kind === 'FIX' && cur.kind === 'EV') {
            // prev を落として EV を採用（cleaned の最後を置き換え）
            var rowP = resultRows[prev.idx];
            rowP[3] = '';
            rowP[4] = '未割当';
            rowP[14] = (rowP[14] || '') + ' / アンカー衝突(EV優先で未割当)';
            unassignedList.push({ date: rowP[1], youbi: rowP[2], pid: rowP[5], pname: rowP[6], needStaff: 1, slot: 1, reason: 'アンカー衝突(EV優先)' });

            cleaned[cleaned.length - 1] = cur;
            continue;
          }

          // FIX同士：後勝ち（cur）を落とす
          if (prev.kind === 'FIX' && cur.kind === 'FIX') {
            var rowF = resultRows[cur.idx];
            rowF[3] = '';
            rowF[4] = '未割当';
            rowF[14] = (rowF[14] || '') + ' / 固定同士衝突で未割当';
            unassignedList.push({ date: rowF[1], youbi: rowF[2], pid: rowF[5], pname: rowF[6], needStaff: 1, slot: 1, reason: '固定同士が衝突' });
            continue;
          }

          // EV同士が衝突してる場合はデータ側問題（どちらも残す、要確認を記録）
          if (prev.kind === 'EV' && cur.kind === 'EV') {
            var rowE = resultRows[cur.idx];
            rowE[14] = (rowE[14] || '') + ' / EV同士衝突(要確認)';
            // 両方残す（どちらも固定なので）
            cleaned.push(cur);
            continue;
          }
        }

        cleaned.push(cur);
      }

      anchors = cleaned;
    })();

    // スタッフのシフト範囲を取得
    var staffId = key.split('|')[0];
    var dateStr = key.split('|')[1];
    var shift = getStaffShift_(staffId);
    var dayStart = (shift.shiftStartMin != null ? shift.shiftStartMin : 540);
    var dayEnd   = (shift.shiftEndMin   != null ? shift.shiftEndMin   : 1080);

    // ★暫定ガード：訪問看護は基本 09:00 以降とする（8時台吸着の防止）
    dayStart = Math.max(dayStart, 9 * 60);

    // 可動訪問を「隙間」に詰める
    flexes.sort(function(aIdx,bIdx){
      var aS = rowStartMin_(resultRows[aIdx]);
      var bS = rowStartMin_(resultRows[bIdx]);
      if (aS == null && bS == null) return 0;
      if (aS == null) return 1;
      if (bS == null) return -1;
      return aS - bS;
    });

    // gapを作る（アンカー前後にバッファを確保）
    var gaps = [];
    var cursor = dayStart;
    anchors.forEach(function(a){
      // アンカー開始前にバッファを空ける
      var gapEnd = a.s - EXTRA_BUFFER_MIN;
      if (cursor < gapEnd) gaps.push({ s: cursor, e: gapEnd });
      // アンカー終了後にバッファを空けて次へ
      cursor = Math.max(cursor, a.e + EXTRA_BUFFER_MIN);
    });
    if (cursor < dayEnd) gaps.push({ s: cursor, e: dayEnd });

    // gap内へ順に配置
    var cursorByGap = gaps.map(function(g){ return g.s; });

    for (var fi = 0; fi < flexes.length; fi++) {
      var rIdx = flexes[fi];
      var row = resultRows[rIdx];

      // 既に未割当になってるものはスキップ
      if (String(row[4] || '') === '未割当') continue;

      var svcMin = Number(row[10]) || 0;
      if (!svcMin) svcMin = 30;

      // 希望時間帯（列12/13：希望最早/希望最遅）
      var earliestMin = toMinutes(row[12]);
      var latestMin   = toMinutes(row[13]);

      // ★ここが肝：希望最早/最遅が取れない or 空扱いのときは timeType で補完して必ず守る
      var tt = String(row[11] || '').trim(); // 時間タイプ
      if (earliestMin == null || latestMin == null) {
        if (tt === '午前') { earliestMin = 9 * 60;  latestMin = 12 * 60; }
        else if (tt === '午後') { earliestMin = 13 * 60; latestMin = 17 * 60; }
        else if (tt === '終日') { earliestMin = 9 * 60;  latestMin = 18 * 60; }
        // '時間帯' は本来 12/13 に入る想定なので、両方nullなら「補完せず」＝制約なし扱いのまま
      }

      // ★安全ガード：earliestMin が dayStart より前なら dayStart に引き上げる
      if (earliestMin != null) earliestMin = Math.max(earliestMin, dayStart);

      var placed = false;

      for (var gi = 0; gi < gaps.length; gi++) {
        var g = gaps[gi];
        var startCand = cursorByGap[gi];

        // 希望がある場合はその範囲に寄せる
        if (earliestMin != null) startCand = Math.max(startCand, earliestMin);
        var endCand = startCand + svcMin;

        // 最新制約（latestMin は「終了上限」として扱う）
        if (latestMin != null && endCand > latestMin) {
          // このgapでは無理かも → 次のgapへ
          continue;
        }

        // gap内に収まるか
        if (startCand >= g.s && endCand <= g.e) {
          setRowTimeByMinutes_(row, startCand, endCand);
          cursorByGap[gi] = endCand + EXTRA_BUFFER_MIN; // 次はバッファ込みで進める
          placed = true;
          break;
        }
      }

      if (!placed) {
        // 置けない → 未割当化
        row[3] = '';
        row[4] = '未割当';
        row[14] = (row[14] || '') + ' / EV優先: 隙間に入らず未割当';
        unassignedList.push({
          date: row[1],
          youbi: row[2],
          pid: row[5],
          pname: row[6],
          needStaff: 1,
          slot: 1,
          reason: 'イベント優先で隙間に入らない'
        });
      }
    }
  });

  // --- 時刻調整後の2名体制ペア同期 ---
  syncCoupledVisitTimes_(resultRows, coupledVisitMap, staffDateMap, tz);

  // ============================================================
  // Level 1: 未割当訪問の再挿入（別スタッフへの振り分け試行）
  // ============================================================

  // 未割当になった訪問を抽出（staff_idが空 or スタッフ名が"未割当"の行）
  var unassignedRows = [];
  for (var ui = 0; ui < resultRows.length; ui++) {
    var uRow = resultRows[ui];
    if (!uRow[3] || uRow[4] === '未割当') {
      // イベント行は除外
      if (String(uRow[0] || '').indexOf('EV_') === 0) continue;
      unassignedRows.push({ idx: ui, row: uRow });
    }
  }

  // --- 2名ペアの原子性保証: 片方が未割当なら、もう片方も未割当にする ---
  Object.keys(coupledVisitMap).forEach(function(baseId) {
    var indices = coupledVisitMap[baseId];
    if (indices.length !== 2) return;
    var r1 = resultRows[indices[0]];
    var r2 = resultRows[indices[1]];
    var assigned1 = !!r1[3] && r1[4] !== '未割当';
    var assigned2 = !!r2[3] && r2[4] !== '未割当';
    if (assigned1 !== assigned2) {
      // 片方だけ割当済み → 両方未割当にする
      var unassignIdx = assigned1 ? indices[0] : indices[1];
      var row = resultRows[unassignIdx];
      // staffDateMapから除去
      var sKey = row[3] + '|' + Utilities.formatDate(row[1], tz, 'yyyy/MM/dd');
      if (staffDateMap[sKey]) {
        staffDateMap[sKey] = staffDateMap[sKey].filter(function(i) { return i !== unassignIdx; });
      }
      row[3] = ''; row[4] = '未割当';
      row[14] = (row[14] || '') + ' [2名体制:ペア未確保のため未割当]';
      // unassignedRowsにも追加
      unassignedRows.push({ idx: unassignIdx, row: row });
    }
  });

  if (unassignedRows.length > 0) {
    // 候補スタッフを選定する関数
    function getCandidateStaffsForVisit_(visitRow, dateStr, youbi) {
      var candidates = [];
      var pid = visitRow[5];
      var pInfo = patientMap[pid] || {};
      var pLat = pInfo.lat;
      var pLng = pInfo.lng;

      for (var si = 0; si < staffList.length; si++) {
        var staff = staffList[si];

        // 1) 曜日チェック
        if (staff.workDays && staff.workDays.length > 0) {
          if (staff.workDays.indexOf(youbi) < 0) continue;
        }

        // 2) スタッフ変更（終日休み等）チェック
        var scKey = staff.id + '|' + dateStr;
        var scRecs = staffChangeMap[scKey] || [];
        var isDayOff = false;
        for (var sci = 0; sci < scRecs.length; sci++) {
          var rt = scRecs[sci].restrictionType;
          if (rt === '休み' || rt === '終日不可' || rt === '終日') {
            isDayOff = true;
            break;
          }
        }
        if (isDayOff) continue;

        // 4) maxPerDay制限チェック
        var staffCount = getAssignCount(staff.id, dateStr);
        if (staffCount >= staff.maxPerDay) continue;

        // 3) 距離でスコア（近い順）
        var distScore = 9999;
        if (pLat != null && pLng != null && staff.lat != null && staff.lng != null) {
          distScore = calcDistanceKm(pLat, pLng, staff.lat, staff.lng);
        }

        candidates.push({ staff: staff, distScore: distScore });
      }

      // 距離近い順にソート
      candidates.sort(function(a, b) { return a.distScore - b.distScore; });

      // 上位N人に絞る
      return candidates.slice(0, 10);
    }

    // スタッフ+日の隙間に訪問が入るかチェック（時間帯制限を考慮）
    function canInsertVisitToStaffDay_(staffId, dateStr, visitRow, svcMin) {
      // ★時間帯制限を取得（列11: 時間タイプ, 列12: 希望最早時刻, 列13: 希望最遅時刻）
      var timeType = String(visitRow[11] || '').trim();
      var earliestRaw = visitRow[12];
      var latestRaw = visitRow[13];
      var effEarliest = toMinutes(earliestRaw);
      var effLatest = toMinutes(latestRaw);

      // 時間タイプに基づいてデフォルト許容範囲を設定
      if (timeType === '午前') {
        if (effEarliest == null) effEarliest = 9 * 60;   // 09:00
        if (effLatest == null) effLatest = 12 * 60;      // 12:00
      } else if (timeType === '午後') {
        if (effEarliest == null) effEarliest = 13 * 60;  // 13:00
        if (effLatest == null) effLatest = 17 * 60;      // 17:00
      } else if (timeType === '終日') {
        if (effEarliest == null) effEarliest = 9 * 60;   // 09:00
        if (effLatest == null) effLatest = 18 * 60;      // 18:00
      } else if (timeType === '時間帯') {
        // ★「時間帯」タイプ：希望最早/最遅時刻を厳守（列12・列13の値を使用）
        // 値がない場合のみデフォルトを適用
        if (effEarliest == null) effEarliest = 9 * 60;
        if (effLatest == null) effLatest = 18 * 60;
      } else {
        // デフォルト（時間帯指定なし・不明なタイプ）
        if (effEarliest == null) effEarliest = 9 * 60;
        if (effLatest == null) effLatest = 18 * 60;
      }

      console.log('[canInsertVisitToStaffDay_] staffId:', staffId, 'timeType:', timeType, 'effEarliest:', effEarliest, 'effLatest:', effLatest);

      var dayKey = staffId + '|' + dateStr;
      var dayIdxList = staffDateMap[dayKey] || [];
      if (dayIdxList.length === 0) {
        // そのスタッフのその日に何もなければ、シフト内なら入る
        var shift = getStaffShift_(staffId);
        var shiftS = shift.shiftStartMin != null ? shift.shiftStartMin : 540;
        var shiftE = shift.shiftEndMin != null ? shift.shiftEndMin : 1080;
        // ★暫定ガード：訪問看護は基本 09:00 以降とする（8時台吸着の防止）
        shiftS = Math.max(shiftS, 9 * 60);

        // ★時間帯制限を適用
        var windowStart = Math.max(shiftS, effEarliest);
        var windowEnd = Math.min(shiftE, effLatest);

        if (windowEnd - windowStart >= svcMin) {
          return { ok: true, startMin: windowStart };
        } else {
          console.log('[canInsertVisitToStaffDay_] 時間帯内に空きなし（empty day）: windowStart:', windowStart, 'windowEnd:', windowEnd, 'svcMin:', svcMin);
          return { ok: false };
        }
      }

      // 既存の予定を集める
      var anchors = [];
      for (var di = 0; di < dayIdxList.length; di++) {
        var rIdx = dayIdxList[di];
        var r = resultRows[rIdx];
        var s = toMinutes(r[8]);
        var e = toMinutes(r[9]);
        if (s != null && e != null && e > s) {
          anchors.push({ start: s, end: e });
        }
      }
      anchors.sort(function(a, b) { return a.start - b.start; });

      // 隙間を探す
      var shift2 = getStaffShift_(staffId);
      var dayStart = shift2.shiftStartMin != null ? shift2.shiftStartMin : 540;
      var dayEnd = shift2.shiftEndMin != null ? shift2.shiftEndMin : 1080;

      // ★暫定ガード：訪問看護は基本 09:00 以降とする（8時台吸着の防止）
      dayStart = Math.max(dayStart, 9 * 60);

      var gaps = [];
      var cursor = dayStart;
      for (var ai = 0; ai < anchors.length; ai++) {
        if (cursor + EXTRA_BUFFER_MIN < anchors[ai].start) {
          gaps.push({ s: cursor, e: anchors[ai].start - EXTRA_BUFFER_MIN });
        }
        cursor = Math.max(cursor, anchors[ai].end + EXTRA_BUFFER_MIN);
      }
      if (cursor < dayEnd) {
        gaps.push({ s: cursor, e: dayEnd });
      }

      // svcMin が入る隙間を探す（★時間帯制限を考慮）
      for (var gi = 0; gi < gaps.length; gi++) {
        // 隙間と許容時間帯の交差部分を計算
        var gapStart = Math.max(gaps[gi].s, effEarliest);
        var gapEnd = Math.min(gaps[gi].e, effLatest);
        var gapLen = gapEnd - gapStart;
        if (gapLen >= svcMin) {
          console.log('[canInsertVisitToStaffDay_] 時間帯内に挿入可能: gapStart:', gapStart, 'gapEnd:', gapEnd);
          return { ok: true, startMin: gapStart };
        }
      }

      console.log('[canInsertVisitToStaffDay_] 時間帯内に空きなし: timeType:', timeType, 'effEarliest:', effEarliest, 'effLatest:', effLatest);
      return { ok: false };
    }

    // 再挿入ループ
    var reinsertedCount = 0;
    for (var uri = 0; uri < unassignedRows.length; uri++) {
      var uItem = unassignedRows[uri];
      var uRow = uItem.row;
      var uIdx = uItem.idx;

      var uDateObj = uRow[1];
      if (!(uDateObj instanceof Date)) continue;
      var uDateStr = Utilities.formatDate(uDateObj, tz, 'yyyy/MM/dd');
      var uYoubi = normalizeYoubi(uRow[2]);
      var uSvcMin = Number(uRow[10]) || 30;

      var candidates = getCandidateStaffsForVisit_(uRow, uDateStr, uYoubi);

      for (var ci = 0; ci < candidates.length; ci++) {
        var cStaff = candidates[ci].staff;
        var cStaffId = cStaff.id;

        // 元のスタッフは除外（すでに試したはず）
        if (cStaffId === uRow[3]) continue;

        var insertResult = canInsertVisitToStaffDay_(cStaffId, uDateStr, uRow, uSvcMin);
        if (insertResult.ok) {
          // 挿入成功！ rowを更新
          uRow[3] = cStaffId;
          uRow[4] = cStaff.name;
          uRow[8] = minutesToSerial_(insertResult.startMin);
          uRow[9] = minutesToSerial_(insertResult.startMin + uSvcMin);
          uRow[14] = (uRow[14] || '') + ' / 再挿入(' + cStaff.name + ')';

          // staffDateMap に追加
          var newKey = cStaffId + '|' + uDateStr;
          if (!staffDateMap[newKey]) staffDateMap[newKey] = [];
          staffDateMap[newKey].push(uIdx);

          incAssignCount(cStaffId, uDateStr);
          reinsertedCount++;
          break;
        }
      }
    }

    // unassignedList から再挿入成功分を除去
    if (reinsertedCount > 0) {
      var newUnassignedList = [];
      for (var nui = 0; nui < unassignedList.length; nui++) {
        var uItem2 = unassignedList[nui];
        // resultRows で同じ patient_id + date が未割当のままかチェック
        var stillUnassigned = false;
        for (var rri = 0; rri < resultRows.length; rri++) {
          var rr = resultRows[rri];
          if (rr[5] === uItem2.pid && rr[1] instanceof Date) {
            var rrDateStr = Utilities.formatDate(rr[1], tz, 'yyyy/MM/dd');
            var uItem2DateStr = (uItem2.date instanceof Date) ?
              Utilities.formatDate(uItem2.date, tz, 'yyyy/MM/dd') : String(uItem2.date);
            if (rrDateStr === uItem2DateStr && (!rr[3] || rr[4] === '未割当')) {
              stillUnassigned = true;
              break;
            }
          }
        }
        if (stillUnassigned) {
          newUnassignedList.push(uItem2);
        }
      }
      unassignedList = newUnassignedList;
    }
  }

  // --- Level 1再挿入後の2名体制ペア同期 ---
  syncCoupledVisitTimes_(resultRows, coupledVisitMap, staffDateMap, tz);

  // ============================================================
  // Level3: 距離最適化（安全版）
  // - EV/固定は動かさない
  // - 希望最早/希望最遅が空の可動訪問のみ並べ替え
  // - 並べ替え後、各「隙間」内で詰め直す（EXTRA_BUFFER_MIN 適用）
  // ============================================================
  function applyLevel3RouteOptimizeSafe_() {

    function getPatientLatLng_(pid) {
      var p = patientMap[pid];
      if (!p) return null;
      if (p.lat == null || p.lng == null) return null;
      return { lat: Number(p.lat), lng: Number(p.lng) };
    }

    function getRowLatLng_(row) {
      var pid = row[5];
      if (!pid) return null;
      return getPatientLatLng_(pid);
    }

    function distKmByLL_(a, b) {
      if (!a || !b) return null;
      return calcDistanceKm(a.lat, a.lng, b.lat, b.lng);
    }

    // 2-optで改善（近い順ベースの経路をちょい改善）
    function twoOpt_(nodes) {
      // nodes: [{idx, ll}]
      if (nodes.length <= 3) return nodes;

      function routeLen(arr) {
        var sum = 0;
        for (var i = 0; i < arr.length - 1; i++) {
          var d = distKmByLL_(arr[i].ll, arr[i+1].ll);
          if (d != null) sum += d;
        }
        return sum;
      }

      var best = nodes.slice();
      var bestLen = routeLen(best);

      var improved = true;
      var guard = 0;
      while (improved && guard < 50) {
        guard++;
        improved = false;

        for (var i = 1; i < best.length - 2; i++) {
          for (var k = i + 1; k < best.length - 1; k++) {
            var cand = best.slice(0, i)
              .concat(best.slice(i, k + 1).reverse())
              .concat(best.slice(k + 1));
            var candLen = routeLen(cand);
            if (candLen + 1e-9 < bestLen) {
              best = cand;
              bestLen = candLen;
              improved = true;
            }
          }
        }
      }
      return best;
    }

    // 近傍法（スタート地点から近い順に作る）
    function nearestNeighbor_(nodes, startLL) {
      if (nodes.length <= 1) return nodes.slice();

      var rest = nodes.slice();
      var route = [];

      var currLL = startLL || rest[0].ll;
      while (rest.length > 0) {
        var bestIdx = 0;
        var bestDist = Infinity;
        for (var i = 0; i < rest.length; i++) {
          var d = distKmByLL_(currLL, rest[i].ll);
          // 位置が取れない場合は後回しになりやすいように大きめ
          var dd = (d == null ? 99999 : d);
          if (dd < bestDist) {
            bestDist = dd;
            bestIdx = i;
          }
        }
        var pick = rest.splice(bestIdx, 1)[0];
        route.push(pick);
        currLL = pick.ll || currLL;
      }
      return route;
    }

    // 2名体制の行はルート最適化の並べ替え対象外（アンカー化用セット）
    var coupledPlacedSet = {};
    Object.keys(coupledVisitMap).forEach(function(baseId) {
      coupledVisitMap[baseId].forEach(function(idx) { coupledPlacedSet[idx] = true; });
    });

    // staffDateMap を使って「スタッフ×日」ごとに処理
    Object.keys(staffDateMap).forEach(function(key){
      var idxList = staffDateMap[key];
      if (!idxList || idxList.length === 0) return;

      var staffId = key.split('|')[0];

      // スタッフ基点（lat/lng）: スタッフマスタの緯度経度
      var staffLL = null;
      for (var si = 0; si < staffList.length; si++) {
        if (staffList[si].id === staffId) {
          var slat = Number(staffList[si].lat);
          var slng = Number(staffList[si].lng);
          if (!isNaN(slat) && !isNaN(slng)) staffLL = { lat: slat, lng: slng };
          break;
        }
      }

      // 予定を「アンカー」と「可動」に分ける
      var anchors = []; // {idx, s, e}
      var flex = [];    // {idx, row, s, e, svcMin, ll}

      idxList.forEach(function(rIdx){
        var row = resultRows[rIdx];
        if (!row) return;

        // 未割当はスキップ
        if (!row[3] || String(row[4] || '') === '未割当') return;

        var isEv = isEventRow_(row);
        var timeType = row[11];
        var s = toMinutes(row[8]);
        var e = toMinutes(row[9]);

        var earliestMin = toMinutes(row[12]);
        var latestMin   = toMinutes(row[13]);

        // アンカー：EV or 固定（時間確定）
        if (isEv || (timeType === '固定' && s != null && e != null && e > s)) {
          if (s != null && e != null && e > s) anchors.push({ idx: rIdx, s: s, e: e });
          return;
        }

        // 2名体制の行はアンカーとして扱う（ルート最適化で並べ替えない）
        if (coupledPlacedSet[rIdx]) {
          if (s != null && e != null && e > s) anchors.push({ idx: rIdx, s: s, e: e });
          return;
        }

        // "安全版"なので、希望最早/最遅があるものは並べ替え対象外（そのまま）
        if (earliestMin != null || latestMin != null) {
          // 触らないが「可動」なので隙間詰め対象にはなり得る
          // ここでは順序最適化から除外するため anchors 側に入れて固定扱いにする
          if (s != null && e != null && e > s) anchors.push({ idx: rIdx, s: s, e: e });
          return;
        }

        var svcMin = Number(row[10]) || 30;
        var ll = getRowLatLng_(row);
        flex.push({ idx: rIdx, row: row, s: s, e: e, svcMin: svcMin, ll: ll });
      });

      if (flex.length <= 2) return; // 少なければ意味が薄い

      // アンカー時刻順
      anchors.sort(function(a,b){ return a.s - b.s; });

      // スタッフのシフト範囲
      var shift = getStaffShift_(staffId);
      var dayStart = (shift.shiftStartMin != null ? shift.shiftStartMin : 540);
      var dayEnd   = (shift.shiftEndMin   != null ? shift.shiftEndMin   : 1080);

      // ★暫定ガード：訪問看護は基本 09:00 以降とする（8時台吸着の防止）
      dayStart = Math.max(dayStart, 9 * 60);

      // "隙間"を作成（アンカー前後にバッファを確保）
      var gaps = [];
      var cursor = dayStart;
      anchors.forEach(function(a){
        // アンカー開始前にバッファを空ける
        var gapEnd = a.s - EXTRA_BUFFER_MIN;
        if (cursor < gapEnd) gaps.push({ s: cursor, e: gapEnd });
        // アンカー終了後にバッファを空けて次へ
        cursor = Math.max(cursor, a.e + EXTRA_BUFFER_MIN);
      });
      if (cursor < dayEnd) gaps.push({ s: cursor, e: dayEnd });

      if (gaps.length === 0) return;

      // いったん flex を「所属gap」に振り分け（現状時刻で判定、取れないものは最初のgap）
      var flexByGap = gaps.map(function(){ return []; });

      flex.forEach(function(f){
        var placed = false;
        if (f.s != null && f.e != null) {
          for (var gi = 0; gi < gaps.length; gi++) {
            var g = gaps[gi];
            if (f.s >= g.s && f.e <= g.e) { flexByGap[gi].push(f); placed = true; break; }
          }
        }
        if (!placed) flexByGap[0].push(f);
      });

      // 各gap内で順序を最適化 → gap先頭から詰め直す
      for (var gi = 0; gi < gaps.length; gi++) {
        var g = gaps[gi];
        var list = flexByGap[gi];
        if (!list || list.length <= 1) continue;

        // llが無いものがあると距離最適化が弱いので、ll無しは末尾へ
        var withLL = list.filter(function(x){ return !!x.ll; });
        var noLL   = list.filter(function(x){ return !x.ll; });

        if (withLL.length >= 2) {
          var nn = nearestNeighbor_(withLL, staffLL);
          var opt = twoOpt_(nn);
          list = opt.concat(noLL);
        } else {
          list = withLL.concat(noLL);
        }

        // gapの先頭から詰め直し（バッファ込み）
        var t = g.s;
        for (var li = 0; li < list.length; li++) {
          var f = list[li];
          var s2 = t;
          var e2 = s2 + f.svcMin;

          if (e2 > g.e) {
            // 入らないなら触らない（安全優先）
            continue;
          }
          setRowTimeByMinutes_(f.row, s2, e2);
          t = e2 + EXTRA_BUFFER_MIN;
        }
      }
    });
  }

  // Level3距離最適化を実行
  applyLevel3RouteOptimizeSafe_();

  // --- Level 3最適化後の2名体制ペア同期（最終同期） ---
  syncCoupledVisitTimes_(resultRows, coupledVisitMap, staffDateMap, tz);

  // ============================================================
  // 出力前に「時刻セル」をシリアル(0〜1)へ正規化（型混在対策）
  // ============================================================
  function normalizeTimeCellToSerial_(v) {
    var m = parseTimeToMinutes_(v);      // 既存のパーサを利用（Date/文字/数値すべて対応）
    if (m == null) return '';
    return minutesToSerial_(m);          // 必ずシリアルに統一
  }

  for (var i = 0; i < resultRows.length; i++) {
    var r = resultRows[i];

    // 開始/終了
    r[8] = normalizeTimeCellToSerial_(r[8]);
    r[9] = normalizeTimeCellToSerial_(r[9]);

    // 希望最早/希望最遅（空でもOK）
    r[12] = normalizeTimeCellToSerial_(r[12]);
    r[13] = normalizeTimeCellToSerial_(r[13]);
  }

  // ★同行展開（新人がmentorと同じ動き）
  var res同行 = applyStaff同行_(ss, resultRows);
  console.log(res同行.message);

  // 割当結果シートに書き込み
  resultSheet.clear();
  var header = ['visit_id','日付','曜日','staff_id','スタッフ名','patient_id','患者名','エリア',
                '開始時刻','終了時刻','サービス時間','時間タイプ','希望最早時刻','希望最遅時刻','備考',
                '前訪問ID','移動距離(km)','移動時間(分)'];
  resultSheet.getRange(1, 1, 1, header.length).setValues([header]);

  if (resultRows.length > 0) {
    var outRows = resultRows.map(function(r, i){ return r.concat([prevVisitIdArr[i], moveKmArr[i], moveMinArr[i]]); });
    resultSheet.getRange(2, 1, outRows.length, header.length).setValues(outRows);
  }

  // 訪問履歴へ追加
  if (resultRows.length > 0) {
    var lastRow = historySheet.getLastRow();
    var histRows = resultRows.map(function(r){ return [r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[14]]; });
    historySheet.getRange(lastRow + 1, 1, histRows.length, 12).setValues(histRows);
  }

  // 割当不可シートへ出力
  var ngSheet = ss.getSheetByName('割当不可');
  if (!ngSheet) ngSheet = ss.insertSheet('割当不可');
  ngSheet.clear();
  ngSheet.getRange(1, 1, 1, 7).setValues([['日付', '曜日', 'patient_id', '患者名', '必要スタッフ数', '未割当枠', '理由']]);
  if (unassignedList.length > 0) {
    var ngOut = unassignedList.map(function(x){ return [x.date, x.youbi, x.pid, x.pname, x.needStaff, x.slot, x.reason]; });
    ngSheet.getRange(2, 1, ngOut.length, 7).setValues(ngOut);
  }

  return { message: '割当結果を ' + resultRows.length + ' 件作成しました。割当不可: ' + unassignedList.length + ' 件' };
}

// ============================================================
// 同行展開（割当結果の後処理）
// - mentorの予定を trainee にコピーして同行を実現
// - 休み優先：mentor側にその日の予定が無ければ同行生成しない
// - 終日/午前/午後（将来拡張用）を最小限サポート
// ============================================================
function applyStaff同行_(ss, resultRows, opts) {
  opts = opts || {};
  var tz = ss.getSpreadsheetTimeZone();

  var staffSheet = ss.getSheetByName('スタッフマスタ');
  var pairSheet  = ss.getSheetByName('スタッフ同行割付'); // ←シート名はこれで固定想定

  if (!pairSheet) {
    // 同行設定が無いなら何もしない
    return { added: 0, removed: 0, message: 'スタッフ同行割付シートが無いので同行展開をスキップしました。' };
  }

  // ----------------------------------------------------------
  // staff_id -> {name, gender} map
  // ----------------------------------------------------------
  var staffMap = {};
  if (staffSheet && staffSheet.getLastRow() > 1) {
    var sValues = staffSheet.getDataRange().getValues();
    var sHeader = sValues[0];
    var sData   = sValues.slice(1);
    var idxId   = sHeader.indexOf('staff_id');
    var idxName = sHeader.indexOf('スタッフ名');
    var idxGen  = sHeader.indexOf('性別');
    sData.forEach(function(r) {
      var id = r[idxId];
      if (!id) return;
      staffMap[id] = {
        name: idxName >= 0 ? (r[idxName] || '') : '',
        gender: idxGen >= 0 ? (r[idxGen] || '') : ''
      };
    });
  }

  // ----------------------------------------------------------
  // helper: parse date (string/date)
  // ----------------------------------------------------------
  function parseDate_(v) {
    if (!v) return null;
    if (v instanceof Date) return v;
    var s = String(v).trim();
    var m = s.match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})$/);
    if (m) {
      var dt = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
      return isNaN(dt.getTime()) ? null : dt;
    }
    var dt2 = new Date(s);
    return isNaN(dt2.getTime()) ? null : dt2;
  }

  function dateStr_(d) {
    return Utilities.formatDate(d, tz, 'yyyy/MM/dd');
  }

  // 時刻→分（resultRowsはシリアル or Date or 文字が混じる可能性があるので最小限対応）
  function toMinutes_(v) {
    if (v === null || v === undefined || v === '') return null;
    if (typeof v === 'number') {
      // 0〜1はシリアル時刻
      if (v >= 0 && v < 1) return Math.round(v * 24 * 60);
      // それ以外は分扱いの可能性があるが安全に %1440
      return Math.round(v * 24 * 60) % 1440;
    }
    if (v instanceof Date) return v.getHours() * 60 + v.getMinutes();
    var s = String(v).trim();
    var m = s.match(/^(\d{1,2}):(\d{2})$/);
    if (m) return Number(m[1]) * 60 + Number(m[2]);
    m = s.match(/(\d{1,2}):(\d{2})/);
    if (m) return Number(m[1]) * 60 + Number(m[2]);
    return null;
  }

  // 時間帯フィルタ（終日/午前/午後）
  function isInTimeBand_(row, band) {
    band = String(band || '').trim();
    if (!band || band === '終日') return true;

    var s = toMinutes_(row[8]);
    var e = toMinutes_(row[9]);
    if (s == null || e == null) {
      // 時刻未確定は「終日以外」では扱いづらいので含めない（必要なら後で拡張）
      return false;
    }

    // 午前: ～12:00開始まで / 午後: 12:00以降開始
    if (band === '午前') return s < 12 * 60;
    if (band === '午後') return s >= 12 * 60;

    // 将来拡張（時間帯 等）を入れるならここ
    return true;
  }

  // ----------------------------------------------------------
  // 既存visit_id集合（重複防止）
  // ----------------------------------------------------------
  var existingVid = {};
  resultRows.forEach(function(r) {
    if (r && r[0]) existingVid[String(r[0])] = true;
  });

  function newVisitId_(baseVid, traineeId) {
    // 例: V012 -> V012_T_S006, EV_E003 -> EV_E003_T_S006
    var cand = String(baseVid) + '_T_' + traineeId;
    if (!existingVid[cand]) { existingVid[cand] = true; return cand; }

    // 念のため連番回避
    var k = 2;
    while (k < 9999) {
      var c2 = String(baseVid) + '_T_' + traineeId + '_' + k;
      if (!existingVid[c2]) { existingVid[c2] = true; return c2; }
      k++;
    }
    // 最悪
    cand = 'T_' + traineeId + '_' + Utilities.getUuid().slice(0, 8);
    existingVid[cand] = true;
    return cand;
  }

  // ----------------------------------------------------------
  // スタッフ同行割付シートを読み、(trainee|date)-> {mentor, band, priority} に展開
  // 期間指定（開始日〜終了日）を日別にばらす
  // ----------------------------------------------------------
  var pValues = pairSheet.getDataRange().getValues();
  if (pValues.length <= 1) {
    return { added: 0, removed: 0, message: 'スタッフ同行割付にデータが無いので同行展開をスキップしました。' };
  }
  var pHeader = pValues[0].map(function(x) { return String(x).trim(); });
  var pData   = pValues.slice(1);

  var idxTrainee = pHeader.indexOf('trainee_staff_id');
  var idxMentor  = pHeader.indexOf('mentor_staff_id');
  var idxStartD  = pHeader.indexOf('開始日');
  var idxEndD    = pHeader.indexOf('終了日');
  var idxBand    = pHeader.indexOf('時間帯');
  var idxPrio    = pHeader.indexOf('優先度');

  if (idxTrainee < 0 || idxMentor < 0 || idxStartD < 0 || idxEndD < 0) {
    throw new Error('スタッフ同行割付のヘッダーが不足しています（trainee_staff_id / mentor_staff_id / 開始日 / 終了日 は必須）');
  }

  // key: trainee|yyyy/MM/dd -> {mentorId, band, prio}
  var pairByDay = {};

  pData.forEach(function(r) {
    var traineeId = r[idxTrainee];
    var mentorId  = r[idxMentor];
    var sd = parseDate_(r[idxStartD]);
    var ed = parseDate_(r[idxEndD]);
    if (!traineeId || !mentorId || !sd || !ed) return;

    var band = idxBand >= 0 ? String(r[idxBand] || '').trim() : '終日';
    var prio = idxPrio >= 0 ? Number(r[idxPrio] || 1) : 1;

    // 日別に展開
    var d0 = new Date(sd); d0.setHours(0,0,0,0);
    var d1 = new Date(ed); d1.setHours(0,0,0,0);

    for (var d = new Date(d0); d <= d1; d.setDate(d.getDate() + 1)) {
      var key = traineeId + '|' + dateStr_(d);

      // 同一日に複数指定がある場合は「優先度が小さい方（1が最優先）」を採用
      if (!pairByDay[key] || prio < pairByDay[key].prio) {
        pairByDay[key] = { mentorId: mentorId, band: band || '終日', prio: prio };
      }
    }
  });

  // ----------------------------------------------------------
  // まず trainee の「その日」の既存行を削除（重複防止）
  // ※同行指定がある日のみ対象
  // ----------------------------------------------------------
  var targetKeys = {};
  Object.keys(pairByDay).forEach(function(k) { targetKeys[k] = true; }); // trainee|date
  var removed = 0;

  // 後ろから消す
  for (var i = resultRows.length - 1; i >= 0; i--) {
    var r = resultRows[i];
    if (!r) continue;
    var staffId = r[3];
    var d = r[1];
    if (!staffId || !(d instanceof Date)) continue;
    var k = staffId + '|' + dateStr_(d);
    if (targetKeys[k]) {
      // 同行指定がある日の trainee 行は全部いったん消す
      resultRows.splice(i, 1);
      removed++;
    }
  }

  // ----------------------------------------------------------
  // mentor行をコピーして trainee行を追加
  // 条件：mentor がその日に1件も無ければ「休み優先」なので追加しない
  // ----------------------------------------------------------
  var added = 0;

  Object.keys(pairByDay).forEach(function(k) {
    var sp = k.split('|');
    var traineeId = sp[0];
    var dStr = sp[1];
    var mentorId = pairByDay[k].mentorId;
    var band     = pairByDay[k].band;

    // mentor のその日の行を抽出
    var mentorRows = resultRows.filter(function(r) {
      if (!r) return false;
      if (r[3] !== mentorId) return false;
      if (!(r[1] instanceof Date)) return false;
      if (dateStr_(r[1]) !== dStr) return false;
      // 時間帯フィルタ
      return isInTimeBand_(r, band);
    });

    if (mentorRows.length === 0) {
      // mentorが休み/終日不可等で結果が無い → 同行生成しない（休み優先）
      return;
    }

    var tInfo = staffMap[traineeId] || { name: '', gender: '' };

    mentorRows.forEach(function(src) {
      // src = [visit_id, 日付, 曜日, staff_id, スタッフ名, patient_id, 患者名, エリア,
      //        開始時刻, 終了時刻, サービス時間, 時間タイプ, 希望最早時刻, 希望最遅時刻, 備考]
      var row = src.slice(); // shallow copy

      row[0] = newVisitId_(src[0], traineeId);
      row[3] = traineeId;
      row[4] = tInfo.name || ('(trainee)' + traineeId);

      // 備考に同行情報を追記（EVも含む）
      var mentorName = (staffMap[mentorId] && staffMap[mentorId].name) ? staffMap[mentorId].name : mentorId;
      var tag = ' / 同行(' + mentorId + ' ' + mentorName + ')';
      row[14] = (row[14] || '') + tag;

      resultRows.push(row);
      added++;
    });
  });

  // 仕上げ：日付→時刻→staff_id順に軽くソート（見た目安定）
  resultRows.sort(function(a,b) {
    var ad = (a[1] instanceof Date) ? a[1].getTime() : 0;
    var bd = (b[1] instanceof Date) ? b[1].getTime() : 0;
    if (ad !== bd) return ad - bd;
    var as = String(a[3] || '').localeCompare(String(b[3] || ''), 'ja');
    if (as !== 0) return as;
    var am = toMinutes_(a[8]);
    if (am == null) am = 9999;
    var bm = toMinutes_(b[8]);
    if (bm == null) bm = 9999;
    return am - bm;
  });

  return { added: added, removed: removed, message: '同行展開完了: 追加=' + added + ', 削除=' + removed };
}

// ============================================================
// 週間リクエストを生成（ss引数版）
// ============================================================

function 週間リクエストを生成_(ss, weekStartStr) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) throw new Error('別の処理が実行中です。少し待ってから再実行してください。');

  try {
    const tz = ss.getSpreadsheetTimeZone();

    const patientSheet = ss.getSheetByName('患者マスタ');
    const weeklySheet  = ss.getSheetByName('週間リクエスト');
    const changeSheet  = ss.getSheetByName('個別変更リクエスト');
    const assignSheet  = ss.getSheetByName('割当結果');
    const historySheet = ss.getSheetByName('訪問履歴');

    if (!patientSheet || !changeSheet || !weeklySheet) {
      throw new Error('患者マスタ / 個別変更リクエスト / 週間リクエスト のシート名を確認してください。');
    }

    const pValues = patientSheet.getDataRange().getValues();
    if (pValues.length <= 1) throw new Error('「患者マスタ」にデータがありません。');
    const pHeader = pValues[0];
    const pData   = pValues.slice(1);

    const requiredHeaders = ['patient_id','患者名','エリア','週訪問回数','希望曜日（複数可）',
      '希望時間帯（開始）','希望時間帯（終了）','曜日NG','性別制限','継続希望','サービス時間','必要スタッフ数',
      '指定スタッフID','指定タイプ','NGスタッフID','備考'];

    const idx = {};
    const missing = [];
    requiredHeaders.forEach(h => { const i = pHeader.indexOf(h); if (i === -1) missing.push(h); else idx[h] = i; });
    if (missing.length > 0) throw new Error('患者マスタのヘッダーが足りません：\n' + missing.join('\n'));

    const timeTypeColIndex = pHeader.indexOf('時間タイプ');
    // 訪問頻度・訪問週（オプション列）
    const visitFrequencyColIndex = pHeader.indexOf('訪問頻度');
    const visitWeeksColIndex = pHeader.indexOf('訪問週');

    const patientInfoMap = {};
    pData.forEach(row => {
      const pid = row[idx['patient_id']];
      if (!pid) return;
      patientInfoMap[pid] = {
        name: row[idx['患者名']] || '', area: row[idx['エリア']] || '', svcMin: row[idx['サービス時間']],
        needStaff: toHalfWidthNumber_(row[idx['必要スタッフ数']], 1), sexLimit: row[idx['性別制限']],
        contPref: row[idx['継続希望']], timeType: (timeTypeColIndex >= 0 ? row[timeTypeColIndex] : '') || '',
        startPref: row[idx['希望時間帯（開始）']], endPref: row[idx['希望時間帯（終了）']],
        staffIds: row[idx['指定スタッフID']] || '', staffType: row[idx['指定タイプ']] || '',
        ngStaffIds: row[idx['NGスタッフID']] || '', note: row[idx['備考']] || '',
        // 訪問頻度・訪問週
        visitFrequency: (visitFrequencyColIndex >= 0 ? row[visitFrequencyColIndex] : '') || '毎週',
        visitWeeks: (visitWeeksColIndex >= 0 ? row[visitWeeksColIndex] : '') || ''
      };
    });

    // weekStartStr が指定されていればその月曜を使用、なければ今週を自動算出
    let weekStart;
    if (weekStartStr) {
      weekStart = parseDateLoose_(weekStartStr);
      if (!weekStart) throw new Error('対象週の日付が不正です: ' + weekStartStr);
      weekStart.setHours(0,0,0,0);
      console.log('[週間リクエスト生成] 指定週=' + weekStartStr);
    } else {
      const today = new Date();
      today.setHours(0,0,0,0);
      const day = today.getDay();
      const diffToMonday = (day + 6) % 7;
      weekStart = new Date(today);
      weekStart.setDate(today.getDate() - diffToMonday);
      console.log('[週間リクエスト生成] 自動算出=今週');
    }
    const weekEnd = new Date(weekStart);
    weekEnd.setDate(weekStart.getDate() + 6);
    const weekStartFmt = Utilities.formatDate(weekStart, tz, 'yyyy/MM/dd');
    const weekEndFmt   = Utilities.formatDate(weekEnd, tz, 'yyyy/MM/dd');

    const lastVisitMap = {};
    if (historySheet) {
      const MAX_HISTORY_ROWS = 3000;
      const lastRow = historySheet.getLastRow();
      const lastCol = historySheet.getLastColumn();
      if (lastRow > 1 && lastCol > 0) {
        const startRow = Math.max(2, lastRow - MAX_HISTORY_ROWS + 1);
        const numRows = lastRow - startRow + 1;
        const hHeader = historySheet.getRange(1, 1, 1, lastCol).getValues()[0];
        const hData   = historySheet.getRange(startRow, 1, numRows, lastCol).getValues();
        const hIdxDate = hHeader.indexOf('日付'), hIdxPid = hHeader.indexOf('patient_id'),
              hIdxStaff = hHeader.indexOf('staff_id'), hIdxName = hHeader.indexOf('スタッフ名');
        hData.forEach(row => {
          const d = row[hIdxDate], pid = row[hIdxPid];
          if (!pid || !(d instanceof Date)) return;
          const ds = Utilities.formatDate(d, tz, 'yyyy/MM/dd');
          if (ds >= weekStartFmt) return;
          const current = lastVisitMap[pid];
          if (!current || d > current.date) lastVisitMap[pid] = { date: d, staffId: row[hIdxStaff] || '', staffName: row[hIdxName] || '' };
        });
      }
    }

    const youbiMap = { 'Sun': 0, 'Mon': 1, 'Tue': 2, 'Wed': 3, 'Thu': 4, 'Fri': 5, 'Sat': 6 };
    const indexToYoubi = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

    function parseDays(str) {
      if (!str) return [];
      const parts = String(str).split(/[,\u3001\/・\s]+/).map(s => s.trim()).filter(Boolean);
      const out = [];
      parts.forEach(p => { const y = normalizeYoubi(p); if (y && out.indexOf(y) === -1) out.push(y); });
      return out;
    }

    function toMinutes(v) {
      if (!v && v !== 0) return null;
      if (v instanceof Date) return v.getHours() * 60 + v.getMinutes();
      if (typeof v === 'number') return Math.round(v * 24 * 60);
      if (typeof v === 'string') { const parts = v.split(':'); if (parts.length >= 2) return Number(parts[0]) * 60 + Number(parts[1]); }
      return null;
    }

    function calcEndTime(startValue, minutes) {
      const m = Number(minutes || 0);
      if (!startValue || !m) return startValue;
      if (typeof startValue === 'number') return startValue + m / (24 * 60);
      else if (startValue instanceof Date) return new Date(startValue.getTime() + m * 60 * 1000);
      return startValue;
    }

    function makeTimeValue(h, m) { return (h * 60 + m) / (24 * 60); }

    function inferTimeType(timeTypeRaw, startPref, endPref, svcMin) {
      let t = (timeTypeRaw || '').trim();
      if (t) return t;
      if (!startPref || !endPref) return '固定';
      const s = toMinutes(startPref), e = toMinutes(endPref);
      if (s == null || e == null) return '固定';
      const span = e - s, svc = Number(svcMin || 0);
      if (!svc || span <= svc + 5) return '固定';
      if (s <= 9 * 60 + 15 && e >= 12 * 60 - 15) return '午前';
      if (s >= 13 * 60 - 15 && e <= 16 * 60 + 15) return '午後';
      if (span >= 7 * 60) return '終日';
      return '時間帯';
    }

    function makeTimeWindow(timeTypeRaw, startPref, endPref, svcMin) {
      const t = inferTimeType(timeTypeRaw, startPref, endPref, svcMin);
      let start = startPref, end = endPref, earliest = null, latest = null;
      if (t === '固定') { if (!end && start && svcMin) end = calcEndTime(start, svcMin); earliest = start; latest = end; }
      else if (t === '時間帯') { earliest = startPref; latest = endPref; if (!start && earliest) start = earliest; if (!end && start && svcMin) end = calcEndTime(start, svcMin); }
      else if (t === '午前') { earliest = makeTimeValue(9, 0); latest = makeTimeValue(12, 0); if (!start) start = earliest; if (!end && start && svcMin) end = calcEndTime(start, svcMin); }
      else if (t === '午後') { earliest = makeTimeValue(13, 0); latest = makeTimeValue(17, 0); if (!start) start = earliest; if (!end && start && svcMin) end = calcEndTime(start, svcMin); }
      else if (t === '終日') { earliest = makeTimeValue(9, 0); latest = makeTimeValue(18, 0); if (!start) start = earliest; if (!end && start && svcMin) end = calcEndTime(start, svcMin); }
      else { if (!end && start && svcMin) end = calcEndTime(start, svcMin); earliest = start; latest = end; }
      return { start, end, earliest, latest, timeType: t };
    }

    // 今週が何週目かを計算（月曜基準）
    const currentWeekOfMonth = getWeekOfMonth_(weekStart);

    const weeklyRequests = [];
    pData.forEach(row => {
      const pid = row[idx['patient_id']];
      if (!pid) return;
      const info = patientInfoMap[pid] || {};
      const visits = toHalfWidthNumber_(row[idx['週訪問回数']], 0);
      if (!visits || visits <= 0) return;

      // 訪問頻度・訪問週による判定
      if (!shouldVisitThisWeek_(info.visitFrequency, info.visitWeeks, currentWeekOfMonth)) {
        // この週は訪問しない患者なのでスキップ
        return;
      }

      const prefDays = parseDays(row[idx['希望曜日（複数可）']]);
      const ngDays = parseDays(row[idx['曜日NG']]);
      const allDays = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
      const availableDays = allDays.filter(d => !ngDays.includes(d));
      if (availableDays.length === 0) return;

      // 希望曜日を優先、足りなければ他の曜日から均等分散で補充
      const prefCandidates = prefDays.filter(d => !ngDays.includes(d));
      const nonPrefCandidates = availableDays.filter(d => !prefDays.includes(d));

      // 均等分散で曜日を選択する関数
      function selectDaysWithSpread(prefList, nonPrefList, targetCount) {
        const selected = [];
        const prefSorted = prefList.slice().sort((a, b) => youbiMap[a] - youbiMap[b]);

        // まず希望曜日を追加
        for (let i = 0; i < prefSorted.length && selected.length < targetCount; i++) {
          selected.push(prefSorted[i]);
        }

        // 足りなければ非希望曜日から均等分散で追加
        if (selected.length < targetCount && nonPrefList.length > 0) {
          const remaining = nonPrefList.slice();
          while (selected.length < targetCount && remaining.length > 0) {
            // 既存の選択曜日との最小距離が最大になる曜日を選ぶ
            let bestDay = null, bestMinDist = -1;
            for (let i = 0; i < remaining.length; i++) {
              const dayIdx = youbiMap[remaining[i]];
              let minDist = 7;
              for (let j = 0; j < selected.length; j++) {
                const selIdx = youbiMap[selected[j]];
                const dist = Math.min(Math.abs(dayIdx - selIdx), 7 - Math.abs(dayIdx - selIdx));
                if (dist < minDist) minDist = dist;
              }
              if (minDist > bestMinDist) { bestMinDist = minDist; bestDay = remaining[i]; }
            }
            if (bestDay) {
              selected.push(bestDay);
              remaining.splice(remaining.indexOf(bestDay), 1);
            } else break;
          }
        }
        return selected.sort((a, b) => youbiMap[a] - youbiMap[b]);
      }

      const actualVisits = Math.min(visits, availableDays.length);
      const candidates = selectDaysWithSpread(prefCandidates, nonPrefCandidates, actualVisits);

      const svcMin = info.svcMin, startPref = info.startPref, endPref = info.endPref, timeTypeRaw = info.timeType;
      const win = makeTimeWindow(timeTypeRaw, startPref, endPref, svcMin);
      const sexLimit = info.sexLimit, contPref = info.contPref, note = info.note || '';

      for (let i = 0; i < actualVisits; i++) {
        const youbi = candidates[i];
        const targetDay = youbiMap[youbi];
        for (let d = 0; d < 7; d++) {
          const dateObj = new Date(weekStart);
          dateObj.setDate(weekStart.getDate() + d);
          if (dateObj.getDay() === targetDay) {
            const dateStr = Utilities.formatDate(dateObj, tz, 'yyyy/MM/dd');
            const weekdayStr = Utilities.formatDate(dateObj, tz, 'EEE');
            const last = lastVisitMap[pid] || {};
            weeklyRequests.push({
              date: dateObj, dateStr: dateStr, weekdayStr: weekdayStr,
              patient_id: pid, patient_name: info.name || '', area: info.area || '',
              start: win.start, end: win.end, svcMin: svcMin, needStaff: info.needStaff || 1,
              specifiedIds: info.staffIds || '', specifiedType: info.staffType || '', ngStaffIds: info.ngStaffIds || '',
              sexLimit: sexLimit, contPref: contPref, changeType: '通常',
              prevStaffId: last.staffId || '', prevStaffName: last.staffName || '', prevDate: last.date || '',
              timeType: win.timeType, earliest: win.earliest, latest: win.latest, note: note
            });
            break;
          }
        }
      }
    });

    const weeklyMap = {};
    weeklyRequests.forEach(req => {
      const key = req.patient_id + '|' + req.dateStr;
      if (!weeklyMap[key]) weeklyMap[key] = [];
      weeklyMap[key].push(req);
    });

    // ============================================================
    // 個別変更リクエストの適用（堅牢版）
    // - 日付が文字でも拾う
    // - ヘッダーの表記ゆれに強い
    // - 操作値の表記ゆれ（空白等）を吸収
    // ============================================================
    if (changeSheet) {
      const cValues = changeSheet.getDataRange().getValues();
      if (cValues.length > 1) {
        const cHeaderRaw = cValues[0];
        const cHeader = cHeaderRaw.map(x => String(x || '').trim());
        const cData = cValues.slice(1);

        function findHeaderIdx_(headers, target) {
          // 完全一致
          let i = headers.indexOf(target);
          if (i >= 0) return i;
          // 部分一致（target を含む or target が含む）
          for (let k = 0; k < headers.length; k++) {
            const h = String(headers[k] || '').trim();
            if (!h) continue;
            if (h.indexOf(target) >= 0 || target.indexOf(h) >= 0) return k;
          }
          return -1;
        }

        const cIdx = {
          patient_id: findHeaderIdx_(cHeader, 'patient_id'),
          name:       findHeaderIdx_(cHeader, '患者名'),
          date:       findHeaderIdx_(cHeader, '日付'),
          op:         findHeaderIdx_(cHeader, '操作'), // ←「操作（...）」に固定しない
          newStart:   findHeaderIdx_(cHeader, '開始時刻(固定)'),
          newEnd:     findHeaderIdx_(cHeader, '終了時刻(固定)'),
          newTimeType: findHeaderIdx_(cHeader, '時間タイプ'),
          newEarliest: findHeaderIdx_(cHeader, '希望最早'),
          newLatest:   findHeaderIdx_(cHeader, '希望最遅'),
          note:       findHeaderIdx_(cHeader, '備考'),
          regAt:      findHeaderIdx_(cHeader, '登録日時')
        };

        // 必須列チェック（最低限）
        if (cIdx.patient_id < 0 || cIdx.date < 0 || cIdx.op < 0) {
          throw new Error(
            '個別変更リクエストのヘッダーが不足しています。\n' +
            '必須: patient_id / 日付 / 操作\n' +
            '現在のヘッダー: ' + cHeader.join(', ')
          );
        }

        const changeMap = {}; // key: pid|yyyy/MM/dd -> latest record

        cData.forEach((row, idxRow) => {
          const pid = row[cIdx.patient_id];
          if (!pid) return;

          // op 正規化（空白吸収＆表記ゆれの最小対応）
          let op = row[cIdx.op];
          op = String(op || '').trim();
          if (!op) return;
          // 表記ゆれ補正（必要なら増やせます）
          if (op === '取消') op = 'キャンセル';

          const dRaw = row[cIdx.date];
          const d = parseDateLoose_(dRaw);
          if (!d) return;

          const dateStr = Utilities.formatDate(d, tz, 'yyyy/MM/dd');
          if (dateStr < weekStartFmt || dateStr > weekEndFmt) return;

          const key = String(pid).trim() + '|' + dateStr;

          let sortKey = idxRow;
          if (cIdx.regAt >= 0) {
            const r = parseDateLoose_(row[cIdx.regAt]);
            if (r) sortKey = r.getTime();
          }

          const change = {
            pid: String(pid).trim(),
            op: op,
            date: d,
            dateStr: dateStr,
            newStart: (cIdx.newStart >= 0 ? row[cIdx.newStart] : ''),
            newEnd:   (cIdx.newEnd >= 0 ? row[cIdx.newEnd] : ''),
            newTimeType: (cIdx.newTimeType >= 0 ? String(row[cIdx.newTimeType] || '').trim() : ''),
            newEarliest: (cIdx.newEarliest >= 0 ? row[cIdx.newEarliest] : ''),
            newLatest:   (cIdx.newLatest >= 0 ? row[cIdx.newLatest] : ''),
            note:     (cIdx.note >= 0 ? row[cIdx.note] : ''),
            patient_name: (cIdx.name >= 0 ? row[cIdx.name] : ''),
            sortKey: sortKey
          };

          if (!changeMap[key] || sortKey > changeMap[key].sortKey) changeMap[key] = change;
        });

        // console.log('[ChangeRequest] picked=', Object.keys(changeMap).length, 'week=', weekStartFmt, '-', weekEndFmt);

        Object.keys(changeMap).forEach(key => {
          const ch = changeMap[key];
          const matches = weeklyMap[ch.pid + '|' + ch.dateStr] || [];

          if (ch.op === 'キャンセル') {
            matches.forEach(req => {
              req.changeType = 'キャンセル';
              if (ch.note) req.note = ch.note;
            });

          } else if (ch.op === '時間変更') {
            matches.forEach(req => {
              // 新時間タイプが指定されている場合、時間帯設定を上書き
              if (ch.newTimeType) {
                const win = makeTimeWindow(ch.newTimeType, ch.newEarliest || ch.newStart, ch.newLatest || ch.newEnd, req.svcMin);
                req.timeType = win.timeType;
                req.earliest = win.earliest;
                req.latest = win.latest;
                req.start = win.start;
                req.end = win.end;
              } else {
                // 従来の固定時間変更
                if (ch.newStart) req.start = ch.newStart;
                let endTime = ch.newEnd;
                if (!endTime && ch.newStart) endTime = calcEndTime(ch.newStart, req.svcMin);
                if (endTime) req.end = endTime;
              }
              req.changeType = '変更';
              if (ch.note) req.note = ch.note;
            });

          } else if (ch.op === '追加') {
            const baseInfo = patientInfoMap[ch.pid] || {};
            const weekdayStr = indexToYoubi[ch.date.getDay()];
            const svcMin  = baseInfo.svcMin || '';
            // 新時間タイプが指定されていればそれを優先、なければ患者マスタの設定を使用
            const useTimeType = ch.newTimeType || baseInfo.timeType;
            let startTime = ch.newEarliest || ch.newStart || baseInfo.startPref;
            let endTime   = ch.newLatest || ch.newEnd || baseInfo.endPref;
            const win = makeTimeWindow(useTimeType, startTime, endTime, svcMin);

            const newReq = {
              date: ch.date, dateStr: ch.dateStr, weekdayStr: weekdayStr,
              patient_id: ch.pid, patient_name: ch.patient_name || baseInfo.name || '',
              area: baseInfo.area || '',
              start: win.start, end: win.end,
              svcMin: svcMin, needStaff: baseInfo.needStaff || 1,
              specifiedIds: baseInfo.staffIds || '', specifiedType: baseInfo.staffType || '', ngStaffIds: baseInfo.ngStaffIds || '',
              sexLimit: baseInfo.sexLimit || '', contPref: baseInfo.contPref || '',
              changeType: '追加',
              note: ch.note || baseInfo.note || '',
              prevStaffId: '', prevStaffName: '', prevDate: null,
              timeType: win.timeType, earliest: win.earliest, latest: win.latest
            };

            weeklyRequests.push(newReq);
            const mapKey = ch.pid + '|' + ch.dateStr;
            if (!weeklyMap[mapKey]) weeklyMap[mapKey] = [];
            weeklyMap[mapKey].push(newReq);
          }
        });
      }
    }

    // 割当結果から前回担当を付与
    const lastAssignMap = {};
    if (assignSheet) {
      const MAX_ASSIGN_ROWS = 2000;
      const lastRow = assignSheet.getLastRow(), lastCol = assignSheet.getLastColumn();
      if (lastRow > 1 && lastCol > 0) {
        const startRow = Math.max(2, lastRow - MAX_ASSIGN_ROWS + 1), numRows = lastRow - startRow + 1;
        const aHeader = assignSheet.getRange(1, 1, 1, lastCol).getValues()[0];
        const aData = assignSheet.getRange(startRow, 1, numRows, lastCol).getValues();
        const aIdxDate = aHeader.indexOf('日付'), aIdxPid = aHeader.indexOf('patient_id'),
              aIdxSid = aHeader.indexOf('staff_id'), aIdxSname = aHeader.indexOf('スタッフ名');
        aData.forEach(row => {
          const d = row[aIdxDate], pid = row[aIdxPid];
          if (!(d instanceof Date) || !pid) return;
          if (d >= weekStart) return;
          const dow = d.getDay(), key = pid + '|' + dow;
          const current = lastAssignMap[key];
          if (!current || d > current.date) lastAssignMap[key] = { staffId: row[aIdxSid], staffName: row[aIdxSname], date: d };
        });
      }
    }

    weeklyRequests.forEach(req => {
      const dow = req.date.getDay(), key = req.patient_id + '|' + dow;
      const last = lastAssignMap[key];
      if (last) { req.prevStaffId = last.staffId || ''; req.prevStaffName = last.staffName || ''; req.prevDate = last.date || null; }
    });

    // ★特別訪問週間を適用（ADD/REPLACE + 個別変更扱い）
    try {
      var merged = applySpecialWeekToWeeklyRequests_({
        ss: ss, tz: tz,
        weekStart: weekStart, weekEnd: weekEnd,
        weeklyRequests: weeklyRequests, weeklyMap: weeklyMap,
        patientInfoMap: patientInfoMap,
        makeTimeWindow: makeTimeWindow,
      });
      // Logger.log('[SpecialWeek] added=' + merged.added + ' removed=' + merged.removed);
    } catch (swErr) {
      console.warn('特別訪問週間の適用でエラー（スキップ）:', swErr);
    }

    if (weeklyRequests.length === 0) {
      throw new Error('週間リクエスト候補が0件でした。\n・「週訪問回数」が0または空ではないか\n・「希望曜日」と「曜日NG」の組み合わせで候補が消えていないか\n・対象週の範囲（' + weekStartFmt + '〜' + weekEndFmt + '）でよいか\nなどを確認してください。');
    }

    weeklyRequests.sort((a, b) => {
      if (a.date - b.date !== 0) return a.date - b.date;
      const am = toMinutes(a.start), bm = toMinutes(b.start);
      if (am == null && bm == null) return 0;
      if (am == null) return 1;
      if (bm == null) return -1;
      return am - bm;
    });

    weeklySheet.clear();
    const headerOut = ['request_id','日付','曜日','patient_id','患者名','エリア',
      '開始時刻','終了時刻','サービス時間','必要スタッフ数','指定スタッフID','指定タイプ','NGスタッフID',
      '性別制限','継続希望','変更区分（通常/変更/追加/キャンセル）',
      '前回担当スタッフID','前回担当スタッフ名','前回訪問日','時間タイプ','希望最早時刻','希望最遅時刻','備考'];
    weeklySheet.getRange(1, 1, 1, headerOut.length).setValues([headerOut]);

    const out = weeklyRequests.map((req, i) => ([
      'R' + Utilities.formatString('%03d', i+1), req.date, req.weekdayStr, req.patient_id, req.patient_name, req.area,
      req.start, req.end, req.svcMin, req.needStaff || 1, req.specifiedIds || '', req.specifiedType || '', req.ngStaffIds || '',
      req.sexLimit, req.contPref, req.changeType, req.prevStaffId || '', req.prevStaffName || '', req.prevDate || '',
      req.timeType || '', req.earliest || '', req.latest || '', req.note
    ]));

    weeklySheet.getRange(2, 1, out.length, headerOut.length).setValues(out);

    return { message: '週間リクエストを ' + weekStartFmt + ' 〜 ' + weekEndFmt + ' 分、' + weeklyRequests.length + ' 件生成しました。' };

  } finally {
    lock.releaseLock();
  }
}

// ============================================================
// 位置情報を更新（ss引数版）
// ============================================================

function 位置情報を更新_(ss) {
  updateSheetLatLng_(ss.getSheetByName('患者マスタ'), '住所', '緯度', '経度');
  updateSheetLatLng_(ss.getSheetByName('スタッフマスタ'), '拠点住所', '緯度', '経度');
  return { message: '位置情報を更新しました' };
}

function updateSheetLatLng_(sheet, addrHeader, latHeader, lngHeader) {
  if (!sheet) return;
  const values = sheet.getDataRange().getValues();
  if (values.length <= 1) return;
  const header = values[0], data = values.slice(1);
  const idxAddr = header.indexOf(addrHeader), idxLat = header.indexOf(latHeader), idxLng = header.indexOf(lngHeader);
  if (idxAddr === -1 || idxLat === -1 || idxLng === -1) return;

  const geocoder = Maps.newGeocoder();
  let changed = false;
  data.forEach((row, i) => {
    const addr = row[idxAddr], lat = row[idxLat], lng = row[idxLng];
    if (addr && (lat === '' || lng === '' || lat == null || lng == null)) {
      const res = geocoder.geocode(addr);
      if (res.status === 'OK' && res.results && res.results.length > 0) {
        const loc = res.results[0].geometry.location;
        data[i][idxLat] = loc.lat;
        data[i][idxLng] = loc.lng;
        changed = true;
      }
      Utilities.sleep(200);
    }
  });
  if (changed) sheet.getRange(2, 1, data.length, header.length).setValues(data);
}

// ============================================================
// ルートサマリを作成（ss引数版）
// ============================================================

function ルートサマリを作成_(ss, weekStartStr) {
  const tz = ss.getSpreadsheetTimeZone();
  const resultSheet = ss.getSheetByName('割当結果');
  const patientSheet = ss.getSheetByName('患者マスタ');
  const staffSheet = ss.getSheetByName('スタッフマスタ');

  if (!resultSheet) throw new Error('「割当結果」シートが見つかりません。');
  if (!patientSheet) throw new Error('「患者マスタ」シートが見つかりません。');
  if (!staffSheet) throw new Error('「スタッフマスタ」シートが見つかりません。');

  // スタッフマスタから拠点情報を取得
  const sValues = staffSheet.getDataRange().getValues();
  const sHeader = sValues[0];
  const sIdx = { id: sHeader.indexOf('staff_id'), addr: sHeader.indexOf('拠点住所'),
                 lat: sHeader.indexOf('緯度'), lng: sHeader.indexOf('経度') };
  const staffMap = {};
  for (let i = 1; i < sValues.length; i++) {
    const row = sValues[i], sid = row[sIdx.id];
    if (!sid) continue;
    staffMap[sid] = { addr: row[sIdx.addr] || '', lat: row[sIdx.lat] || '', lng: row[sIdx.lng] || '' };
  }

  const pValues = patientSheet.getDataRange().getValues();
  const pHeader = pValues[0];
  const pIdx = { pid: pHeader.indexOf('patient_id'), addr: pHeader.indexOf('住所'),
                 lat: pHeader.indexOf('緯度'), lng: pHeader.indexOf('経度') };
  if (pIdx.pid === -1 || pIdx.addr === -1 || pIdx.lat === -1 || pIdx.lng === -1) {
    throw new Error('「患者マスタ」のヘッダー名（patient_id, 住所, 緯度, 経度）を確認してください。');
  }

  const patientMap = {};
  for (let i = 1; i < pValues.length; i++) {
    const row = pValues[i], pid = row[pIdx.pid];
    if (!pid) continue;
    patientMap[pid] = { addr: row[pIdx.addr] || '', lat: row[pIdx.lat] || '', lng: row[pIdx.lng] || '' };
  }

  const values = resultSheet.getDataRange().getValues();
  if (values.length <= 1) throw new Error('「割当結果」にデータがありません。');

  const header = values[0], data = values.slice(1);
  const idx = { date: header.indexOf('日付'), youbi: header.indexOf('曜日'), staffId: header.indexOf('staff_id'),
                sname: header.indexOf('スタッフ名'), dist: header.indexOf('移動距離(km)'), mtime: header.indexOf('移動時間(分)'),
                pid: header.indexOf('patient_id'), pname: header.indexOf('患者名'), start: header.indexOf('開始時刻') };

  if (idx.date === -1 || idx.staffId === -1 || idx.sname === -1 || idx.dist === -1 || idx.mtime === -1 ||
      idx.pid === -1 || idx.pname === -1 || idx.start === -1) {
    throw new Error('「割当結果」のヘッダー名を確認してください。');
  }

  // weekStartStr が指定されていれば週フィルタ用の範囲を計算
  let filterStartStr = null, filterEndStr = null;
  if (weekStartStr) {
    const ws = parseDateLoose_(weekStartStr);
    if (!ws) throw new Error('対象週の日付が不正です: ' + weekStartStr);
    ws.setHours(0,0,0,0);
    const we = new Date(ws); we.setDate(ws.getDate() + 6);
    filterStartStr = Utilities.formatDate(ws, tz, 'yyyy/MM/dd');
    filterEndStr   = Utilities.formatDate(we, tz, 'yyyy/MM/dd');
    console.log('[ルートサマリ作成] 週フィルタ=' + filterStartStr + '〜' + filterEndStr);
  } else {
    console.log('[ルートサマリ作成] 全データ対象');
  }

  const map = {};
  data.forEach(row => {
    const d = row[idx.date];
    if (!(d instanceof Date)) return;
    const staffId = row[idx.staffId], staffName = row[idx.sname];
    if (!staffId) return;
    const dateStr = Utilities.formatDate(d, tz, 'yyyy/MM/dd'), youbi = row[idx.youbi];
    // 週フィルタ（指定時のみ）
    if (filterStartStr && (dateStr < filterStartStr || dateStr > filterEndStr)) return;
    const distKm = Number(row[idx.dist] || 0), moveMin = Number(row[idx.mtime] || 0);
    const key = staffId + '|' + dateStr;

    if (!map[key]) map[key] = { staffId, staffName, dateObj: d, dateStr, youbi,
                                visitCount: 0, moveCount: 0, distTotal: 0, timeTotal: 0, visits: [] };
    const rec = map[key];
    rec.visitCount++;
    if (distKm > 0 || moveMin > 0) { rec.moveCount++; rec.distTotal += distKm; rec.timeTotal += moveMin; }
    rec.visits.push({ start: row[idx.start], pid: row[idx.pid], pname: row[idx.pname] });
  });

  const records = Object.keys(map).map(k => map[k]);
  records.sort((a, b) => { if (a.staffName !== b.staffName) return a.staffName > b.staffName ? 1 : -1; return a.dateObj - b.dateObj; });

  let summarySheet = ss.getSheetByName('ルートサマリ');
  if (!summarySheet) summarySheet = ss.insertSheet('ルートサマリ');
  summarySheet.clear();

  const outHeader = ['staff_id','スタッフ名','日付','曜日','訪問件数','移動回数（前訪問あり）',
                     '総移動距離(km)','総移動時間(分)','ルート順（No. 患者ID 患者名 住所 (緯度, 経度)）'];
  summarySheet.getRange(1, 1, 1, outHeader.length).setValues([outHeader]);

  if (records.length > 0) {
    const out = records.map(r => {
      const visits = r.visits.slice().sort((a, b) => {
        if (!a.start && !b.start) return 0; if (!a.start) return 1; if (!b.start) return -1; return a.start - b.start;
      });
      // 出発点（営業所）をNo.0として追加
      const staff = staffMap[r.staffId] || {};
      const startPoint = 'No.0 ' + r.staffId + ' 出発 ' + (staff.addr || '') + ' (' + (staff.lat || '') + ', ' + (staff.lng || '') + ')';
      const visitTexts = visits.map((v, idx) => {
        const p = patientMap[v.pid] || {};
        return 'No.' + (idx + 1) + ' ' + v.pid + ' ' + v.pname + ' ' + (p.addr || '') + ' (' + (p.lat || '') + ', ' + (p.lng || '') + ')';
      });
      const routeText = [startPoint].concat(visitTexts).join(' → ');
      return [r.staffId, r.staffName, r.dateObj, r.youbi, r.visitCount, r.moveCount, r.distTotal, r.timeTotal, routeText];
    });
    summarySheet.getRange(2, 1, out.length, outHeader.length).setValues(out);
  }

  return { message: 'ルートサマリを ' + records.length + ' 行作成しました。' };
}


// ============================================================
// シート生成ユーティリティ
// ============================================================

/**
 * イベントリクエストシートを作成
 * GASエディタから直接実行可能
 */
function createEventRequestSheet() {
  var ssId = PropertiesService.getScriptProperties().getProperty("SS_ID");
  if (!ssId) {
    throw new Error("SS_ID が設定されていません");
  }
  var ss = SpreadsheetApp.openById(ssId);
  
  var sheetName = "イベントリクエスト";
  var existingSheet = ss.getSheetByName(sheetName);
  if (existingSheet) {
    throw new Error("シート「" + sheetName + "」は既に存在します");
  }
  
  var sheet = ss.insertSheet(sheetName);
  
  var headers = [
    "event_id",
    "日付",
    "曜日",
    "staff_id",
    "スタッフ名",
    "イベント種別",
    "タイトル",
    "住所",
    "緯度",
    "経度",
    "時間指定方法",
    "開始時刻",
    "終了時刻",
    "所要時間(分)",
    "固定枠",
    "患者紐づき",
    "patient_id",
    "患者影響",
    "事前事務所戻り",
    "事後事務所戻り",
    "理由",
    "備考"
  ];
  
  sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  
  // ヘッダー行の書式設定
  var headerRange = sheet.getRange(1, 1, 1, headers.length);
  headerRange.setBackground("#4a7c59");
  headerRange.setFontColor("#ffffff");
  headerRange.setFontWeight("bold");
  
  // 列幅の調整
  sheet.setColumnWidth(1, 80);   // event_id
  sheet.setColumnWidth(2, 100);  // 日付
  sheet.setColumnWidth(3, 50);   // 曜日
  sheet.setColumnWidth(4, 80);   // staff_id
  sheet.setColumnWidth(5, 100);  // スタッフ名
  sheet.setColumnWidth(6, 120);  // イベント種別
  sheet.setColumnWidth(7, 150);  // タイトル
  sheet.setColumnWidth(8, 200);  // 住所
  sheet.setColumnWidth(11, 100); // 時間指定方法
  sheet.setColumnWidth(14, 100); // 所要時間(分)
  sheet.setColumnWidth(21, 200); // 理由
  sheet.setColumnWidth(22, 200); // 備考
  
  // 1行目を固定
  sheet.setFrozenRows(1);
  
  Logger.log("シート「" + sheetName + "」を作成しました");
  return "シート「" + sheetName + "」を作成しました";
}


// ============================================================
// 特別訪問週間：シート生成/初期化（ヘッダ/明細の2シート構成）
// ============================================================

function ensureSpecialWeekSheets_(ss) {
  // ヘッダシート（ユーザー指定の列順序）
  var headerName = '特別訪問週間_ヘッダ';
  var headerSh = ss.getSheetByName(headerName);
  if (!headerSh) headerSh = ss.insertSheet(headerName);
  if (headerSh.getLastRow() === 0) {
    var hCols = ['special_week_id','patient_id','患者名','週開始日','週終了日','適用モード(ADD/REPLACE)','理由','状態','登録日時'];
    headerSh.getRange(1,1,1,hCols.length).setValues([hCols]);
    headerSh.setFrozenRows(1);
  }

  // 明細シート（明細A仕様：15列）
  var detailName = '特別訪問週間_明細';
  var detailSh = ss.getSheetByName(detailName);
  if (!detailSh) detailSh = ss.insertSheet(detailName);
  if (detailSh.getLastRow() === 0) {
    var dCols = ['special_week_id','patient_id','日付','曜日','行ラベル','時間タイプ','開始時刻','終了時刻','希望最早','希望最遅','サービス時間','必要スタッフ数','備考','個別変更の扱い','更新日時'];
    detailSh.getRange(1,1,1,dCols.length).setValues([dCols]);
    detailSh.setFrozenRows(1);
  }

  return { header: headerSh, detail: detailSh };
}

// ============================================================
// 旧明細→明細A（15列）へのマイグレーション
// ============================================================
function migrateSpecialWeekDetailToNewFormat_(ss) {
  var tz = ss.getSpreadsheetTimeZone();
  var headerSh = ss.getSheetByName('特別訪問週間_ヘッダ');
  var detailSh = ss.getSheetByName('特別訪問週間_明細');
  if (!detailSh || detailSh.getLastRow() <= 1) return { migrated: false, message: '明細データなし' };

  var dValues = detailSh.getDataRange().getValues();
  var dHeader = dValues[0];

  // 新フォーマットチェック（special_week_idがあれば移行済み）
  if (dHeader.indexOf('special_week_id') >= 0) {
    return { migrated: false, message: '既に新フォーマット' };
  }

  // 旧フォーマットのインデックス
  var oldIdx = {
    specialId: dHeader.indexOf('special_id'),
    date: dHeader.indexOf('日付'),
    rowLabel: dHeader.indexOf('行ラベル'),
    timeType: dHeader.indexOf('timeType'),
    start: dHeader.indexOf('開始時刻'),
    end: dHeader.indexOf('終了時刻'),
    earliest: dHeader.indexOf('希望最早時刻'),
    latest: dHeader.indexOf('希望最遅時刻'),
    svcMin: dHeader.indexOf('サービス時間'),
    note: dHeader.indexOf('備考')
  };

  // ヘッダシートからpatient_id、個別変更扱いを取得（JOINデータ）
  var headerMap = {};
  if (headerSh && headerSh.getLastRow() > 1) {
    var hValues = headerSh.getDataRange().getValues();
    var hHeader = hValues[0];
    var hIdx = {
      specialId: hHeader.indexOf('special_id'),
      pid: hHeader.indexOf('patient_id'),
      changeHandle: hHeader.indexOf('個別変更扱い')
    };
    hValues.slice(1).forEach(function(r) {
      var spId = r[hIdx.specialId];
      if (spId) {
        headerMap[spId] = {
          patient_id: r[hIdx.pid] || '',
          changeHandle: r[hIdx.changeHandle] || 'そのまま残す'
        };
      }
    });
  }

  // 曜日変換関数（Mon/Tue/Wed/Thu/Fri/Sat/Sun）
  function getDayOfWeekStr(dateObj) {
    if (!dateObj) return '';
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    return days[dateObj.getDay()];
  }

  // 新フォーマットデータ作成
  var newHeader = ['special_week_id','patient_id','日付','曜日','行ラベル','時間タイプ','開始時刻','終了時刻','希望最早','希望最遅','サービス時間','必要スタッフ数','備考','個別変更の扱い','更新日時'];
  var newData = [newHeader];
  var now = new Date();

  dValues.slice(1).forEach(function(r) {
    var spId = r[oldIdx.specialId];
    if (!spId) return;
    var hInfo = headerMap[spId] || { patient_id: '', changeHandle: 'そのまま残す' };

    var dateVal = r[oldIdx.date];
    var dateObj = parseDateLoose_(dateVal);
    var dayOfWeek = getDayOfWeekStr(dateObj);

    newData.push([
      spId,                                           // special_week_id
      hInfo.patient_id,                               // patient_id
      dateVal,                                        // 日付
      dayOfWeek,                                      // 曜日
      r[oldIdx.rowLabel] || '特別1',                  // 行ラベル
      r[oldIdx.timeType] || '時間帯',                 // 時間タイプ（名称変更）
      r[oldIdx.start] || '',                          // 開始時刻
      r[oldIdx.end] || '',                            // 終了時刻
      r[oldIdx.earliest] || '',                       // 希望最早（名称短縮）
      r[oldIdx.latest] || '',                         // 希望最遅（名称短縮）
      r[oldIdx.svcMin] || '',                         // サービス時間
      1,                                              // 必要スタッフ数（デフォルト1）
      r[oldIdx.note] || '',                           // 備考
      hInfo.changeHandle,                             // 個別変更の扱い
      now                                             // 更新日時
    ]);
  });

  // シートを更新
  detailSh.clear();
  if (newData.length > 0) {
    detailSh.getRange(1, 1, newData.length, newHeader.length).setValues(newData);
    detailSh.setFrozenRows(1);
  }

  return { migrated: true, message: 'マイグレーション完了: ' + (newData.length - 1) + '行', rowCount: newData.length - 1 };
}

// 後方互換性のため旧関数も残す
function ensureSpecialWeekSheet_(ss) {
  var sheets = ensureSpecialWeekSheets_(ss);
  return sheets.header;
}

function parseDateLoose_(v) {
  if (!v) return null;
  if (v instanceof Date) return new Date(v.getFullYear(), v.getMonth(), v.getDate());
  var s = String(v).trim();
  // "2026/01/12" "2026-01-12" "2026/01/10 9:00" 等を許容（時刻は捨てて日付だけ）
  var m = s.match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$/);
  if (m) {
    var dt = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return isNaN(dt.getTime()) ? null : dt;
  }
  var dt2 = new Date(s);
  return isNaN(dt2.getTime()) ? null : dt2;
}

function parseTimeCell_(v) {
  if (v === '' || v == null) return '';
  if (v instanceof Date) return (v.getHours() * 60 + v.getMinutes()) / (24 * 60);
  if (typeof v === 'number') return v;
  var s = String(v).trim();
  var m = s.match(/^(\d{1,2}):(\d{2})(?::\d{2})?$/);
  if (!m) return v;
  var h = Number(m[1]), mi = Number(m[2]);
  return (h * 60 + mi) / (24 * 60);
}

function yyyymmdd_(d, tz) {
  return Utilities.formatDate(d, tz, 'yyyy/MM/dd');
}

function normalizeChangePolicy_(v) {
  var s = (v == null ? '' : String(v)).trim();
  if (!s) return 'そのまま残す';
  if (s.indexOf('上書') >= 0 || s.indexOf('置換') >= 0) return '上書き';
  if (s.indexOf('無視') >= 0) return '無視';
  return 'そのまま残す';
}

function timeToMinutesLoose_(v) {
  if (v === null || v === undefined || v === '') return null;

  if (typeof v === 'number') {
    if (v >= 0 && v < 1) return Math.round(v * 24 * 60);
    if (v >= 1 && v < 1440) return Math.round(v);
    return Math.round(v * 24 * 60) % 1440;
  }
  if (v instanceof Date) return v.getHours() * 60 + v.getMinutes();

  var s = String(v).trim();
  var m = s.match(/^(\d{1,2}):(\d{2})$/);
  if (m) return Number(m[1]) * 60 + Number(m[2]);
  m = s.match(/^(\d{1,2}):(\d{2}):(\d{2})$/);
  if (m) return Number(m[1]) * 60 + Number(m[2]);
  m = s.match(/(\d{1,2})時(\d{1,2})分/);
  if (m) return Number(m[1]) * 60 + Number(m[2]);
  m = s.match(/^(\d{1,2})時$/);
  if (m) return Number(m[1]) * 60;

  m = s.match(/(\d{1,2}):(\d{2})/);
  if (m) return Number(m[1]) * 60 + Number(m[2]);

  return null;
}

function minutesToSerial_(min) {
  if (min == null) return '';
  return min / (24 * 60);
}

function serialToTimeStr_(serial, tz) {
  if (serial === '' || serial === null || serial === undefined) return '';
  var m = timeToMinutesLoose_(serial);
  if (m == null) return '';
  var hh = String(Math.floor(m / 60)).padStart(2,'0');
  var mm = String(m % 60).padStart(2,'0');
  return hh + ':' + mm;
}

function getWeekRangeFromMonday_(mondayDate) {
  var start = new Date(mondayDate);
  start.setHours(0,0,0,0);
  var end = new Date(start);
  end.setDate(start.getDate() + 6);
  return { start: start, end: end };
}

// ============================================================
// Special Week loader（ヘッダ/明細シートからデータ取得）
// ============================================================
function loadSpecialWeekForWeek_(ss, weekStart, weekEnd, tz) {
  var SHEET_H = '特別訪問週間_ヘッダ';
  var SHEET_D = '特別訪問週間_明細';

  var shH = ss.getSheetByName(SHEET_H);
  var shD = ss.getSheetByName(SHEET_D);
  if (!shH || !shD) return { headers: [], details: [] };

  var hValues = shH.getDataRange().getValues();
  var dValues = shD.getDataRange().getValues();
  if (hValues.length <= 1) return { headers: [], details: [] };

  var hHeader = hValues[0];
  var hData = hValues.slice(1);
  var dHeader = dValues.length ? dValues[0] : [];
  var dData = dValues.length > 1 ? dValues.slice(1) : [];

  var idxH = {};
  hHeader.forEach(function(k, i) { idxH[k] = i; });
  var idxD = {};
  dHeader.forEach(function(k, i) { idxD[k] = i; });

  // 新旧両フォーマット対応（special_week_id or special_id）
  var hIdColName = idxH['special_week_id'] != null ? 'special_week_id' : 'special_id';
  var dIdColName = idxD['special_week_id'] != null ? 'special_week_id' : 'special_id';
  // モード列名（適用モード(ADD/REPLACE) or モード）
  var hModeColName = idxH['適用モード(ADD/REPLACE)'] != null ? '適用モード(ADD/REPLACE)' : 'モード';
  // 時間タイプ列名（時間タイプ or timeType）
  var dTimeTypeColName = idxD['時間タイプ'] != null ? '時間タイプ' : 'timeType';
  // 希望最早/最遅列名
  var dEarliestColName = idxD['希望最早'] != null ? '希望最早' : '希望最早時刻';
  var dLatestColName = idxD['希望最遅'] != null ? '希望最遅' : '希望最遅時刻';

  var weekStartStr = yyyymmdd_(weekStart, tz);

  // 状態=有効 & 週開始日一致
  var headers = [];
  hData.forEach(function(r) {
    var id = r[idxH[hIdColName]];
    if (!id) return;

    var ws = parseDateLoose_(r[idxH['週開始日']]);
    var we = idxH['週終了日'] != null ? parseDateLoose_(r[idxH['週終了日']]) : null;
    if (!we && ws) {
      we = new Date(ws);
      we.setDate(we.getDate() + 6);
    }

    var status = idxH['状態'] != null ? String(r[idxH['状態']] || '').trim() : '有効';
    if (status && status !== '有効') return;

    var wsStr = ws ? yyyymmdd_(ws, tz) : '';
    if (wsStr !== weekStartStr) return;

    var modeRaw = r[idxH[hModeColName]] || '';
    var mode = String(modeRaw).trim().toUpperCase();
    if (mode === '追加' || mode === 'ADD') mode = 'ADD';
    else if (mode === '置換' || mode === 'REPLACE') mode = 'REPLACE';
    else mode = 'ADD';

    headers.push({
      special_week_id: id,
      patient_id: r[idxH['patient_id']],
      patient_name: idxH['患者名'] != null ? (r[idxH['患者名']] || '') : '',
      week_start: ws,
      week_end: we,
      mode: mode,
      reason: idxH['理由'] != null ? (r[idxH['理由']] || '') : '',
      status: status,
      reg_at: idxH['登録日時'] != null ? r[idxH['登録日時']] : ''
    });
  });

  if (headers.length === 0) return { headers: [], details: [] };

  var idSet = {};
  headers.forEach(function(h) { idSet[h.special_week_id] = true; });

  var details = [];
  dData.forEach(function(r) {
    var id = r[idxD[dIdColName]];
    if (!id || !idSet[id]) return;

    var dateObj = parseDateLoose_(r[idxD['日付']]);
    if (!dateObj || dateObj < weekStart || dateObj > weekEnd) return;

    details.push({
      special_week_id: id,
      patient_id: r[idxD['patient_id']],
      date: dateObj,
      youbi: idxD['曜日'] != null ? (r[idxD['曜日']] || '') : '',
      row_label: idxD['行ラベル'] != null ? (r[idxD['行ラベル']] || '') : '',
      time_type: r[idxD[dTimeTypeColName]] || '時間帯',
      start: idxD['開始時刻'] != null ? parseTimeCell_(r[idxD['開始時刻']]) : '',
      end: idxD['終了時刻'] != null ? parseTimeCell_(r[idxD['終了時刻']]) : '',
      earliest: idxD[dEarliestColName] != null ? parseTimeCell_(r[idxD[dEarliestColName]]) : '',
      latest: idxD[dLatestColName] != null ? parseTimeCell_(r[idxD[dLatestColName]]) : '',
      svc_min: r[idxD['サービス時間']],
      need_staff: r[idxD['必要スタッフ数']],
      note: idxD['備考'] != null ? (r[idxD['備考']] || '') : '',
      change_policy: normalizeChangePolicy_(idxD['個別変更の扱い'] != null ? r[idxD['個別変更の扱い']] : ''),
      updated_at: idxD['更新日時'] != null ? r[idxD['更新日時']] : ''
    });
  });

  return { headers: headers, details: details };
}

function _weekStartMonday_(baseDate) {
  var d = new Date(baseDate);
  d.setHours(0,0,0,0);
  var day = d.getDay();
  var diffToMonday = (day + 6) % 7;
  d.setDate(d.getDate() - diffToMonday);
  return d;
}

// ============================================================
// 特別訪問週間：UI用 API（新仕様：2シート構成）
// ============================================================

/**
 * 特別訪問週間のコンテキスト取得（タブ/ウィザード用）
 * payload: { weekStartStr, patient_id }（patient_id省略時は全患者）
 */
function api_getSpecialWeekContext_(payload) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var tz = ss.getSpreadsheetTimeZone();
  ensureSpecialWeekSheets_(ss);

  var patientSheet = ss.getSheetByName('患者マスタ');
  var changeSheet  = ss.getSheetByName('個別変更リクエスト');
  if (!patientSheet) throw new Error('患者マスタがありません');
  if (!changeSheet) throw new Error('個別変更リクエストがありません');

  var weekStartStr = payload.weekStartStr;
  var patientId = payload.patient_id || null;

  var weekStart = parseDateLoose_(weekStartStr);
  if (!weekStart) throw new Error('週開始日が不正です: ' + weekStartStr);

  var range = getWeekRangeFromMonday_(weekStart);
  var start = range.start;
  var end = range.end;
  var startStr = Utilities.formatDate(start, tz, 'yyyy/MM/dd');
  var endStr   = Utilities.formatDate(end,   tz, 'yyyy/MM/dd');

  // 患者情報取得
  var patient = null;
  if (patientId) {
    var pValues = patientSheet.getDataRange().getValues();
    var pHeader = pValues[0];
    var pData   = pValues.slice(1);
    var pIdx = {
      pid: pHeader.indexOf('patient_id'),
      name: pHeader.indexOf('患者名'),
      visits: pHeader.indexOf('週訪問回数'),
      prefDays: pHeader.indexOf('希望曜日（複数可）'),
      ngDays: pHeader.indexOf('曜日NG'),
      svcMin: pHeader.indexOf('サービス時間'),
      timeType: pHeader.indexOf('時間タイプ'),
      startPref: pHeader.indexOf('希望時間帯（開始）'),
      endPref: pHeader.indexOf('希望時間帯（終了）'),
    };
    if (pIdx.pid < 0) throw new Error('患者マスタに patient_id がありません');

    for (var i = 0; i < pData.length; i++) {
      if (String(pData[i][pIdx.pid]||'') === String(patientId)) {
        patient = {
          patient_id: patientId,
          patient_name: pData[i][pIdx.name] || '',
          visits: Number(pData[i][pIdx.visits] || 0) || 0,
          prefDays: pData[i][pIdx.prefDays] || '',
          ngDays: pData[i][pIdx.ngDays] || '',
          svcMin: Number(pData[i][pIdx.svcMin] || 0) || 0,
          timeType: pData[i][pIdx.timeType] || '',
          startPref: pData[i][pIdx.startPref],
          endPref: pData[i][pIdx.endPref]
        };
        break;
      }
    }
    if (!patient) throw new Error('患者が見つかりません: ' + patientId);
  }

  // 日付配列を生成
  var days = [];
  for (var i = 0; i < 7; i++) {
    var d = new Date(start);
    d.setDate(start.getDate() + i);
    days.push({
      dateStr: Utilities.formatDate(d, tz, 'yyyy/MM/dd'),
      label: Utilities.formatDate(d, tz, 'MM/dd(EEE)')
    });
  }

  // 通常予定のプレビュー（patientId指定時のみ）
  var normalByDate = {};
  if (patient) {
    function parseDays_(str) {
      if (!str) return [];
      var parts = String(str).split(/[,\u3001\/・\s]+/).map(function(s) { return s.trim(); }).filter(Boolean);
      var out = [];
      parts.forEach(function(p) {
        var y = normalizeYoubi(p);
        if (y && out.indexOf(y) === -1) out.push(y);
      });
      return out;
    }

    function makeTimeValue_(h, m) { return (h*60+m) / (24*60); }

    function inferTimeType_(timeTypeRaw, startPref, endPref, svcMin) {
      var t = (timeTypeRaw || '').trim();
      if (t) return t;
      if (!startPref || !endPref) return '固定';
      var s = timeToMinutesLoose_(startPref), e = timeToMinutesLoose_(endPref);
      if (s == null || e == null) return '固定';
      var span = e - s, svc = Number(svcMin || 0);
      if (!svc || span <= svc + 5) return '固定';
      if (s <= 9*60+15 && e >= 12*60-15) return '午前';
      if (s >= 13*60-15 && e <= 17*60+15) return '午後';
      if (span >= 7*60) return '終日';
      return '時間帯';
    }

    var prefDays = parseDays_(patient.prefDays);
    var ngDays = parseDays_(patient.ngDays);
    if (prefDays.length === 0) prefDays = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    var candidates = prefDays.filter(function(d) { return ngDays.indexOf(d) < 0; });
    var actual = Math.min(patient.visits, candidates.length);

    var timeType = inferTimeType_(patient.timeType, patient.startPref, patient.endPref, patient.svcMin);

    for (var i = 0; i < actual; i++) {
      var youbi = candidates[i];
      for (var d = 0; d < 7; d++) {
        var dateObj = new Date(start);
        dateObj.setDate(start.getDate() + d);
        var y = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][dateObj.getDay()];
        if (y === youbi) {
          var ds = Utilities.formatDate(dateObj, tz, 'yyyy/MM/dd');
          if (!normalByDate[ds]) normalByDate[ds] = [];
          var e1 = serialToTimeStr_(patient.startPref, tz);
          var e2 = serialToTimeStr_(patient.endPref, tz);
          var text = '';
          if (timeType === '固定' && patient.startPref && patient.endPref) {
            text = e1 + '〜' + e2 + ' (通常)';
          } else {
            text = e1 + '〜' + e2 + ' の間で ' + (patient.svcMin||'') + '分 (通常)';
          }
          normalByDate[ds].push({ type:'通常', text: text });
          break;
        }
      }
    }
  }

  // 個別変更リクエスト（その週の対象患者のみ）
  var changeByDate = {};
  if (patientId) {
    var cValues = changeSheet.getDataRange().getValues();
    var cHeader = cValues[0];
    var cData   = cValues.slice(1);
    var cIdx = {
      pid: cHeader.indexOf('patient_id'),
      date: cHeader.indexOf('日付'),
      op: cHeader.indexOf('操作（キャンセル/時間変更/追加）'),
      newStart: cHeader.indexOf('開始時刻(固定)'),
      newEnd: cHeader.indexOf('終了時刻(固定)'),
      note: cHeader.indexOf('備考'),
    };

    cData.forEach(function(r) {
      var pid = r[cIdx.pid];
      var d = r[cIdx.date];
      if (String(pid||'') !== String(patientId)) return;

      // 日付をより柔軟に解析（Date型でなくても対応）
      var dObj = (d instanceof Date) ? d : parseDateLoose_(d);
      if (!dObj) return;
      var ds = Utilities.formatDate(dObj, tz, 'yyyy/MM/dd');
      if (ds < startStr || ds > endStr) return;

      var op = String(r[cIdx.op]||'').trim();
      var s = r[cIdx.newStart];
      var e = r[cIdx.newEnd];
      var text = '';
      if (op === 'キャンセル') text = 'キャンセル(個別変更)';
      else if (op === '時間変更') text = serialToTimeStr_(s, tz) + '〜' + serialToTimeStr_(e, tz) + ' (時間変更)';
      else if (op === '追加') text = serialToTimeStr_(s, tz) + '〜' + serialToTimeStr_(e, tz) + ' (追加)';
      else text = op + '(個別変更)';

      if (!changeByDate[ds]) changeByDate[ds] = [];
      changeByDate[ds].push({ type:'個別変更', op: op, text: text, keep:true, note: r[cIdx.note] || '' });
    });
  }

  // 特別訪問週間（ヘッダ/明細シートから読み込み）
  var specialEntries = [];
  var headerSh = ss.getSheetByName('特別訪問週間_ヘッダ');
  var detailSh = ss.getSheetByName('特別訪問週間_明細');

  if (headerSh && headerSh.getLastRow() > 1) {
    var hValues = headerSh.getDataRange().getValues();
    var hHeader = hValues[0];
    var hData = hValues.slice(1);
    // 新旧両フォーマット対応
    var isNewHeaderFormat = hHeader.indexOf('special_week_id') >= 0;
    var hModeCol = hHeader.indexOf('適用モード(ADD/REPLACE)') >= 0 ? '適用モード(ADD/REPLACE)' : 'モード';
    var hIdx = {
      specialId: isNewHeaderFormat ? hHeader.indexOf('special_week_id') : hHeader.indexOf('special_id'),
      weekStart: hHeader.indexOf('週開始日'),
      weekEnd: hHeader.indexOf('週終了日'),
      pid: hHeader.indexOf('patient_id'),
      pname: hHeader.indexOf('患者名'),
      mode: hHeader.indexOf(hModeCol),
      changeHandle: hHeader.indexOf('個別変更扱い'),
      reason: hHeader.indexOf('理由'),
      status: hHeader.indexOf('状態'),
    };

    // 明細を読み込み（新旧両フォーマット対応）
    var detailMap = {};
    if (detailSh && detailSh.getLastRow() > 1) {
      var dValues = detailSh.getDataRange().getValues();
      var dHeader = dValues[0];
      var dData = dValues.slice(1);
      // 新フォーマット（special_week_id）か旧フォーマット（special_id）かを判定
      var isNewFormat = dHeader.indexOf('special_week_id') >= 0;
      var dIdx = {
        specialId: isNewFormat ? dHeader.indexOf('special_week_id') : dHeader.indexOf('special_id'),
        patientId: dHeader.indexOf('patient_id'),
        date: dHeader.indexOf('日付'),
        dayOfWeek: dHeader.indexOf('曜日'),
        rowLabel: dHeader.indexOf('行ラベル'),
        timeType: isNewFormat ? dHeader.indexOf('時間タイプ') : dHeader.indexOf('timeType'),
        start: dHeader.indexOf('開始時刻'),
        end: dHeader.indexOf('終了時刻'),
        earliest: isNewFormat ? dHeader.indexOf('希望最早') : dHeader.indexOf('希望最早時刻'),
        latest: isNewFormat ? dHeader.indexOf('希望最遅') : dHeader.indexOf('希望最遅時刻'),
        svcMin: dHeader.indexOf('サービス時間'),
        needStaff: dHeader.indexOf('必要スタッフ数'),
        note: dHeader.indexOf('備考'),
        changeHandle: dHeader.indexOf('個別変更の扱い'),
        updatedAt: dHeader.indexOf('更新日時'),
      };

      dData.forEach(function(r) {
        var spId = r[dIdx.specialId];
        if (!spId) return;
        if (!detailMap[spId]) detailMap[spId] = [];

        var dObj = parseDateLoose_(r[dIdx.date]);
        if (!dObj) return;
        var ds = Utilities.formatDate(dObj, tz, 'yyyy/MM/dd');

        detailMap[spId].push({
          dateStr: ds,
          dayOfWeek: dIdx.dayOfWeek >= 0 ? (r[dIdx.dayOfWeek] || '') : '',
          rowLabel: r[dIdx.rowLabel] || '特別1',
          timeType: r[dIdx.timeType] || '時間帯',
          start: serialToTimeStr_(r[dIdx.start], tz),
          end: serialToTimeStr_(r[dIdx.end], tz),
          earliest: serialToTimeStr_(r[dIdx.earliest], tz),
          latest: serialToTimeStr_(r[dIdx.latest], tz),
          svcMin: r[dIdx.svcMin],
          needStaff: dIdx.needStaff >= 0 ? (Number(r[dIdx.needStaff]) || 1) : 1,
          note: r[dIdx.note] || '',
          changeHandle: dIdx.changeHandle >= 0 ? (r[dIdx.changeHandle] || 'そのまま残す') : 'そのまま残す'
        });
      });
    }

    // ヘッダをフィルタして明細を結合（状態=有効のみ）
    hData.forEach(function(r) {
      // 状態チェック（「有効」のみ採用、列が無い場合は全て採用）
      if (hIdx.status >= 0) {
        var status = String(r[hIdx.status] || '').trim();
        if (status && status !== '有効') return;
      }

      // patient_id指定時はフィルタ
      if (patientId && String(r[hIdx.pid]||'') !== String(patientId)) return;

      var ws = r[hIdx.weekStart];
      var wsObj = parseDateLoose_(ws);
      if (!wsObj) return;
      var wsStr = Utilities.formatDate(wsObj, tz, 'yyyy/MM/dd');
      if (wsStr !== startStr) return;

      var spId = r[hIdx.specialId];
      var details = detailMap[spId] || [];

      // 週終了日を取得
      var we = hIdx.weekEnd >= 0 ? r[hIdx.weekEnd] : null;
      var weObj = we ? ((we instanceof Date) ? we : parseDateLoose_(we)) : null;
      var weStr = weObj ? Utilities.formatDate(weObj, tz, 'yyyy/MM/dd') : wsStr;

      specialEntries.push({
        special_id: spId,
        patient_id: r[hIdx.pid],
        patient_name: r[hIdx.pname] || '',
        weekStartStr: wsStr,
        weekEndStr: weStr,
        mode: r[hIdx.mode] || '追加',
        changeHandle: r[hIdx.changeHandle] || '残す',
        reason: r[hIdx.reason] || '',
        status: hIdx.status >= 0 ? (r[hIdx.status] || '有効') : '有効',
        details: details
      });
    });
  }

  return {
    weekStartStr: startStr,
    weekEndStr: endStr,
    patient: patient,
    days: days,
    normalByDate: normalByDate,
    changeByDate: changeByDate,
    specialEntries: specialEntries
  };
}

// 後方互換性のためのラッパー（ウィザード用形式に変換）
function api_getSpecialWeekWizardData(weekStartStr, patientId) {
  var result = api_getSpecialWeekContext_({ weekStartStr: weekStartStr, patient_id: patientId });

  // ウィザードが期待する形式に変換（明細A対応：needStaff, changeHandle追加）
  var specialRows = [];
  (result.specialEntries || []).forEach(function(entry) {
    (entry.details || []).forEach(function(d) {
      specialRows.push({
        dateStr: d.dateStr,
        rowLabel: d.rowLabel,
        mode: entry.mode,
        reason: entry.reason,
        timeType: d.timeType,
        earliest: d.earliest,
        latest: d.latest,
        svcMin: d.svcMin,
        needStaff: d.needStaff || 1,
        start: d.start,
        end: d.end,
        note: d.note,
        changeHandle: d.changeHandle || 'そのまま残す'
      });
    });
  });

  return {
    weekStartStr: result.weekStartStr,
    weekEndStr: result.weekEndStr,
    patientId: result.patient ? result.patient.patient_id : patientId,
    patientName: result.patient ? result.patient.patient_name : '',
    days: result.days,
    normalByDate: result.normalByDate,
    changesByDate: result.changeByDate,  // 複数形に変換
    specialRows: specialRows,
    specialEntries: result.specialEntries  // シートUI用（ヘッダ+明細構造）
  };
}

// ============================================================
// 特別訪問週間：保存API（ヘッダ/明細の2シート構成）
// ============================================================

function api_saveSpecialWeekWizard(payload) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var tz = ss.getSpreadsheetTimeZone();
  var sheets = ensureSpecialWeekSheets_(ss);
  var headerSh = sheets.header;
  var detailSh = sheets.detail;

  var weekStart = parseDateLoose_(payload.weekStartStr);
  if (!weekStart) throw new Error('週開始日が不正です');
  var weekStartStr = Utilities.formatDate(weekStart, tz, 'yyyy/MM/dd');

  var pid = payload.patientId;
  if (!pid) throw new Error('patientIdが空です');

  // ヘッダシートから既存のspecial_week_idを取得（削除対象）（新旧フォーマット対応）
  var hValues = headerSh.getDataRange().getValues();
  var hHeader = hValues[0];
  var hData = hValues.slice(1);
  var isNewHeaderFormat = hHeader.indexOf('special_week_id') >= 0;
  var hIdx = {
    specialId: isNewHeaderFormat ? hHeader.indexOf('special_week_id') : hHeader.indexOf('special_id'),
    weekStart: hHeader.indexOf('週開始日'),
    pid: hHeader.indexOf('patient_id'),
  };

  var deleteIds = [];
  var keepHeader = [hHeader];
  hData.forEach(function(r) {
    var ws = parseDateLoose_(r[hIdx.weekStart]);
    var wsStr = ws ? Utilities.formatDate(ws, tz, 'yyyy/MM/dd') : '';
    var rpid = r[hIdx.pid];
    if (String(rpid||'') === String(pid) && wsStr === weekStartStr) {
      deleteIds.push(r[hIdx.specialId]);
    } else {
      keepHeader.push(r);
    }
  });

  // ヘッダシートを更新
  headerSh.clear();
  headerSh.getRange(1,1,1,hHeader.length).setValues([hHeader]);
  if (keepHeader.length > 1) {
    headerSh.getRange(2,1,keepHeader.length-1,hHeader.length).setValues(keepHeader.slice(1));
  }

  // 明細シートから削除対象のspecial_idの行を削除（新旧フォーマット対応）
  if (deleteIds.length > 0 && detailSh.getLastRow() > 1) {
    var dValues = detailSh.getDataRange().getValues();
    var dHeader = dValues[0];
    var dData = dValues.slice(1);
    // special_week_id（新）または special_id（旧）を判定
    var isNewFormat = dHeader.indexOf('special_week_id') >= 0;
    var dIdx = { specialId: isNewFormat ? dHeader.indexOf('special_week_id') : dHeader.indexOf('special_id') };

    var keepDetail = [dHeader];
    dData.forEach(function(r) {
      if (deleteIds.indexOf(r[dIdx.specialId]) < 0) {
        keepDetail.push(r);
      }
    });

    detailSh.clear();
    detailSh.getRange(1,1,1,dHeader.length).setValues([dHeader]);
    if (keepDetail.length > 1) {
      detailSh.getRange(2,1,keepDetail.length-1,dHeader.length).setValues(keepDetail.slice(1));
    }
  }

  // 新規ヘッダ追加（新フォーマット）
  var now = new Date();
  var specialId = 'SW_' + Utilities.getUuid();

  // 週終了日を計算（週開始日 + 6日）
  var weekStartDate = parseDateLoose_(weekStartStr);
  var weekEndDate = new Date(weekStartDate);
  weekEndDate.setDate(weekEndDate.getDate() + 6);

  // モードを適用モード形式に変換
  var modeVal = (payload.mode || '追加').trim();
  if (modeVal === '追加') modeVal = 'ADD';
  else if (modeVal === '置換') modeVal = 'REPLACE';

  // ヘッダ列順（新フォーマット）: special_week_id, patient_id, 患者名, 週開始日, 週終了日, 適用モード(ADD/REPLACE), 理由, 状態, 登録日時
  var newHeaderRow = [
    specialId,
    pid,
    payload.patientName || '',
    weekStartDate,
    weekEndDate,
    modeVal,
    payload.reason || '',
    '有効',
    now
  ];
  headerSh.getRange(headerSh.getLastRow() + 1, 1, 1, newHeaderRow.length).setValues([newHeaderRow]);

  // 曜日変換関数（Mon/Tue/Wed/Thu/Fri/Sat/Sun）
  function getDayOfWeekStr(dateObj) {
    if (!dateObj) return '';
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    return days[dateObj.getDay()];
  }

  // モードに応じた個別変更の扱いデフォルト値
  var defaultChangeHandle = (payload.mode === '置換') ? '置換する' : 'そのまま残す';

  // 新規明細追加（明細A：15列フォーマット）
  var detailRows = [];
  (payload.items || []).forEach(function(it) {
    if (!it || !it.dateStr) return;

    var dateObj = parseDateLoose_(it.dateStr);
    if (!dateObj) return;

    var timeType = (it.timeType || '時間帯').trim();
    var earliestS = '', latestS = '', startS = '', endS = '';

    if (timeType === '固定') {
      var sm = timeToMinutesLoose_(it.start);
      var em = timeToMinutesLoose_(it.end);
      startS = minutesToSerial_(sm);
      endS = minutesToSerial_(em);
      earliestS = startS;
      latestS = endS;
    } else {
      var em1 = timeToMinutesLoose_(it.earliest);
      var em2 = timeToMinutesLoose_(it.latest);
      earliestS = minutesToSerial_(em1);
      latestS = minutesToSerial_(em2);
    }

    var svcMin = Number(it.svcMin || 0) || '';
    var needStaff = Number(it.needStaff || 1) || 1;
    var changeHandle = it.changeHandle || defaultChangeHandle;

    // 明細A（15列）：special_week_id, patient_id, 日付, 曜日, 行ラベル, 時間タイプ, 開始時刻, 終了時刻, 希望最早, 希望最遅, サービス時間, 必要スタッフ数, 備考, 個別変更の扱い, 更新日時
    detailRows.push([
      specialId,                                // special_week_id
      pid,                                      // patient_id
      dateObj,                                  // 日付
      getDayOfWeekStr(dateObj),                 // 曜日
      it.rowLabel || '特別1',                   // 行ラベル
      timeType,                                 // 時間タイプ
      startS,                                   // 開始時刻
      endS,                                     // 終了時刻
      earliestS,                                // 希望最早
      latestS,                                  // 希望最遅
      svcMin,                                   // サービス時間
      needStaff,                                // 必要スタッフ数
      it.note || '',                            // 備考
      changeHandle,                             // 個別変更の扱い
      now                                       // 更新日時
    ]);
  });

  if (detailRows.length > 0) {
    var startRow = detailSh.getLastRow() + 1;
    detailSh.getRange(startRow, 1, detailRows.length, 15).setValues(detailRows);
  }

  return { ok: true, success: true, message: '特別訪問週間を保存しました: ' + detailRows.length + '行', detailCount: detailRows.length };
}

// ============================================================
// 週間リクエスト生成へ「特別訪問週間」を差し込む（ADD/REPLACE + 個別変更扱い）
// ============================================================

function applySpecialWeekToWeeklyRequests_(ctx) {
  var ss = ctx.ss;
  var tz = ctx.tz;
  var weekStart = ctx.weekStart;
  var weekEnd = ctx.weekEnd;
  var weeklyRequests = ctx.weeklyRequests;
  var weeklyMap = ctx.weeklyMap;
  var patientInfoMap = ctx.patientInfoMap;
  var makeTimeWindow = ctx.makeTimeWindow;

  var sw = loadSpecialWeekForWeek_(ss, weekStart, weekEnd, tz);
  if (!sw.headers.length) return { added: 0, removed: 0 };

  // REPLACE対象の患者を先に消す（週内）
  var removed = 0;
  var replacePatientSet = {};
  sw.headers.forEach(function(h) {
    if (h.mode === 'REPLACE') replacePatientSet[h.patient_id] = true;
  });

  if (Object.keys(replacePatientSet).length) {
    for (var i = weeklyRequests.length - 1; i >= 0; i--) {
      var req = weeklyRequests[i];
      if (!req || !req.patient_id || !req.date) continue;
      if (!replacePatientSet[req.patient_id]) continue;
      if (req.date >= weekStart && req.date <= weekEnd) {
        weeklyRequests.splice(i, 1);
        removed++;
      }
    }

    // weeklyMap 再構築
    Object.keys(weeklyMap).forEach(function(k) { delete weeklyMap[k]; });
    weeklyRequests.forEach(function(req) {
      var key = req.patient_id + '|' + req.dateStr;
      if (!weeklyMap[key]) weeklyMap[key] = [];
      weeklyMap[key].push(req);
    });
  }

  // 明細を1件ずつ反映
  var added = 0;

  sw.details.forEach(function(d) {
    var pid = d.patient_id;
    var info = patientInfoMap[pid] || {};
    var dateStr = yyyymmdd_(d.date, tz);

    // 既存（通常/個別変更）と同一日があるか
    var mapKey = pid + '|' + dateStr;
    var existing = weeklyMap[mapKey] || [];

    // 個別変更扱い
    if (existing.length > 0) {
      if (d.change_policy === '無視') {
        // 既存がある日は、この明細は適用しない
        return;
      }
      if (d.change_policy === '上書き') {
        // その日の既存を削除してから入れる
        for (var i = weeklyRequests.length - 1; i >= 0; i--) {
          var req = weeklyRequests[i];
          if (req.patient_id === pid && req.dateStr === dateStr) {
            weeklyRequests.splice(i, 1);
            removed++;
          }
        }
        delete weeklyMap[mapKey];
      }
      // 'そのまま残す' は何もしない（既存は残しつつ追加する）
    }

    // 時間窓を作る
    var svcMin = Number(d.svc_min || info.svcMin || 0);
    var timeTypeRaw = (d.time_type || '').trim();
    var startPref = d.start !== '' ? d.start : (info.startPref || '');
    var endPref   = d.end   !== '' ? d.end   : (info.endPref || '');

    var win = makeTimeWindow(timeTypeRaw, startPref, endPref, svcMin);

    // weeklyRequestsに追加
    var reqObj = {
      date: d.date,
      dateStr: dateStr,
      weekdayStr: Utilities.formatDate(d.date, tz, 'EEE'),
      patient_id: pid,
      patient_name: info.name || '',
      area: info.area || '',
      start: win.start,
      end: win.end,
      svcMin: svcMin,
      needStaff: Number(d.need_staff || info.needStaff || 1),
      specifiedIds: info.staffIds || '',
      specifiedType: info.staffType || '',
      ngStaffIds: info.ngStaffIds || '',
      sexLimit: info.sexLimit || '',
      contPref: info.contPref || '',
      changeType: '特別',
      prevStaffId: '',
      prevStaffName: '',
      prevDate: null,
      timeType: win.timeType,
      earliest: (d.earliest !== '' ? d.earliest : win.earliest),
      latest:   (d.latest   !== '' ? d.latest   : win.latest),
      note: (d.note || '') + (d.row_label ? '（' + d.row_label + '）' : '')
    };

    weeklyRequests.push(reqObj);
    if (!weeklyMap[mapKey]) weeklyMap[mapKey] = [];
    weeklyMap[mapKey].push(reqObj);
    added++;
  });

  return { added: added, removed: removed };
}
