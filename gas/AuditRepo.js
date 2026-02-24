/**
 * 監査（合否判定）ビュー - データローダー
 *
 * 週単位で必要なデータを一括ロードしてMap化する
 * 既存シートは読み取り専用（変更しない）
 */

// ============================================================
// メインローダー：週単位データセット取得
// ============================================================

/**
 * 週単位のデータセットを一括ロード
 * @param {string} weekStartStr - 週開始日（yyyy/MM/dd）
 * @return {Object} データセット
 */
function audit_loadWeekDataset_(weekStartStr) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var tz = ss.getSpreadsheetTimeZone();

  var weekStart = audit_parseDate_(weekStartStr);
  if (!weekStart) {
    throw new Error('週開始日のパースに失敗: ' + weekStartStr);
  }
  weekStart = audit_getWeekStart_(weekStart);
  var weekEnd = audit_getWeekEnd_(weekStart);

  var weekStartStrNorm = audit_formatDateStr_(weekStart, tz);
  var weekEndStr = audit_formatDateStr_(weekEnd, tz);

  // 週の各日付を生成
  var weekDates = [];
  var d = new Date(weekStart);
  for (var i = 0; i < 7; i++) {
    weekDates.push({
      // DateオブジェクトはJSON化でnullになる可能性があるため、文字列として保持
      dateIso: d.toISOString(),
      dateStr: audit_formatDateStr_(d, tz),
      youbi: audit_getYoubiEn_(d)
    });
    d.setDate(d.getDate() + 1);
  }

  // 警告を収集
  var warnings = [];

  // 各データをロード（エラー時は空を返す）
  var patientMasterMap = audit_safeLoad_(function() {
    return audit_loadPatientMaster_(ss, tz);
  }, '患者マスタ', warnings);

  var staffMasterMap = audit_safeLoad_(function() {
    return audit_loadStaffMaster_(ss, tz);
  }, 'スタッフマスタ', warnings);

  var changeMap = audit_safeLoad_(function() {
    return audit_loadChangeRequests_(ss, tz, weekStartStrNorm, weekEndStr);
  }, '個別変更リクエスト', warnings);

  var eventMap = audit_safeLoad_(function() {
    return audit_loadPatientLinkedEvents_(ss, tz, weekStartStrNorm, weekEndStr);
  }, 'イベントリクエスト', warnings);

  var specialWeekMap = audit_safeLoad_(function() {
    return audit_loadSpecialWeek_(ss, tz, weekStartStrNorm, weekEndStr);
  }, '特別訪問週間', warnings);

  var actualPlanMap = audit_safeLoad_(function() {
    return audit_loadActualPlans_(ss, tz, weekStartStrNorm, weekEndStr);
  }, '割当結果', warnings);

  return {
    weekStartStr: weekStartStrNorm,
    weekEndStr: weekEndStr,
    weekDates: weekDates,
    tz: tz,
    patientMasterMap: patientMasterMap,
    staffMasterMap: staffMasterMap,
    changeMap: changeMap,
    eventMap: eventMap,
    specialWeekMap: specialWeekMap,
    actualPlanMap: actualPlanMap,
    warnings: warnings,
    loadedAt: new Date().toISOString()
  };
}

/**
 * 安全にデータをロード（エラー時は空オブジェクトを返し、警告を記録）
 * @param {Function} loadFn - ロード関数
 * @param {string} sourceName - データソース名（警告用）
 * @param {Array} warnings - 警告配列
 * @return {Object}
 */
function audit_safeLoad_(loadFn, sourceName, warnings) {
  try {
    return loadFn();
  } catch (e) {
    warnings.push({
      type: 'LOAD_ERROR',
      source: sourceName,
      message: e.message || String(e)
    });
    return {};
  }
}

// ============================================================
// 患者マスタ読み込み
// ============================================================

/**
 * 患者マスタをロードしてMap化
 * @param {Spreadsheet} ss
 * @param {string} tz
 * @return {Object} pid -> patientInfo
 */
function audit_loadPatientMaster_(ss, tz) {
  var sheet = ss.getSheetByName(SHEETS.PATIENT_MASTER);
  if (!sheet || sheet.getLastRow() <= 1) return {};

  var values = sheet.getDataRange().getValues();
  var header = values[0].map(function(h) { return String(h || '').trim(); });
  var data = values.slice(1);

  var idx = {
    pid: audit_findHeaderIdx_(header, 'patient_id'),
    name: audit_findHeaderIdx_(header, '患者名'),
    area: audit_findHeaderIdx_(header, 'エリア'),
    svcMin: audit_findHeaderIdx_(header, 'サービス時間'),
    timeType: audit_findHeaderIdx_(header, '時間タイプ'),
    startPref: audit_findHeaderIdx_(header, '希望時間帯（開始）'),
    endPref: audit_findHeaderIdx_(header, '希望時間帯（終了）'),
    prefDays: audit_findHeaderIdx_(header, '希望曜日'),
    ngDays: audit_findHeaderIdx_(header, '曜日NG'),
    needStaff: audit_findHeaderIdx_(header, '必要スタッフ数'),
    sexLimit: audit_findHeaderIdx_(header, '性別制限'),
    contPref: audit_findHeaderIdx_(header, '継続希望'),
    fixedStaff: audit_findHeaderIdx_(header, '指定スタッフID'),
    fixedType: audit_findHeaderIdx_(header, '指定タイプ'),
    ngStaff: audit_findHeaderIdx_(header, 'NGスタッフID'),
    weeklyCount: audit_findHeaderIdx_(header, '週訪問回数')
  };

  var map = {};
  data.forEach(function(row) {
    var pid = idx.pid >= 0 ? String(row[idx.pid] || '').trim() : '';
    if (!pid) return;

    map[pid] = {
      pid: pid,
      name: idx.name >= 0 ? String(row[idx.name] || '') : '',
      area: idx.area >= 0 ? String(row[idx.area] || '') : '',
      svcMin: idx.svcMin >= 0 ? audit_toNumber_(row[idx.svcMin], 60) : 60,
      timeType: idx.timeType >= 0 ? String(row[idx.timeType] || '').trim() : '',
      startPref: idx.startPref >= 0 ? audit_parseTimeToMin_(row[idx.startPref]) : null,
      endPref: idx.endPref >= 0 ? audit_parseTimeToMin_(row[idx.endPref]) : null,
      prefDays: idx.prefDays >= 0 ? audit_parseDays_(row[idx.prefDays]) : [],
      ngDays: idx.ngDays >= 0 ? audit_parseDays_(row[idx.ngDays]) : [],
      needStaff: idx.needStaff >= 0 ? audit_toNumber_(row[idx.needStaff], 1) : 1,
      sexLimit: idx.sexLimit >= 0 ? String(row[idx.sexLimit] || '') : '',
      contPref: idx.contPref >= 0 ? String(row[idx.contPref] || '') : '',
      fixedStaff: idx.fixedStaff >= 0 ? String(row[idx.fixedStaff] || '') : '',
      fixedType: idx.fixedType >= 0 ? String(row[idx.fixedType] || '') : '',
      ngStaff: idx.ngStaff >= 0 ? String(row[idx.ngStaff] || '') : '',
      weeklyCount: idx.weeklyCount >= 0 ? audit_toNumber_(row[idx.weeklyCount], 0) : 0
    };
  });

  return map;
}

// ============================================================
// スタッフマスタ読み込み
// ============================================================

/**
 * スタッフマスタをロードしてMap化
 * @param {Spreadsheet} ss
 * @param {string} tz
 * @return {Object} staffId -> staffInfo
 */
function audit_loadStaffMaster_(ss, tz) {
  var sheet = ss.getSheetByName(SHEETS.STAFF_MASTER);
  if (!sheet || sheet.getLastRow() <= 1) return {};

  var values = sheet.getDataRange().getValues();
  var header = values[0].map(function(h) { return String(h || '').trim(); });
  var data = values.slice(1);

  var idx = {
    staffId: audit_findHeaderIdx_(header, 'staff_id'),
    name: audit_findHeaderIdx_(header, 'スタッフ名')
  };

  var map = {};
  data.forEach(function(row) {
    var staffId = idx.staffId >= 0 ? String(row[idx.staffId] || '').trim() : '';
    if (!staffId) return;

    map[staffId] = {
      staffId: staffId,
      name: idx.name >= 0 ? String(row[idx.name] || '') : ''
    };
  });

  return map;
}

// ============================================================
// 個別変更リクエスト読み込み
// ============================================================

/**
 * 個別変更リクエストをロードしてMap化（週範囲内のみ）
 * @param {Spreadsheet} ss
 * @param {string} tz
 * @param {string} weekStartStr
 * @param {string} weekEndStr
 * @return {Object} pid|dateStr -> changeRecord（同日最新1件）
 */
function audit_loadChangeRequests_(ss, tz, weekStartStr, weekEndStr) {
  var sheet = ss.getSheetByName(SHEETS.CHANGE_REQUEST);
  if (!sheet || sheet.getLastRow() <= 1) return {};

  var values = sheet.getDataRange().getValues();
  var header = values[0].map(function(h) { return String(h || '').trim(); });
  var data = values.slice(1);

  var idx = {
    pid: audit_findHeaderIdx_(header, 'patient_id'),
    name: audit_findHeaderIdx_(header, '患者名'),
    date: audit_findHeaderIdx_(header, '日付'),
    op: audit_findHeaderIdx_(header, '操作'),
    newStart: audit_findHeaderIdx_(header, '開始時刻(固定)'),
    newEnd: audit_findHeaderIdx_(header, '終了時刻(固定)'),
    newTimeType: audit_findHeaderIdx_(header, '時間タイプ'),
    newEarliest: audit_findHeaderIdx_(header, '希望最早'),
    newLatest: audit_findHeaderIdx_(header, '希望最遅'),
    note: audit_findHeaderIdx_(header, '備考'),
    regAt: audit_findHeaderIdx_(header, '登録日時')
  };

  var map = {};
  data.forEach(function(row, rowIdx) {
    var pid = idx.pid >= 0 ? String(row[idx.pid] || '').trim() : '';
    if (!pid) return;

    var dateObj = idx.date >= 0 ? audit_parseDate_(row[idx.date]) : null;
    if (!dateObj) return;

    var dateStr = audit_formatDateStr_(dateObj, tz);
    if (dateStr < weekStartStr || dateStr > weekEndStr) return;

    var op = idx.op >= 0 ? String(row[idx.op] || '').trim() : '';
    if (!op) return;

    // 表記揺れ補正
    if (op === '取消') op = 'キャンセル';

    var key = audit_makePdKey_(pid, dateStr);

    // ソートキー（登録日時 or 行番号）
    var sortKey = rowIdx;
    if (idx.regAt >= 0) {
      var regDate = audit_parseDate_(row[idx.regAt]);
      if (regDate) sortKey = regDate.getTime();
    }

    var record = {
      pid: pid,
      pname: idx.name >= 0 ? String(row[idx.name] || '') : '',
      dateStr: dateStr,
      op: op,
      newStartMin: idx.newStart >= 0 ? audit_parseTimeToMin_(row[idx.newStart]) : null,
      newEndMin: idx.newEnd >= 0 ? audit_parseTimeToMin_(row[idx.newEnd]) : null,
      newTimeType: idx.newTimeType >= 0 ? String(row[idx.newTimeType] || '').trim() : '',
      newEarliestMin: idx.newEarliest >= 0 ? audit_parseTimeToMin_(row[idx.newEarliest]) : null,
      newLatestMin: idx.newLatest >= 0 ? audit_parseTimeToMin_(row[idx.newLatest]) : null,
      note: idx.note >= 0 ? String(row[idx.note] || '') : '',
      sortKey: sortKey
    };

    // 同日複数は登録日時で最新のみ採用
    if (!map[key] || sortKey > map[key].sortKey) {
      map[key] = record;
    }
  });

  return map;
}

// ============================================================
// イベントリクエスト読み込み（患者紐づきのみ）
// ============================================================

/**
 * 患者紐づきイベントをロードしてMap化（週範囲内のみ）
 * @param {Spreadsheet} ss
 * @param {string} tz
 * @param {string} weekStartStr
 * @param {string} weekEndStr
 * @return {Object} pid|dateStr -> eventRecords[]
 */
function audit_loadPatientLinkedEvents_(ss, tz, weekStartStr, weekEndStr) {
  var sheet = ss.getSheetByName(SHEETS.EVENT_REQUEST);
  if (!sheet || sheet.getLastRow() <= 1) return {};

  var values = sheet.getDataRange().getValues();
  var header = values[0].map(function(h) { return String(h || '').trim(); });
  var data = values.slice(1);

  var idx = {
    eventId: audit_findHeaderIdx_(header, 'event_id'),
    staffId: audit_findHeaderIdx_(header, 'staff_id'),
    date: audit_findHeaderIdx_(header, '日付'),
    title: audit_findHeaderIdx_(header, 'タイトル'),
    timeMode: audit_findHeaderIdx_(header, '時間指定方法'),
    startTime: audit_findHeaderIdx_(header, '開始時刻'),
    endTime: audit_findHeaderIdx_(header, '終了時刻'),
    durationMin: audit_findHeaderIdx_(header, '所要時間'),
    patientLinked: audit_findHeaderIdx_(header, '患者紐づき'),
    patientId: audit_findHeaderIdx_(header, 'patient_id'),
    patientAffect: audit_findHeaderIdx_(header, '患者影響')
  };

  var map = {};
  data.forEach(function(row) {
    // 患者紐づきチェック
    var isLinked = idx.patientLinked >= 0 &&
      (row[idx.patientLinked] === true || row[idx.patientLinked] === 'TRUE');
    if (!isLinked) return;

    var pid = idx.patientId >= 0 ? String(row[idx.patientId] || '').trim() : '';
    if (!pid) return;

    var dateObj = idx.date >= 0 ? audit_parseDate_(row[idx.date]) : null;
    if (!dateObj) return;

    var dateStr = audit_formatDateStr_(dateObj, tz);
    if (dateStr < weekStartStr || dateStr > weekEndStr) return;

    var startMin = idx.startTime >= 0 ? audit_parseTimeToMin_(row[idx.startTime]) : null;
    var endMin = idx.endTime >= 0 ? audit_parseTimeToMin_(row[idx.endTime]) : null;

    // duration から endMin を補完
    if (startMin !== null && endMin === null) {
      var duration = idx.durationMin >= 0 ? audit_toNumber_(row[idx.durationMin], 60) : 60;
      endMin = startMin + duration;
    }

    var key = audit_makePdKey_(pid, dateStr);
    if (!map[key]) map[key] = [];

    map[key].push({
      eventId: idx.eventId >= 0 ? String(row[idx.eventId] || '') : '',
      pid: pid,
      dateStr: dateStr,
      title: idx.title >= 0 ? String(row[idx.title] || '') : '',
      timeMode: idx.timeMode >= 0 ? String(row[idx.timeMode] || '') : '',
      startMin: startMin,
      endMin: endMin,
      patientAffect: idx.patientAffect >= 0 ? String(row[idx.patientAffect] || '') : ''
    });
  });

  return map;
}

// ============================================================
// 特別訪問週間読み込み
// ============================================================

/**
 * 特別訪問週間をロードしてMap化（週範囲内のみ）
 * @param {Spreadsheet} ss
 * @param {string} tz
 * @param {string} weekStartStr
 * @param {string} weekEndStr
 * @return {Object} pid|dateStr -> specialRecords[]（mode付き）
 */
function audit_loadSpecialWeek_(ss, tz, weekStartStr, weekEndStr) {
  var headerSheet = ss.getSheetByName('特別訪問週間_ヘッダ');
  var detailSheet = ss.getSheetByName('特別訪問週間_明細');

  if (!headerSheet || !detailSheet) return {};
  if (headerSheet.getLastRow() <= 1 || detailSheet.getLastRow() <= 1) return {};

  // ヘッダー読み込み
  var hValues = headerSheet.getDataRange().getValues();
  var hHeader = hValues[0].map(function(h) { return String(h || '').trim(); });
  var hData = hValues.slice(1);

  var hIdx = {
    specialId: audit_findHeaderIdx_(hHeader, 'special_week_id'),
    pid: audit_findHeaderIdx_(hHeader, 'patient_id'),
    pname: audit_findHeaderIdx_(hHeader, '患者名'),
    weekStart: audit_findHeaderIdx_(hHeader, '週開始日'),
    weekEnd: audit_findHeaderIdx_(hHeader, '週終了日'),
    mode: audit_findHeaderIdx_(hHeader, '適用モード'),
    status: audit_findHeaderIdx_(hHeader, '状態')
  };

  // 旧フォーマット対応
  if (hIdx.specialId < 0) hIdx.specialId = audit_findHeaderIdx_(hHeader, 'special_id');
  if (hIdx.mode < 0) hIdx.mode = audit_findHeaderIdx_(hHeader, 'モード');

  // ヘッダーをspecialId -> info のマップに
  var headerMap = {};
  hData.forEach(function(row) {
    var specialId = hIdx.specialId >= 0 ? String(row[hIdx.specialId] || '').trim() : '';
    if (!specialId) return;

    var wStart = hIdx.weekStart >= 0 ? audit_parseDate_(row[hIdx.weekStart]) : null;
    if (!wStart) return;

    var wStartStr = audit_formatDateStr_(wStart, tz);
    // 週開始日が対象週と一致するもののみ
    if (wStartStr !== weekStartStr) return;

    var status = hIdx.status >= 0 ? String(row[hIdx.status] || '').trim() : '';
    // status が 'active' 以外（削除済み等）は除外（statusがない場合は含める）
    if (status && status !== 'active' && status !== '') return;

    headerMap[specialId] = {
      specialId: specialId,
      pid: hIdx.pid >= 0 ? String(row[hIdx.pid] || '').trim() : '',
      pname: hIdx.pname >= 0 ? String(row[hIdx.pname] || '') : '',
      mode: hIdx.mode >= 0 ? String(row[hIdx.mode] || '').trim().toUpperCase() : 'ADD'
    };
  });

  // 明細読み込み
  var dValues = detailSheet.getDataRange().getValues();
  var dHeader = dValues[0].map(function(h) { return String(h || '').trim(); });
  var dData = dValues.slice(1);

  var isNewFormat = audit_findHeaderIdx_(dHeader, 'special_week_id') >= 0;
  var dIdx = {
    specialId: isNewFormat ? audit_findHeaderIdx_(dHeader, 'special_week_id') : audit_findHeaderIdx_(dHeader, 'special_id'),
    pid: audit_findHeaderIdx_(dHeader, 'patient_id'),
    date: audit_findHeaderIdx_(dHeader, '日付'),
    dayOfWeek: audit_findHeaderIdx_(dHeader, '曜日'),
    rowLabel: audit_findHeaderIdx_(dHeader, '行ラベル'),
    timeType: isNewFormat ? audit_findHeaderIdx_(dHeader, '時間タイプ') : audit_findHeaderIdx_(dHeader, 'timeType'),
    start: audit_findHeaderIdx_(dHeader, '開始時刻'),
    end: audit_findHeaderIdx_(dHeader, '終了時刻'),
    earliest: isNewFormat ? audit_findHeaderIdx_(dHeader, '希望最早') : audit_findHeaderIdx_(dHeader, '希望最早時刻'),
    latest: isNewFormat ? audit_findHeaderIdx_(dHeader, '希望最遅') : audit_findHeaderIdx_(dHeader, '希望最遅時刻'),
    svcMin: audit_findHeaderIdx_(dHeader, 'サービス時間'),
    needStaff: audit_findHeaderIdx_(dHeader, '必要スタッフ数'),
    note: audit_findHeaderIdx_(dHeader, '備考'),
    changeHandle: audit_findHeaderIdx_(dHeader, '個別変更の扱い')
  };

  var map = {};
  dData.forEach(function(row) {
    var specialId = dIdx.specialId >= 0 ? String(row[dIdx.specialId] || '').trim() : '';
    var headerInfo = headerMap[specialId];
    if (!headerInfo) return;  // 対象週のヘッダーに紐づかない明細はスキップ

    var pid = headerInfo.pid;
    if (!pid) return;

    var dateObj = dIdx.date >= 0 ? audit_parseDate_(row[dIdx.date]) : null;
    if (!dateObj) return;

    var dateStr = audit_formatDateStr_(dateObj, tz);
    if (dateStr < weekStartStr || dateStr > weekEndStr) return;

    var key = audit_makePdKey_(pid, dateStr);
    if (!map[key]) map[key] = [];

    var timeType = dIdx.timeType >= 0 ? String(row[dIdx.timeType] || '').trim() : '時間帯';

    map[key].push({
      specialId: specialId,
      pid: pid,
      pname: headerInfo.pname,
      dateStr: dateStr,
      mode: headerInfo.mode,  // ADD or REPLACE
      rowLabel: dIdx.rowLabel >= 0 ? String(row[dIdx.rowLabel] || '') : '',
      timeType: timeType,
      startMin: dIdx.start >= 0 ? audit_parseTimeToMin_(row[dIdx.start]) : null,
      endMin: dIdx.end >= 0 ? audit_parseTimeToMin_(row[dIdx.end]) : null,
      earliestMin: dIdx.earliest >= 0 ? audit_parseTimeToMin_(row[dIdx.earliest]) : null,
      latestMin: dIdx.latest >= 0 ? audit_parseTimeToMin_(row[dIdx.latest]) : null,
      svcMin: dIdx.svcMin >= 0 ? audit_toNumber_(row[dIdx.svcMin], 60) : 60,
      needStaff: dIdx.needStaff >= 0 ? audit_toNumber_(row[dIdx.needStaff], 1) : 1,
      note: dIdx.note >= 0 ? String(row[dIdx.note] || '') : '',
      changeHandle: dIdx.changeHandle >= 0 ? String(row[dIdx.changeHandle] || '') : ''
    });
  });

  return map;
}

// ============================================================
// 割当結果読み込み（実績＝Actual）
// ============================================================

/**
 * 割当結果（実績）をロードしてMap化（週範囲内のみ）
 * @param {Spreadsheet} ss
 * @param {string} tz
 * @param {string} weekStartStr
 * @param {string} weekEndStr
 * @return {Object} pid|dateStr -> actualRecords[]
 */
function audit_loadActualPlans_(ss, tz, weekStartStr, weekEndStr) {
  var sheet = ss.getSheetByName(SHEETS.ASSIGN_RESULT);
  console.log('audit_loadActualPlans_: シート存在 =', !!sheet);
  if (!sheet || sheet.getLastRow() <= 1) {
    console.log('audit_loadActualPlans_: シートが空または存在しない');
    return {};
  }

  var values = sheet.getDataRange().getValues();
  var header = values[0].map(function(h) { return String(h || '').trim(); });
  var data = values.slice(1);
  console.log('audit_loadActualPlans_: 全行数 =', data.length, ', 週範囲 =', weekStartStr, '〜', weekEndStr);

  var idx = {
    visitId: audit_findHeaderIdx_(header, 'visit_id'),
    date: audit_findHeaderIdx_(header, '日付'),
    youbi: audit_findHeaderIdx_(header, '曜日'),
    staffId: audit_findHeaderIdx_(header, 'staff_id'),
    staffName: audit_findHeaderIdx_(header, 'スタッフ名'),
    pid: audit_findHeaderIdx_(header, 'patient_id'),
    pname: audit_findHeaderIdx_(header, '患者名'),
    area: audit_findHeaderIdx_(header, 'エリア'),
    start: audit_findHeaderIdx_(header, '開始時刻'),
    end: audit_findHeaderIdx_(header, '終了時刻'),
    svcMin: audit_findHeaderIdx_(header, 'サービス時間'),
    timeType: audit_findHeaderIdx_(header, '時間タイプ'),
    earliest: audit_findHeaderIdx_(header, '希望最早時刻'),
    latest: audit_findHeaderIdx_(header, '希望最遅時刻'),
    note: audit_findHeaderIdx_(header, '備考')
  };

  var map = {};
  var debugCounts = { total: 0, evSkipped: 0, noPid: 0, noDate: 0, outOfRange: 0, added: 0 };
  data.forEach(function(row) {
    debugCounts.total++;
    var visitId = idx.visitId >= 0 ? String(row[idx.visitId] || '').trim() : '';
    // EV_ で始まるものはイベント行なのでスキップ
    if (visitId.indexOf('EV_') === 0) { debugCounts.evSkipped++; return; }

    var pid = idx.pid >= 0 ? String(row[idx.pid] || '').trim() : '';
    if (!pid) { debugCounts.noPid++; return; }

    var dateObj = idx.date >= 0 ? audit_parseDate_(row[idx.date]) : null;
    if (!dateObj) { debugCounts.noDate++; return; }

    var dateStr = audit_formatDateStr_(dateObj, tz);
    if (dateStr < weekStartStr || dateStr > weekEndStr) { debugCounts.outOfRange++; return; }
    debugCounts.added++;

    var staffId = idx.staffId >= 0 ? String(row[idx.staffId] || '').trim() : '';

    var key = audit_makePdKey_(pid, dateStr);
    if (!map[key]) map[key] = [];

    map[key].push({
      visitId: visitId,
      pid: pid,
      pname: idx.pname >= 0 ? String(row[idx.pname] || '') : '',
      dateStr: dateStr,
      youbi: idx.youbi >= 0 ? String(row[idx.youbi] || '') : '',
      staffId: staffId,
      staffName: idx.staffName >= 0 ? String(row[idx.staffName] || '') : '',
      area: idx.area >= 0 ? String(row[idx.area] || '') : '',
      startMin: idx.start >= 0 ? audit_parseTimeToMin_(row[idx.start]) : null,
      endMin: idx.end >= 0 ? audit_parseTimeToMin_(row[idx.end]) : null,
      svcMin: idx.svcMin >= 0 ? audit_toNumber_(row[idx.svcMin], 60) : 60,
      timeType: idx.timeType >= 0 ? String(row[idx.timeType] || '') : '',
      earliestMin: idx.earliest >= 0 ? audit_parseTimeToMin_(row[idx.earliest]) : null,
      latestMin: idx.latest >= 0 ? audit_parseTimeToMin_(row[idx.latest]) : null,
      note: idx.note >= 0 ? String(row[idx.note] || '') : '',
      isUnassigned: !staffId
    });
  });

  console.log('audit_loadActualPlans_: フィルタ結果', JSON.stringify(debugCounts));
  console.log('audit_loadActualPlans_: map keys =', Object.keys(map).length);
  return map;
}

// ============================================================
// キャッシュ関連
// ============================================================

/**
 * データセットをキャッシュから取得（なければロード）
 * @param {string} weekStartStr
 * @return {Object}
 */
function audit_getOrLoadDataset_(weekStartStr) {
  var cacheKey = AUDIT_CONFIG.CACHE_KEY_PREFIX + weekStartStr.replace(/\//g, '');
  var cache = CacheService.getScriptCache();

  // キャッシュから取得を試みる
  try {
    var cached = cache.get(cacheKey);
    if (cached) {
      var parsed = JSON.parse(cached);
      // キャッシュにExpectedがなければ再生成
      if (!parsed.expectedByPidDate) {
        audit_enrichDatasetWithExpected_(parsed);
      }
      parsed.fromCache = true;
      return parsed;
    }
  } catch (e) {
    // パース失敗時は再ロード
    console.log('Cache parse error, reloading: ' + e.message);
  }

  // ロード
  var dataset = audit_loadWeekDataset_(weekStartStr);

  // Expected（期待）を生成してデータセットに付与
  audit_enrichDatasetWithExpected_(dataset);

  // キャッシュに保存（サイズ超過時は分割保存を試みる）
  audit_saveToCache_(cache, cacheKey, dataset);

  dataset.fromCache = false;
  return dataset;
}

/**
 * データセットをキャッシュに保存
 * GAS CacheServiceは100KB制限があるため、サイズ超過時は主要部分のみ保存
 * @param {Cache} cache
 * @param {string} cacheKey
 * @param {Object} dataset
 */
function audit_saveToCache_(cache, cacheKey, dataset) {
  try {
    var json = JSON.stringify(dataset);
    var sizeKB = json.length / 1024;

    if (sizeKB <= 95) {
      // 100KB以下なら直接保存
      cache.put(cacheKey, json, AUDIT_CONFIG.CACHE_TTL_SEC);
    } else {
      // サイズ超過時はExpectedを除いた基本データのみ保存
      var lightDataset = {
        weekStartStr: dataset.weekStartStr,
        weekEndStr: dataset.weekEndStr,
        weekDates: dataset.weekDates,
        tz: dataset.tz,
        patientMasterMap: dataset.patientMasterMap,
        staffMasterMap: dataset.staffMasterMap,
        changeMap: dataset.changeMap,
        eventMap: dataset.eventMap,
        specialWeekMap: dataset.specialWeekMap,
        actualPlanMap: dataset.actualPlanMap,
        loadedAt: dataset.loadedAt
        // expectedByPidDate は除外（再生成可能なため）
      };
      var lightJson = JSON.stringify(lightDataset);
      if (lightJson.length / 1024 <= 95) {
        cache.put(cacheKey, lightJson, AUDIT_CONFIG.CACHE_TTL_SEC);
      }
      // それでも大きい場合はキャッシュを諦める
    }
  } catch (e) {
    // キャッシュ保存失敗は無視（サイズ超過等）
    console.log('Cache save error (ignored): ' + e.message);
  }
}

/**
 * 監査データセットキャッシュをクリア
 * @param {string} weekStartStr - 特定週のみクリア（省略時は全クリア）
 */
function audit_clearCache(weekStartStr) {
  var cache = CacheService.getScriptCache();

  if (weekStartStr) {
    var cacheKey = AUDIT_CONFIG.CACHE_KEY_PREFIX + weekStartStr.replace(/\//g, '');
    cache.remove(cacheKey);
  } else {
    // 全キャッシュクリア（スクリプトキャッシュ全体）
    // 注意: 他の機能のキャッシュも消える
    // cache.removeAll([]); // 引数が必要なため使えない
    // 代替として、最近4週間分を消す
    var today = new Date();
    for (var i = -2; i <= 2; i++) {
      var d = new Date(today);
      d.setDate(d.getDate() + i * 7);
      var weekStart = audit_getWeekStart_(d);
      var tz = SpreadsheetApp.openById(SPREADSHEET_ID).getSpreadsheetTimeZone();
      var dateStr = audit_formatDateStr_(weekStart, tz);
      if (dateStr) {
        var key = AUDIT_CONFIG.CACHE_KEY_PREFIX + dateStr.replace(/\//g, '');
        cache.remove(key);
      }
    }
  }
}
