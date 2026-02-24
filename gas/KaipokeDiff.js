/**
 * カイポケ差分検出
 *
 * 外部（カイポケ）と内部（割当結果）の差分を検出し、
 * レポートを生成する
 */

// ============================================================
// 設定
// ============================================================

var KAIPOKE_DIFF_CONFIG = {
  SHEET_DIFF_REPORT: '差分レポート',
  SHEET_ASSIGN_RESULT: '割当結果',

  // 差分判定
  STATUS: {
    OK: 'OK',                    // 完全一致
    TIME_MISMATCH: 'TIME_MISMATCH', // 時間差異
    MISSING_INTERNAL: 'MISSING_INTERNAL', // 内部にない（カイポケにある）
    EXTRA_INTERNAL: 'EXTRA_INTERNAL',     // 内部にだけある
    STAFF_MISMATCH: 'STAFF_MISMATCH',     // 担当者が違う
    ID_UNKNOWN: 'ID_UNKNOWN'     // ID不明で比較不可
  },

  // 時間許容範囲（分）
  TIME_TOLERANCE_MIN: 5
};

// ============================================================
// 公開API
// ============================================================

/**
 * 週単位で差分検出を実行
 * @param {string} weekStartStr - 週開始日（"2026/01/19"形式）
 * @param {Object} options - オプション { includeAccompany: true/false }
 * @return {Object} 差分結果
 */
function kaipoke_detectDiff(weekStartStr, options) {
  options = options || { includeAccompany: true };

  var ssId = PropertiesService.getScriptProperties().getProperty('SS_ID');
  if (!ssId) {
    throw new Error('スプレッドシートIDが設定されていません（SS_ID）');
  }
  var ss = SpreadsheetApp.openById(ssId);

  // 外部（カイポケ）データ取得
  var external = kaipoke_getNormalizedData(weekStartStr);

  // 内部（割当結果）データ取得・正規化
  var internal = kaipoke_loadInternalData_(ss, weekStartStr);

  // 同行者を含めるかどうか
  if (!options.includeAccompany) {
    external = external.filter(function(r) { return r.role === 'MAIN'; });
  }

  // 差分検出
  var diffResult = kaipoke_compareSets_(external, internal);

  // レポートシートに出力
  kaipoke_writeDiffReport_(ss, diffResult, weekStartStr);

  return {
    success: true,
    weekStartStr: weekStartStr,
    summary: {
      total_external: external.length,
      total_internal: internal.length,
      ok: diffResult.ok.length,
      time_mismatch: diffResult.timeMismatch.length,
      missing_internal: diffResult.missingInternal.length,
      extra_internal: diffResult.extraInternal.length,
      staff_mismatch: diffResult.staffMismatch.length,
      id_unknown: diffResult.idUnknown.length
    },
    details: diffResult
  };
}

/**
 * 差分サマリを取得（UI表示用）
 */
function kaipoke_getDiffSummary(weekStartStr) {
  var result = kaipoke_detectDiff(weekStartStr, { includeAccompany: true });
  return result.summary;
}

// ============================================================
// 内部関数：データ読み込み
// ============================================================

/**
 * 割当結果から内部データを正規化形式で取得
 */
function kaipoke_loadInternalData_(ss, weekStartStr) {
  var sheet = ss.getSheetByName(KAIPOKE_DIFF_CONFIG.SHEET_ASSIGN_RESULT);
  if (!sheet) return [];

  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return [];

  var headers = data[0];
  var rows = data.slice(1);

  // 週の範囲
  var weekStart = new Date(weekStartStr.replace(/\//g, '-'));
  var weekEnd = new Date(weekStart);
  weekEnd.setDate(weekEnd.getDate() + 6);

  // カラムインデックス
  var dateIdx = headers.indexOf('日付');
  var staffIdIdx = headers.indexOf('staff_id');
  var patientIdIdx = headers.indexOf('patient_id');
  var startIdx = headers.indexOf('開始時刻');
  var endIdx = headers.indexOf('終了時刻');

  var result = [];

  rows.forEach(function(row) {
    var dateVal = row[dateIdx];
    if (!dateVal) return;

    var d;
    if (dateVal instanceof Date) {
      d = dateVal;
    } else {
      d = new Date(String(dateVal).replace(/\//g, '-'));
    }

    if (d >= weekStart && d <= weekEnd) {
      var ymd = Utilities.formatDate(d, Session.getScriptTimeZone(), 'yyyy/MM/dd');

      result.push({
        source: 'INTERNAL',
        ymd: ymd,
        staff_id: row[staffIdIdx] || '',
        patient_id: row[patientIdIdx] || '',
        start: kaipoke_normalizeTimeInternal_(row[startIdx]),
        end: kaipoke_normalizeTimeInternal_(row[endIdx]),
        role: 'MAIN'
      });
    }
  });

  return result;
}

/**
 * 内部時刻を正規化
 */
function kaipoke_normalizeTimeInternal_(time) {
  if (!time) return '';

  if (time instanceof Date) {
    return Utilities.formatDate(time, Session.getScriptTimeZone(), 'HH:mm');
  }

  if (typeof time === 'number') {
    var totalMinutes = Math.round(time * 24 * 60);
    var hours = Math.floor(totalMinutes / 60);
    var minutes = totalMinutes % 60;
    return ('0' + hours).slice(-2) + ':' + ('0' + minutes).slice(-2);
  }

  var str = String(time);
  var match = str.match(/(\d{1,2}):(\d{2})/);
  if (match) {
    return ('0' + match[1]).slice(-2) + ':' + match[2];
  }

  return str;
}

// ============================================================
// 内部関数：差分比較
// ============================================================

/**
 * 外部と内部を比較
 */
function kaipoke_compareSets_(external, internal) {
  var STATUS = KAIPOKE_DIFF_CONFIG.STATUS;
  var TOLERANCE = KAIPOKE_DIFF_CONFIG.TIME_TOLERANCE_MIN;

  var result = {
    ok: [],
    timeMismatch: [],
    missingInternal: [],
    extraInternal: [],
    staffMismatch: [],
    idUnknown: []
  };

  // 内部データをキーでインデックス化
  var internalByKey = {};
  var internalUsed = {};

  internal.forEach(function(rec, idx) {
    // 厳密キー（日付+スタッフ+患者+時間）
    var strictKey = rec.ymd + '|' + rec.staff_id + '|' + rec.patient_id + '|' + rec.start;
    // 緩いキー（日付+患者）
    var looseKey = rec.ymd + '|' + rec.patient_id;

    if (!internalByKey[strictKey]) internalByKey[strictKey] = [];
    internalByKey[strictKey].push({ rec: rec, idx: idx });

    if (!internalByKey[looseKey]) internalByKey[looseKey] = [];
    internalByKey[looseKey].push({ rec: rec, idx: idx });
  });

  // 外部データを順にチェック
  external.forEach(function(extRec) {
    // ID不明チェック
    if (!extRec.staff_id || !extRec.patient_id) {
      result.idUnknown.push({
        status: STATUS.ID_UNKNOWN,
        external: extRec,
        internal: null,
        reason: !extRec.staff_id ? 'スタッフID不明' : '患者ID不明'
      });
      return;
    }

    // 厳密マッチ（日付+スタッフ+患者+開始時間）
    var strictKey = extRec.ymd + '|' + extRec.staff_id + '|' + extRec.patient_id + '|' + extRec.start;
    var strictMatches = internalByKey[strictKey] || [];

    var matched = strictMatches.find(function(m) {
      return !internalUsed[m.idx];
    });

    if (matched) {
      // 完全一致
      internalUsed[matched.idx] = true;
      result.ok.push({
        status: STATUS.OK,
        external: extRec,
        internal: matched.rec
      });
      return;
    }

    // 緩いマッチ（日付+患者）で時間差異を探す
    var looseKey = extRec.ymd + '|' + extRec.patient_id;
    var looseMatches = (internalByKey[looseKey] || []).filter(function(m) {
      return !internalUsed[m.idx];
    });

    // 同じスタッフで時間が違うものを探す
    var sameStaffDiffTime = looseMatches.find(function(m) {
      return m.rec.staff_id === extRec.staff_id;
    });

    if (sameStaffDiffTime) {
      // 時間差異
      internalUsed[sameStaffDiffTime.idx] = true;
      result.timeMismatch.push({
        status: STATUS.TIME_MISMATCH,
        external: extRec,
        internal: sameStaffDiffTime.rec,
        reason: '開始時間: ' + extRec.start + ' vs ' + sameStaffDiffTime.rec.start
      });
      return;
    }

    // 違うスタッフが担当
    var diffStaff = looseMatches.find(function(m) {
      return m.rec.staff_id !== extRec.staff_id;
    });

    if (diffStaff) {
      // スタッフ差異
      internalUsed[diffStaff.idx] = true;
      result.staffMismatch.push({
        status: STATUS.STAFF_MISMATCH,
        external: extRec,
        internal: diffStaff.rec,
        reason: 'スタッフ: ' + extRec.staff_id + ' vs ' + diffStaff.rec.staff_id
      });
      return;
    }

    // 内部に存在しない
    result.missingInternal.push({
      status: STATUS.MISSING_INTERNAL,
      external: extRec,
      internal: null,
      reason: '内部に該当訪問なし'
    });
  });

  // 内部にだけあるものを抽出
  internal.forEach(function(intRec, idx) {
    if (!internalUsed[idx]) {
      result.extraInternal.push({
        status: STATUS.EXTRA_INTERNAL,
        external: null,
        internal: intRec,
        reason: '外部に該当訪問なし'
      });
    }
  });

  return result;
}

// ============================================================
// 内部関数：レポート出力
// ============================================================

/**
 * 差分レポートをシートに出力
 */
function kaipoke_writeDiffReport_(ss, diffResult, weekStartStr) {
  var sheetName = KAIPOKE_DIFF_CONFIG.SHEET_DIFF_REPORT;
  var sheet = ss.getSheetByName(sheetName);

  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
  }

  sheet.clear();

  // ヘッダー
  var headers = [
    '判定', '日付', '曜日',
    '外部スタッフ', '外部患者', '外部開始', '外部終了',
    '内部スタッフ', '内部患者', '内部開始', '内部終了',
    '詳細'
  ];

  var rows = [headers];

  // 各カテゴリのデータを追加
  var allDiffs = [].concat(
    diffResult.ok,
    diffResult.timeMismatch,
    diffResult.staffMismatch,
    diffResult.missingInternal,
    diffResult.extraInternal,
    diffResult.idUnknown
  );

  allDiffs.forEach(function(diff) {
    var ext = diff.external || {};
    var int = diff.internal || {};

    rows.push([
      diff.status,
      ext.ymd || int.ymd || '',
      ext.dow || '',
      ext.staff_name || ext.staff_id || '',
      ext.patient_name || ext.patient_id || '',
      ext.start || '',
      ext.end || '',
      int.staff_id || '',
      int.patient_id || '',
      int.start || '',
      int.end || '',
      diff.reason || ''
    ]);
  });

  sheet.getRange(1, 1, rows.length, headers.length).setValues(rows);

  // ヘッダー書式
  var headerRange = sheet.getRange(1, 1, 1, headers.length);
  headerRange.setBackground('#7DA7D9');
  headerRange.setFontColor('#FFFFFF');
  headerRange.setFontWeight('bold');

  // 判定列の色分け
  var STATUS = KAIPOKE_DIFF_CONFIG.STATUS;
  var colorMap = {};
  colorMap[STATUS.OK] = '#D4E8B8';           // 緑
  colorMap[STATUS.TIME_MISMATCH] = '#F9E0A6'; // 黄
  colorMap[STATUS.STAFF_MISMATCH] = '#FACCAA'; // オレンジ
  colorMap[STATUS.MISSING_INTERNAL] = '#E88B8B'; // 赤
  colorMap[STATUS.EXTRA_INTERNAL] = '#B8D4F0';   // 青
  colorMap[STATUS.ID_UNKNOWN] = '#CCCCCC';   // グレー

  for (var i = 1; i < rows.length; i++) {
    var status = rows[i][0];
    var color = colorMap[status];
    if (color) {
      sheet.getRange(i + 1, 1).setBackground(color);
    }
  }

  // 列幅調整
  sheet.autoResizeColumns(1, headers.length);

  // サマリ追加（右側に）
  var summaryCol = headers.length + 2;
  var summaryData = [
    ['差分サマリ'],
    ['週開始日', weekStartStr],
    [''],
    ['OK（一致）', diffResult.ok.length],
    ['時間差異', diffResult.timeMismatch.length],
    ['スタッフ差異', diffResult.staffMismatch.length],
    ['内部に無い', diffResult.missingInternal.length],
    ['内部にだけある', diffResult.extraInternal.length],
    ['ID不明', diffResult.idUnknown.length]
  ];

  sheet.getRange(1, summaryCol, summaryData.length, 2).setValues(summaryData);
  sheet.getRange(1, summaryCol).setFontWeight('bold');
}

// ============================================================
// テスト用
// ============================================================

/**
 * テスト実行
 */
function test_kaipokeDiff() {
  var result = kaipoke_detectDiff('2026/01/19', { includeAccompany: true });
  console.log(JSON.stringify(result.summary, null, 2));
}
