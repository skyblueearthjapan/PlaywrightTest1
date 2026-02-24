/**
 * カイポケCSV取り込み・正規化
 *
 * 外部システム（カイポケ）からエクスポートしたCSVを
 * 内部フォーマットに正規化し、差分比較の基盤を作る
 */

// ============================================================
// 設定
// ============================================================

var KAIPOKE_CONFIG = {
  // シート名
  SHEET_CSV_RAW: '外部CSV_RAW',
  SHEET_NORMALIZED: '外部_正規化',
  SHEET_WEEK_VIEW: '外部_週ビュー',

  // CSVカラムマッピング（0始まり）
  CSV_COL: {
    STAFF1_NAME: 0,    // 職員名１
    STAFF1_TYPE: 1,    // 職種１
    STAFF2_NAME: 2,    // 職員名２
    STAFF2_TYPE: 3,    // 職種２
    ACCOMPANY2: 4,     // 同行２
    STAFF3_NAME: 5,    // 職員名３
    STAFF3_TYPE: 6,    // 職種３
    ACCOMPANY3: 7,     // 同行３
    FACILITY: 8,       // 事業所名
    DAY: 9,            // 日付（日のみ）
    DOW: 10,           // 曜日
    PATIENT_NAME: 11,  // 利用者
    BUSINESS_TYPE: 12, // 業務種別
    SERVICE_TYPE: 13,  // サービス種別
    START_TIME: 14,    // サービス開始時間
    END_TIME: 15,      // 終了時間
    DURATION: 16,      // 提供時間
    NOTE: 17           // 備考
  }
};

// ============================================================
// 公開API
// ============================================================

/**
 * CSVデータを正規化シートに変換
 * @param {string} yearMonth - 対象年月（"2026/01"形式）
 */
function kaipoke_importFromRawSheet(yearMonth) {
  var ssId = PropertiesService.getScriptProperties().getProperty('SS_ID');
  if (!ssId) {
    throw new Error('スプレッドシートIDが設定されていません（SS_ID）');
  }
  var ss = SpreadsheetApp.openById(ssId);
  var rawSheet = ss.getSheetByName(KAIPOKE_CONFIG.SHEET_CSV_RAW);

  if (!rawSheet) {
    throw new Error('シート「' + KAIPOKE_CONFIG.SHEET_CSV_RAW + '」が見つかりません');
  }

  var data = rawSheet.getDataRange().getValues();
  if (data.length < 2) {
    throw new Error('CSVデータがありません');
  }

  // ヘッダー行をスキップ
  var rows = data.slice(1);

  // スタッフマスタ読み込み（名前→ID変換用）
  var staffMap = kaipoke_loadStaffNameMap_(ss);

  // 患者マスタ読み込み（名前→ID変換用）
  var patientMap = kaipoke_loadPatientNameMap_(ss);

  // 正規化
  var normalized = kaipoke_normalizeRows_(rows, yearMonth, staffMap, patientMap);

  // 正規化シートに出力
  kaipoke_writeNormalizedSheet_(ss, normalized);

  return {
    success: true,
    count: normalized.length,
    message: normalized.length + '件の訪問データを正規化しました'
  };
}

/**
 * UI用：CSVインポートダイアログ表示
 */
function kaipoke_showImportDialog() {
  var html = HtmlService.createHtmlOutput(kaipoke_getImportDialogHtml_())
    .setWidth(500)
    .setHeight(400)
    .setTitle('カイポケCSVインポート');
  SpreadsheetApp.getUi().showModalDialog(html, 'カイポケCSVインポート');
}

/**
 * 正規化データを取得（週指定）
 * @param {string} weekStartStr - 週開始日（"2026/01/19"形式）
 * @return {Array} 正規化レコード配列
 */
function kaipoke_getNormalizedData(weekStartStr) {
  var ssId = PropertiesService.getScriptProperties().getProperty('SS_ID');
  if (!ssId) {
    throw new Error('スプレッドシートIDが設定されていません（SS_ID）');
  }
  var ss = SpreadsheetApp.openById(ssId);
  var sheet = ss.getSheetByName(KAIPOKE_CONFIG.SHEET_NORMALIZED);

  if (!sheet) return [];

  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return [];

  var headers = data[0];
  var rows = data.slice(1);

  // 週の範囲を計算
  var weekStart = new Date(weekStartStr.replace(/\//g, '-'));
  var weekEnd = new Date(weekStart);
  weekEnd.setDate(weekEnd.getDate() + 6);

  var result = [];
  var ymdIdx = headers.indexOf('ymd');

  rows.forEach(function(row) {
    var ymd = row[ymdIdx];
    if (!ymd) return;

    var d = new Date(ymd.replace(/\//g, '-'));
    if (d >= weekStart && d <= weekEnd) {
      var record = {};
      headers.forEach(function(h, i) {
        record[h] = row[i];
      });
      result.push(record);
    }
  });

  return result;
}

// ============================================================
// 内部関数：データ変換
// ============================================================

/**
 * CSV行を正規化レコードに変換
 */
function kaipoke_normalizeRows_(rows, yearMonth, staffMap, patientMap) {
  var COL = KAIPOKE_CONFIG.CSV_COL;
  var normalized = [];
  var ymParts = yearMonth.split('/');
  var year = ymParts[0];
  var month = ymParts[1];

  rows.forEach(function(row, rowIdx) {
    var day = row[COL.DAY];
    if (!day) return; // 日付がない行はスキップ

    // 日付を完全形式に
    var dayStr = ('0' + day).slice(-2);
    var ymd = year + '/' + month + '/' + dayStr;

    var dow = row[COL.DOW] || '';
    var patientName = kaipoke_normalizeName_(row[COL.PATIENT_NAME]);
    var patientId = patientMap[patientName] || '';
    var startTime = kaipoke_normalizeTime_(row[COL.START_TIME]);
    var endTime = kaipoke_normalizeTime_(row[COL.END_TIME]);
    var duration = row[COL.DURATION] || '';
    var note = row[COL.NOTE] || '';
    var businessType = row[COL.BUSINESS_TYPE] || '';
    var serviceType = row[COL.SERVICE_TYPE] || '';

    // 職員1（主担当として扱う）
    var staff1Name = kaipoke_normalizeName_(row[COL.STAFF1_NAME]);
    if (staff1Name) {
      normalized.push({
        source: 'KAIPOKE',
        ymd: ymd,
        dow: dow,
        staff_name: staff1Name,
        staff_id: staffMap[staff1Name] || '',
        patient_name: patientName,
        patient_id: patientId,
        start: startTime,
        end: endTime,
        duration_min: duration,
        role: 'MAIN',  // 主担当
        business_type: businessType,
        service_type: serviceType,
        note: note,
        raw_row: rowIdx + 2 // 元CSV行番号（ヘッダー+1始まり）
      });
    }

    // 職員2（同行）
    var staff2Name = kaipoke_normalizeName_(row[COL.STAFF2_NAME]);
    if (staff2Name) {
      normalized.push({
        source: 'KAIPOKE',
        ymd: ymd,
        dow: dow,
        staff_name: staff2Name,
        staff_id: staffMap[staff2Name] || '',
        patient_name: patientName,
        patient_id: patientId,
        start: startTime,
        end: endTime,
        duration_min: duration,
        role: 'ACCOMPANY',  // 同行
        business_type: businessType,
        service_type: serviceType,
        note: note,
        raw_row: rowIdx + 2
      });
    }

    // 職員3（同行）
    var staff3Name = kaipoke_normalizeName_(row[COL.STAFF3_NAME]);
    if (staff3Name) {
      normalized.push({
        source: 'KAIPOKE',
        ymd: ymd,
        dow: dow,
        staff_name: staff3Name,
        staff_id: staffMap[staff3Name] || '',
        patient_name: patientName,
        patient_id: patientId,
        start: startTime,
        end: endTime,
        duration_min: duration,
        role: 'ACCOMPANY',  // 同行
        business_type: businessType,
        service_type: serviceType,
        note: note,
        raw_row: rowIdx + 2
      });
    }
  });

  return normalized;
}

/**
 * 名前を正規化（スペース除去）
 */
function kaipoke_normalizeName_(name) {
  if (!name) return '';
  return String(name).replace(/[\s　]+/g, '').trim();
}

/**
 * 時刻を正規化（HH:MM形式）
 */
function kaipoke_normalizeTime_(time) {
  if (!time) return '';
  var str = String(time);

  // 数値（シリアル値）の場合
  if (typeof time === 'number') {
    var totalMinutes = Math.round(time * 24 * 60);
    var hours = Math.floor(totalMinutes / 60);
    var minutes = totalMinutes % 60;
    return ('0' + hours).slice(-2) + ':' + ('0' + minutes).slice(-2);
  }

  // "16:00"形式
  var match = str.match(/(\d{1,2}):(\d{2})/);
  if (match) {
    return ('0' + match[1]).slice(-2) + ':' + match[2];
  }

  return str;
}

// ============================================================
// 内部関数：マスタ読み込み
// ============================================================

/**
 * スタッフマスタから名前→IDマップを作成
 */
function kaipoke_loadStaffNameMap_(ss) {
  var sheet = ss.getSheetByName('スタッフマスタ');
  if (!sheet) return {};

  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return {};

  var headers = data[0];
  var idIdx = headers.indexOf('staff_id');
  var nameIdx = headers.indexOf('氏名');

  if (idIdx === -1 || nameIdx === -1) return {};

  var map = {};
  data.slice(1).forEach(function(row) {
    var id = row[idIdx];
    var name = kaipoke_normalizeName_(row[nameIdx]);
    if (id && name) {
      map[name] = id;
    }
  });

  return map;
}

/**
 * 患者マスタから名前→IDマップを作成
 */
function kaipoke_loadPatientNameMap_(ss) {
  var sheet = ss.getSheetByName('患者マスタ');
  if (!sheet) return {};

  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return {};

  var headers = data[0];
  var idIdx = headers.indexOf('patient_id');
  var nameIdx = headers.indexOf('患者名');

  if (idIdx === -1 || nameIdx === -1) return {};

  var map = {};
  data.slice(1).forEach(function(row) {
    var id = row[idIdx];
    var name = kaipoke_normalizeName_(row[nameIdx]);
    if (id && name) {
      map[name] = id;
    }
  });

  return map;
}

// ============================================================
// 内部関数：シート出力
// ============================================================

/**
 * 正規化データをシートに出力
 */
function kaipoke_writeNormalizedSheet_(ss, normalized) {
  var sheetName = KAIPOKE_CONFIG.SHEET_NORMALIZED;
  var sheet = ss.getSheetByName(sheetName);

  // シートがなければ作成
  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
  }

  // クリア
  sheet.clear();

  // ヘッダー
  var headers = [
    'source', 'ymd', 'dow', 'staff_name', 'staff_id',
    'patient_name', 'patient_id', 'start', 'end', 'duration_min',
    'role', 'business_type', 'service_type', 'note', 'raw_row'
  ];

  // データ行
  var rows = [headers];
  normalized.forEach(function(rec) {
    rows.push(headers.map(function(h) { return rec[h] || ''; }));
  });

  sheet.getRange(1, 1, rows.length, headers.length).setValues(rows);

  // ヘッダー書式
  var headerRange = sheet.getRange(1, 1, 1, headers.length);
  headerRange.setBackground('#7DA7D9');
  headerRange.setFontColor('#FFFFFF');
  headerRange.setFontWeight('bold');

  // 列幅調整
  sheet.autoResizeColumns(1, headers.length);
}

/**
 * インポートダイアログHTML
 */
function kaipoke_getImportDialogHtml_() {
  return '\
<!DOCTYPE html>\
<html>\
<head>\
  <style>\
    body { font-family: sans-serif; padding: 16px; }\
    .form-group { margin-bottom: 16px; }\
    label { display: block; margin-bottom: 4px; font-weight: bold; }\
    input, select { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 4px; }\
    button { padding: 10px 20px; background: #B7D38A; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; }\
    button:hover { background: #9BC06A; }\
    .note { font-size: 12px; color: #666; margin-top: 4px; }\
    .status { margin-top: 16px; padding: 10px; border-radius: 4px; }\
    .status.success { background: #D4E8B8; }\
    .status.error { background: #FACCAA; }\
  </style>\
</head>\
<body>\
  <h3>カイポケCSVインポート</h3>\
  <div class="form-group">\
    <label>対象年月</label>\
    <input type="month" id="yearMonth" value="2026-01">\
    <div class="note">CSVの日付は「日」のみなので、年月を指定してください</div>\
  </div>\
  <div class="form-group">\
    <label>手順</label>\
    <ol style="font-size:13px; padding-left:20px;">\
      <li>カイポケからCSVをエクスポート</li>\
      <li>「外部CSV_RAW」シートにCSVデータを貼り付け</li>\
      <li>下の「インポート実行」をクリック</li>\
    </ol>\
  </div>\
  <button onclick="runImport()">インポート実行</button>\
  <div id="status"></div>\
  <script>\
    function runImport() {\
      var yearMonth = document.getElementById("yearMonth").value.replace("-", "/");\
      document.getElementById("status").innerHTML = "処理中...";\
      google.script.run\
        .withSuccessHandler(function(result) {\
          document.getElementById("status").innerHTML = \
            \'<div class="status success">\' + result.message + \'</div>\';\
        })\
        .withFailureHandler(function(e) {\
          document.getElementById("status").innerHTML = \
            \'<div class="status error">エラー: \' + e.message + \'</div>\';\
        })\
        .kaipoke_importFromRawSheet(yearMonth);\
    }\
  </script>\
</body>\
</html>';
}

// ============================================================
// テスト用
// ============================================================

/**
 * テスト実行
 */
function test_kaipokeImport() {
  var result = kaipoke_importFromRawSheet('2026/01');
  console.log(JSON.stringify(result, null, 2));
}
