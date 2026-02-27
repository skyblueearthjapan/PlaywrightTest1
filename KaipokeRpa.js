// ============================================================================
// KaipokeRpa.js - 差分適用関数の変更箇所
// ============================================================================
//
// 変更内容: runApply() を3つの短い関数に分割
//   - startApply(month, weekStart)  — 適用開始（即座に返る）
//   - pollApplyResult()             — 1回のポーリング（即座に返る）
//   - finalizeApply(applyResultJson, weekStart) — 完了後処理
//
// 目的: google.script.run のタイムアウトを回避するため、
//       ポーリングループをGAS関数内からHTML側（ブラウザ）に移行する
//
// 既存の runApply() 関数は削除してください。
// 以下の3関数を KaipokeRpa.js に追加してください。
// その他の関数（checkServerStatus, runExpand, runExport, diffPreview,
//   writeApplyResultToSheet, storeApplyResult_, getStoredCorrections_ 等）
//   は変更不要です。
// ============================================================================


// ============================================================================
// 1. startApply(month, weekStart) — 適用開始（即座に返る）
// ============================================================================
/**
 * 差分適用を開始する。バリデーション + POST /api/apply のみ。
 * ポーリングは行わない。HTML側で pollApplyResult() を繰り返し呼ぶ。
 *
 * @param {string} month - 対象月 "YYYY-MM"
 * @param {string} weekStart - 対象週開始日 "YYYY-MM-DD"
 * @returns {Object} { success: boolean, message: string, weekRange?: Object }
 */
function startApply(month, weekStart) {
  var url = API_BASE_URL + "/api/apply";
  var targetMonth = month || getCurrentMonth();

  // weekStart必須チェック
  if (!weekStart) {
    return {
      "success": false,
      "message": "エラー: 対象週が指定されていません。"
    };
  }

  // 差分検証済みチェック
  var props = PropertiesService.getScriptProperties();
  var verified = props.getProperty('diff_verified');
  var verifiedWeek = props.getProperty('diff_week_start');
  if (verified !== 'true') {
    return {
      "success": false,
      "message": "エラー: 差分検証が完了していません。\n先に「差分確認（プレビュー）」を実行してください。"
    };
  }
  if (verifiedWeek && verifiedWeek !== weekStart) {
    return {
      "success": false,
      "message": "エラー: 検証済みの週（" + verifiedWeek + "）と適用対象週（" + weekStart + "）が異なります。\n再度「差分確認（プレビュー）」を実行してください。"
    };
  }

  // 保存済みの修正データを取得
  var corrections = getStoredCorrections_();
  if (!corrections || corrections.length === 0) {
    return {
      "success": false,
      "message": "エラー: 修正データが見つかりません。\n再度「差分確認（プレビュー）」を実行してください。"
    };
  }

  var weekRange = getWeekRange_(weekStart);
  console.log('[startApply] month=' + targetMonth + ' weekStart=' + weekStart + ' corrections=' + corrections.length + '件');

  // correction_dataを直接送信
  var payload = {
    "correction_data": corrections,
    "month": targetMonth,
    "dry_run": false,
    "headed": true
  };

  var options = {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify(payload),
    "muteHttpExceptions": true
  };

  try {
    // POST /api/apply（即座に返る — サーバーはバックグラウンドで処理開始）
    var response = UrlFetchApp.fetch(url, options);
    var statusCode = response.getResponseCode();
    var startResult = JSON.parse(response.getContentText());

    console.log('[startApply] statusCode=' + statusCode + ' responseBody=' + response.getContentText().substring(0, 500));

    if (statusCode === 400) {
      return {
        "success": false,
        "message": "パラメータエラー: " + (startResult.error || startResult.message || "不明なエラー")
      };
    }
    if (statusCode === 409) {
      return {
        "success": false,
        "message": "エラー: " + (startResult.error || startResult.message || "不明なエラー")
      };
    }
    if (statusCode !== 200 || !startResult.success) {
      return {
        "success": false,
        "message": "エラー: " + (startResult.error || startResult.message || "不明なエラー")
      };
    }

    return {
      "success": true,
      "message": "適用を開始しました",
      "weekRange": weekRange
    };
  } catch (e) {
    return {
      "success": false,
      "message": "サーバー接続エラー: " + e.message
    };
  }
}


// ============================================================================
// 2. pollApplyResult() — 1回のポーリング（即座に返る）
// ============================================================================
/**
 * GET /api/apply/result を1回呼んで結果をそのまま返す。
 * HTML側から setInterval で繰り返し呼ばれる。
 * 実行時間: 1-2秒
 *
 * @returns {Object} サーバーのレスポンス
 *   status="running"   → { status, progress: { processed, total, phase, current_name, success, failed, skipped, updated_at } }
 *   status="completed" → { status, result: { ... } }
 *   status="error"     → { status, error: "..." }
 *   status="no_result" → { status, message: "..." }
 */
function pollApplyResult() {
  var pollUrl = API_BASE_URL + "/api/apply/result";
  var resp = UrlFetchApp.fetch(pollUrl, { "method": "get", "muteHttpExceptions": true });
  return JSON.parse(resp.getContentText());
}


// ============================================================================
// 3. finalizeApply(applyResultJson, weekStart) — 完了後処理
// ============================================================================
/**
 * 適用完了後の後処理を行う。
 * - 適用結果シートへの書き込み
 * - キャッシュ保存
 * - PropertiesService フラグクリア
 * - 結果メッセージ構築
 *
 * HTML側から google.script.run.finalizeApply(JSON.stringify(data.result), weekStart) で呼ばれる。
 *
 * @param {string} applyResultJson - 適用結果のJSON文字列
 * @param {string} weekStart - 対象週開始日 "YYYY-MM-DD"
 * @returns {Object} { success: boolean, message: string }
 */
function finalizeApply(applyResultJson, weekStart) {
  var r = JSON.parse(applyResultJson);
  var weekRange = getWeekRange_(weekStart);
  var props = PropertiesService.getScriptProperties();

  // 適用結果シートへ書き込み
  try {
    writeApplyResultToSheet(r);
  } catch (sheetErr) {
    console.error('[finalizeApply] writeApplyResultToSheet error:', sheetErr);
  }

  // 適用結果をキャッシュに保存（適用後検証で使用）
  try {
    storeApplyResult_(r);
  } catch (cacheErr) {
    console.error('[finalizeApply] storeApplyResult_ error:', cacheErr);
  }

  // 適用完了後、検証フラグをクリア
  props.setProperty('diff_verified', 'false');
  props.deleteProperty('diff_week_start');
  props.deleteProperty('diff_file_id');

  // 結果メッセージ構築
  var total = r.total || 0;
  var successCount = r.success || 0;
  var failed = r.failed || 0;
  var skipped = r.skipped || 0;
  var scheduleTotal = r.schedule_total || 0;
  var eventTotal = r.event_total || 0;

  var executionTime = r.execution_time_sec || 0;
  var completedAt = r.completed_at || '';

  var msg = "差分適用完了!（" + weekStart + " 〜 " + weekRange.endDate + "）\n\n" +
            "成功: " + successCount + "件\n" +
            "失敗: " + failed + "件\n" +
            "スキップ: " + skipped + "件\n" +
            "合計: " + total + "件\n" +
            "（スケジュール: " + scheduleTotal + "件、イベント: " + eventTotal + "件）\n\n" +
            "実行時間: " + executionTime + "秒\n" +
            "完了時刻: " + completedAt;

  // 失敗・スキップの詳細
  var details = r.details || [];
  var failedItems = [];
  var skippedItems = [];
  for (var i = 0; i < details.length; i++) {
    var d = details[i];
    if (d.status === 'failed' || d.status === 'error') {
      failedItems.push('  ' + (d.user || d.staff || '') + ' ' + d.date + '日 ' + d.action + ' [' + (d.reason || '不明') + ']');
    } else if (d.status === 'skipped') {
      skippedItems.push('  ' + (d.user || d.staff || '') + ' ' + d.date + '日 ' + d.action + ' [' + (d.reason || '不明') + ']');
    }
  }

  if (failedItems.length > 0) {
    msg += '\n\n--- 失敗 ---\n' + failedItems.join('\n');
  }
  if (skippedItems.length > 0) {
    msg += '\n\n--- スキップ ---\n' + skippedItems.join('\n');
  }

  if (failed > 0 || skipped > 0) {
    msg += '\n\n詳細は「適用結果」シートを確認してください。';
  }

  return {
    "success": true,
    "message": msg,
    "data": r
  };
}


// ============================================================================
// 削除: 旧 runApply() 関数
// ============================================================================
// 以下の関数は削除してください:
//   function runApply(month, weekStart) { ... }
//
// この関数のロジックは上記3関数に分割されました:
//   - バリデーション + POST → startApply()
//   - ポーリングループ    → HTML側の setInterval + pollApplyResult()
//   - 結果処理            → finalizeApply()
// ============================================================================


// ============================================================================
// 4. startPostApplyVerification(month) — 適用後検証: CSV再出力開始
// ============================================================================
/**
 * 適用後にカイポケからCSVを再出力する（非同期モード）。
 * POST /api/export に async:true を付与して即座に返る。
 * HTML側で pollExportResult() をポーリングして完了を待つ。
 *
 * @param {string} month - 対象月 "YYYY-MM"
 * @returns {Object} { success: boolean, message: string }
 */
function startPostApplyVerification(month) {
  var url = API_BASE_URL + "/api/export";
  var targetMonth = month || getCurrentMonth();

  var payload = {
    "month": targetMonth,
    "async": true
  };

  var options = {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify(payload),
    "muteHttpExceptions": true
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var statusCode = response.getResponseCode();
    var result = JSON.parse(response.getContentText());

    console.log('[startPostApplyVerification] statusCode=' + statusCode +
                ' response=' + response.getContentText().substring(0, 300));

    if (statusCode === 409) {
      return {
        "success": false,
        "message": "エラー: " + (result.error || "別のタスクが実行中です")
      };
    }
    if (statusCode !== 200 || !result.success) {
      return {
        "success": false,
        "message": "CSV出力開始エラー: " + (result.error || result.message || "不明なエラー")
      };
    }

    return {
      "success": true,
      "message": "CSV出力を開始しました"
    };
  } catch (e) {
    return {
      "success": false,
      "message": "サーバー接続エラー: " + e.message
    };
  }
}


// ============================================================================
// 5. pollExportResult() — 適用後検証: CSV出力ポーリング
// ============================================================================
/**
 * GET /api/export/result を1回呼んで結果をそのまま返す。
 * HTML側から setInterval で繰り返し呼ばれる。
 *
 * @returns {Object} サーバーのレスポンス
 *   status="running"   → { status, message }
 *   status="completed" → { status, result: { csv_content, ... } }
 *   status="error"     → { status, error: "..." }
 *   status="no_result" → { status, message: "..." }
 */
function pollExportResult() {
  var pollUrl = API_BASE_URL + "/api/export/result";
  var resp = UrlFetchApp.fetch(pollUrl, { "method": "get", "muteHttpExceptions": true });
  return JSON.parse(resp.getContentText());
}


// ============================================================================
// 6. finalizePostApplyVerification(exportResultJson, month) — 適用後検証: 完了処理
// ============================================================================
/**
 * CSV再出力が完了した後の検証処理。
 * - CSV内容を取得
 * - 保存済みの修正データ（corrections）と照合
 * - 「検証結果」シートに書き込み
 * - サマリーメッセージを返す
 *
 * @param {string} exportResultJson - export結果のJSON文字列
 * @param {string} month - 対象月 "YYYY-MM"
 * @returns {Object} { success: boolean, message: string }
 */
function finalizePostApplyVerification(exportResultJson, month) {
  try {
    var exportResult = JSON.parse(exportResultJson);
    var csvContent = exportResult.csv_content;

    if (!csvContent) {
      return {
        "success": false,
        "message": "エラー: CSV内容が空です。再度実行してください。"
      };
    }

    // CSVをGoogle Driveに保存
    var monthStr = (month || getCurrentMonth()).replace("-", "");
    var driveFilename = "kaipoke_current_" + monthStr + "_post_apply.csv";
    try {
      var folderId = "1tQJKZDjonFwiY6wYYgx1iVgu4cM98vRp";
      var blob = Utilities.newBlob(csvContent, "text/csv", driveFilename);
      var folder = DriveApp.getFolderById(folderId);
      // 既存ファイルがあれば削除して再作成
      var existing = folder.getFilesByName(driveFilename);
      while (existing.hasNext()) {
        existing.next().setTrashed(true);
      }
      folder.createFile(blob);
      console.log('[finalizePostApplyVerification] Drive保存完了: ' + driveFilename);
    } catch (driveErr) {
      console.error('[finalizePostApplyVerification] Drive保存エラー:', driveErr);
      // Drive保存失敗は検証を続行
    }

    // 保存済みの修正データを取得
    var corrections = getStoredCorrections_();
    if (!corrections || corrections.length === 0) {
      return {
        "success": true,
        "message": "CSV再出力は完了しましたが、修正データが見つからないため照合をスキップしました。\n" +
                   "ファイル: " + driveFilename
      };
    }

    // 適用結果を取得
    var applyResult = getStoredApplyResult_();

    // CSV照合
    var verifyResults = verifyApplyResult_(corrections, csvContent, applyResult);

    // 「検証結果」シートに書き込み
    writeVerificationResultToSheet_(verifyResults);

    // サマリー構築
    var okCount = 0, failCount = 0, skipCount = 0;
    for (var i = 0; i < verifyResults.length; i++) {
      var v = verifyResults[i].verification;
      if (v === 'OK') okCount++;
      else if (v === 'FAIL') failCount++;
      else skipCount++;
    }

    var msg = "適用後検証完了\n\n" +
              "検証対象: " + verifyResults.length + "件\n" +
              "OK: " + okCount + "件\n" +
              "FAIL: " + failCount + "件\n" +
              "スキップ: " + skipCount + "件\n\n" +
              "Drive保存: " + driveFilename;

    if (failCount > 0) {
      msg += "\n\n--- 不一致 ---";
      for (var j = 0; j < verifyResults.length; j++) {
        if (verifyResults[j].verification === 'FAIL') {
          var r = verifyResults[j];
          msg += "\n  " + r.user_name + " " + r.date + "日 " + r.action + ": " + r.reason;
        }
      }
      msg += "\n\n詳細は「検証結果」シートを確認してください。";
    }

    return {
      "success": failCount === 0,
      "message": msg,
      "summary": {
        "total": verifyResults.length,
        "ok": okCount,
        "fail": failCount,
        "skipped": skipCount
      }
    };
  } catch (e) {
    console.error('[finalizePostApplyVerification] エラー:', e);
    return {
      "success": false,
      "message": "検証処理エラー: " + e.message
    };
  }
}


// ============================================================================
// 7. verifyApplyResult_(corrections, csvContent, applyResult) — CSV照合
// ============================================================================
/**
 * 修正データの各件と適用後CSVを照合する。
 *
 * @param {Array} corrections - 修正データ配列
 * @param {string} csvContent - 適用後CSV全文
 * @param {Object} applyResult - 適用結果（details含む）
 * @returns {Array} 検証結果配列
 */
function verifyApplyResult_(corrections, csvContent, applyResult) {
  var rows = Utilities.parseCsv(csvContent);
  var results = [];
  var details = (applyResult && applyResult.details) || [];

  for (var i = 0; i < corrections.length; i++) {
    var c = corrections[i];

    // 適用結果から該当するdetailを探す
    var applyStatus = findApplyStatus_(c, details);

    // 適用時に失敗/スキップだったものは検証もスキップ
    if (applyStatus === 'failed' || applyStatus === 'error' || applyStatus === 'skipped') {
      results.push({
        "user_name": c.user_name || '',
        "date": c.date_to || c.date_from || '',
        "action": c.action || '',
        "business_type": c.business_type || '',
        "service_type": c.service_type || '',
        "verification": "skipped",
        "reason": "適用時ステータス: " + applyStatus,
        "apply_status": applyStatus
      });
      continue;
    }

    // イベント追加（event_add）は適用結果ステータスで判定
    if (c.action === 'event_add') {
      results.push({
        "user_name": c.user_name || '',
        "date": c.date_to || c.date_from || '',
        "action": c.action || '',
        "business_type": c.business_type || '',
        "service_type": c.service_type || '',
        "verification": applyStatus === 'success' ? 'OK' : 'skipped',
        "reason": "イベント追加: 適用結果ステータスで判定",
        "apply_status": applyStatus
      });
      continue;
    }

    // CSV照合
    var verifyResult = verifySingleCorrection_(c, rows);
    verifyResult.apply_status = applyStatus;
    results.push(verifyResult);
  }

  return results;
}


// ============================================================================
// 8. verifySingleCorrection_(correction, rows) — 1件ずつの検証
// ============================================================================
/**
 * CSVの列構造（カイポケ18列）:
 *   0:職員名１, 1:職種１, 2:職員名２, 3:職種２, 4:同行２,
 *   5:職員名３, 6:職種３, 7:同行３, 8:事業所名,
 *   9:日付, 10:曜日, 11:利用者, 12:業務種別, 13:サービス内容,
 *   14:開始時間, 15:終了時間, 16:提供時間（分）, 17:備考
 */
function verifySingleCorrection_(correction, rows) {
  var c = correction;
  var userName = (c.user_name || '').trim();
  var action = c.action || '';
  var dateTo = (c.date_to || '').trim();
  var dateFrom = (c.date_from || '').trim();
  var startTo = (c.start_time_to || '').trim();
  var endTo = (c.end_time_to || '').trim();
  var startFrom = (c.start_time_from || '').trim();
  var staff1To = (c.staff1_to || '').trim();
  var staff1From = (c.staff1_from || '').trim();

  var result = {
    "user_name": userName,
    "date": dateTo || dateFrom,
    "date_from": dateFrom,
    "date_to": dateTo,
    "action": action,
    "business_type": c.business_type || '',
    "service_type": c.service_type || '',
    "start_time_to": startTo,
    "end_time_to": endTo,
    "staff1_to": staff1To,
    "verification": "FAIL",
    "reason": "",
    "failCategory": ""
  };

  // user_name が「なし」または空欄の場合 → イベント/個別業務でCSV照合不可
  if ((action === 'add' || action === 'edit') && (userName === 'なし' || userName === '')) {
    result.reason = '利用者名「' + (userName || '空欄') + '」はカイポケ上に存在しないユーザーです。' +
                    'イベント・個別業務は利用者なしの登録のため、CSV利用者欄での照合ができません。';
    result.failCategory = 'user_not_found';
    return result;
  }

  if (action === 'delete') {
    // 削除: 旧エントリがCSVに存在しないことを確認
    var found = findCsvRow_(rows, userName, dateFrom, startFrom);
    if (!found) {
      result.verification = 'OK';
      result.reason = '削除確認: エントリが正しく削除されている';
    } else {
      result.reason = '削除対象がまだCSVに存在しています。' + userName + 'の' + dateFrom + '日 ' + startFrom + ' のエントリが残っています。';
      result.failCategory = 'entry_mismatch';
    }
  } else if (action === 'add' || action === 'edit') {
    var found = findCsvRow_(rows, userName, dateTo, startTo);
    if (found) {
      result.verification = 'OK';
      result.reason = (action === 'add' ? '追加' : '編集') + '確認: ' +
                      (action === 'add' ? 'エントリが正しく追加されている' : '変更が正しく反映されている');
    } else {
      // 未割当の検出
      if (staff1To === '未割当') {
        result.reason = '【未割当】最適化の結果、' + userName + 'さんの' + dateTo + '日 ' + startTo + '〜' + endTo +
                        ' に割当可能なスタッフが存在しませんでした。' +
                        '職員欄は変更前の「' + (staff1From || '不明') + '」のままです。';
        result.failCategory = 'unassigned_staff';
      } else {
        // 同日・同ユーザーで別時間のエントリがあるか検索
        var actualEntries = [];
        for (var a = 1; a < rows.length; a++) {
          var rowA = rows[a];
          if (rowA.length < 15) continue;
          if (normalizeSpaces_((rowA[11] || '').trim()) === normalizeSpaces_(userName) &&
              (rowA[9] || '').trim() === dateTo) {
            actualEntries.push((rowA[14] || '').trim() + '-' + (rowA[15] || '').trim() +
                               '（' + (rowA[0] || '').trim() + '）');
          }
        }
        var actualInfo = actualEntries.length > 0
          ? ' 同日のCSV実績: ' + actualEntries.join(', ')
          : ' 同日のCSVにこの利用者のエントリはありません。';

        result.reason = (action === 'add' ? '追加' : '編集後') + 'エントリがCSVに未反映。' +
                        '期待: ' + dateTo + '日 ' + startTo + '-' + endTo + '。' + actualInfo;
        result.failCategory = 'entry_mismatch';
      }
    }
  } else if (action === 'date_change') {
    var dcStartFrom = startFrom || startTo;
    var oldFound = findCsvRow_(rows, userName, dateFrom, dcStartFrom);
    var newFound = findCsvRow_(rows, userName, dateTo, startTo);
    if (!oldFound && newFound) {
      result.verification = 'OK';
      result.reason = '日付移動確認: 旧日付(' + dateFrom + '日)から消え、新日付(' + dateTo + '日)に存在';
    } else if (oldFound && newFound) {
      result.reason = '日付移動不完全: 新日付に存在するが旧日付(' + dateFrom + '日)のエントリが残っている';
      result.failCategory = 'entry_mismatch';
    } else if (!newFound) {
      // 新日付のCSVエントリを検索して表示
      var dcActualEntries = [];
      for (var b = 1; b < rows.length; b++) {
        var rowB = rows[b];
        if (rowB.length < 15) continue;
        if (normalizeSpaces_((rowB[11] || '').trim()) === normalizeSpaces_(userName) &&
            (rowB[9] || '').trim() === dateTo) {
          dcActualEntries.push((rowB[14] || '').trim() + '-' + (rowB[15] || '').trim() +
                               '（' + (rowB[0] || '').trim() + '）');
        }
      }
      var dcActualInfo = dcActualEntries.length > 0
        ? ' 同日のCSV実績: ' + dcActualEntries.join(', ')
        : ' 同日のCSVにこの利用者のエントリはありません。';

      result.reason = '日付移動失敗: 新日付(' + dateTo + '日)にエントリが見つからない。' +
                      '期待: ' + dateTo + '日 ' + startTo + '-' + endTo + '。' + dcActualInfo;
      result.failCategory = 'entry_mismatch';
    }
  } else {
    result.verification = 'skipped';
    result.reason = '不明なアクション: ' + action;
  }

  return result;
}


// ============================================================================
// 9. findCsvRow_(rows, userName, day, startTime) — CSV行検索ヘルパー
// ============================================================================
/**
 * CSVの行からユーザー名・日付・開始時間が一致する行を探す。
 *
 * @param {Array} rows - Utilities.parseCsv() の結果
 * @param {string} userName - 利用者名
 * @param {string} day - 日（例: "4", "15"）
 * @param {string} startTime - 開始時間（例: "09:00"）
 * @returns {Array|null} 一致する行、またはnull
 */
function findCsvRow_(rows, userName, day, startTime) {
  if (!userName || !day) return null;
  var targetDay = String(day).trim();
  var targetName = userName.trim();
  var targetTime = (startTime || '').trim();

  for (var i = 1; i < rows.length; i++) {  // ヘッダースキップ
    var row = rows[i];
    if (row.length < 15) continue;

    var csvName = (row[11] || '').trim();     // 利用者（インデックス11）
    var csvDay = (row[9] || '').trim();       // 日付（インデックス9）
    var csvTime = (row[14] || '').trim();     // 開始時間（インデックス14）

    // 名前の一致（全角/半角スペース正規化）
    var nameMatch = normalizeSpaces_(csvName) === normalizeSpaces_(targetName);
    var dayMatch = csvDay === targetDay;

    if (nameMatch && dayMatch) {
      // 開始時間が指定されていない場合は名前+日付だけで一致
      if (!targetTime) return row;
      // 開始時間も一致するか確認
      if (csvTime === targetTime) return row;
    }
  }
  return null;
}


// ============================================================================
// 10. normalizeSpaces_(str) — スペース正規化ヘルパー
// ============================================================================
function normalizeSpaces_(str) {
  if (!str) return '';
  // 全角スペースを半角に統一し、連続スペースを1つに
  return str.replace(/\u3000/g, ' ').replace(/\s+/g, ' ').trim();
}


// ============================================================================
// 11. findApplyStatus_(correction, details) — 適用結果ステータス検索
// ============================================================================
/**
 * 適用結果のdetails配列から、該当する修正のステータスを探す。
 */
function findApplyStatus_(correction, details) {
  if (!details || details.length === 0) return 'unknown';

  var userName = normalizeSpaces_(correction.user_name || '');
  var date = String(correction.date_to || correction.date_from || '').trim();
  var action = correction.action || '';

  for (var i = 0; i < details.length; i++) {
    var d = details[i];
    var dName = normalizeSpaces_(d.user || d.staff || '');
    var dDate = String(d.date || '').trim();
    var dAction = d.action || '';

    if (dName === userName && dDate === date && dAction === action) {
      return d.status || 'unknown';
    }
  }
  // 名前+日付のみでフォールバック検索
  for (var i = 0; i < details.length; i++) {
    var d = details[i];
    var dName = normalizeSpaces_(d.user || d.staff || '');
    var dDate = String(d.date || '').trim();

    if (dName === userName && dDate === date) {
      return d.status || 'unknown';
    }
  }
  return 'unknown';
}


// ============================================================================
// 12. getStoredApplyResult_() — 適用結果取得ヘルパー
// ============================================================================
/**
 * キャッシュから適用結果を取得する。
 * storeApplyResult_() で保存されたデータを読み取る。
 */
function getStoredApplyResult_() {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName('_apply_result_cache');
    if (!sheet) return null;
    var json = sheet.getRange('A1').getValue();
    if (!json) return null;
    return JSON.parse(json);
  } catch (e) {
    console.error('[getStoredApplyResult_] エラー:', e);
    return null;
  }
}


// ============================================================================
// 13. storeApplyResult_(result) — 適用結果保存ヘルパー（finalizeApplyから呼出済み）
// ============================================================================
// ※ 既存の storeApplyResult_() が存在する場合はそちらを使用。
//    存在しない場合は以下を追加:
//
// function storeApplyResult_(result) {
//   try {
//     var ss = SpreadsheetApp.getActiveSpreadsheet();
//     var sheet = ss.getSheetByName('_apply_result_cache');
//     if (!sheet) {
//       sheet = ss.insertSheet('_apply_result_cache');
//       sheet.hideSheet();
//     }
//     sheet.getRange('A1').setValue(JSON.stringify(result));
//   } catch (e) {
//     console.error('[storeApplyResult_] エラー:', e);
//   }
// }


// ============================================================================
// 14. writeVerificationResultToSheet_(verifyResults) — 検証結果シート書き込み
// ============================================================================
/**
 * 「検証結果」シートに色分けで結果を書き込む。
 */
function writeVerificationResultToSheet_(verifyResults) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName('検証結果');
  if (!sheet) {
    sheet = ss.insertSheet('検証結果');
  } else {
    sheet.clear();
  }

  // failCategory → 日本語変換マップ
  var categoryLabels = {
    'user_not_found': 'ユーザー不在',
    'unassigned_staff': '未割当',
    'entry_mismatch': '時間不一致/未反映'
  };

  // ヘッダー（12列）
  var headers = ['利用者', '日付(前)', '日付(後)', 'アクション', '業務種別',
                 '期待時間', '期待スタッフ', 'サービス内容',
                 '検証結果', 'FAIL原因', '理由', 'タイムスタンプ'];
  sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  sheet.getRange(1, 1, 1, headers.length)
    .setFontWeight('bold')
    .setBackground('#4a86c8')
    .setFontColor('#ffffff');

  // サマリー行
  var okCount = 0, failCount = 0, skipCount = 0;
  for (var i = 0; i < verifyResults.length; i++) {
    var v = verifyResults[i].verification;
    if (v === 'OK') okCount++;
    else if (v === 'FAIL') failCount++;
    else skipCount++;
  }
  var summaryText = '検証合計: ' + verifyResults.length + '件 | OK: ' + okCount +
                    '件 | FAIL: ' + failCount + '件 | スキップ: ' + skipCount + '件';
  sheet.getRange(2, 1).setValue(summaryText);
  sheet.getRange(2, 1, 1, headers.length)
    .setFontWeight('bold')
    .setBackground('#e2e3e5');

  // データ行
  var timestamp = new Date().toLocaleString('ja-JP');
  for (var i = 0; i < verifyResults.length; i++) {
    var r = verifyResults[i];
    var expectedTime = '';
    if (r.start_time_to || r.end_time_to) {
      expectedTime = (r.start_time_to || '') + ' - ' + (r.end_time_to || '');
    }
    var expectedStaff = r.staff1_to || '';
    var failCategoryLabel = r.failCategory ? (categoryLabels[r.failCategory] || r.failCategory) : '';

    var row = [
      r.user_name || '',
      r.date_from || '',
      r.date_to || r.date || '',
      r.action || '',
      r.business_type || '',
      expectedTime,
      expectedStaff,
      r.service_type || '',
      r.verification || '',
      failCategoryLabel,
      r.reason || '',
      timestamp
    ];
    var rowNum = i + 3;  // ヘッダー(1) + サマリー(2) の後
    sheet.getRange(rowNum, 1, 1, row.length).setValues([row]);

    // 色分け
    var bgColor = '#ffffff';
    if (r.verification === 'OK') bgColor = '#d4edda';
    else if (r.verification === 'FAIL') bgColor = '#f8d7da';
    else bgColor = '#fff3cd';
    sheet.getRange(rowNum, 1, 1, row.length).setBackground(bgColor);

    // 未割当スタッフは赤色で強調
    if (expectedStaff === '未割当') {
      sheet.getRange(rowNum, 7).setFontColor('#cc0000').setFontWeight('bold');
    }
  }

  // 列幅自動調整
  for (var c = 1; c <= headers.length; c++) {
    sheet.autoResizeColumn(c);
  }

  console.log('[writeVerificationResultToSheet_] 書き込み完了: ' + verifyResults.length + '件');
}


// ============================================================================
// 15. exportVerificationToDoc(verifyResults) — 検証結果HTMLレポート生成
// ============================================================================
/**
 * 検証結果をHTMLレポートとしてGoogle Docsに出力する。
 * - サマリー
 * - FAIL原因分析セクション（カテゴリ別集計）
 * - FAIL一覧テーブル（期待内容・FAIL原因・詳細列付き）
 * - 全件一覧
 *
 * @param {Array} verifyResults - verifyApplyResult_() の結果
 * @param {string} month - 対象月 "YYYY-MM"
 * @returns {Object} { success: boolean, docUrl: string, message: string }
 */
function exportVerificationToDoc(verifyResults, month) {
  var targetMonth = month || getCurrentMonth();

  // failCategory → 日本語変換マップ
  var categoryLabels = {
    'user_not_found': 'ユーザー不在',
    'unassigned_staff': '未割当',
    'entry_mismatch': '時間不一致/未反映'
  };
  var categoryDescriptions = {
    'user_not_found': 'イベント・個別業務は利用者なしの登録のため検証不可',
    'unassigned_staff': '最適化の結果、割当可能なスタッフが不在のため反映不可',
    'entry_mismatch': '同日の複数修正で適用順序が影響し期待と異なる、または未反映'
  };

  // 集計
  var okCount = 0, failCount = 0, skipCount = 0;
  var failItems = [];
  var categoryCounts = {};

  for (var i = 0; i < verifyResults.length; i++) {
    var r = verifyResults[i];
    if (r.verification === 'OK') okCount++;
    else if (r.verification === 'FAIL') {
      failCount++;
      failItems.push(r);
      var cat = r.failCategory || 'unknown';
      categoryCounts[cat] = (categoryCounts[cat] || 0) + 1;
    } else {
      skipCount++;
    }
  }

  // HTML構築
  var html = '';
  html += '<html><head><style>';
  html += 'body { font-family: "Meiryo", sans-serif; margin: 20px; }';
  html += 'h1 { color: #333; border-bottom: 2px solid #4a86c8; padding-bottom: 8px; }';
  html += 'h2 { color: #4a86c8; margin-top: 30px; }';
  html += 'table { border-collapse: collapse; width: 100%; margin: 10px 0; }';
  html += 'th { background: #4a86c8; color: white; padding: 8px 12px; text-align: left; font-size: 13px; }';
  html += 'td { border: 1px solid #ddd; padding: 6px 10px; font-size: 12px; }';
  html += 'tr:nth-child(even) { background: #f9f9f9; }';
  html += '.ok { background: #d4edda; }';
  html += '.fail { background: #f8d7da; }';
  html += '.skip { background: #fff3cd; }';
  html += '.summary-box { background: #f0f4f8; border: 1px solid #4a86c8; border-radius: 8px; padding: 15px; margin: 15px 0; }';
  html += '.summary-box .num { font-size: 24px; font-weight: bold; }';
  html += '.cat-unassigned { color: #cc0000; font-weight: bold; }';
  html += '.cat-user-not-found { color: #e67e00; font-weight: bold; }';
  html += '.cat-entry-mismatch { color: #0066cc; font-weight: bold; }';
  html += '</style></head><body>';

  // タイトル
  html += '<h1>適用後検証レポート（' + targetMonth + '）</h1>';
  html += '<p>生成日時: ' + new Date().toLocaleString('ja-JP') + '</p>';

  // サマリー
  html += '<div class="summary-box">';
  html += '<p>検証対象: <span class="num">' + verifyResults.length + '</span>件</p>';
  html += '<p>OK: <span class="num" style="color:green">' + okCount + '</span>件 | ';
  html += 'FAIL: <span class="num" style="color:red">' + failCount + '</span>件 | ';
  html += 'スキップ: <span class="num" style="color:orange">' + skipCount + '</span>件</p>';
  html += '</div>';

  // FAIL原因分析セクション
  if (failCount > 0) {
    html += '<h2>FAIL原因分析</h2>';
    html += '<table>';
    html += '<tr><th>原因カテゴリ</th><th>件数</th><th>説明</th></tr>';
    var catKeys = Object.keys(categoryCounts);
    for (var k = 0; k < catKeys.length; k++) {
      var catKey = catKeys[k];
      var catLabel = categoryLabels[catKey] || catKey;
      var catDesc = categoryDescriptions[catKey] || '';
      var cssClass = '';
      if (catKey === 'unassigned_staff') cssClass = 'cat-unassigned';
      else if (catKey === 'user_not_found') cssClass = 'cat-user-not-found';
      else if (catKey === 'entry_mismatch') cssClass = 'cat-entry-mismatch';

      html += '<tr>';
      html += '<td class="' + cssClass + '">' + catLabel + '</td>';
      html += '<td>' + categoryCounts[catKey] + '件</td>';
      html += '<td>' + catDesc + '</td>';
      html += '</tr>';
    }
    html += '</table>';

    // FAIL一覧テーブル
    html += '<h2>FAIL一覧（' + failCount + '件）</h2>';
    html += '<table>';
    html += '<tr><th>#</th><th>利用者</th><th>日付</th><th>アクション</th><th>期待内容</th><th>FAIL原因</th><th>詳細</th></tr>';

    for (var f = 0; f < failItems.length; f++) {
      var fi = failItems[f];
      var expectedContent = '';
      if (fi.start_time_to || fi.end_time_to) {
        expectedContent = (fi.start_time_to || '') + ' - ' + (fi.end_time_to || '');
      }
      if (fi.staff1_to) {
        expectedContent += (expectedContent ? ' ' : '') + '(' + fi.staff1_to + ')';
      }
      var catLabel = fi.failCategory ? (categoryLabels[fi.failCategory] || fi.failCategory) : '';
      var cssClass = '';
      if (fi.failCategory === 'unassigned_staff') cssClass = 'cat-unassigned';
      else if (fi.failCategory === 'user_not_found') cssClass = 'cat-user-not-found';
      else if (fi.failCategory === 'entry_mismatch') cssClass = 'cat-entry-mismatch';

      html += '<tr class="fail">';
      html += '<td>' + (f + 1) + '</td>';
      html += '<td>' + (fi.user_name || '') + '</td>';
      html += '<td>' + (fi.date_to || fi.date || '') + '日</td>';
      html += '<td>' + (fi.action || '') + '</td>';
      html += '<td>' + expectedContent + '</td>';
      html += '<td class="' + cssClass + '">' + catLabel + '</td>';
      html += '<td>' + (fi.reason || '') + '</td>';
      html += '</tr>';
    }
    html += '</table>';
  }

  // 全件一覧
  html += '<h2>全件一覧（' + verifyResults.length + '件）</h2>';
  html += '<table>';
  html += '<tr><th>#</th><th>利用者</th><th>日付</th><th>アクション</th><th>業務種別</th><th>検証結果</th><th>理由</th></tr>';

  for (var j = 0; j < verifyResults.length; j++) {
    var vr = verifyResults[j];
    var rowClass = '';
    if (vr.verification === 'OK') rowClass = 'ok';
    else if (vr.verification === 'FAIL') rowClass = 'fail';
    else rowClass = 'skip';

    html += '<tr class="' + rowClass + '">';
    html += '<td>' + (j + 1) + '</td>';
    html += '<td>' + (vr.user_name || '') + '</td>';
    html += '<td>' + (vr.date_to || vr.date || '') + '日</td>';
    html += '<td>' + (vr.action || '') + '</td>';
    html += '<td>' + (vr.business_type || '') + '</td>';
    html += '<td>' + (vr.verification || '') + '</td>';
    html += '<td>' + (vr.reason || '') + '</td>';
    html += '</tr>';
  }
  html += '</table>';

  html += '</body></html>';

  // Google Docsに出力
  try {
    var docTitle = '適用後検証レポート_' + targetMonth + '_' + Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd_HHmmss');
    var doc = DocumentApp.create(docTitle);
    var body = doc.getBody();
    body.clear();
    body.appendParagraph(docTitle).setHeading(DocumentApp.ParagraphHeading.HEADING1);

    // HTMLはGoogle Docsに直接挿入できないため、HTML文字列をDriveにファイルとして保存
    var folderId = '1tQJKZDjonFwiY6wYYgx1iVgu4cM98vRp';
    var htmlFilename = '検証レポート_' + targetMonth + '.html';
    var blob = Utilities.newBlob(html, 'text/html', htmlFilename);
    var folder = DriveApp.getFolderById(folderId);

    // 既存ファイルがあれば削除して再作成
    var existing = folder.getFilesByName(htmlFilename);
    while (existing.hasNext()) {
      existing.next().setTrashed(true);
    }
    var file = folder.createFile(blob);
    var fileUrl = file.getUrl();

    // Docは不要なので削除
    DriveApp.getFileById(doc.getId()).setTrashed(true);

    console.log('[exportVerificationToDoc] HTMLレポート保存完了: ' + htmlFilename);

    return {
      "success": true,
      "docUrl": fileUrl,
      "message": "検証レポートを生成しました: " + htmlFilename
    };
  } catch (e) {
    console.error('[exportVerificationToDoc] エラー:', e);
    return {
      "success": false,
      "docUrl": "",
      "message": "レポート生成エラー: " + e.message
    };
  }
}
