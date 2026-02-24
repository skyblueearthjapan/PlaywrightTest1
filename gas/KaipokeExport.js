/**
 * カイポケCSV出力
 *
 * 割当結果シートからカイポケ形式のCSVを生成し、
 * Google Driveに保存する
 */

// ============================================================
// 設定
// ============================================================

var KAIPOKE_EXPORT_CONFIG = {
  // 出力フォルダ名
  OUTPUT_FOLDER_NAME: 'カイポケCSV出力',

  // 事業所名（固定値）
  PROVIDER_NAME: '訪問看護ステーションよりより',

  // 職種（固定値）
  JOB_TYPE: '看護師',

  // シート名
  SHEET_ASSIGN_RESULT: '割当結果'
};

// ============================================================
// 公開API（Webアプリから呼び出し）
// ============================================================

/**
 * 割当結果からカイポケ形式CSVを生成・保存
 * @param {string} weekStartStr - 週開始日（"2026/01/26"形式）省略時は全データ
 * @return {Object} { success, fileId, url, fileName, rowCount, message }
 */
function kaipoke_exportCsv(weekStartStr) {
  var ssId = PropertiesService.getScriptProperties().getProperty('SS_ID');
  if (!ssId) {
    throw new Error('スプレッドシートIDが設定されていません（SS_ID）');
  }
  var ss = SpreadsheetApp.openById(ssId);
  var tz = ss.getSpreadsheetTimeZone();

  // 割当結果シート読み込み
  var sheet = ss.getSheetByName(KAIPOKE_EXPORT_CONFIG.SHEET_ASSIGN_RESULT);
  if (!sheet) {
    throw new Error('「割当結果」シートが見つかりません');
  }

  var values = sheet.getDataRange().getValues();
  if (values.length <= 1) {
    throw new Error('割当結果にデータがありません');
  }

  var header = values[0].map(function(h) { return String(h || '').trim(); });
  var data = values.slice(1);

  // カラムインデックス取得
  var idx = {
    visitId: kaipoke_findIdx_(header, 'visit_id'),
    date: kaipoke_findIdx_(header, '日付'),
    staffId: kaipoke_findIdx_(header, 'staff_id'),
    staffName: kaipoke_findIdx_(header, 'スタッフ名'),
    patientId: kaipoke_findIdx_(header, 'patient_id'),
    patientName: kaipoke_findIdx_(header, '患者名'),
    start: kaipoke_findIdx_(header, '開始時刻'),
    end: kaipoke_findIdx_(header, '終了時刻'),
    svcMin: kaipoke_findIdx_(header, 'サービス時間'),
    note: kaipoke_findIdx_(header, '備考')
  };

  // 必須カラムチェック
  if (idx.date < 0 || idx.patientId < 0 || idx.start < 0) {
    throw new Error('割当結果シートに必須カラム（日付、patient_id、開始時刻）がありません');
  }

  // 週範囲フィルタ
  var weekStart = null, weekEnd = null;
  if (weekStartStr) {
    weekStart = kaipoke_parseDate_(weekStartStr);
    weekEnd = new Date(weekStart);
    weekEnd.setDate(weekEnd.getDate() + 6);
  }

  // 患者マスタ読み込み（保険区分・患者名参照用）
  var patientMap = kaipoke_loadPatientMaster_(ss);

  // イベントリクエスト読み込み（住所参照用）
  var eventAddressMap = kaipoke_loadEventAddresses_(ss);

  // 同一訪問をグルーピング（イベント含む）
  var visitGroups = kaipoke_groupVisits_(data, idx, tz, weekStart, weekEnd, patientMap, eventAddressMap);

  // グループをCSV行に変換（保険区分対応）
  var csvRows = kaipoke_buildCsvRows_(visitGroups, tz, patientMap);

  if (csvRows.length === 0) {
    throw new Error('出力対象のデータがありません');
  }

  // CSV文字列生成（ヘッダー付き）
  var csvHeader = [
    '職員名1', '職種1', '職員名2', '職種2', '同行2',
    '職員名3', '職種3', '同行3', '事業所名',
    '日付', '曜日', '利用者', '業務種別', 'サービス内容',
    '開始時間', '終了時間', '提供時間', '備考'
  ];
  var allRows = [csvHeader].concat(csvRows);
  var csvContent = kaipoke_rowsToCsv_(allRows);

  // ファイル名生成（gas_optimized_YYYYMMDD_YYYYMMDD.csv 形式 = 週の範囲）
  var now = new Date();
  var startLabel, endLabel;
  if (weekStartStr) {
    var ws = kaipoke_parseDate_(weekStartStr);
    var we = new Date(ws); we.setDate(we.getDate() + 6);
    startLabel = Utilities.formatDate(ws, tz, 'yyyyMMdd');
    endLabel   = Utilities.formatDate(we, tz, 'yyyyMMdd');
  } else {
    // weekStartStr未指定時は今週を自動算出
    var today = new Date(); today.setHours(0,0,0,0);
    var day = today.getDay();
    var diff = (day + 6) % 7;
    var ws2 = new Date(today); ws2.setDate(today.getDate() - diff);
    var we2 = new Date(ws2); we2.setDate(ws2.getDate() + 6);
    startLabel = Utilities.formatDate(ws2, tz, 'yyyyMMdd');
    endLabel   = Utilities.formatDate(we2, tz, 'yyyyMMdd');
  }
  var fileName = 'gas_optimized_' + startLabel + '_' + endLabel + '.csv';

  // Google Driveに保存（DRIVE_FOLDER_ID を使用、同名ファイルは削除してから保存）
  var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
  var existing = folder.getFilesByName(fileName);
  while (existing.hasNext()) {
    existing.next().setTrashed(true);
  }
  var file = folder.createFile(fileName, csvContent, MimeType.CSV);

  return {
    success: true,
    fileId: file.getId(),
    url: file.getUrl(),
    fileName: fileName,
    rowCount: csvRows.length,
    message: csvRows.length + '件のデータをCSV出力しました'
  };
}

/**
 * 出力フォルダのURLを取得
 * @return {string} フォルダURL
 */
function kaipoke_getExportFolderUrl() {
  var folder = kaipoke_getOrCreateFolder_();
  return folder.getUrl();
}

// ============================================================
// 内部関数：データ変換
// ============================================================

/**
 * 割当結果を同一訪問でグルーピング（イベント含む）
 * 通常訪問: groupKey = 日付|patient_id|開始時刻|終了時刻
 * イベント: groupKey = EV|日付|visit_id|開始時刻|終了時刻
 */
function kaipoke_groupVisits_(data, idx, tz, weekStart, weekEnd, patientMap, eventAddressMap) {
  var groups = {};

  data.forEach(function(row) {
    var visitId = idx.visitId >= 0 ? String(row[idx.visitId] || '') : '';
    var isEvent = visitId.indexOf('EV_') === 0;

    // 日付チェック
    var dateObj = idx.date >= 0 ? kaipoke_parseDate_(row[idx.date]) : null;
    if (!dateObj) return;

    // 週範囲フィルタ
    if (weekStart && weekEnd) {
      if (dateObj < weekStart || dateObj > weekEnd) return;
    }

    var startStr = kaipoke_formatTime_(row[idx.start], tz);
    var endStr = kaipoke_formatTime_(row[idx.end], tz);
    if (!startStr) return;

    var dateStr = Utilities.formatDate(dateObj, tz, 'yyyy/MM/dd');

    if (isEvent) {
      // イベント行の処理
      var noteRaw = idx.note >= 0 ? String(row[idx.note] || '').trim() : '';

      // 備考からpatient_idを抽出（例: "（P004）"）
      var evPatientId = '';
      var evPatientName = 'なし';
      var pidMatch = noteRaw.match(/（(P\d+)）/);
      if (pidMatch) {
        evPatientId = pidMatch[1];
        if (patientMap[evPatientId]) {
          evPatientName = patientMap[evPatientId].name;
        }
      }

      var groupKey = 'EV|' + dateStr + '|' + visitId + '|' + startStr + '|' + endStr;

      if (!groups[groupKey]) {
        groups[groupKey] = {
          dateObj: dateObj,
          dateStr: dateStr,
          patientId: evPatientId,
          patientName: evPatientName,
          startStr: startStr,
          endStr: endStr,
          svcMin: idx.svcMin >= 0 ? row[idx.svcMin] : '',
          isEvent: true,
          staff: [],
          notes: []
        };
      }

      // スタッフ追加
      var evStaffId = idx.staffId >= 0 ? String(row[idx.staffId] || '').trim() : '';
      var evStaffName = idx.staffName >= 0 ? String(row[idx.staffName] || '').trim() : '';
      if (evStaffName) {
        groups[groupKey].staff.push({ staffId: evStaffId, staffName: evStaffName });
      }

      // 備考追加
      if (noteRaw && groups[groupKey].notes.indexOf(noteRaw) === -1) {
        groups[groupKey].notes.push(noteRaw);
      }

      // イベントの住所を備考に追加（event_id = visitIdから"EV_"を除いた部分）
      var eventId = visitId.replace(/^EV_/, '');
      var evAddress = eventAddressMap[eventId] || '';
      if (evAddress && groups[groupKey].notes.indexOf(evAddress) === -1) {
        groups[groupKey].notes.push(evAddress);
      }

    } else {
      // 通常訪問の処理
      var patientId = idx.patientId >= 0 ? String(row[idx.patientId] || '').trim() : '';
      if (!patientId) return;

      var groupKey = dateStr + '|' + patientId + '|' + startStr + '|' + endStr;

      if (!groups[groupKey]) {
        groups[groupKey] = {
          dateObj: dateObj,
          dateStr: dateStr,
          patientId: patientId,
          patientName: idx.patientName >= 0 ? String(row[idx.patientName] || '') : '',
          startStr: startStr,
          endStr: endStr,
          svcMin: idx.svcMin >= 0 ? row[idx.svcMin] : '',
          isEvent: false,
          staff: [],
          notes: []
        };
      }

      // スタッフ追加
      var staffId = idx.staffId >= 0 ? String(row[idx.staffId] || '').trim() : '';
      var staffName = idx.staffName >= 0 ? String(row[idx.staffName] || '').trim() : '';
      if (staffName) {
        groups[groupKey].staff.push({ staffId: staffId, staffName: staffName });
      }

      // 備考追加
      var note = idx.note >= 0 ? String(row[idx.note] || '').trim() : '';
      if (note && groups[groupKey].notes.indexOf(note) === -1) {
        groups[groupKey].notes.push(note);
      }
    }
  });

  return groups;
}

/**
 * グループをCSV行に変換（保険区分・イベント対応）
 */
function kaipoke_buildCsvRows_(groups, tz, patientMap) {
  var rows = [];
  var config = KAIPOKE_EXPORT_CONFIG;

  // 日付順でソート
  var sortedKeys = Object.keys(groups).sort();

  sortedKeys.forEach(function(key) {
    var g = groups[key];

    // スタッフをstaff_id順でソート・重複除去
    var staffSorted = kaipoke_uniqueStaff_(g.staff);

    // スタッフ名取得（最大3人）
    var staff1 = staffSorted[0] ? staffSorted[0].staffName : '';
    var staff2 = staffSorted[1] ? staffSorted[1].staffName : '';
    var staff3 = staffSorted[2] ? staffSorted[2].staffName : '';

    // 4人以上いる場合は備考に追記
    var noteExtra = '';
    if (staffSorted.length > 3) {
      noteExtra = '同行：他' + (staffSorted.length - 3) + '名';
    }

    // 備考を連結
    var notes = g.notes.slice();
    if (noteExtra) notes.push(noteExtra);
    var noteStr = notes.join(' / ');

    // 曜日取得
    var dowJp = kaipoke_getDayOfWeek_(g.dateObj);

    // 日付は日のみ（カイポケ形式）
    var dayOnly = g.dateObj.getDate();

    // 業務種別・サービス内容の決定
    var gyomuType = '';
    var serviceContent = '';

    if (g.isEvent) {
      // イベント
      gyomuType = 'イベント';
      serviceContent = '';
    } else {
      // 通常訪問：患者マスタの保険区分を参照
      var insurance = '医療保険'; // デフォルト
      if (g.patientId && patientMap[g.patientId]) {
        insurance = patientMap[g.patientId].insurance || '医療保険';
      }
      if (insurance === '介護保険') {
        gyomuType = '介護保険';
        serviceContent = '訪看Ⅰ３';
      } else {
        gyomuType = '医療保険';
        serviceContent = '精神基本療養費Ⅰ・正看';
      }
    }

    // 行データ作成
    // [職員名1, 職種1, 職員名2, 職種2, 同行2, 職員名3, 職種3, 同行3,
    //  事業所名, 日付, 曜日, 利用者, 業務種別, サービス内容, 開始時間, 終了時間, 提供時間, 備考]
    rows.push([
      staff1,              // 職員名1
      config.JOB_TYPE,     // 職種1
      staff2,              // 職員名2
      staff2 ? config.JOB_TYPE : '',  // 職種2
      staff2 ? '○' : '',   // 同行2
      staff3,              // 職員名3
      staff3 ? config.JOB_TYPE : '',  // 職種3
      staff3 ? '○' : '',   // 同行3
      config.PROVIDER_NAME, // 事業所名
      dayOnly,             // 日付（日のみ）
      dowJp,               // 曜日
      g.patientName,       // 利用者（イベント時：患者名 or "なし"）
      gyomuType,           // 業務種別
      serviceContent,      // サービス内容
      g.startStr,          // 開始時間
      g.endStr,            // 終了時間
      g.svcMin,            // 提供時間
      noteStr              // 備考
    ]);
  });

  return rows;
}

/**
 * スタッフ配列を重複除去・ソート
 */
function kaipoke_uniqueStaff_(staffList) {
  var seen = {};
  var unique = [];

  staffList.forEach(function(s) {
    if (s.staffName && !seen[s.staffName]) {
      seen[s.staffName] = true;
      unique.push(s);
    }
  });

  // staff_id順でソート
  unique.sort(function(a, b) {
    if (!a.staffId && !b.staffId) return 0;
    if (!a.staffId) return 1;
    if (!b.staffId) return -1;
    return a.staffId.localeCompare(b.staffId);
  });

  return unique;
}

/**
 * 曜日取得（日本語）
 */
function kaipoke_getDayOfWeek_(dateObj) {
  var days = ['日', '月', '火', '水', '木', '金', '土'];
  return days[dateObj.getDay()];
}

/**
 * 患者マスタを読み込み、patient_id → { name, insurance } のMapを返す
 */
function kaipoke_loadPatientMaster_(ss) {
  var map = {};
  var sheet = ss.getSheetByName('患者マスタ');
  if (!sheet || sheet.getLastRow() <= 1) return map;

  var values = sheet.getDataRange().getValues();
  var header = values[0].map(function(h) { return String(h || '').trim(); });

  var idIdx = kaipoke_findIdx_(header, 'patient_id');
  var nameIdx = kaipoke_findIdx_(header, '患者名');
  var insuranceIdx = kaipoke_findIdx_(header, '保険区分');

  if (idIdx < 0) return map;

  for (var i = 1; i < values.length; i++) {
    var row = values[i];
    var pid = String(row[idIdx] || '').trim();
    if (!pid) continue;

    map[pid] = {
      name: nameIdx >= 0 ? String(row[nameIdx] || '').trim() : '',
      insurance: insuranceIdx >= 0 ? String(row[insuranceIdx] || '').trim() : '医療保険'
    };
  }

  return map;
}

/**
 * イベントリクエストを読み込み、event_id → 住所 のMapを返す
 */
function kaipoke_loadEventAddresses_(ss) {
  var map = {};
  var sheet = ss.getSheetByName('イベントリクエスト');
  if (!sheet || sheet.getLastRow() <= 1) return map;

  var values = sheet.getDataRange().getValues();
  var header = values[0].map(function(h) { return String(h || '').trim(); });

  var idIdx = kaipoke_findIdx_(header, 'event_id');
  var addressIdx = kaipoke_findIdx_(header, '住所');

  if (idIdx < 0 || addressIdx < 0) return map;

  for (var i = 1; i < values.length; i++) {
    var row = values[i];
    var eid = String(row[idIdx] || '').trim();
    var addr = String(row[addressIdx] || '').trim();
    if (eid && addr) {
      map[eid] = addr;
    }
  }

  return map;
}

// ============================================================
// 内部関数：ユーティリティ
// ============================================================

/**
 * ヘッダーからカラムインデックスを取得
 */
function kaipoke_findIdx_(header, colName) {
  for (var i = 0; i < header.length; i++) {
    if (header[i] === colName) return i;
  }
  return -1;
}

/**
 * 日付をパース
 */
function kaipoke_parseDate_(value) {
  if (!value) return null;

  if (value instanceof Date) {
    return value;
  }

  var str = String(value);

  // "2026/01/26" 形式
  var match = str.match(/(\d{4})\/(\d{1,2})\/(\d{1,2})/);
  if (match) {
    return new Date(parseInt(match[1]), parseInt(match[2]) - 1, parseInt(match[3]));
  }

  // "2026-01-26" 形式
  match = str.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (match) {
    return new Date(parseInt(match[1]), parseInt(match[2]) - 1, parseInt(match[3]));
  }

  return null;
}

/**
 * 時刻をHH:mm形式にフォーマット
 */
function kaipoke_formatTime_(value, tz) {
  if (!value) return '';

  if (value instanceof Date) {
    return Utilities.formatDate(value, tz, 'HH:mm');
  }

  // 数値（シリアル値）の場合
  if (typeof value === 'number') {
    var totalMinutes = Math.round(value * 24 * 60);
    var hours = Math.floor(totalMinutes / 60);
    var minutes = totalMinutes % 60;
    return ('0' + hours).slice(-2) + ':' + ('0' + minutes).slice(-2);
  }

  // 文字列の場合、HH:mm形式に正規化
  var str = String(value);
  var match = str.match(/(\d{1,2}):(\d{2})/);
  if (match) {
    return ('0' + match[1]).slice(-2) + ':' + match[2];
  }

  return str;
}

/**
 * 行配列をCSV文字列に変換
 */
function kaipoke_rowsToCsv_(rows) {
  return rows.map(function(row) {
    return row.map(function(cell) {
      var str = String(cell === null || cell === undefined ? '' : cell);
      // ダブルクォートをエスケープ
      str = str.replace(/"/g, '""');
      // カンマ、改行、ダブルクォートを含む場合は囲む
      if (str.indexOf(',') >= 0 || str.indexOf('\n') >= 0 || str.indexOf('"') >= 0) {
        return '"' + str + '"';
      }
      return str;
    }).join(',');
  }).join('\n');
}

/**
 * 出力フォルダを取得（なければ作成）
 */
function kaipoke_getOrCreateFolder_() {
  var folderName = KAIPOKE_EXPORT_CONFIG.OUTPUT_FOLDER_NAME;
  var folders = DriveApp.getFoldersByName(folderName);

  if (folders.hasNext()) {
    return folders.next();
  }

  return DriveApp.createFolder(folderName);
}

// ============================================================
// テスト用
// ============================================================

/**
 * テスト実行（全データ）
 */
function test_kaipokeExport() {
  var result = kaipoke_exportCsv();
  console.log(JSON.stringify(result, null, 2));
}

/**
 * テスト実行（週指定）
 */
function test_kaipokeExportWeek() {
  var result = kaipoke_exportCsv('2026/01/26');
  console.log(JSON.stringify(result, null, 2));
}
