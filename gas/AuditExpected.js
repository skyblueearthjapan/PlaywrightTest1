/**
 * 監査（合否判定）ビュー - Expected（期待）生成エンジン
 *
 * 患者マスタ・個別変更・特別訪問週間から「期待」を構築する
 * 優先順位: SPECIAL_REPLACE > CHANGE(キャンセル/時間変更) > MASTER > SPECIAL_ADD > CHANGE(追加)
 */

// ============================================================
// Expected生成メイン
// ============================================================

/**
 * 週単位の期待（Expected）を生成
 * @param {Object} dataset - audit_loadWeekDataset_ の戻り値
 * @return {Object} expectedByPidDate - pid|dateStr -> DayExpected
 */
function audit_buildExpected_(dataset) {
  var expectedByPidDate = {};
  var weekDates = dataset.weekDates;
  var patientMasterMap = dataset.patientMasterMap;
  var changeMap = dataset.changeMap;
  var specialWeekMap = dataset.specialWeekMap;
  var actualPlanMap = dataset.actualPlanMap;

  // 対象患者を決定（実績に出現する患者 + マスタ全員）
  var targetPids = {};

  // マスタから取得
  for (var pid in patientMasterMap) {
    targetPids[pid] = true;
  }

  // 実績から取得
  for (var key in actualPlanMap) {
    var actuals = actualPlanMap[key];
    actuals.forEach(function(a) {
      if (a.pid) targetPids[a.pid] = true;
    });
  }

  // 各患者×各日で期待を生成
  for (var pid in targetPids) {
    var master = patientMasterMap[pid];

    weekDates.forEach(function(wd) {
      var dateStr = wd.dateStr;
      var youbi = wd.youbi;
      var key = audit_makePdKey_(pid, dateStr);

      var dayExpected = {
        key: key,
        visits: [],
        meta: {
          hasReplace: false,
          hasCancel: false,
          appliedChangeOp: null,
          appliedSpecialMode: null
        }
      };

      // 特別訪問週間（同日のもの）を取得
      var specials = specialWeekMap[key] || [];
      var replaceDetails = specials.filter(function(sp) { return sp.mode === 'REPLACE'; });
      var addDetails = specials.filter(function(sp) { return sp.mode === 'ADD'; });

      // 個別変更（同日最新1件）を取得
      var change = changeMap[key] || null;

      // === (1) SPECIAL_REPLACE がある場合：通常期待を置換 ===
      if (replaceDetails.length > 0) {
        dayExpected.meta.hasReplace = true;
        dayExpected.meta.appliedSpecialMode = 'REPLACE';

        replaceDetails.forEach(function(det) {
          var exp = audit_buildExpectedFromSpecial_(pid, dateStr, det, 'SPECIAL_REPLACE', master);
          dayExpected.visits.push(exp);
        });

      } else {
        // === (2) MASTER（通常期待）を作る ===
        if (master && audit_isMasterVisitDay_(master, youbi)) {
          var exp = audit_buildExpectedFromMaster_(pid, dateStr, master);
          dayExpected.visits.push(exp);
        }
      }

      // === (3) CHANGE（キャンセル/時間変更/追加）を適用 ===
      if (change) {
        if (change.op === AUDIT_OP.CANCEL) {
          // キャンセル：既存の期待枠をキャンセル扱いにする
          dayExpected.meta.hasCancel = true;
          dayExpected.meta.appliedChangeOp = 'キャンセル';

          dayExpected.visits.forEach(function(exp) {
            exp.op = 'キャンセル';
            exp.isCancelled = true;
            exp.note = (exp.note ? exp.note + ' | ' : '') + 'change:キャンセル';
          });

          // 期待がない場合でもキャンセルを記録
          if (dayExpected.visits.length === 0) {
            var cancelExp = audit_buildExpectedFromChange_(pid, dateStr, change, master);
            cancelExp.op = 'キャンセル';
            cancelExp.isCancelled = true;
            dayExpected.visits.push(cancelExp);
          }

        } else if (change.op === AUDIT_OP.TIME_CHANGE) {
          // 時間変更：メイン枠の時間を変更
          dayExpected.meta.appliedChangeOp = '時間変更';

          var targetIdx = audit_findPrimaryExpectedIndex_(dayExpected.visits);
          if (targetIdx >= 0) {
            var exp = dayExpected.visits[targetIdx];
            audit_applyTimeChange_(exp, change, master);
            exp.op = '時間変更';
            exp.source = 'CHANGE';
            exp.note = (exp.note ? exp.note + ' | ' : '') + 'change:時間変更';
          } else {
            // 期待がない日に時間変更 → 追加扱い
            var newExp = audit_buildExpectedFromChange_(pid, dateStr, change, master);
            newExp.op = '時間変更(期待無し)';
            newExp.source = 'CHANGE';
            dayExpected.visits.push(newExp);
          }

        } else if (change.op === AUDIT_OP.ADD) {
          // 追加：新しい期待枠を追加
          dayExpected.meta.appliedChangeOp = '追加';

          var addExp = audit_buildExpectedFromChange_(pid, dateStr, change, master);
          addExp.op = '追加';
          addExp.source = 'CHANGE';
          dayExpected.visits.push(addExp);
        }
      }

      // === (4) SPECIAL_ADD を最後に積む ===
      if (addDetails.length > 0) {
        if (!dayExpected.meta.appliedSpecialMode) {
          dayExpected.meta.appliedSpecialMode = 'ADD';
        }

        addDetails.forEach(function(det) {
          var exp = audit_buildExpectedFromSpecial_(pid, dateStr, det, 'SPECIAL_ADD', master);
          dayExpected.visits.push(exp);
        });
      }

      // === (5) expected_id 付番 ===
      if (dayExpected.visits.length > 0) {
        // 開始時刻順でソート
        dayExpected.visits.sort(function(a, b) {
          var aMin = a.startMin !== null ? a.startMin : (a.earliestMin !== null ? a.earliestMin : 9999);
          var bMin = b.startMin !== null ? b.startMin : (b.earliestMin !== null ? b.earliestMin : 9999);
          return aMin - bMin;
        });

        // ID付番
        dayExpected.visits.forEach(function(exp, i) {
          exp.expected_id = 'EXP|' + pid + '|' + dateStr + '|' + ('0' + (i + 1)).slice(-2);
        });

        expectedByPidDate[key] = dayExpected;
      }
    });
  }

  return expectedByPidDate;
}

// ============================================================
// 期待構築ヘルパー
// ============================================================

/**
 * マスタからExpectedVisitを構築
 */
function audit_buildExpectedFromMaster_(pid, dateStr, master) {
  var timeWindow = audit_makeTimeWindow_(master.timeType, master.startPref, master.endPref, master.svcMin);

  return {
    expected_id: null,
    pid: pid,
    dateStr: dateStr,
    source: AUDIT_SOURCE.MASTER,
    op: '',
    isCancelled: false,
    timeType: timeWindow.timeType,
    startMin: timeWindow.startMin,
    endMin: timeWindow.endMin,
    earliestMin: timeWindow.earliestMin,
    latestMin: timeWindow.latestMin,
    svcMin: master.svcMin || 60,
    needStaff: master.needStaff || 1,
    constraints: {
      sexLimit: master.sexLimit,
      contPref: master.contPref,
      fixedStaff: master.fixedStaff,
      fixedType: master.fixedType,
      ngStaff: master.ngStaff
    },
    note: 'MASTER'
  };
}

/**
 * 特別訪問週間からExpectedVisitを構築
 */
function audit_buildExpectedFromSpecial_(pid, dateStr, detail, source, master) {
  var timeType = detail.timeType || '時間帯';
  var startMin = detail.startMin;
  var endMin = detail.endMin;
  var earliestMin = detail.earliestMin;
  var latestMin = detail.latestMin;
  var svcMin = detail.svcMin || (master ? master.svcMin : 60) || 60;

  // timeType に応じてデフォルトを適用
  if (timeType === '固定') {
    // 固定の場合は start/end を使用
    if (startMin === null && earliestMin !== null) startMin = earliestMin;
    if (endMin === null && startMin !== null) endMin = startMin + svcMin;
  } else {
    // 時間帯/午前/午後/終日の場合
    var defaults = AUDIT_TIME_DEFAULTS[timeType];
    if (defaults) {
      if (earliestMin === null) earliestMin = defaults.earliestMin;
      if (latestMin === null) latestMin = defaults.latestMin;
    }
    if (earliestMin === null && startMin !== null) earliestMin = startMin;
    if (latestMin === null && endMin !== null) latestMin = endMin;
  }

  return {
    expected_id: null,
    pid: pid,
    dateStr: dateStr,
    source: source,
    op: source === 'SPECIAL_REPLACE' ? '置換' : '追加',
    isCancelled: false,
    timeType: timeType,
    startMin: startMin,
    endMin: endMin,
    earliestMin: earliestMin,
    latestMin: latestMin,
    svcMin: svcMin,
    needStaff: detail.needStaff || (master ? master.needStaff : 1) || 1,
    constraints: master ? {
      sexLimit: master.sexLimit,
      contPref: master.contPref,
      fixedStaff: master.fixedStaff,
      fixedType: master.fixedType,
      ngStaff: master.ngStaff
    } : {},
    note: source + (detail.rowLabel ? '(' + detail.rowLabel + ')' : '')
  };
}

/**
 * 個別変更からExpectedVisitを構築
 */
function audit_buildExpectedFromChange_(pid, dateStr, change, master) {
  var svcMin = (master ? master.svcMin : 60) || 60;

  // 新時間タイプが指定されている場合はそれを使用
  var timeType = change.newTimeType || (master ? master.timeType : '') || '時間帯';

  var startMin, endMin, earliestMin, latestMin;

  if (change.newTimeType) {
    // 新時間タイプが指定されている場合、時間窓を再計算
    var win = audit_makeTimeWindow_(change.newTimeType,
      change.newEarliestMin !== null ? audit_minToTimeStr_(change.newEarliestMin) : (change.newStartMin !== null ? audit_minToTimeStr_(change.newStartMin) : null),
      change.newLatestMin !== null ? audit_minToTimeStr_(change.newLatestMin) : (change.newEndMin !== null ? audit_minToTimeStr_(change.newEndMin) : null),
      svcMin);
    startMin = win.startMin;
    endMin = win.endMin;
    earliestMin = win.earliestMin;
    latestMin = win.latestMin;
    timeType = win.timeType;
  } else {
    // 従来のロジック
    startMin = change.newStartMin;
    endMin = change.newEndMin;

    // 片方だけの場合はsvcMinで補完
    if (startMin !== null && endMin === null) {
      endMin = startMin + svcMin;
    }
    if (endMin !== null && startMin === null) {
      startMin = endMin - svcMin;
      if (startMin < 0) startMin = 0;
    }
    earliestMin = startMin;
    latestMin = endMin;
  }

  return {
    expected_id: null,
    pid: pid,
    dateStr: dateStr,
    source: AUDIT_SOURCE.CHANGE,
    op: change.op,
    isCancelled: change.op === AUDIT_OP.CANCEL,
    timeType: timeType,
    startMin: startMin,
    endMin: endMin,
    earliestMin: earliestMin,
    latestMin: latestMin,
    svcMin: svcMin,
    needStaff: (master ? master.needStaff : 1) || 1,
    constraints: master ? {
      sexLimit: master.sexLimit,
      contPref: master.contPref,
      fixedStaff: master.fixedStaff,
      fixedType: master.fixedType,
      ngStaff: master.ngStaff
    } : {},
    note: 'CHANGE(' + change.op + ')' + (change.newTimeType ? ' timeType:' + change.newTimeType : '')
  };
}

/**
 * 時間変更を期待枠に適用
 */
function audit_applyTimeChange_(exp, change, master) {
  var svcMin = exp.svcMin || (master ? master.svcMin : 60) || 60;

  // 新時間タイプが指定されている場合、時間窓を完全に上書き
  if (change.newTimeType) {
    var win = audit_makeTimeWindow_(change.newTimeType,
      change.newEarliestMin !== null ? audit_minToTimeStr_(change.newEarliestMin) : (change.newStartMin !== null ? audit_minToTimeStr_(change.newStartMin) : null),
      change.newLatestMin !== null ? audit_minToTimeStr_(change.newLatestMin) : (change.newEndMin !== null ? audit_minToTimeStr_(change.newEndMin) : null),
      svcMin);
    exp.timeType = win.timeType;
    exp.startMin = win.startMin;
    exp.endMin = win.endMin;
    exp.earliestMin = win.earliestMin;
    exp.latestMin = win.latestMin;
    return;
  }

  // 従来のロジック（固定時間変更）
  if (change.newStartMin !== null) {
    exp.startMin = change.newStartMin;
    exp.earliestMin = change.newStartMin;
  }
  if (change.newEndMin !== null) {
    exp.endMin = change.newEndMin;
    exp.latestMin = change.newEndMin;
  }

  // 片方だけの場合はsvcMinで補完
  if (exp.startMin !== null && exp.endMin === null) {
    exp.endMin = exp.startMin + svcMin;
    exp.latestMin = exp.endMin;
  }
  if (exp.endMin !== null && exp.startMin === null) {
    exp.startMin = exp.endMin - svcMin;
    if (exp.startMin < 0) exp.startMin = 0;
    exp.earliestMin = exp.startMin;
  }
}

/**
 * 時間窓を生成
 */
function audit_makeTimeWindow_(timeTypeRaw, startPref, endPref, svcMin) {
  var timeType = String(timeTypeRaw || '').trim() || '時間帯';
  svcMin = svcMin || 60;

  var startMin = audit_parseTimeToMin_(startPref);
  var endMin = audit_parseTimeToMin_(endPref);
  var earliestMin = null;
  var latestMin = null;

  if (timeType === '固定') {
    // 固定: start/end を使用
    if (startMin !== null && endMin === null) {
      endMin = startMin + svcMin;
    }
    earliestMin = startMin;
    latestMin = endMin;

  } else if (timeType === '時間帯') {
    // 時間帯: earliest/latest を希望時間から
    earliestMin = startMin;
    latestMin = endMin;

  } else {
    // 午前/午後/終日: デフォルト範囲を使用
    var defaults = AUDIT_TIME_DEFAULTS[timeType];
    if (defaults) {
      earliestMin = startMin !== null ? startMin : defaults.earliestMin;
      latestMin = endMin !== null ? endMin : defaults.latestMin;
    } else {
      earliestMin = startMin;
      latestMin = endMin;
    }
  }

  return {
    timeType: timeType,
    startMin: (timeType === '固定') ? startMin : null,
    endMin: (timeType === '固定') ? endMin : null,
    earliestMin: earliestMin,
    latestMin: latestMin
  };
}

/**
 * マスタの希望曜日に該当するか判定
 */
function audit_isMasterVisitDay_(master, youbi) {
  if (!master) return false;

  var prefDays = master.prefDays || [];
  var ngDays = master.ngDays || [];

  // prefDays が空の場合は全曜日
  if (prefDays.length === 0) {
    prefDays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  }

  // NG曜日を除外
  var candidates = prefDays.filter(function(d) {
    return ngDays.indexOf(d) < 0;
  });

  return candidates.indexOf(youbi) >= 0;
}

/**
 * メイン期待枠のインデックスを取得
 */
function audit_findPrimaryExpectedIndex_(visits) {
  if (!visits || visits.length === 0) return -1;

  // MASTER由来を優先
  for (var i = 0; i < visits.length; i++) {
    if (visits[i].source === AUDIT_SOURCE.MASTER) return i;
  }
  // SPECIAL_REPLACE由来
  for (var j = 0; j < visits.length; j++) {
    if (visits[j].source === AUDIT_SOURCE.SPECIAL_REPLACE) return j;
  }
  // その他は先頭
  return 0;
}
