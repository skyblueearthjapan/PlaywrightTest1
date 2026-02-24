/**
 * 監査（合否判定）ビュー - 判定エンジン
 *
 * Expected（期待）とActual（実績）を比較して○/△/×を判定
 */

// ============================================================
// 判定メイン
// ============================================================

/**
 * 患者×日の判定を実行
 * @param {Object} dataset - 週データセット
 * @param {Object} expectedByPidDate - 期待Map
 * @param {string} pid - 患者ID
 * @param {string} dateStr - 日付
 * @return {Object} { status, tags, checks }
 */
function audit_judgePatientDay_(dataset, expectedByPidDate, pid, dateStr) {
  var key = audit_makePdKey_(pid, dateStr);
  var dayExpected = expectedByPidDate[key];
  var actuals = dataset.actualPlanMap[key] || [];
  var events = dataset.eventMap[key] || [];
  var bufferMin = AUDIT_CONFIG.TIME_BUFFER_MIN;

  var checks = [];
  var tags = [];
  var status = AUDIT_STATUS.OK;

  // 期待がない場合
  if (!dayExpected || dayExpected.visits.length === 0) {
    if (actuals.length > 0) {
      // 期待がないのに実績がある → EXTRA_ACTUAL (WARN)
      status = AUDIT_STATUS.WARN;
      tags.push(AUDIT_TAGS.EXTRA_ACTUAL);
      checks.push({
        type: 'extra',
        status: AUDIT_STATUS.WARN,
        reason: '期待がないのに実績があります'
      });
    }
    return { status: status, tags: tags, checks: checks };
  }

  var expectedVisits = dayExpected.visits;

  // === キャンセル判定 ===
  var cancelledExpects = expectedVisits.filter(function(e) { return e.isCancelled; });
  if (cancelledExpects.length > 0 && actuals.length > 0) {
    // キャンセルなのに実績がある → NG
    status = AUDIT_STATUS.NG;
    tags.push(AUDIT_TAGS.CANCELLED_BUT_VISITED);
    checks.push({
      type: 'cancel',
      status: AUDIT_STATUS.NG,
      reason: 'キャンセル期待だが実績があります'
    });
    return { status: status, tags: tags, checks: checks };
  }

  if (cancelledExpects.length > 0 && actuals.length === 0) {
    // キャンセル期待で実績なし → OK
    tags.push('CHANGE(キャンセル)');
    checks.push({
      type: 'cancel',
      status: AUDIT_STATUS.OK,
      reason: 'キャンセル期待通り'
    });
    return { status: status, tags: tags, checks: checks };
  }

  // === イベント衝突判定 ===
  if (events.length > 0 && actuals.length > 0) {
    var conflictEvent = null;
    var conflictActual = null;

    actuals.forEach(function(actual) {
      events.forEach(function(event) {
        if (audit_isTimeOverlap_(actual.startMin, actual.endMin, event.startMin, event.endMin)) {
          conflictEvent = event;
          conflictActual = actual;
        }
      });
    });

    if (conflictEvent) {
      status = AUDIT_STATUS.NG;
      tags.push(AUDIT_TAGS.EVENT_CONFLICT);
      checks.push({
        type: 'eventConflict',
        status: AUDIT_STATUS.NG,
        event: conflictEvent,
        actual: conflictActual,
        reason: 'イベント(' + (conflictEvent.title || conflictEvent.eventId) + ')と時間が重複'
      });
      // イベント衝突があっても他のチェックも続行
    }
  }

  // === 未割当判定 ===
  var unassignedActuals = actuals.filter(function(a) { return a.isUnassigned; });
  if (unassignedActuals.length > 0) {
    status = AUDIT_STATUS.NG;
    tags.push(AUDIT_TAGS.UNASSIGNED);
    checks.push({
      type: 'unassigned',
      status: AUDIT_STATUS.NG,
      reason: '未割当の実績があります'
    });
  }

  // === 期待と実績の突合 ===
  var activeExpects = expectedVisits.filter(function(e) { return !e.isCancelled; });
  var usedActualIdxs = {};

  activeExpects.forEach(function(exp) {
    // 最も近い実績を探す
    var bestActualIdx = -1;
    var bestDiff = Infinity;

    actuals.forEach(function(actual, idx) {
      if (usedActualIdxs[idx]) return;

      var diff = audit_calcTimeDiff_(exp, actual);
      if (diff < bestDiff) {
        bestDiff = diff;
        bestActualIdx = idx;
      }
    });

    if (bestActualIdx >= 0) {
      usedActualIdxs[bestActualIdx] = true;
      var actual = actuals[bestActualIdx];

      // 時間判定
      var timeCheck = audit_checkTimeWindow_(exp, actual, bufferMin);
      checks.push(timeCheck);

      if (timeCheck.status === AUDIT_STATUS.NG) {
        status = AUDIT_STATUS.NG;
        tags.push(AUDIT_TAGS.OUT_OF_WINDOW);
      } else if (timeCheck.status === AUDIT_STATUS.WARN && status !== AUDIT_STATUS.NG) {
        status = AUDIT_STATUS.WARN;
        if (timeCheck.diffMin !== 0) {
          var diffStr = (timeCheck.diffMin > 0 ? '+' : '') + timeCheck.diffMin + 'm';
          tags.push(AUDIT_TAGS.TIME_DIFF + '(' + diffStr + ')');
        }
      }
    } else {
      // 期待に対応する実績がない → MISSING_ACTUAL
      if (status !== AUDIT_STATUS.NG) {
        status = AUDIT_STATUS.WARN;
      }
      tags.push(AUDIT_TAGS.MISSING_ACTUAL);
      checks.push({
        type: 'missing',
        status: AUDIT_STATUS.WARN,
        expected: exp,
        reason: '期待に対応する実績がありません'
      });
    }

    // ソースタグを追加
    if (tags.indexOf(exp.source) < 0 && tags.indexOf(AUDIT_TAGS.OUT_OF_WINDOW) < 0) {
      var sourceTag = audit_getSourceTag_(exp);
      if (sourceTag && tags.indexOf(sourceTag) < 0) {
        tags.push(sourceTag);
      }
    }
  });

  // 使われなかった実績（余剰）
  actuals.forEach(function(actual, idx) {
    if (!usedActualIdxs[idx]) {
      if (status !== AUDIT_STATUS.NG) {
        status = AUDIT_STATUS.WARN;
      }
      tags.push(AUDIT_TAGS.EXTRA_ACTUAL);
      checks.push({
        type: 'extra',
        status: AUDIT_STATUS.WARN,
        actual: actual,
        reason: '余剰の実績があります'
      });
    }
  });

  // === C-1: 2人訪問チェック ===
  // needStaff=2 の期待がある場合のみ実行
  var twoStaffExpects = activeExpects.filter(function(e) { return (e.needStaff || 1) >= 2; });
  if (twoStaffExpects.length > 0 && actuals.length > 0) {
    if (actuals.length < 2) {
      // 2人必要なのに実績が2件未満
      status = AUDIT_STATUS.NG;
      tags.push(AUDIT_TAGS.TWO_STAFF_MISSING);
      checks.push({
        type: 'twoStaffMissing',
        status: AUDIT_STATUS.NG,
        reason: '2人訪問が必要ですが実績が' + actuals.length + '件のみです'
      });
    } else if (actuals.length >= 2) {
      // staffIdが同一かチェック
      var staffIds = {};
      var hasDuplicate = false;
      actuals.forEach(function(a) {
        if (a.staffId && staffIds[a.staffId]) {
          hasDuplicate = true;
        }
        if (a.staffId) staffIds[a.staffId] = true;
      });

      if (hasDuplicate) {
        status = AUDIT_STATUS.NG;
        tags.push(AUDIT_TAGS.TWO_STAFF_DUPLICATE);
        checks.push({
          type: 'twoStaffDuplicate',
          status: AUDIT_STATUS.NG,
          reason: '2人訪問で同一スタッフが割り当てられています'
        });
      }

      // 時間ズレチェック（先頭2件で比較）
      var a0 = actuals[0];
      var a1 = actuals[1];
      if (a0.startMin !== null && a1.startMin !== null &&
          a0.endMin !== null && a1.endMin !== null) {
        var startDiff = Math.abs(a0.startMin - a1.startMin);
        var endDiff = Math.abs(a0.endMin - a1.endMin);
        if (startDiff > bufferMin || endDiff > bufferMin) {
          if (status !== AUDIT_STATUS.NG) {
            status = AUDIT_STATUS.WARN;
          }
          tags.push(AUDIT_TAGS.TWO_STAFF_TIME_MISMATCH);
          checks.push({
            type: 'twoStaffTimeMismatch',
            status: AUDIT_STATUS.WARN,
            reason: '2人訪問の時間にズレがあります（開始差:' + startDiff + '分, 終了差:' + endDiff + '分）'
          });
        }
      }
    }
  }

  // === C-3: スタッフ勤務時間チェック (SHIFT_VIOLATION) ===
  // === C-4: スタッフ勤務日チェック (WORKDAY_VIOLATION) ===
  // === C-5: スタッフ個別変更チェック (STAFF_DAY_OFF, STAFF_HALF_DAY_OFF, STAFF_RESTRICTED) ===
  // === C-7: サービス時間長チェック (SVC_DURATION_MISMATCH) ===
  actuals.forEach(function(actual) {
    var staffId = actual.staffId;
    if (!staffId) return;

    var staffMaster = dataset.staffMasterMap ? dataset.staffMasterMap[staffId] : null;

    // C-2: 性別制限チェック (GENDER_VIOLATION)
    // needStaffに関係なく、sexLimitがある患者の全actualをチェック
    var master = dataset.patientMasterMap ? dataset.patientMasterMap[pid] : null;
    if (master && master.sexLimit && master.sexLimit !== '指定なし' && master.sexLimit !== '-'
        && master.sexLimit !== '選択なし' && master.sexLimit !== '選択肢なし' && master.sexLimit !== 'なし') {
      if (staffMaster && staffMaster.sex) {
        // sexLimitが「女性のみ」等のパターンから性別を抽出
        var requiredSex = master.sexLimit.replace('のみ', '').replace('スタッフ', '').trim();
        if (staffMaster.sex !== requiredSex) {
          status = AUDIT_STATUS.NG;
          tags.push(AUDIT_TAGS.GENDER_VIOLATION);
          checks.push({
            type: 'genderViolation',
            status: AUDIT_STATUS.NG,
            staffId: staffId,
            staffName: staffMaster.name || actual.staffName || '',
            reason: '性別制限違反: ' + requiredSex + '希望だが' + staffMaster.sex + 'スタッフ（' + (staffMaster.name || staffId) + '）'
          });
        }
      }
    }

    // C-3: シフト時間チェック
    if (staffMaster && staffMaster.shiftStart !== null && staffMaster.shiftEnd !== null) {
      if (actual.startMin !== null && actual.endMin !== null) {
        if (actual.startMin < staffMaster.shiftStart - bufferMin ||
            actual.endMin > staffMaster.shiftEnd + bufferMin) {
          if (status !== AUDIT_STATUS.NG) {
            status = AUDIT_STATUS.WARN;
          }
          tags.push(AUDIT_TAGS.SHIFT_VIOLATION);
          checks.push({
            type: 'shiftViolation',
            status: AUDIT_STATUS.WARN,
            staffId: staffId,
            staffName: staffMaster.name || actual.staffName || '',
            reason: 'シフト時間外: スタッフ(' + (staffMaster.name || staffId) + ')のシフト'
              + audit_minToTimeStr_(staffMaster.shiftStart) + '-' + audit_minToTimeStr_(staffMaster.shiftEnd)
              + ' に対し実績' + audit_minToTimeStr_(actual.startMin) + '-' + audit_minToTimeStr_(actual.endMin)
          });
        }
      }
    }

    // C-4: 勤務日チェック
    if (staffMaster && staffMaster.workDays && staffMaster.workDays.length > 0) {
      // dateStrから曜日を取得
      var dateObj = audit_parseDate_(dateStr);
      if (dateObj) {
        var youbi = audit_getYoubiEn_(dateObj);
        if (staffMaster.workDays.indexOf(youbi) < 0) {
          status = AUDIT_STATUS.NG;
          tags.push(AUDIT_TAGS.WORKDAY_VIOLATION);
          checks.push({
            type: 'workdayViolation',
            status: AUDIT_STATUS.NG,
            staffId: staffId,
            staffName: staffMaster.name || actual.staffName || '',
            reason: '勤務日外: スタッフ(' + (staffMaster.name || staffId) + ')の勤務曜日は'
              + staffMaster.workDays.join(',') + ' ですが' + youbi + 'に訪問しています'
          });
        }
      }
    }

    // C-5: スタッフ個別変更チェック
    var staffChangeMap = dataset.staffChangeMap || {};
    var scKey = audit_makeSdKey_(staffId, dateStr);
    var staffChanges = staffChangeMap[scKey] || [];
    staffChanges.forEach(function(change) {
      if (change.restrictionType === '休み') {
        status = AUDIT_STATUS.NG;
        tags.push(AUDIT_TAGS.STAFF_DAY_OFF);
        checks.push({
          type: 'staffDayOff',
          status: AUDIT_STATUS.NG,
          staffId: staffId,
          staffName: staffMaster ? staffMaster.name : (actual.staffName || ''),
          reason: 'スタッフ(' + (staffMaster ? staffMaster.name : staffId) + ')は休みですが訪問が割り当てられています'
        });
      } else if (change.restrictionType === '午前休') {
        if (actual.startMin !== null && actual.startMin < 780) {
          if (status !== AUDIT_STATUS.NG) {
            status = AUDIT_STATUS.WARN;
          }
          tags.push(AUDIT_TAGS.STAFF_HALF_DAY_OFF);
          checks.push({
            type: 'staffHalfDayOff',
            status: AUDIT_STATUS.WARN,
            staffId: staffId,
            staffName: staffMaster ? staffMaster.name : (actual.staffName || ''),
            reason: 'スタッフ(' + (staffMaster ? staffMaster.name : staffId) + ')は午前休ですが午前中（'
              + audit_minToTimeStr_(actual.startMin) + '）に訪問が割り当てられています'
          });
        }
      } else if (change.restrictionType === '午後休') {
        if (actual.endMin !== null && actual.endMin > 780) {
          if (status !== AUDIT_STATUS.NG) {
            status = AUDIT_STATUS.WARN;
          }
          tags.push(AUDIT_TAGS.STAFF_HALF_DAY_OFF);
          checks.push({
            type: 'staffHalfDayOff',
            status: AUDIT_STATUS.WARN,
            staffId: staffId,
            staffName: staffMaster ? staffMaster.name : (actual.staffName || ''),
            reason: 'スタッフ(' + (staffMaster ? staffMaster.name : staffId) + ')は午後休ですが午後（'
              + audit_minToTimeStr_(actual.endMin) + '）まで訪問が割り当てられています'
          });
        }
      } else if (change.restrictionType === '遅刻') {
        if (actual.startMin !== null && change.startMin !== null && actual.startMin < change.startMin) {
          if (status !== AUDIT_STATUS.NG) {
            status = AUDIT_STATUS.WARN;
          }
          tags.push(AUDIT_TAGS.STAFF_RESTRICTED);
          checks.push({
            type: 'staffRestricted',
            status: AUDIT_STATUS.WARN,
            staffId: staffId,
            staffName: staffMaster ? staffMaster.name : (actual.staffName || ''),
            reason: 'スタッフ(' + (staffMaster ? staffMaster.name : staffId) + ')は遅刻（'
              + audit_minToTimeStr_(change.startMin) + '出勤予定）ですが'
              + audit_minToTimeStr_(actual.startMin) + 'に訪問が割り当てられています'
          });
        }
      } else if (change.restrictionType === '早退') {
        if (actual.endMin !== null && change.endMin !== null && actual.endMin > change.endMin) {
          if (status !== AUDIT_STATUS.NG) {
            status = AUDIT_STATUS.WARN;
          }
          tags.push(AUDIT_TAGS.STAFF_RESTRICTED);
          checks.push({
            type: 'staffRestricted',
            status: AUDIT_STATUS.WARN,
            staffId: staffId,
            staffName: staffMaster ? staffMaster.name : (actual.staffName || ''),
            reason: 'スタッフ(' + (staffMaster ? staffMaster.name : staffId) + ')は早退（'
              + audit_minToTimeStr_(change.endMin) + '退勤予定）ですが'
              + audit_minToTimeStr_(actual.endMin) + 'まで訪問が割り当てられています'
          });
        }
      } else if (change.restrictionType === '時間指定') {
        // 制限時間帯と実績が重複する場合
        if (change.startMin !== null && change.endMin !== null &&
            actual.startMin !== null && actual.endMin !== null) {
          if (audit_isTimeOverlap_(actual.startMin, actual.endMin, change.startMin, change.endMin)) {
            if (status !== AUDIT_STATUS.NG) {
              status = AUDIT_STATUS.WARN;
            }
            tags.push(AUDIT_TAGS.STAFF_RESTRICTED);
            checks.push({
              type: 'staffRestricted',
              status: AUDIT_STATUS.WARN,
              staffId: staffId,
              staffName: staffMaster ? staffMaster.name : (actual.staffName || ''),
              reason: 'スタッフ(' + (staffMaster ? staffMaster.name : staffId) + ')の制限時間（'
                + audit_minToTimeStr_(change.startMin) + '-' + audit_minToTimeStr_(change.endMin)
                + '）と訪問時間（' + audit_minToTimeStr_(actual.startMin) + '-' + audit_minToTimeStr_(actual.endMin)
                + '）が重複しています'
            });
          }
        }
      }
    });

    // C-7: サービス時間長チェック
    if (actual.startMin !== null && actual.endMin !== null) {
      var actualDuration = actual.endMin - actual.startMin;
      // マッチした期待のsvcMinと比較
      // usedActualIdxsから対応する期待を探す
      activeExpects.forEach(function(exp, expIdx) {
        // この actual がどの expected に対応するか探す
        // usedActualIdxs は actualIdx -> true なので逆引きが必要
        // 簡易的に: 各actualに最も近いexpectedのsvcMinを使う
      });
      // より確実な方法: 全activeExpectsのsvcMinを確認（needStaff考慮）
      var patientMaster = dataset.patientMasterMap ? dataset.patientMasterMap[pid] : null;
      var expectedSvcMin = patientMaster ? patientMaster.svcMin : null;
      // 期待のsvcMinが明示的にある場合はそれを使用
      if (activeExpects.length > 0 && activeExpects[0].svcMin) {
        expectedSvcMin = activeExpects[0].svcMin;
      }
      if (expectedSvcMin !== null && expectedSvcMin > 0) {
        if (Math.abs(actualDuration - expectedSvcMin) > bufferMin) {
          if (status !== AUDIT_STATUS.NG) {
            status = AUDIT_STATUS.WARN;
          }
          tags.push(AUDIT_TAGS.SVC_DURATION_MISMATCH);
          checks.push({
            type: 'svcDurationMismatch',
            status: AUDIT_STATUS.WARN,
            staffId: staffId,
            reason: 'サービス時間不一致: 期待' + expectedSvcMin + '分に対し実績' + actualDuration + '分（差:'
              + Math.abs(actualDuration - expectedSvcMin) + '分）'
          });
        }
      }
    }
  });

  // タグの重複除去
  tags = tags.filter(function(tag, idx, arr) {
    return arr.indexOf(tag) === idx;
  });

  return { status: status, tags: tags, checks: checks };
}

/**
 * 時間窓チェック（説明力強化版）
 */
function audit_checkTimeWindow_(expected, actual, bufferMin) {
  var timeType = expected.timeType || '時間帯';
  var result = {
    type: 'timeWindow',
    status: AUDIT_STATUS.OK,
    expected: {
      timeType: timeType,
      startMin: expected.startMin,
      endMin: expected.endMin,
      earliestMin: expected.earliestMin,
      latestMin: expected.latestMin
    },
    actual: {
      startMin: actual.startMin,
      endMin: actual.endMin
    },
    diffMin: 0,
    reason: '',
    explanation: ''  // 詳細説明（クライアント向け）
  };

  // 時刻文字列を生成（説明用）
  var actualStartStr = audit_minToTimeStr_(actual.startMin);
  var actualEndStr = audit_minToTimeStr_(actual.endMin);

  if (actual.startMin === null || actual.endMin === null) {
    result.status = AUDIT_STATUS.WARN;
    result.reason = '実績の時刻が不明';
    result.explanation = '実績データに時刻が設定されていません';
    return result;
  }

  if (timeType === '固定') {
    // 固定: 開始時刻のずれで判定
    var expStartStr = audit_minToTimeStr_(expected.startMin);

    if (expected.startMin !== null) {
      var diff = actual.startMin - expected.startMin;
      result.diffMin = diff;

      if (Math.abs(diff) <= bufferMin) {
        if (diff === 0) {
          result.status = AUDIT_STATUS.OK;
          result.reason = '';
          result.explanation = '固定時刻(' + expStartStr + ')通りに訪問';
        } else {
          result.status = AUDIT_STATUS.WARN;
          result.reason = '開始時刻が' + diff + '分ずれ';
          result.explanation = '固定(' + expStartStr + ')希望だが実績は' + actualStartStr + '開始（' + (diff > 0 ? '+' : '') + diff + '分）';
        }
      } else {
        result.status = AUDIT_STATUS.NG;
        result.reason = '開始時刻が' + diff + '分ずれ（バッファ±' + bufferMin + '分超過）';
        result.explanation = '固定(' + expStartStr + ')希望だが実績は' + actualStartStr + '開始 → バッファ(' + bufferMin + '分)を超えてNG';
      }
    }
  } else {
    // 時間帯/午前/午後/終日: 範囲内かどうかで判定
    var earliest = expected.earliestMin;
    var latest = expected.latestMin;
    var earliestStr = audit_minToTimeStr_(earliest);
    var latestStr = audit_minToTimeStr_(latest);
    var rangeStr = (earliestStr || '?') + '〜' + (latestStr || '?');

    if (earliest === null && latest === null) {
      // 範囲が不明 → OK扱い
      result.explanation = timeType + '（範囲不明のためOK扱い）';
      return result;
    }

    var startOk = (earliest === null || actual.startMin >= earliest - bufferMin);
    var endOk = (latest === null || actual.endMin <= latest + bufferMin);

    if (earliest !== null && actual.startMin < earliest) {
      result.diffMin = actual.startMin - earliest;
    } else if (latest !== null && actual.endMin > latest) {
      result.diffMin = actual.endMin - latest;
    }

    var startInRange = (earliest === null || actual.startMin >= earliest);
    var endInRange = (latest === null || actual.endMin <= latest);

    if (startInRange && endInRange) {
      result.status = AUDIT_STATUS.OK;
      result.explanation = timeType + '(' + rangeStr + ')希望内で訪問(' + actualStartStr + '-' + actualEndStr + ')';
    } else if (startOk && endOk) {
      result.status = AUDIT_STATUS.WARN;
      var diffStr = (result.diffMin > 0 ? '+' : '') + result.diffMin + '分';
      result.reason = '期待範囲外だがバッファ内(' + diffStr + ')';
      result.explanation = timeType + '(' + rangeStr + ')希望だが実績' + actualStartStr + '-' + actualEndStr + ' → 範囲外(' + diffStr + ')だがバッファ内';
    } else {
      result.status = AUDIT_STATUS.NG;
      var diffStr = (result.diffMin > 0 ? '+' : '') + result.diffMin + '分';
      result.reason = '期待範囲外（バッファ超過 ' + diffStr + '）';
      result.explanation = timeType + '(' + rangeStr + ')希望だが実績' + actualStartStr + '-' + actualEndStr + ' → バッファ(' + bufferMin + '分)を超えてNG';
    }
  }

  return result;
}

/**
 * 時刻差分を計算（突合用）
 */
function audit_calcTimeDiff_(expected, actual) {
  var expStart = expected.startMin !== null ? expected.startMin : expected.earliestMin;
  var actStart = actual.startMin;

  if (expStart === null || actStart === null) return 9999;

  return Math.abs(actStart - expStart);
}

/**
 * ソースからタグを取得
 */
function audit_getSourceTag_(exp) {
  if (exp.source === AUDIT_SOURCE.MASTER) return AUDIT_TAGS.MASTER;
  if (exp.source === AUDIT_SOURCE.CHANGE) {
    if (exp.op === '時間変更') return AUDIT_TAGS.CHANGE_TIME;
    if (exp.op === '追加') return AUDIT_TAGS.CHANGE_ADD;
  }
  if (exp.source === AUDIT_SOURCE.SPECIAL_ADD) return AUDIT_TAGS.SPECIAL_ADD;
  if (exp.source === AUDIT_SOURCE.SPECIAL_REPLACE) return AUDIT_TAGS.SPECIAL_REPLACE;
  return null;
}

// ============================================================
// データセット拡張（Expected付き）
// ============================================================

/**
 * データセットにExpectedを付与
 */
function audit_enrichDatasetWithExpected_(dataset) {
  var expectedByPidDate = audit_buildExpected_(dataset);
  dataset.expectedByPidDate = expectedByPidDate;
  return dataset;
}

// ============================================================
// 条件重要度定義
// ============================================================

/**
 * 条件の重要度レベル
 */
var AUDIT_SEVERITY = {
  CRITICAL: 'CRITICAL',   // 絶対条件（必須）- 違反するとNG
  WARNING: 'WARNING',     // 注意条件 - 違反するとWARN
  INFO: 'INFO'            // 情報のみ - 判定には影響しない
};

/**
 * 条件カテゴリ
 */
var AUDIT_CONDITION_CATEGORY = {
  TIME: 'TIME',           // 時間条件
  DAY: 'DAY',             // 曜日条件
  STAFF: 'STAFF',         // スタッフ条件
  EVENT: 'EVENT',         // イベント条件
  CHANGE: 'CHANGE',       // 個別変更
  SPECIAL: 'SPECIAL'      // 特別訪問週間
};

// ============================================================
// 日別条件分析
// ============================================================

/**
 * 患者×日の適用条件を分析
 * @param {Object} dataset - 週データセット
 * @param {string} pid - 患者ID
 * @param {string} dateStr - 日付
 * @param {string} youbi - 曜日（英語）
 * @return {Object} 適用条件の詳細
 */
function audit_analyzeConditions_(dataset, pid, dateStr, youbi) {
  var key = audit_makePdKey_(pid, dateStr);
  var master = dataset.patientMasterMap[pid];
  var change = dataset.changeMap[key];
  var events = dataset.eventMap[key] || [];
  var specials = dataset.specialWeekMap[key] || [];
  var expectedByPidDate = dataset.expectedByPidDate || {};
  var dayExpected = expectedByPidDate[key];

  var conditions = [];

  // === マスタ条件 ===
  if (master) {
    // 曜日条件
    var prefDays = master.prefDays || [];
    var ngDays = master.ngDays || [];
    var isDayOk = prefDays.length === 0 || prefDays.indexOf(youbi) >= 0;
    var isDayNg = ngDays.indexOf(youbi) >= 0;

    conditions.push({
      category: AUDIT_CONDITION_CATEGORY.DAY,
      source: 'MASTER',
      label: '希望曜日',
      severity: AUDIT_SEVERITY.WARNING,
      expected: prefDays.length > 0 ? prefDays.join(',') : '指定なし',
      actual: youbi,
      isOk: isDayOk,
      description: isDayOk ? '希望曜日内' : '希望曜日外（' + prefDays.join(',') + '）'
    });

    if (ngDays.length > 0) {
      conditions.push({
        category: AUDIT_CONDITION_CATEGORY.DAY,
        source: 'MASTER',
        label: 'NG曜日',
        severity: AUDIT_SEVERITY.CRITICAL,
        expected: ngDays.join(',') + 'はNG',
        actual: youbi,
        isOk: !isDayNg,
        description: isDayNg ? 'NG曜日に訪問' : 'NG曜日ではない'
      });
    }

    // 時間条件
    var timeType = master.timeType || '時間帯';
    var timeCondition = {
      category: AUDIT_CONDITION_CATEGORY.TIME,
      source: 'MASTER',
      label: '時間条件',
      severity: AUDIT_SEVERITY.CRITICAL,
      timeType: timeType,
      isOk: null,  // 後で実績と比較して判定
      description: ''
    };

    if (timeType === '固定') {
      timeCondition.expected = audit_minToTimeStr_(master.startPref) + ' 開始';
      timeCondition.description = '固定時刻 ' + audit_minToTimeStr_(master.startPref) + ' に訪問必要';
    } else {
      var earliest = master.earliestMin || master.startPref;
      var latest = master.latestMin || master.endPref;
      if (!earliest && !latest) {
        var defaults = AUDIT_TIME_DEFAULTS[timeType];
        if (defaults) {
          earliest = defaults.earliestMin;
          latest = defaults.latestMin;
        }
      }
      timeCondition.expected = audit_minToTimeStr_(earliest) + '〜' + audit_minToTimeStr_(latest);
      timeCondition.description = timeType + '（' + timeCondition.expected + '）の範囲内で訪問必要';
    }
    conditions.push(timeCondition);

    // サービス時間
    if (master.svcMin) {
      conditions.push({
        category: AUDIT_CONDITION_CATEGORY.TIME,
        source: 'MASTER',
        label: 'サービス時間',
        severity: AUDIT_SEVERITY.INFO,
        expected: master.svcMin + '分',
        isOk: null,
        description: 'サービス時間 ' + master.svcMin + '分'
      });
    }

    // 性別制限（「選択なし」「選択肢なし」「指定なし」「-」「なし」は制限なしとして除外）
    if (master.sexLimit && master.sexLimit !== '指定なし' && master.sexLimit !== '-'
        && master.sexLimit !== '選択なし' && master.sexLimit !== '選択肢なし' && master.sexLimit !== 'なし') {
      conditions.push({
        category: AUDIT_CONDITION_CATEGORY.STAFF,
        source: 'MASTER',
        label: '性別制限',
        severity: AUDIT_SEVERITY.CRITICAL,
        expected: master.sexLimit + 'スタッフのみ',
        isOk: null,  // スタッフ情報があれば後で判定
        description: master.sexLimit + 'スタッフのみ訪問可能'
      });
    }

    // 指定スタッフ
    if (master.fixedStaff) {
      var fixedType = master.fixedType || '希望';
      var severity = fixedType === '必須' ? AUDIT_SEVERITY.CRITICAL : AUDIT_SEVERITY.WARNING;
      conditions.push({
        category: AUDIT_CONDITION_CATEGORY.STAFF,
        source: 'MASTER',
        label: '指定スタッフ',
        severity: severity,
        expected: master.fixedStaff + '（' + fixedType + '）',
        isOk: null,
        description: 'スタッフ ' + master.fixedStaff + ' を' + fixedType
      });
    }

    // NGスタッフ（「選択なし」等は制限なしとして除外）
    if (master.ngStaff && master.ngStaff !== '選択なし' && master.ngStaff !== '選択肢なし'
        && master.ngStaff !== '指定なし' && master.ngStaff !== '-' && master.ngStaff !== 'なし') {
      conditions.push({
        category: AUDIT_CONDITION_CATEGORY.STAFF,
        source: 'MASTER',
        label: 'NGスタッフ',
        severity: AUDIT_SEVERITY.CRITICAL,
        expected: master.ngStaff + 'はNG',
        isOk: null,
        description: 'スタッフ ' + master.ngStaff + ' は訪問不可'
      });
    }

    // 継続希望（「選択なし」等は制限なしとして除外）
    if (master.contPref && master.contPref !== '指定なし' && master.contPref !== '-'
        && master.contPref !== '選択なし' && master.contPref !== '選択肢なし' && master.contPref !== 'なし') {
      conditions.push({
        category: AUDIT_CONDITION_CATEGORY.STAFF,
        source: 'MASTER',
        label: '継続希望',
        severity: AUDIT_SEVERITY.WARNING,
        expected: master.contPref,
        isOk: null,
        description: '継続性: ' + master.contPref
      });
    }
  }

  // === 個別変更条件 ===
  if (change) {
    var changeCondition = {
      category: AUDIT_CONDITION_CATEGORY.CHANGE,
      source: 'CHANGE',
      label: '個別変更',
      severity: change.op === 'キャンセル' ? AUDIT_SEVERITY.CRITICAL : AUDIT_SEVERITY.CRITICAL,
      op: change.op,
      isOk: null,
      description: ''
    };

    if (change.op === 'キャンセル') {
      changeCondition.expected = '訪問なし（キャンセル）';
      changeCondition.description = 'この日はキャンセル済み';
    } else if (change.op === '時間変更') {
      changeCondition.expected = audit_minToTimeStr_(change.newStartMin) + '-' + audit_minToTimeStr_(change.newEndMin);
      changeCondition.description = '時間変更: ' + changeCondition.expected + ' に変更';
    } else if (change.op === '追加') {
      changeCondition.expected = audit_minToTimeStr_(change.newStartMin) + '-' + audit_minToTimeStr_(change.newEndMin);
      changeCondition.description = '追加訪問: ' + changeCondition.expected;
    }

    if (change.note) {
      changeCondition.note = change.note;
    }

    conditions.push(changeCondition);
  }

  // === イベント条件 ===
  events.forEach(function(event) {
    conditions.push({
      category: AUDIT_CONDITION_CATEGORY.EVENT,
      source: 'EVENT',
      label: 'イベント',
      severity: AUDIT_SEVERITY.CRITICAL,
      expected: event.title + '（' + audit_minToTimeStr_(event.startMin) + '-' + audit_minToTimeStr_(event.endMin) + '）',
      eventId: event.eventId,
      startMin: event.startMin,
      endMin: event.endMin,
      isOk: null,
      description: 'イベント「' + event.title + '」との時間重複を避ける必要あり'
    });
  });

  // === 特別訪問週間条件 ===
  specials.forEach(function(special) {
    var specialCondition = {
      category: AUDIT_CONDITION_CATEGORY.SPECIAL,
      source: special.mode === 'REPLACE' ? 'SPECIAL_REPLACE' : 'SPECIAL_ADD',
      label: '特別訪問週間',
      severity: AUDIT_SEVERITY.CRITICAL,
      mode: special.mode,
      rowLabel: special.rowLabel,
      timeType: special.timeType,
      isOk: null,
      description: ''
    };

    if (special.mode === 'REPLACE') {
      specialCondition.description = '【置換】通常スケジュールを無視し、この設定に従う';
    } else {
      specialCondition.description = '【追加】通常スケジュールに加えて訪問';
    }

    if (special.timeType === '固定') {
      specialCondition.expected = audit_minToTimeStr_(special.startMin) + ' 開始';
    } else {
      var earliest = special.earliestMin || special.startMin;
      var latest = special.latestMin || special.endMin;
      specialCondition.expected = audit_minToTimeStr_(earliest) + '〜' + audit_minToTimeStr_(latest);
    }

    conditions.push(specialCondition);
  });

  return {
    conditions: conditions,
    hasMaster: !!master,
    hasChange: !!change,
    hasEvents: events.length > 0,
    hasSpecial: specials.length > 0,
    dayExpected: dayExpected
  };
}

/**
 * 条件と実績を照合して詳細な判定結果を生成
 * @param {Object} conditionAnalysis - analyzeConditions_の結果
 * @param {Array} actuals - 実績配列
 * @param {Object} dataset - データセット（スタッフ情報参照用）
 * @return {Array} 詳細な判定結果配列
 */
function audit_evaluateConditions_(conditionAnalysis, actuals, dataset) {
  var conditions = conditionAnalysis.conditions;
  var results = [];

  conditions.forEach(function(cond) {
    var result = {
      category: cond.category,
      source: cond.source,
      label: cond.label,
      severity: cond.severity,
      expected: cond.expected,
      description: cond.description,
      status: AUDIT_STATUS.OK,
      actual: null,
      diffDetail: null
    };

    // 実績がない場合の判定
    if (actuals.length === 0) {
      if (cond.category === AUDIT_CONDITION_CATEGORY.CHANGE && cond.op === 'キャンセル') {
        result.status = AUDIT_STATUS.OK;
        result.actual = '実績なし（キャンセル期待通り）';
      } else if (cond.category === AUDIT_CONDITION_CATEGORY.TIME ||
                 cond.category === AUDIT_CONDITION_CATEGORY.CHANGE ||
                 cond.category === AUDIT_CONDITION_CATEGORY.SPECIAL) {
        // 期待があるのに実績がない場合は別途MISSING_ACTUALで判定
        result.status = AUDIT_STATUS.WARN;
        result.actual = '実績なし';
        result.diffDetail = '期待に対する実績がありません';
      }
      results.push(result);
      return;
    }

    // キャンセル条件だが実績がある
    if (cond.category === AUDIT_CONDITION_CATEGORY.CHANGE && cond.op === 'キャンセル') {
      result.status = AUDIT_STATUS.NG;
      result.actual = '実績あり（' + actuals.length + '件）';
      result.diffDetail = 'キャンセル済みだが訪問実績があります';
      results.push(result);
      return;
    }

    // イベント衝突チェック
    if (cond.category === AUDIT_CONDITION_CATEGORY.EVENT) {
      var hasConflict = false;
      var conflictActual = null;

      actuals.forEach(function(actual) {
        if (audit_isTimeOverlap_(actual.startMin, actual.endMin, cond.startMin, cond.endMin)) {
          hasConflict = true;
          conflictActual = actual;
        }
      });

      if (hasConflict) {
        result.status = AUDIT_STATUS.NG;
        result.actual = audit_minToTimeStr_(conflictActual.startMin) + '-' + audit_minToTimeStr_(conflictActual.endMin);
        result.diffDetail = 'イベント時間と重複しています';
      } else {
        result.status = AUDIT_STATUS.OK;
        result.actual = '重複なし';
      }
      results.push(result);
      return;
    }

    // スタッフ条件チェック
    if (cond.category === AUDIT_CONDITION_CATEGORY.STAFF) {
      actuals.forEach(function(actual) {
        var staffId = actual.staffId;
        var staffMaster = dataset.staffMasterMap ? dataset.staffMasterMap[staffId] : null;

        if (cond.label === '性別制限') {
          if (staffMaster && staffMaster.sex) {
            result.actual = staffMaster.name + '（' + staffMaster.sex + '）';
            var expectedSex = cond.expected.replace('スタッフのみ', '');
            if (staffMaster.sex !== expectedSex) {
              result.status = AUDIT_STATUS.NG;
              result.diffDetail = '性別制限違反: ' + expectedSex + '希望だが' + staffMaster.sex + 'スタッフ';
            }
          } else {
            result.actual = actual.staffName || staffId || '不明';
            result.status = AUDIT_STATUS.INFO;
            result.diffDetail = 'スタッフ性別情報なし';
          }
        } else if (cond.label === '指定スタッフ') {
          result.actual = actual.staffName || staffId;
          var expectedStaffId = cond.expected.split('（')[0];
          if (staffId !== expectedStaffId) {
            result.status = cond.severity === AUDIT_SEVERITY.CRITICAL ? AUDIT_STATUS.NG : AUDIT_STATUS.WARN;
            result.diffDetail = '指定スタッフ外: ' + expectedStaffId + ' 希望だが ' + (actual.staffName || staffId);
          }
        } else if (cond.label === 'NGスタッフ') {
          result.actual = actual.staffName || staffId;
          var ngStaffId = cond.expected.replace('はNG', '');
          if (staffId === ngStaffId) {
            result.status = AUDIT_STATUS.NG;
            result.diffDetail = 'NGスタッフが訪問しています';
          }
        }
      });
      results.push(result);
      return;
    }

    // 曜日条件は既に判定済み
    if (cond.category === AUDIT_CONDITION_CATEGORY.DAY) {
      result.actual = cond.actual;
      if (!cond.isOk) {
        result.status = cond.severity === AUDIT_SEVERITY.CRITICAL ? AUDIT_STATUS.NG : AUDIT_STATUS.WARN;
        result.diffDetail = cond.description;
      }
      results.push(result);
      return;
    }

    // その他の条件はそのまま追加
    results.push(result);
  });

  return results;
}

/**
 * 時間重複判定
 */
function audit_isTimeOverlap_(start1, end1, start2, end2) {
  if (start1 === null || end1 === null || start2 === null || end2 === null) {
    return false;
  }
  return start1 < end2 && end1 > start2;
}
