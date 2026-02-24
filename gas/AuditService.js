/**
 * 監査（合否判定）ビュー - サービス層（API）
 *
 * フロントエンドから呼び出されるAPIエンドポイント
 * Phase 2: Expected vs Actual 本番版判定ロジック
 */

// ============================================================
// 公開API
// ============================================================

/**
 * 週サマリ取得API
 * @param {string} weekStartStr - 週開始日（yyyy/MM/dd）
 * @return {Object} 週サマリデータ
 */
function audit_getWeekSummary(weekStartStr) {
  try {
    var dataset = audit_getOrLoadDataset_(weekStartStr);
    return audit_buildWeekSummary_(dataset);
  } catch (e) {
    console.error('audit_getWeekSummary error:', e);
    return { error: e.message || String(e) };
  }
}

/**
 * セル詳細取得API（スタッフ×日の患者一覧）
 * @param {string} staffId - スタッフID
 * @param {string} dateStr - 日付（yyyy/MM/dd）
 * @param {string} weekStartStr - 週開始日（yyyy/MM/dd）
 * @return {Object} セル詳細データ
 */
function audit_getCellDetail(staffId, dateStr, weekStartStr) {
  try {
    var dataset = audit_getOrLoadDataset_(weekStartStr);
    return audit_buildCellDetail_(dataset, staffId, dateStr);
  } catch (e) {
    console.error('audit_getCellDetail error:', e);
    return { error: e.message || String(e) };
  }
}

/**
 * 患者詳細取得API
 * @param {string} pid - 患者ID
 * @param {string} weekStartStr - 週開始日（yyyy/MM/dd）
 * @return {Object} 患者詳細データ
 */
function audit_getPatientDetail(pid, weekStartStr) {
  try {
    var dataset = audit_getOrLoadDataset_(weekStartStr);
    return audit_buildPatientDetail_(dataset, pid);
  } catch (e) {
    console.error('audit_getPatientDetail error:', e);
    return { error: e.message || String(e) };
  }
}

/**
 * 監査設定取得API
 * @return {Object} 設定情報
 */
function audit_getConfig() {
  return {
    bufferMin: AUDIT_CONFIG.TIME_BUFFER_MIN,
    timeDefaults: AUDIT_TIME_DEFAULTS
  };
}

// ============================================================
// 週サマリ構築
// ============================================================

/**
 * 週サマリを構築
 * @param {Object} dataset
 * @return {Object}
 */
function audit_buildWeekSummary_(dataset) {
  var cellSummary = {};   // sdKey -> items[]
  var patientSummary = {}; // pid -> summary
  var expectedByPidDate = dataset.expectedByPidDate || {};

  // デバッグログ
  var actualKeys = Object.keys(dataset.actualPlanMap || {});
  console.log('audit_buildWeekSummary_: actualPlanMap keys count =', actualKeys.length);
  if (actualKeys.length > 0) {
    console.log('audit_buildWeekSummary_: sample keys =', actualKeys.slice(0, 5).join(', '));
  }
  console.log('audit_buildWeekSummary_: expectedByPidDate keys count =', Object.keys(expectedByPidDate).length);

  // 実績（Actual）から患者×日×スタッフを抽出
  var actualPlanMap = dataset.actualPlanMap;

  for (var pdKey in actualPlanMap) {
    var actuals = actualPlanMap[pdKey];
    if (!actuals || actuals.length === 0) continue;

    actuals.forEach(function(actual) {
      var pid = actual.pid;
      var dateStr = actual.dateStr;
      var staffId = actual.staffId || '_UNASSIGNED_';

      // 本番判定（Phase 2）: Expected vs Actual
      var judgement = audit_judgePatientDay_(dataset, expectedByPidDate, pid, dateStr);

      // セルサマリに追加
      var sdKey = audit_makeSdKey_(staffId, dateStr);
      if (!cellSummary[sdKey]) cellSummary[sdKey] = [];

      // 重複チェック（同一患者は1件のみ）
      var exists = cellSummary[sdKey].some(function(item) { return item.pid === pid; });
      if (!exists) {
        cellSummary[sdKey].push({
          pid: pid,
          pname: actual.pname || '',
          status: judgement.status,
          tags: judgement.tags,
          detailKey: audit_makePdKey_(pid, dateStr)
        });
      }

      // 患者サマリを更新
      if (!patientSummary[pid]) {
        patientSummary[pid] = {
          pid: pid,
          pname: actual.pname || '',
          overallStatus: AUDIT_STATUS.OK,
          okCount: 0,
          warnCount: 0,
          ngCount: 0,
          reasons: []
        };
      }

      // カウント更新
      if (judgement.status === AUDIT_STATUS.NG) {
        patientSummary[pid].ngCount++;
        patientSummary[pid].overallStatus = AUDIT_STATUS.NG;
        if (judgement.tags.length > 0) {
          patientSummary[pid].reasons.push(dateStr + ': ' + judgement.tags.join(', '));
        }
      } else if (judgement.status === AUDIT_STATUS.WARN) {
        patientSummary[pid].warnCount++;
        if (patientSummary[pid].overallStatus !== AUDIT_STATUS.NG) {
          patientSummary[pid].overallStatus = AUDIT_STATUS.WARN;
        }
        if (judgement.tags.length > 0) {
          patientSummary[pid].reasons.push(dateStr + ': ' + judgement.tags.join(', '));
        }
      } else {
        patientSummary[pid].okCount++;
      }
    });
  }

  // Expected-only（実績がない期待）もチェック
  for (var expKey in expectedByPidDate) {
    var dayExpected = expectedByPidDate[expKey];
    if (!dayExpected || dayExpected.visits.length === 0) continue;

    // キャンセル期待のみの場合はスキップ（実績なしでOK）
    var activeExpects = dayExpected.visits.filter(function(e) { return !e.isCancelled; });
    if (activeExpects.length === 0) continue;

    // 実績があるかチェック
    var actuals = dataset.actualPlanMap[expKey] || [];
    if (actuals.length > 0) continue;  // 実績があれば上のループで処理済み

    // 実績がない場合 → MISSING_ACTUAL
    var parts = expKey.split('|');
    var pid = parts[0];
    var dateStr = parts[1];

    var judgement = audit_judgePatientDay_(dataset, expectedByPidDate, pid, dateStr);

    // 患者サマリを更新
    var master = dataset.patientMasterMap[pid];
    if (!patientSummary[pid]) {
      patientSummary[pid] = {
        pid: pid,
        pname: master ? master.name : '',
        overallStatus: AUDIT_STATUS.OK,
        okCount: 0,
        warnCount: 0,
        ngCount: 0,
        reasons: []
      };
    }

    // カウント更新
    if (judgement.status === AUDIT_STATUS.NG) {
      patientSummary[pid].ngCount++;
      patientSummary[pid].overallStatus = AUDIT_STATUS.NG;
      if (judgement.tags.length > 0) {
        patientSummary[pid].reasons.push(dateStr + ': ' + judgement.tags.join(', '));
      }
    } else if (judgement.status === AUDIT_STATUS.WARN) {
      patientSummary[pid].warnCount++;
      if (patientSummary[pid].overallStatus !== AUDIT_STATUS.NG) {
        patientSummary[pid].overallStatus = AUDIT_STATUS.WARN;
      }
      if (judgement.tags.length > 0) {
        patientSummary[pid].reasons.push(dateStr + ': ' + judgement.tags.join(', '));
      }
    } else {
      patientSummary[pid].okCount++;
    }
  }

  // スタッフ一覧を取得（表示用）
  var staffList = [];
  for (var staffId in dataset.staffMasterMap) {
    staffList.push({
      staffId: staffId,
      name: dataset.staffMasterMap[staffId].name || ''
    });
  }
  staffList.sort(function(a, b) {
    return a.staffId.localeCompare(b.staffId);
  });

  // デバッグ情報
  var cellSummaryKeyCount = Object.keys(cellSummary).length;
  var patientSummaryKeyCount = Object.keys(patientSummary).length;
  console.log('audit_buildWeekSummary_: cellSummary keys =', cellSummaryKeyCount);
  console.log('audit_buildWeekSummary_: patientSummary keys =', patientSummaryKeyCount);

  return {
    weekStartStr: dataset.weekStartStr,
    weekEndStr: dataset.weekEndStr,
    bufferMin: AUDIT_CONFIG.TIME_BUFFER_MIN,
    weekDates: dataset.weekDates,
    staffList: staffList,
    cellSummary: cellSummary,
    patientSummary: patientSummary,
    warnings: dataset.warnings || [],
    fromCache: dataset.fromCache || false,
    // デバッグ用
    _debug: {
      actualPlanMapKeys: Object.keys(dataset.actualPlanMap || {}).length,
      expectedByPidDateKeys: Object.keys(dataset.expectedByPidDate || {}).length,
      cellSummaryKeys: cellSummaryKeyCount,
      patientSummaryKeys: patientSummaryKeyCount
    }
  };
}

// ============================================================
// セル詳細構築
// ============================================================

/**
 * セル詳細（スタッフ×日の患者一覧）を構築
 * @param {Object} dataset
 * @param {string} staffId
 * @param {string} dateStr
 * @return {Object}
 */
function audit_buildCellDetail_(dataset, staffId, dateStr) {
  var sdKey = audit_makeSdKey_(staffId, dateStr);
  var items = [];
  var expectedByPidDate = dataset.expectedByPidDate || {};

  // 実績から該当スタッフ×日の患者を抽出
  var actualPlanMap = dataset.actualPlanMap;

  for (var pdKey in actualPlanMap) {
    var actuals = actualPlanMap[pdKey];
    actuals.forEach(function(actual) {
      if (actual.dateStr !== dateStr) return;
      if ((actual.staffId || '_UNASSIGNED_') !== staffId) return;

      var judgement = audit_judgePatientDay_(dataset, expectedByPidDate, actual.pid, dateStr);

      items.push({
        pid: actual.pid,
        pname: actual.pname || '',
        status: judgement.status,
        tags: judgement.tags,
        startMin: actual.startMin,
        endMin: actual.endMin,
        startStr: audit_minToTimeStr_(actual.startMin),
        endStr: audit_minToTimeStr_(actual.endMin),
        detailKey: audit_makePdKey_(actual.pid, dateStr)
      });
    });
  }

  // 開始時刻順にソート
  items.sort(function(a, b) {
    return (a.startMin || 0) - (b.startMin || 0);
  });

  return {
    sdKey: sdKey,
    staffId: staffId,
    staffName: dataset.staffMasterMap[staffId] ? dataset.staffMasterMap[staffId].name : '',
    dateStr: dateStr,
    items: items
  };
}

// ============================================================
// 患者詳細構築
// ============================================================

/**
 * 患者詳細を構築
 * @param {Object} dataset
 * @param {string} pid
 * @return {Object}
 */
function audit_buildPatientDetail_(dataset, pid) {
  var master = dataset.patientMasterMap[pid] || null;
  var weekDates = dataset.weekDates;
  var expectedByPidDate = dataset.expectedByPidDate || {};

  // その週の個別変更を収集
  var changes = [];
  weekDates.forEach(function(wd) {
    var key = audit_makePdKey_(pid, wd.dateStr);
    var ch = dataset.changeMap[key];
    if (ch) changes.push(ch);
  });

  // その週のイベントを収集
  var events = [];
  weekDates.forEach(function(wd) {
    var key = audit_makePdKey_(pid, wd.dateStr);
    var evts = dataset.eventMap[key];
    if (evts) events = events.concat(evts);
  });

  // その週の特別訪問週間を収集
  var specials = [];
  weekDates.forEach(function(wd) {
    var key = audit_makePdKey_(pid, wd.dateStr);
    var sps = dataset.specialWeekMap[key];
    if (sps) specials = specials.concat(sps);
  });

  // 特別訪問週間のヘッダー情報（ADD/REPLACE）
  var specialWeekInfo = null;
  if (specials.length > 0) {
    var modes = {};
    specials.forEach(function(sp) {
      modes[sp.mode] = true;
    });
    specialWeekInfo = {
      modes: Object.keys(modes),
      details: specials
    };
  }

  // その週の実績を収集
  var actuals = [];
  weekDates.forEach(function(wd) {
    var key = audit_makePdKey_(pid, wd.dateStr);
    var acts = dataset.actualPlanMap[key];
    if (acts) actuals = actuals.concat(acts);
  });

  // 日別判定
  var dayJudgements = [];
  weekDates.forEach(function(wd) {
    var key = audit_makePdKey_(pid, wd.dateStr);

    // 本番判定（Phase 2）: Expected vs Actual
    var judgement = audit_judgePatientDay_(dataset, expectedByPidDate, pid, wd.dateStr);

    // その日の実績
    var dayActuals = dataset.actualPlanMap[key] || [];
    var dayChange = dataset.changeMap[key] || null;
    var dayEvents = dataset.eventMap[key] || [];
    var daySpecials = dataset.specialWeekMap[key] || [];

    // その日のExpected
    var dayExpected = expectedByPidDate[key];
    var expectedVisits = dayExpected ? dayExpected.visits : [];

    // 条件分析（新機能）
    var conditionAnalysis = audit_analyzeConditions_(dataset, pid, wd.dateStr, wd.youbi);
    var conditionResults = audit_evaluateConditions_(conditionAnalysis, dayActuals, dataset);

    // 条件を重要度でソート（CRITICAL > WARNING > INFO）
    var severityOrder = { 'CRITICAL': 0, 'WARNING': 1, 'INFO': 2 };
    conditionResults.sort(function(a, b) {
      return (severityOrder[a.severity] || 2) - (severityOrder[b.severity] || 2);
    });

    // 条件違反のサマリ（NG/WARNのみ抽出）
    var violations = conditionResults.filter(function(r) {
      return r.status === AUDIT_STATUS.NG || r.status === AUDIT_STATUS.WARN;
    });

    dayJudgements.push({
      dateStr: wd.dateStr,
      youbi: wd.youbi,
      status: judgement.status,
      tags: judgement.tags,
      checks: judgement.checks,  // 詳細チェック結果を追加
      // 新しい条件分析情報
      conditionAnalysis: {
        conditions: conditionResults,
        violations: violations,
        violationCount: violations.length,
        ngCount: violations.filter(function(v) { return v.status === AUDIT_STATUS.NG; }).length,
        warnCount: violations.filter(function(v) { return v.status === AUDIT_STATUS.WARN; }).length
      },
      hasExpected: expectedVisits.length > 0,
      expectedCount: expectedVisits.length,
      expectedVisits: expectedVisits.map(function(exp) {
        return {
          expected_id: exp.expected_id,
          source: exp.source,
          op: exp.op,
          isCancelled: exp.isCancelled,
          timeType: exp.timeType,
          startStr: audit_minToTimeStr_(exp.startMin),
          endStr: audit_minToTimeStr_(exp.endMin),
          earliestStr: audit_minToTimeStr_(exp.earliestMin),
          latestStr: audit_minToTimeStr_(exp.latestMin),
          svcMin: exp.svcMin,
          note: exp.note
        };
      }),
      hasActual: dayActuals.length > 0,
      actualCount: dayActuals.length,
      actuals: dayActuals.map(function(a) {
        return {
          visitId: a.visitId,
          staffId: a.staffId,
          staffName: a.staffName,
          startStr: audit_minToTimeStr_(a.startMin),
          endStr: audit_minToTimeStr_(a.endMin),
          isUnassigned: a.isUnassigned
        };
      }),
      hasChange: !!dayChange,
      change: dayChange ? {
        op: dayChange.op,
        newStartStr: audit_minToTimeStr_(dayChange.newStartMin),
        newEndStr: audit_minToTimeStr_(dayChange.newEndMin),
        note: dayChange.note
      } : null,
      hasEvents: dayEvents.length > 0,
      events: dayEvents.map(function(ev) {
        return {
          eventId: ev.eventId,
          title: ev.title,
          startStr: audit_minToTimeStr_(ev.startMin),
          endStr: audit_minToTimeStr_(ev.endMin)
        };
      }),
      hasSpecial: daySpecials.length > 0,
      specials: daySpecials.map(function(sp) {
        return {
          mode: sp.mode,
          rowLabel: sp.rowLabel,
          timeType: sp.timeType,
          startStr: audit_minToTimeStr_(sp.startMin),
          endStr: audit_minToTimeStr_(sp.endMin),
          earliestStr: audit_minToTimeStr_(sp.earliestMin),
          latestStr: audit_minToTimeStr_(sp.latestMin)
        };
      }),
      expectedMeta: dayExpected ? dayExpected.meta : null
    });
  });

  return {
    weekStartStr: dataset.weekStartStr,
    pid: pid,
    pname: master ? master.name : '',
    bufferMin: AUDIT_CONFIG.TIME_BUFFER_MIN,
    master: master ? {
      timeType: master.timeType,
      prefDays: master.prefDays,
      ngDays: master.ngDays,
      startPrefStr: audit_minToTimeStr_(master.startPref),
      endPrefStr: audit_minToTimeStr_(master.endPref),
      svcMin: master.svcMin,
      needStaff: master.needStaff,
      sexLimit: master.sexLimit,
      contPref: master.contPref,
      fixedStaff: master.fixedStaff,
      fixedType: master.fixedType,
      ngStaff: master.ngStaff
    } : null,
    changes: changes.map(function(ch) {
      return {
        dateStr: ch.dateStr,
        op: ch.op,
        newStartStr: audit_minToTimeStr_(ch.newStartMin),
        newEndStr: audit_minToTimeStr_(ch.newEndMin),
        note: ch.note
      };
    }),
    events: events.map(function(ev) {
      return {
        dateStr: ev.dateStr,
        eventId: ev.eventId,
        title: ev.title,
        startStr: audit_minToTimeStr_(ev.startMin),
        endStr: audit_minToTimeStr_(ev.endMin)
      };
    }),
    specialWeek: specialWeekInfo,
    actuals: actuals.map(function(a) {
      return {
        dateStr: a.dateStr,
        visitId: a.visitId,
        staffId: a.staffId,
        staffName: a.staffName,
        startStr: audit_minToTimeStr_(a.startMin),
        endStr: audit_minToTimeStr_(a.endMin),
        isUnassigned: a.isUnassigned
      };
    }),
    dayJudgements: dayJudgements
  };
}

// ============================================================
// 判定ロジック（Phase 1: 暫定版）※非推奨、Phase 2ではAuditJudge.gsを使用
// ============================================================

/**
 * Phase 1 暫定判定：Actualの有無で判定
 * @deprecated Phase 2以降は audit_judgePatientDay_ (AuditJudge.gs) を使用
 * @param {Object} dataset
 * @param {string} pid
 * @param {string} dateStr
 * @return {Object} { status, tags }
 */
function audit_judgePatientDay_Phase1_(dataset, pid, dateStr) {
  var key = audit_makePdKey_(pid, dateStr);
  var actuals = dataset.actualPlanMap[key] || [];
  var change = dataset.changeMap[key];
  var events = dataset.eventMap[key] || [];
  var specials = dataset.specialWeekMap[key] || [];

  var tags = [];
  var status = AUDIT_STATUS.OK;

  // キャンセルがある場合
  if (change && change.op === AUDIT_OP.CANCEL) {
    if (actuals.length > 0) {
      // キャンセルなのに実績がある → NG
      status = AUDIT_STATUS.NG;
      tags.push(AUDIT_TAGS.CANCELLED_BUT_VISITED);
    } else {
      // キャンセルで実績なし → OK（期待通り）
      tags.push('CHANGE(キャンセル)');
    }
    return { status: status, tags: tags };
  }

  // 実績がない場合
  if (actuals.length === 0) {
    // 個別変更（追加）または特別訪問週間がある場合
    if ((change && change.op === AUDIT_OP.ADD) || specials.length > 0) {
      status = AUDIT_STATUS.WARN;
      tags.push(AUDIT_TAGS.MISSING_ACTUAL);
    } else {
      // 通常期待も確認（Phase 2で実装）
      // Phase 1では実績がなければWARN
      status = AUDIT_STATUS.WARN;
      tags.push(AUDIT_TAGS.MISSING_ACTUAL);
    }
    return { status: status, tags: tags };
  }

  // 実績がある場合
  // 未割当チェック
  var hasUnassigned = actuals.some(function(a) { return a.isUnassigned; });
  if (hasUnassigned) {
    status = AUDIT_STATUS.NG;
    tags.push(AUDIT_TAGS.UNASSIGNED);
    return { status: status, tags: tags };
  }

  // イベント衝突チェック（Phase 1簡易版）
  if (events.length > 0) {
    var hasConflict = false;
    actuals.forEach(function(actual) {
      events.forEach(function(event) {
        if (audit_isTimeOverlap_(actual.startMin, actual.endMin, event.startMin, event.endMin)) {
          hasConflict = true;
        }
      });
    });
    if (hasConflict) {
      status = AUDIT_STATUS.NG;
      tags.push(AUDIT_TAGS.EVENT_CONFLICT);
      return { status: status, tags: tags };
    }
  }

  // タグ付け
  if (change) {
    if (change.op === AUDIT_OP.TIME_CHANGE) {
      tags.push(AUDIT_TAGS.CHANGE_TIME);
    } else if (change.op === AUDIT_OP.ADD) {
      tags.push(AUDIT_TAGS.CHANGE_ADD);
    }
  }

  if (specials.length > 0) {
    var hasReplace = specials.some(function(sp) { return sp.mode === 'REPLACE'; });
    var hasAdd = specials.some(function(sp) { return sp.mode === 'ADD'; });
    if (hasReplace) tags.push(AUDIT_TAGS.SPECIAL_REPLACE);
    if (hasAdd) tags.push(AUDIT_TAGS.SPECIAL_ADD);
  }

  if (tags.length === 0) {
    tags.push(AUDIT_TAGS.MASTER);
  }

  return { status: status, tags: tags };
}

// 注: audit_isTimeOverlap_ は AuditJudge.gs で定義されています
