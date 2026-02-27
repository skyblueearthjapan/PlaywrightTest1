// ==========================================
// カイポケ自動化 API連携
// ==========================================

// APIサーバーのベースURL（本番環境に合わせて変更）
var API_BASE_URL = "https://kaipoke-api.net";

// Google DriveのフォルダID（共有フォルダのID）
var DRIVE_FOLDER_ID = "1tQJKZDjonFwiY6wYYgx1iVgu4cM98vRp";

// ==========================================
// サーバー状態確認
// ==========================================
function checkServerStatus() {
  var url = API_BASE_URL + "/api/status";

  var options = {
    "method": "get",
    "muteHttpExceptions": true,
    "headers": {
      "Content-Type": "application/json"
    }
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var result = JSON.parse(response.getContentText());

    if (result.status === "running") {
      if (result.current_task && result.current_task.running) {
        return {
          "status": "busy",
          "message": "タスク実行中: " + result.current_task.command
        };
      } else {
        return {
          "status": "ready",
          "message": "サーバー稼働中（待機中）"
        };
      }
    }
    return {
      "status": "unknown",
      "message": "サーバー状態不明"
    };
  } catch (e) {
    return {
      "status": "error",
      "message": "サーバーに接続できません: " + e.message
    };
  }
}

// ==========================================
// 非常停止（Playwright処理を緊急停止）
// ==========================================

/**
 * VPS上のPlaywright処理を緊急停止する
 * POST /api/stop → 現在処理中の利用者の操作が完了した後に停止
 * @returns {Object} {success, message}
 */
function emergencyStop() {
  var url = API_BASE_URL + "/api/stop";

  var options = {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify({}),
    "muteHttpExceptions": true
  };

  try {
    console.log('[emergencyStop] 非常停止を要求');
    var response = UrlFetchApp.fetch(url, options);
    var statusCode = response.getResponseCode();
    var result = JSON.parse(response.getContentText());

    console.log('[emergencyStop] statusCode=' + statusCode + ' response=' + response.getContentText().substring(0, 300));

    if (statusCode === 200 && result.success) {
      var taskName = (result.current_task && result.current_task.command) ? result.current_task.command : 'なし';
      return {
        "success": true,
        "message": "非常停止を要求しました。\n\n" +
                   result.message + "\n\n" +
                   "対象タスク: " + taskName
      };
    } else {
      return {
        "success": false,
        "message": "停止に失敗しました: " + (result.error || result.message || "不明なエラー")
      };
    }
  } catch (e) {
    console.error('[emergencyStop] エラー:', e);
    return {
      "success": false,
      "message": "非常停止リクエストに失敗しました。\nサーバーに接続できません。\nエラー: " + e.message
    };
  }
}

// ==========================================
// 月間スケジュール展開
// ==========================================
function runExpand(month) {
  var url = API_BASE_URL + "/api/expand";
  var statusUrl = API_BASE_URL + "/api/status";

  var payload = {
    "month": month || getCurrentMonth()
  };

  var options = {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify(payload),
    "muteHttpExceptions": true  // 524でも例外にしない
  };

  // 1) expand リクエスト送信
  var result = null;
  try {
    var response = UrlFetchApp.fetch(url, options);
    var statusCode = response.getResponseCode();

    if (statusCode === 409) {
      var errBody = JSON.parse(response.getContentText());
      return {
        "success": false,
        "message": "エラー: " + errBody.error
      };
    }

    result = JSON.parse(response.getContentText());
  } catch (e) {
    // 524タイムアウト（Cloudflare）またはJSONパースエラー
    // → サーバー側は処理継続中なのでポーリングで待つ
    console.log('[runExpand] レスポンスタイムアウト（524想定）。サーバー側の完了を待ちます... error=' + e.message);
    result = null;
  }

  // 2) 正常レスポンスが得られた場合はそのまま返す
  if (result && result.success) {
    var r = result.result;
    return {
      "success": true,
      "message": "展開完了!\n" +
                 "成功: " + r.success + "件\n" +
                 "スキップ: " + r.skipped + "件\n" +
                 "失敗: " + r.failed + "件\n" +
                 "合計: " + r.total + "件",
      "data": r
    };
  }

  // 3) 正常レスポンスが得られなかった場合、/api/status をポーリングで完了を待つ
  console.log('[runExpand] Polling /api/status for completion...');
  var maxRetries = 40; // 最大40回 × 15秒 = 10分
  for (var i = 0; i < maxRetries; i++) {
    Utilities.sleep(15000); // 15秒待機

    try {
      var statusRes = UrlFetchApp.fetch(statusUrl, { "muteHttpExceptions": true });
      var status = JSON.parse(statusRes.getContentText());

      // current_task.running が false になったら完了
      if (!status.current_task || !status.current_task.running) {
        console.log('[runExpand] 展開完了（サーバー確認済み） poll #' + (i + 1));
        return {
          "success": true,
          "message": "展開完了!（サーバー側で正常終了）\n\n※ Cloudflareタイムアウトにより詳細結果は取得できませんでした。\nVPSログで結果を確認してください。"
        };
      }
      console.log('[runExpand] 実行中... (' + (i + 1) + '/' + maxRetries + ')');
    } catch (pollError) {
      console.error('[runExpand] ステータス確認エラー:', pollError.message);
      // ネットワークエラーは無視して次のポーリングへ
    }
  }

  // タイムアウト
  return {
    "success": false,
    "message": "タイムアウト: 展開の完了確認が10分以内に完了しませんでした。\nVPSサーバーの状態を確認してください。"
  };
}

// ==========================================
// CSV出力（Google Driveアップロード付き）
// ==========================================
function runExport(month) {
  var url = API_BASE_URL + "/api/export";
  var targetMonth = month || getCurrentMonth();

  var payload = {
    "month": targetMonth
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

    if (statusCode === 200 && result.success) {
      var csvContent = result.result.csv_content;
      var driveResult = null;

      // csv_contentがあればGoogle Driveに保存
      if (csvContent) {
        driveResult = saveCsvToDrive_(csvContent, targetMonth);
      }

      var msg = "CSV出力完了!";
      if (result.result.file_path) {
        msg += "\nVPS出力先: " + result.result.file_path;
      }
      if (driveResult && driveResult.success) {
        msg += "\nGoogle Drive保存: " + driveResult.fileName;
      } else if (csvContent && driveResult && !driveResult.success) {
        msg += "\nDrive保存エラー: " + driveResult.message;
      }

      return {
        "success": true,
        "message": msg,
        "data": result.result,
        "driveFileId": driveResult ? driveResult.fileId : null
      };
    } else if (statusCode === 409) {
      return {
        "success": false,
        "message": "エラー: " + result.error
      };
    } else {
      return {
        "success": false,
        "message": "エラー: " + (result.error || "不明なエラー")
      };
    }
  } catch (e) {
    return {
      "success": false,
      "message": "サーバー接続エラー: " + e.message
    };
  }
}

/**
 * CSVテキストをGoogle Driveに保存
 * @param {string} csvContent - CSV文字列
 * @param {string} month - 対象月（YYYY-MM形式）
 * @return {Object} {success, fileId, fileName, message}
 */
function saveCsvToDrive_(csvContent, month) {
  try {
    var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
    var monthStr = month.replace("-", "");
    var fileName = "kaipoke_current_" + monthStr + ".csv";

    // 既存ファイルがあれば削除（上書き）
    var existingFiles = folder.getFilesByName(fileName);
    while (existingFiles.hasNext()) {
      existingFiles.next().setTrashed(true);
    }

    // BOM付きUTF-8で保存（Excel対応）
    var bom = "\uFEFF";
    var blob = Utilities.newBlob(bom + csvContent, "text/csv", fileName);
    var file = folder.createFile(blob);

    return {
      "success": true,
      "fileId": file.getId(),
      "fileName": fileName,
      "message": "保存完了"
    };
  } catch (e) {
    return {
      "success": false,
      "fileId": null,
      "fileName": null,
      "message": e.message
    };
  }
}

// ==========================================
// 差分確認（プレビュー）- パターンB: CSV内容を直接送信
// ==========================================
function checkDiff(month, weekStart) {
  var url = API_BASE_URL + "/api/diff";
  var targetMonth = month || getCurrentMonth();
  var monthStr = targetMonth.replace("-", "");

  // weekStart必須チェック
  if (!weekStart) {
    return {
      "success": false,
      "message": "エラー: 対象週が指定されていません。"
    };
  }

  // 週範囲を算出（YYYYMMDD形式 + YYYY-MM-DD形式）
  var weekRange = getWeekRange_(weekStart);
  console.log('[checkDiff] month=' + targetMonth + ' weekStart=' + weekStart + ' weekEnd=' + weekRange.endDate);

  // --- Google DriveからCSVを読み込み ---
  var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);

  // 1. kaipoke_current_YYYYMM.csv を読み込み
  var currentFileName = "kaipoke_current_" + monthStr + ".csv";
  var currentCsvContent = readCsvContentFromDrive_(folder, currentFileName);
  if (currentCsvContent === null) {
    return {
      "success": false,
      "message": "エラー: Google Driveに「" + currentFileName + "」が見つかりません。\n先にCSV出力（ステップ3）を実行してください。"
    };
  }
  console.log('[checkDiff] currentCSV読み込み完了: ' + currentFileName + ' (' + currentCsvContent.length + '文字)');

  // 2. gas_optimized_YYYYMMDD_YYYYMMDD.csv を読み込み
  var optimizedFileName = "gas_optimized_" + weekRange.startStr + "_" + weekRange.endStr + ".csv";
  var optimizedCsvContent = readCsvContentFromDrive_(folder, optimizedFileName);
  if (optimizedCsvContent === null) {
    return {
      "success": false,
      "message": "エラー: Google Driveに「" + optimizedFileName + "」が見つかりません。\n先にGAS側のCSV出力を実行してください。"
    };
  }
  console.log('[checkDiff] optimizedCSV読み込み完了: ' + optimizedFileName + ' (' + optimizedCsvContent.length + '文字)');

  // 3. CSV内容を直接送信（week_start/week_endはYYYYMMDD形式）
  var payload = {
    "current_csv_content": currentCsvContent,
    "optimized_csv_content": optimizedCsvContent,
    "week_start": weekRange.startStr,
    "week_end": weekRange.endStr
  };
  console.log('[checkDiff] payload keys: ' + Object.keys(payload).join(', ') + ' week_start=' + weekRange.startStr + ' week_end=' + weekRange.endStr);

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

    console.log('[checkDiff] statusCode=' + statusCode + ' responseBody=' + response.getContentText().substring(0, 500));

    if (statusCode === 200 && result.success) {
      // レスポンス構造: { success, result: { total_corrections, summary: {...}, corrections: [...], drive_file: {...}, csv_content: "..." } }
      var r = result.result || {};
      var s = r.summary || {};
      var additions    = s.additions    || 0;
      var deletions    = s.deletions    || 0;
      var edits        = s.edits        || 0;
      var dateChangeActions = s.date_change_actions || 0;
      var timeChanges  = s.time_changes || 0;
      var staffChanges = s.staff_changes || 0;
      var dateChanges  = s.date_changes || 0;
      var events       = s.events       || 0;
      var totalChanges = r.total_corrections || (additions + deletions + edits + dateChangeActions);

      console.log('[checkDiff] summary: additions=' + additions + ' deletions=' + deletions + ' edits=' + edits + ' date_change_actions=' + dateChangeActions + ' events=' + events + ' total=' + totalChanges);

      // --- GAS側でcsv_contentをDriveにアップロード ---
      var uploadedFileId = null;
      var csvContentStr = r.csv_content || '';
      var driveFileInfo = r.drive_file || {};
      if (csvContentStr && driveFileInfo.folder_id && driveFileInfo.filename) {
        try {
          var uploadFolder = DriveApp.getFolderById(driveFileInfo.folder_id);
          // 既存の同名ファイルを削除
          var existingFiles = uploadFolder.getFilesByName(driveFileInfo.filename);
          while (existingFiles.hasNext()) {
            existingFiles.next().setTrashed(true);
          }
          var newFile = uploadFolder.createFile(driveFileInfo.filename, csvContentStr, MimeType.CSV);
          uploadedFileId = newFile.getId();
          console.log('[checkDiff] Drive upload完了: ' + driveFileInfo.filename + ' fileId=' + uploadedFileId);
        } catch (uploadErr) {
          console.error('[checkDiff] Drive upload失敗:', uploadErr);
        }
      } else {
        console.log('[checkDiff] csv_contentまたはdrive_file情報が不足のためDriveアップロードをスキップ');
      }

      // 差分検証を実行（現行CSVも渡して削除対象の存在確認に使用）
      var verification = verifyDiffResult(r, weekRange, currentCsvContent);
      console.log('[checkDiff] verification ok=' + verification.ok);

      // 差分結果シートへ書き込み
      try {
        displayDiffSummary(r, uploadedFileId);
      } catch (dispErr) {
        console.error('[checkDiff] displayDiffSummary error:', dispErr);
      }

      // 検証フラグをPropertiesServiceに保存
      var props = PropertiesService.getScriptProperties();
      props.setProperty('diff_verified', verification.ok ? 'true' : 'false');
      props.setProperty('diff_week_start', weekStart);
      if (uploadedFileId) {
        props.setProperty('diff_file_id', uploadedFileId);
      }

      // 修正データを隠しシートに保存（適用時にcorrection_dataとして使用）
      try {
        storeCorrections_(r.corrections || []);
        console.log('[checkDiff] corrections保存完了: ' + (r.corrections || []).length + '件');
      } catch (storeErr) {
        console.error('[checkDiff] corrections保存エラー:', storeErr);
      }

      var summaryMsg = "差分確認結果（" + weekStart + " 〜 " + weekRange.endDate + "）:\n" +
                   "追加予定: " + additions + "件\n" +
                   "削除予定: " + deletions + "件\n" +
                   "編集予定: " + edits + "件\n" +
                   "日付移動: " + dateChangeActions + "件\n" +
                   "時間変更: " + timeChanges + "件\n" +
                   "職員変更: " + staffChanges + "件\n" +
                   "日付変更: " + dateChanges + "件\n" +
                   "イベント: " + events + "件\n" +
                   "合計: " + totalChanges + "件";

      // 業務種別別の内訳
      var byBT = s.by_business_type || {};
      var btKeys = Object.keys(byBT);
      if (btKeys.length > 0) {
        summaryMsg += "\n\n【業務種別別】";
        for (var bi = 0; bi < btKeys.length; bi++) {
          summaryMsg += "\n" + btKeys[bi] + ": " + byBT[btKeys[bi]] + "件";
        }
      }

      // 検証結果を追加
      summaryMsg += "\n\n" + verification.message;

      return {
        "success": true,
        "message": summaryMsg,
        "data": r,
        "verified": verification.ok
      };
    } else {
      return {
        "success": false,
        "message": "エラー: " + (result.error || result.message || "不明なエラー")
      };
    }
  } catch (e) {
    return {
      "success": false,
      "message": "サーバー接続エラー: " + e.message
    };
  }
}

// ==========================================
// 差分適用 - 3分割（クライアントサイドポーリング対応）
// ==========================================
// startApply()  → HTML側のsetInterval → pollApplyResult() → finalizeApply()
// google.script.run のタイムアウトを回避するため、ポーリングをHTML側に移行

/**
 * 差分適用を開始する（即座に返る）
 * バリデーション + POST /api/apply のみ。ポーリングは行わない。
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
  console.log('[runApply] month=' + targetMonth + ' weekStart=' + weekStart + ' corrections=' + corrections.length + '件');

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

/**
 * 1回のポーリング（即座に返る）
 * GET /api/apply/result を1回呼んで結果をそのまま返す。
 * HTML側から setInterval で繰り返し呼ばれる。実行時間: 1-2秒
 * @returns {Object} サーバーのレスポンス
 */
function pollApplyResult() {
  var pollUrl = API_BASE_URL + "/api/apply/result";
  var resp = UrlFetchApp.fetch(pollUrl, { "method": "get", "muteHttpExceptions": true });
  var text = resp.getContentText();
  var code = resp.getResponseCode();
  try {
    return JSON.parse(text);
  } catch (e) {
    // Cloudflareエラーページ等、非JSONレスポンスの場合
    return { status: 'error', error: 'サーバー応答エラー (HTTP ' + code + '): JSONではないレスポンスを受信しました' };
  }
}

/**
 * 適用完了後の後処理（即座に返る）
 * シート書き込み・キャッシュ保存・フラグクリア・結果メッセージ構築
 * HTML側から google.script.run.finalizeApply(JSON.stringify(data.result), weekStart) で呼ばれる。
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

  // 結果メッセージ
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

// ==========================================
// 差分検証
// ==========================================

/**
 * 差分結果を最適化CSVと照合し、整合性を検証する
 * チェック1: csv_content存在確認
 * チェック2: CSV行数一致
 * チェック3: 追加利用者の存在確認
 * チェック4: アクション合計一致 (add + delete + edit + date_change_actions == total)
 * チェック5: 業務種別の整合性 (addアクションの業務種別が最適化CSVと一致)
 * チェック6: 業務種別合計一致
 * チェック7: addエントリの内容照合（最適化CSVの利用者+日付+開始時間で一致確認）
 * チェック8: deleteエントリの存在確認（現行CSVに削除対象が存在するか）
 *
 * @param {Object} diffResult - /api/diff のレスポンス result
 * @param {Object} weekRange - { startStr, endStr, endDate }
 * @param {string} [currentCsvContent] - カイポケ現行CSVの内容（チェック8用）
 * @returns {Object} { ok: boolean, message: string }
 */
function verifyDiffResult(diffResult, weekRange, currentCsvContent) {
  var errors = [];
  var warnings = [];

  // === チェック1: csv_contentが存在するか ===
  var csvContentStr = diffResult.csv_content || '';
  if (!csvContentStr) {
    errors.push('APIレスポンスにcsv_contentが含まれていません');
    // csv_contentがないとチェック2もできないので早期リターン
    var message1 = '【検証NG】差分適用は実行できません';
    message1 += '\n\n[エラー]\n- ' + errors[0];
    return { ok: false, message: message1 };
  }

  // === チェック2: CSV行数一致 ===
  try {
    var csvForParse = csvContentStr;
    // BOM除去
    if (csvForParse.charCodeAt(0) === 0xFEFF) {
      csvForParse = csvForParse.substring(1);
    }
    var csvRows = Utilities.parseCsv(csvForParse);
    var csvDataRows = csvRows.length - 1; // ヘッダー除く

    if (csvDataRows !== diffResult.total_corrections) {
      errors.push('CSV行数とAPI件数が不一致: CSV=' + csvDataRows + '行, API=' + diffResult.total_corrections + '件');
    }
  } catch (csvErr) {
    warnings.push('CSVパースエラー: ' + csvErr.message);
  }

  // === チェック3: 最適化CSVと照合（追加利用者の存在確認） ===
  var corrections = diffResult.corrections || [];
  if (corrections.length > 0) {
    try {
      var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
      var optimizedFileName = 'gas_optimized_' + weekRange.startStr + '_' + weekRange.endStr + '.csv';
      var optimizedContent = readCsvContentFromDrive_(folder, optimizedFileName);

      if (!optimizedContent) {
        warnings.push('最適化CSVがDriveに見つかりません。照合をスキップします。');
      } else {
        var optimizedRows = Utilities.parseCsv(optimizedContent);

        // 最適化CSVの利用者リストを取得（列12=利用者, 0-indexed: index 11）
        var optimizedUsers = {};
        for (var oi = 1; oi < optimizedRows.length; oi++) {
          var userName = (optimizedRows[oi][11] || '').trim();
          if (userName) {
            optimizedUsers[userName] = true;
          }
        }

        // 追加アクションの利用者が最適化CSVに存在するか
        for (var ci = 0; ci < corrections.length; ci++) {
          var c = corrections[ci];
          if (c.action === 'add' && c.user_name) {
            if (!optimizedUsers[c.user_name] && c.user_name !== 'なし') {
              errors.push('追加予定の利用者「' + c.user_name + '」が最適化CSVに存在しません');
            }
          }
        }

        // === チェック5: 業務種別の整合性チェック（addアクションの業務種別が最適化CSVと一致） ===
        var optimizedBusinessTypes = {};
        for (var obi = 1; obi < optimizedRows.length; obi++) {
          var obUserName = (optimizedRows[obi][11] || '').trim(); // 列12=利用者
          var obBT = (optimizedRows[obi][12] || '').trim();       // 列13=業務種別
          if (obUserName && obBT) {
            if (!optimizedBusinessTypes[obUserName]) {
              optimizedBusinessTypes[obUserName] = {};
            }
            optimizedBusinessTypes[obUserName][obBT] = true;
          }
        }

        for (var cbi = 0; cbi < corrections.length; cbi++) {
          var cb = corrections[cbi];
          if (cb.action === 'add' && cb.business_type && cb.user_name) {
            var userBTs = optimizedBusinessTypes[cb.user_name];
            if (userBTs && !userBTs[cb.business_type]) {
              var existingBTs = Object.keys(userBTs).join(', ');
              warnings.push('「' + cb.user_name + '」の業務種別「' + cb.business_type +
                '」が最適化CSVと異なります（最適化CSV: ' + existingBTs + '）');
            }
          }
        }

        // === チェック7: addエントリの内容照合（最適化CSV） ===
        // 最適化CSVをマップ化（利用者名+日付+開始時間 → true）
        var optimizedMap = {};
        for (var omi = 1; omi < optimizedRows.length; omi++) {
          var omRow = optimizedRows[omi];
          var omKey = (omRow[11] || '').trim() + '|' + (omRow[9] || '').trim() + '|' + (omRow[14] || '').trim();
          optimizedMap[omKey] = true;
        }

        for (var aci = 0; aci < corrections.length; aci++) {
          var ac = corrections[aci];
          if (ac.action === 'add') {
            var addKey = (ac.user_name || '') + '|' + (ac.date_to || '') + '|' + (ac.start_time_to || '');
            if (!optimizedMap[addKey]) {
              errors.push('追加「' + ac.user_name + '」(' + ac.date_to + '日 ' + ac.start_time_to + ')が最適化CSVに見つかりません');
            }
          }
        }
      }
    } catch (optErr) {
      warnings.push('最適化CSV照合エラー: ' + optErr.message);
    }
  }

  // === チェック8: deleteエントリの存在確認（現行CSV） ===
  if (currentCsvContent) {
    try {
      var currentForParse = currentCsvContent;
      if (currentForParse.charCodeAt(0) === 0xFEFF) {
        currentForParse = currentForParse.substring(1);
      }
      var currentRows = Utilities.parseCsv(currentForParse);

      // 現行CSVをマップ化（利用者名+日付+開始時間 → true）
      var currentMap = {};
      for (var cri = 1; cri < currentRows.length; cri++) {
        var crRow = currentRows[cri];
        var crKey = (crRow[11] || '').trim() + '|' + (crRow[9] || '').trim() + '|' + (crRow[14] || '').trim();
        currentMap[crKey] = true;
      }

      for (var dci = 0; dci < corrections.length; dci++) {
        var dc = corrections[dci];
        if (dc.action === 'delete') {
          var delKey = (dc.user_name || '') + '|' + (dc.date_from || '') + '|' + (dc.start_time_from || '');
          if (!currentMap[delKey]) {
            warnings.push('削除対象「' + dc.user_name + '」(' + dc.date_from + '日 ' + dc.start_time_from + ')が現行CSVに見つかりません');
          }
        }
      }
    } catch (curErr) {
      warnings.push('現行CSV照合エラー: ' + curErr.message);
    }
  } else {
    warnings.push('現行CSVが利用できないため、削除対象の存在確認をスキップしました');
  }

  // === チェック4: サマリーの妥当性チェック（アクション合計一致） ===
  var summary = diffResult.summary || {};
  var totalActions = (summary.additions || 0) + (summary.deletions || 0) + (summary.edits || 0) + (summary.date_change_actions || 0);
  if (diffResult.total_corrections && totalActions !== diffResult.total_corrections) {
    warnings.push('アクション合計: add(' + (summary.additions || 0) +
      ')+delete(' + (summary.deletions || 0) +
      ')+edit(' + (summary.edits || 0) +
      ')+date_change(' + (summary.date_change_actions || 0) +
      ')=' + totalActions +
      ' vs total=' + diffResult.total_corrections);
  }

  // === チェック6: 業務種別の分布チェック ===
  var byBT = summary.by_business_type || {};
  var btKeys = Object.keys(byBT);
  if (btKeys.length > 0) {
    var btTotal = 0;
    for (var bk = 0; bk < btKeys.length; bk++) {
      btTotal += byBT[btKeys[bk]];
    }
    if (diffResult.total_corrections && btTotal !== diffResult.total_corrections) {
      warnings.push('業務種別合計(' + btTotal + ')と総修正数(' + diffResult.total_corrections + ')が不一致');
    }
  }

  // === 結果まとめ ===
  var ok = errors.length === 0;
  var message = '';

  if (ok) {
    message = '【検証OK】差分適用を実行できます';
  } else {
    message = '【検証NG】差分適用は実行できません';
  }

  if (errors.length > 0) {
    message += '\n\n[エラー]';
    for (var ei = 0; ei < errors.length; ei++) {
      message += '\n- ' + errors[ei];
    }
  }

  if (warnings.length > 0) {
    message += '\n\n[警告]';
    for (var wi = 0; wi < warnings.length; wi++) {
      message += '\n- ' + warnings[wi];
    }
  }

  return { ok: ok, message: message };
}

// ==========================================
// 差分結果シートへの書き込み
// ==========================================

/**
 * 差分結果のサマリーを「差分結果」シートに表示する
 * @param {Object} result - /api/diff レスポンスの result
 * @param {string} [uploadedFileId] - GAS側でDriveにアップロードしたファイルのID
 */
function displayDiffSummary(result, uploadedFileId) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName('差分結果');

  // シートがなければ作成
  if (!sheet) {
    sheet = ss.insertSheet('差分結果');
  }

  // シートクリア
  sheet.clear();

  var corrections = result.corrections || [];
  if (corrections.length === 0) {
    sheet.getRange('A1').setValue('差分データなし');
    return;
  }

  // ヘッダー（15列）
  var headers = [
    '利用者', '日付(前)', '日付(後)',
    '開始時間(前)', '開始時間(後)', '終了時間(前)', '終了時間(後)',
    '職員1(前)', '職員1(後)', '職員2(前)', '職員2(後)',
    'サービス内容', 'アクション', '業務種別', '備考'
  ];
  sheet.getRange(1, 1, 1, 15).setValues([headers]);

  // ヘッダースタイル
  var headerRange = sheet.getRange(1, 1, 1, 15);
  headerRange.setFontWeight('bold');
  headerRange.setBackground('#e0e0e0');

  // データ行
  var data = [];
  for (var i = 0; i < corrections.length; i++) {
    var c = corrections[i];
    data.push([
      c.user_name || '',
      c.date_from || '', c.date_to || '',
      c.start_time_from || '', c.start_time_to || '',
      c.end_time_from || '', c.end_time_to || '',
      c.staff1_from || '', c.staff1_to || '',
      c.staff2_from || '', c.staff2_to || '',
      c.service_type || '', c.action || '',
      c.business_type || '', c.remarks || ''
    ]);
  }
  sheet.getRange(2, 1, data.length, 15).setValues(data);

  // 色分け（アクション別）
  for (var j = 0; j < corrections.length; j++) {
    var row = j + 2;
    var action = corrections[j].action || '';
    var range = sheet.getRange(row, 1, 1, 15);
    if (action === 'add') {
      range.setBackground('#d4edda');  // 緑（追加）
    } else if (action === 'delete') {
      range.setBackground('#f8d7da');  // 赤（削除）
    } else if (action === 'edit') {
      range.setBackground('#fff3cd');  // 黄（編集）
    } else if (action === 'date_change') {
      range.setBackground('#cce5ff');  // 青（日付移動）
    }
  }

  // Drive情報を最下部に表示
  var driveFileInfo = result.drive_file || {};
  var infoRow = corrections.length + 4;
  if (uploadedFileId) {
    sheet.getRange(infoRow, 1).setValue('Drive File ID:');
    sheet.getRange(infoRow, 2).setValue(uploadedFileId);
  }
  if (driveFileInfo.filename) {
    sheet.getRange(infoRow + 1, 1).setValue('Drive Filename:');
    sheet.getRange(infoRow + 1, 2).setValue(driveFileInfo.filename);
  }

  // 列幅自動調整
  for (var col = 1; col <= 15; col++) {
    sheet.autoResizeColumn(col);
  }
}

// ==========================================
// 接続テスト
// ==========================================
function runConnectionTest() {
  var url = API_BASE_URL + "/api/test";

  var testPayload = {
    "action": "ping",
    "timestamp": new Date().toISOString(),
    "source": "gas_sidebar"
  };

  var options = {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify(testPayload),
    "muteHttpExceptions": true
  };

  try {
    var startTime = new Date().getTime();
    var response = UrlFetchApp.fetch(url, options);
    var endTime = new Date().getTime();
    var responseTime = endTime - startTime;

    var statusCode = response.getResponseCode();
    var result = JSON.parse(response.getContentText());

    if (statusCode === 200 && result.success) {
      return {
        "success": true,
        "message": "接続テスト成功!\n" +
                   "ステータス: OK\n" +
                   "応答時間: " + responseTime + "ms\n" +
                   "サーバー時刻: " + (result.server_time || "N/A") + "\n" +
                   "メッセージ: " + (result.message || "テスト完了")
      };
    } else {
      return {
        "success": false,
        "message": "接続テスト失敗\n" +
                   "ステータスコード: " + statusCode + "\n" +
                   "エラー: " + (result.error || "不明なエラー")
      };
    }
  } catch (e) {
    // /api/test が存在しない場合は /api/status にフォールバック
    try {
      var statusUrl = API_BASE_URL + "/api/status";
      var statusOptions = {
        "method": "get",
        "muteHttpExceptions": true
      };

      var startTime2 = new Date().getTime();
      var statusResponse = UrlFetchApp.fetch(statusUrl, statusOptions);
      var endTime2 = new Date().getTime();
      var responseTime2 = endTime2 - startTime2;

      var statusResult = JSON.parse(statusResponse.getContentText());

      if (statusResult.status === "running") {
        return {
          "success": true,
          "message": "接続テスト成功!\n" +
                     "（/api/status で確認）\n" +
                     "ステータス: " + statusResult.status + "\n" +
                     "応答時間: " + responseTime2 + "ms"
        };
      } else {
        return {
          "success": false,
          "message": "サーバー状態が異常です: " + statusResult.status
        };
      }
    } catch (e2) {
      return {
        "success": false,
        "message": "サーバーに接続できません\n" +
                   "URL: " + API_BASE_URL + "\n" +
                   "エラー: " + e.message
      };
    }
  }
}

// ==========================================
// ログ取得
// ==========================================
function kaipoke_logs(tail) {
  var url = API_BASE_URL + "/api/kaipoke/logs";
  if (tail) {
    url += "?tail=" + tail;
  }

  var options = {
    "method": "get",
    "muteHttpExceptions": true,
    "headers": {
      "Content-Type": "application/json"
    }
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var statusCode = response.getResponseCode();
    var result = JSON.parse(response.getContentText());

    if (statusCode === 200 && result.ok) {
      return {
        "success": true,
        "lines": result.lines || []
      };
    } else {
      return {
        "success": false,
        "lines": [],
        "message": result.error || "ログ取得エラー"
      };
    }
  } catch (e) {
    return {
      "success": false,
      "lines": ["[ローカル] ログ取得失敗: " + e.message],
      "message": e.message
    };
  }
}

// ==========================================
// VNC URL取得
// ==========================================
function kaipoke_vncUrl() {
  var url = API_BASE_URL + "/api/kaipoke/vnc-url";

  var options = {
    "method": "get",
    "muteHttpExceptions": true,
    "headers": {
      "Content-Type": "application/json"
    }
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var statusCode = response.getResponseCode();
    var result = JSON.parse(response.getContentText());

    if (statusCode === 200 && result.ok) {
      return {
        "success": true,
        "url": result.url,
        "ready": result.ready || false
      };
    } else {
      return {
        "success": false,
        "url": null,
        "message": result.error || "VNC URL取得エラー"
      };
    }
  } catch (e) {
    return {
      "success": false,
      "url": null,
      "message": e.message
    };
  }
}

// ==========================================
// 拡張ステータス取得（VNC URL含む）
// ==========================================
function kaipoke_status() {
  var url = API_BASE_URL + "/api/kaipoke/status";

  var options = {
    "method": "get",
    "muteHttpExceptions": true,
    "headers": {
      "Content-Type": "application/json"
    }
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var statusCode = response.getResponseCode();
    var result = JSON.parse(response.getContentText());

    if (statusCode === 200 && result.ok) {
      return {
        "success": true,
        "server": result.server || {},
        "job": result.job || {},
        "vnc": result.vnc || {},
        "message": result.message || "OK"
      };
    } else {
      return {
        "success": false,
        "message": result.error || "ステータス取得エラー"
      };
    }
  } catch (e) {
    return {
      "success": false,
      "message": "サーバー接続エラー: " + e.message
    };
  }
}

// ==========================================
// 設定更新（DriveフォルダID）
// ==========================================
function setDriveFolderId(folderId) {
  var url = API_BASE_URL + "/api/config";

  var payload = {
    "folder_id": folderId
  };

  var options = {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify(payload),
    "muteHttpExceptions": true
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var result = JSON.parse(response.getContentText());

    if (result.success) {
      return {
        "success": true,
        "message": "設定を更新しました"
      };
    } else {
      return {
        "success": false,
        "message": "エラー: " + result.error
      };
    }
  } catch (e) {
    return {
      "success": false,
      "message": "接続エラー: " + e.message
    };
  }
}

// ==========================================
// 修正データの永続化（隠しシート方式）
// ==========================================

/**
 * 修正データを隠しシートに保存（適用時にcorrection_dataとして使用）
 * PropertiesServiceは9KB制限があるため、シートに保存する
 * @param {Array} corrections - 修正データ配列
 */
function storeCorrections_(corrections) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName('_corrections_cache');
  if (!sheet) {
    sheet = ss.insertSheet('_corrections_cache');
    sheet.hideSheet();
  }
  sheet.clear();
  sheet.getRange(1, 1).setValue(JSON.stringify(corrections));
}

/**
 * 保存済みの修正データを取得
 * @returns {Array} 修正データ配列
 */
function getStoredCorrections_() {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName('_corrections_cache');
  if (!sheet) return [];
  var json = sheet.getRange(1, 1).getValue();
  return json ? JSON.parse(json) : [];
}

// ==========================================
// 未割当職員チェック
// ==========================================

/**
 * 保存済みの修正データから未割当（staff1_to="未割当"）の件数と詳細を返す
 * サイドバーから呼び出され、適用前の警告に使用
 * @returns {Object} { hasUnassigned, count, message }
 */
function getUnassignedWarning() {
  var corrections = getStoredCorrections_();
  var unassigned = [];
  for (var i = 0; i < corrections.length; i++) {
    var c = corrections[i];
    if (c.staff1_to === '未割当') {
      unassigned.push(c);
    }
  }

  if (unassigned.length === 0) {
    return { hasUnassigned: false, count: 0, message: '' };
  }

  var lines = [];
  for (var j = 0; j < unassigned.length; j++) {
    var u = unassigned[j];
    lines.push('  ' + (u.user_name || '') + ' ' + (u.date_to || '') + '日 ' +
      (u.start_time_to || '') + '-' + (u.end_time_to || '') + ' (' + (u.action || '') + ')');
  }

  return {
    hasUnassigned: true,
    count: unassigned.length,
    message: '以下の ' + unassigned.length + ' 件は職員が「未割当」です。\n' +
      'カイポケ上では職員未選択（\'-\'）として登録されます。\n\n' +
      lines.join('\n') + '\n\nこのまま適用しますか？'
  };
}

// ==========================================
// 適用結果シートへの書き込み
// ==========================================

/**
 * 適用結果を「適用結果」シートに書き込む
 * @param {Object} result - /api/apply レスポンスの result
 */
function writeApplyResultToSheet(result) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName('適用結果');
  if (!sheet) {
    sheet = ss.insertSheet('適用結果');
  }
  sheet.clear();

  // ヘッダー（9列）
  var headers = ['利用者/職員', '日付', 'アクション', '業務種別',
                  'ステータス', '理由', 'イベント名', 'Phase', 'タイムスタンプ'];
  sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  sheet.getRange(1, 1, 1, headers.length).setFontWeight('bold').setBackground('#4a86c8').setFontColor('#ffffff');

  // サマリー行（2行目）
  var timestamp = result.completed_at || Utilities.formatDate(new Date(), 'JST', 'yyyy-MM-dd HH:mm:ss');
  var executionTimeStr = result.execution_time_sec ? (result.execution_time_sec + '秒') : '';
  var summaryRow = [
    '合計: ' + (result.total || 0) + '件', executionTimeStr,
    '成功: ' + (result.success || 0), '',
    '失敗: ' + (result.failed || 0),
    'スキップ: ' + (result.skipped || 0), '', '', timestamp
  ];
  sheet.getRange(2, 1, 1, headers.length).setValues([summaryRow]);
  sheet.getRange(2, 1, 1, headers.length).setFontWeight('bold').setBackground('#e2e3e5');

  // 詳細データ（3行目～）
  var details = result.details || [];
  if (details.length > 0) {
    var rows = [];
    for (var i = 0; i < details.length; i++) {
      var d = details[i];
      rows.push([
        d.user || d.staff || '',
        d.date || '',
        d.action || '',
        d.business_type || '',
        d.status || '',
        d.reason || '',
        d.event_name || '',
        (d.action === 'event_add') ? 'Phase 2' : 'Phase 1',
        timestamp
      ]);
    }
    sheet.getRange(3, 1, rows.length, headers.length).setValues(rows);

    // 色分け
    var colorMap = {
      'success': '#d4edda',
      'failed': '#f8d7da',
      'error': '#f8d7da',
      'skipped': '#fff3cd'
    };
    for (var j = 0; j < rows.length; j++) {
      var status = rows[j][4];
      var color = colorMap[status] || '#ffffff';
      sheet.getRange(3 + j, 1, 1, headers.length).setBackground(color);
    }
  }

  // warnings行を追加（詳細データの後）
  var warnings = result.warnings || [];
  if (warnings.length > 0) {
    var nextRow = 3 + (details.length > 0 ? details.length : 0);
    // 空行を挟む
    nextRow++;
    // 警告ヘッダー行
    var warningHeader = ['--- 警告 (' + warnings.length + '件) ---', '', '', '', '', '', '', '', ''];
    sheet.getRange(nextRow, 1, 1, headers.length).setValues([warningHeader]);
    sheet.getRange(nextRow, 1, 1, headers.length).setFontWeight('bold').setBackground('#fff3cd');
    nextRow++;
    // 各警告行
    var warningRows = [];
    for (var w = 0; w < warnings.length; w++) {
      warningRows.push([warnings[w], '', '', '', 'warning', '', '', '', timestamp]);
    }
    sheet.getRange(nextRow, 1, warningRows.length, headers.length).setValues(warningRows);
    sheet.getRange(nextRow, 1, warningRows.length, headers.length).setBackground('#fff3cd');
  }

  // 列幅自動調整
  for (var col = 1; col <= headers.length; col++) {
    sheet.autoResizeColumn(col);
  }
}

// ==========================================
// Google Drive操作（最適化CSV保存）
// ==========================================

/**
 * 最適化結果をCSVとしてGoogle Driveに保存
 * @param {Array} data - 2次元配列のスケジュールデータ
 * @param {string} month - 対象月（YYYY-MM形式）
 * @return {Object} 結果
 */
function saveOptimizedCsvToDrive(data, month) {
  try {
    var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
    var monthStr = month.replace("-", "");
    var fileName = "gas_optimized_" + monthStr + ".csv";

    // ヘッダー行
    var headers = [
      "職員名1", "職種1", "職員名2", "職種2", "同行2",
      "職員名3", "職種3", "同行3", "事業所名",
      "日付", "曜日", "利用者", "業務種別", "サービス内容",
      "開始時間", "終了時間", "提供時間", "備考"
    ];

    // CSVコンテンツを生成
    var csvContent = headers.join(",") + "\n";
    for (var i = 0; i < data.length; i++) {
      var row = data[i].map(function(cell) {
        // カンマや改行を含む場合はダブルクォートで囲む
        if (cell && (cell.toString().indexOf(",") >= 0 || cell.toString().indexOf("\n") >= 0)) {
          return '"' + cell.toString().replace(/"/g, '""') + '"';
        }
        return cell || "";
      });
      csvContent += row.join(",") + "\n";
    }

    // 既存ファイルがあれば削除
    var existingFiles = folder.getFilesByName(fileName);
    while (existingFiles.hasNext()) {
      existingFiles.next().setTrashed(true);
    }

    // 新規作成
    var blob = Utilities.newBlob(csvContent, "text/csv", fileName);
    var file = folder.createFile(blob);

    return {
      "success": true,
      "message": "保存完了: " + fileName,
      "fileId": file.getId()
    };
  } catch (e) {
    return {
      "success": false,
      "message": "保存エラー: " + e.message
    };
  }
}

/**
 * Google DriveからCSVを読み込み
 * @param {string} fileName - ファイル名
 * @return {Array} 2次元配列
 */
function loadCsvFromDrive(fileName) {
  try {
    var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
    var files = folder.getFilesByName(fileName);

    if (!files.hasNext()) {
      throw new Error("ファイルが見つかりません: " + fileName);
    }

    var file = files.next();
    var content = file.getBlob().getDataAsString("UTF-8");

    // BOM除去
    if (content.charCodeAt(0) === 0xFEFF) {
      content = content.substring(1);
    }

    // CSV解析
    var rows = Utilities.parseCsv(content);
    return rows;
  } catch (e) {
    throw new Error("CSV読み込みエラー: " + e.message);
  }
}

// ==========================================
// ユーティリティ関数
// ==========================================

/**
 * Google DriveフォルダからCSVファイルの内容を読み込む
 * @param {Folder} folder - Google Driveフォルダ
 * @param {string} fileName - ファイル名
 * @return {string|null} CSV文字列（BOM除去済み）、ファイルが無い場合はnull
 */
function readCsvContentFromDrive_(folder, fileName) {
  var files = folder.getFilesByName(fileName);
  if (!files.hasNext()) {
    console.log('[readCsvContentFromDrive_] ファイルが見つかりません: ' + fileName);
    return null;
  }
  var file = files.next();
  var content = file.getBlob().getDataAsString("UTF-8");
  // BOM除去
  if (content.charCodeAt(0) === 0xFEFF) {
    content = content.substring(1);
  }
  return content;
}

/**
 * 週の開始日から週範囲を算出
 * @param {string} weekStartStr - 週開始日（YYYY-MM-DD形式）
 * @return {Object} { startStr: 'YYYYMMDD', endStr: 'YYYYMMDD', endDate: 'YYYY-MM-DD' }
 */
function getWeekRange_(weekStartStr) {
  var parts = weekStartStr.split("-");
  var ws = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
  var we = new Date(ws);
  we.setDate(we.getDate() + 6);

  var startStr = Utilities.formatDate(ws, "JST", "yyyyMMdd");
  var endStr = Utilities.formatDate(we, "JST", "yyyyMMdd");
  var endDate = Utilities.formatDate(we, "JST", "yyyy-MM-dd");

  return {
    startStr: startStr,
    endStr: endStr,
    endDate: endDate
  };
}

/**
 * 現在の月をYYYY-MM形式で取得
 */
function getCurrentMonth() {
  var now = new Date();
  var year = now.getFullYear();
  var month = ("0" + (now.getMonth() + 1)).slice(-2);
  return year + "-" + month;
}

/**
 * 週の開始日（月曜日）を取得
 */
function getWeekStart(date) {
  var d = new Date(date);
  var day = d.getDay();
  var diff = d.getDate() - day + (day === 0 ? -6 : 1);
  d.setDate(diff);
  return Utilities.formatDate(d, "JST", "yyyy-MM-dd");
}

// ==========================================
// パスワード認証
// ==========================================

/**
 * スプレッドシートの「管理者」シートからカイポケパスワードを取得
 * 管理者シートの任意の行で、A列に「パスワード」（または「カイポケパスワード」）と書いて
 * B列にパスワードを設定する
 * @returns {string|null} パスワード文字列、未設定の場合はnull
 */
function getKaipokePassword_() {
  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = ss.getSheetByName('管理者');
    if (!sheet) return null;

    var data = sheet.getDataRange().getValues();
    for (var i = 0; i < data.length; i++) {
      var cellA = String(data[i][0] || '').trim();
      if (cellA === 'パスワード' || cellA === 'カイポケパスワード') {
        var pw = String(data[i][1] || '').trim();
        return pw || null;
      }
    }
    return null;
  } catch (e) {
    console.error('getKaipokePassword_ error:', e);
    return null;
  }
}

/**
 * カイポケ自動化のパスワードを検証
 * @param {string} inputPassword - ユーザーが入力したパスワード
 * @returns {Object} {success: boolean, message: string}
 */
function verifyKaipokePassword(inputPassword) {
  var storedPassword = getKaipokePassword_();

  if (!storedPassword) {
    // パスワード未設定の場合はアクセスを許可
    return { success: true, message: 'パスワード未設定のためアクセス許可' };
  }

  if (String(inputPassword || '').trim() === storedPassword) {
    return { success: true, message: '認証成功' };
  } else {
    return { success: false, message: 'パスワードが正しくありません' };
  }
}

// ==========================================
// インターロック設定
// ==========================================

/**
 * インターロック設定を取得
 * @returns {Object} {expandMinMonth, expandCompleted[], applyMinWeek, applyCompleted[]}
 */
function getInterlockSettings() {
  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = ss.getSheetByName('管理者');
    if (!sheet) return { expandMinMonth: null, expandCompleted: [], applyMinWeek: null, applyCompleted: [] };

    var data = sheet.getDataRange().getValues();
    var settings = {
      expandMinMonth: null,
      expandCompleted: [],
      applyMinWeek: null,
      applyCompleted: []
    };

    for (var i = 0; i < data.length; i++) {
      var key = String(data[i][0] || '').trim();
      var rawVal = data[i][1];

      // セルの値がDateオブジェクトの場合は適切なフォーマットに変換
      var val;
      if (rawVal instanceof Date) {
        val = Utilities.formatDate(rawVal, "JST", "yyyy-MM-dd");
        console.log('[getInterlockSettings] Date型検出: key=' + key + ' -> ' + val);
      } else {
        val = String(rawVal || '').trim();
      }

      if (key === '展開制限月') {
        // YYYY-MM形式に正規化（YYYY-MM-DDが来たらYYYY-MMに切る）
        settings.expandMinMonth = val ? val.substring(0, 7) : null;
      } else if (key === '展開完了月') {
        settings.expandCompleted = val ? val.split(',').map(function(s) { return s.trim().substring(0, 7); }).filter(Boolean) : [];
      } else if (key === '差分制限週') {
        // YYYY-MM-DD形式のまま使用
        settings.applyMinWeek = val || null;
      } else if (key === '差分完了週') {
        settings.applyCompleted = val ? val.split(',').map(function(s) { return s.trim(); }).filter(Boolean) : [];
      }
    }
    console.log('[getInterlockSettings] expandMinMonth=' + settings.expandMinMonth + ' applyMinWeek=' + settings.applyMinWeek);

    return settings;
  } catch (e) {
    console.error('getInterlockSettings error:', e);
    return { expandMinMonth: null, expandCompleted: [], applyMinWeek: null, applyCompleted: [] };
  }
}

/**
 * インターロック設定を保存（パスワード必須）
 * @param {string} key - 設定キー（展開制限月/展開完了月/差分制限週/差分完了週）
 * @param {string} value - 設定値
 * @param {string} password - パスワード
 * @returns {Object} {success: boolean, message: string}
 */
function saveInterlockSetting(key, value, password) {
  var result = verifyKaipokePassword(password);
  if (!result.success) {
    return { success: false, message: 'パスワードが正しくありません' };
  }

  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = ss.getSheetByName('管理者');
    if (!sheet) return { success: false, message: '管理者シートが見つかりません' };

    var data = sheet.getDataRange().getValues();
    var found = false;

    for (var i = 0; i < data.length; i++) {
      if (String(data[i][0] || '').trim() === key) {
        sheet.getRange(i + 1, 2).setValue(value);
        found = true;
        break;
      }
    }

    if (!found) {
      var lastRow = sheet.getLastRow();
      sheet.getRange(lastRow + 1, 1).setValue(key);
      sheet.getRange(lastRow + 1, 2).setValue(value);
    }

    return { success: true, message: '設定を保存しました' };
  } catch (e) {
    return { success: false, message: '保存エラー: ' + e.message };
  }
}

/**
 * 展開完了月を自動追加（パスワード不要）
 */
function markExpandCompleted(month) {
  return addToCompletedList_('展開完了月', month);
}

/**
 * 差分適用完了週を自動追加（パスワード不要）
 */
function markApplyCompleted(weekStart) {
  return addToCompletedList_('差分完了週', weekStart);
}

/**
 * 完了リストに値を追加するヘルパー
 */
function addToCompletedList_(key, value) {
  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = ss.getSheetByName('管理者');
    if (!sheet) return { success: false };

    var data = sheet.getDataRange().getValues();
    var found = false;

    for (var i = 0; i < data.length; i++) {
      if (String(data[i][0] || '').trim() === key) {
        var existing = String(data[i][1] || '').trim();
        var items = existing ? existing.split(',').map(function(s) { return s.trim(); }).filter(Boolean) : [];
        if (items.indexOf(value) < 0) {
          items.push(value);
        }
        sheet.getRange(i + 1, 2).setValue(items.join(','));
        found = true;
        break;
      }
    }

    if (!found) {
      var lastRow = sheet.getLastRow();
      sheet.getRange(lastRow + 1, 1).setValue(key);
      sheet.getRange(lastRow + 1, 2).setValue(value);
    }

    return { success: true };
  } catch (e) {
    return { success: false, message: e.message };
  }
}

// ==========================================
// サイドバー表示
// ==========================================

/**
 * 非常停止パネルをモードレスダイアログとして表示
 * サイドパネルとは独立した実行コンテキストなので、
 * サイドパネルがフリーズしても操作可能
 */
function showEmergencyStopDialog() {
  var html = HtmlService.createHtmlOutputFromFile('EmergencyStopDialog')
    .setWidth(320)
    .setHeight(220);
  SpreadsheetApp.getUi().showModelessDialog(html, '非常停止');
}

/**
 * カイポケ自動化サイドバーを表示
 */
function showKaipokeRpaSidebar() {
  var html = HtmlService.createHtmlOutputFromFile('KaipokeRpaSidebar')
    .setTitle('カイポケ自動化')
    .setWidth(350);
  SpreadsheetApp.getUi().showSidebar(html);
}

// ==========================================
// 適用後検証（ステップ6・7）— クライアントサイドポーリング対応
// ==========================================

/**
 * 適用後検証: CSV出力を非同期で開始（即座に返る）
 * @param {string} month - 対象月（YYYY-MM形式）
 * @returns {Object} { success: boolean, message: string }
 */
function startExportForVerification(month) {
  var targetMonth = month || getCurrentMonth();

  var corrections = getStoredCorrections_();
  if (!corrections || corrections.length === 0) {
    return { success: false, message: 'エラー: 修正データが見つかりません。差分確認を再実行してください。' };
  }

  var url = API_BASE_URL + "/api/export";
  var payload = { "month": targetMonth, "async": true };
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

    if (statusCode === 409) {
      return { success: false, message: '別のタスクが実行中です。完了後に再試行してください。' };
    }
    if (statusCode !== 200 || !result.success) {
      return { success: false, message: 'CSV出力開始エラー: ' + (result.error || result.message || '不明なエラー') };
    }

    return { success: true, message: 'CSV出力を開始しました' };
  } catch (e) {
    return { success: false, message: 'サーバー接続エラー: ' + e.message };
  }
}

/**
 * 適用後検証: CSV出力結果を1回ポーリング（即座に返る）
 * @returns {Object} { status: "running"|"completed"|"error", result: {...} }
 */
function pollExportResult() {
  var pollUrl = API_BASE_URL + "/api/export/result";
  var resp = UrlFetchApp.fetch(pollUrl, { "method": "get", "muteHttpExceptions": true });
  var text = resp.getContentText();
  var code = resp.getResponseCode();
  try {
    return JSON.parse(text);
  } catch (e) {
    return { status: 'error', error: 'サーバー応答エラー (HTTP ' + code + '): JSONではないレスポンスを受信しました' };
  }
}

/**
 * 適用後検証: CSV出力完了後の検証処理（即座に返る）
 * @param {string} exportResultJson - CSV出力結果のJSON文字列
 * @param {string} month - 対象月（YYYY-MM形式）
 * @returns {Object} { success: boolean, message: string }
 */
function finalizePostApplyVerification(exportResultJson, month) {
  var targetMonth = month || getCurrentMonth();
  var exportResult = JSON.parse(exportResultJson);

  var csvContent = exportResult.csv_content;
  if (!csvContent) {
    return { success: false, message: 'CSV再出力エラー: csv_contentが空です' };
  }

  var corrections = getStoredCorrections_();
  if (!corrections || corrections.length === 0) {
    return { success: false, message: 'エラー: 修正データが見つかりません。' };
  }

  try {
    var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
    var monthStr = targetMonth.replace("-", "");
    var fileName = "kaipoke_current_" + monthStr + "_post_apply.csv";
    var existingFiles = folder.getFilesByName(fileName);
    while (existingFiles.hasNext()) {
      existingFiles.next().setTrashed(true);
    }
    var bom = "\uFEFF";
    var blob = Utilities.newBlob(bom + csvContent, "text/csv", fileName);
    folder.createFile(blob);
  } catch (e) {
    console.error('[postApplyVerify] Drive save error:', e);
  }

  var applyResult = getStoredApplyResult_();
  var verifyResults = verifyApplyResult(corrections, csvContent, applyResult);

  try {
    writeVerificationResultToSheet(verifyResults);
  } catch (e) {
    console.error('[postApplyVerify] writeVerificationResultToSheet error:', e);
  }

  var okCount = 0, failCount = 0, skipCount = 0;
  for (var i = 0; i < verifyResults.length; i++) {
    var v = verifyResults[i].verification;
    if (v === 'OK') okCount++;
    else if (v === 'FAIL') failCount++;
    else skipCount++;
  }

  var msg = '【適用後検証完了】\n\n' +
            '合計: ' + verifyResults.length + '件\n' +
            'OK: ' + okCount + '件\n' +
            'FAIL: ' + failCount + '件\n' +
            'スキップ: ' + skipCount + '件';

  if (failCount > 0) {
    msg += '\n\n--- 不一致 ---';
    for (var j = 0; j < verifyResults.length; j++) {
      if (verifyResults[j].verification === 'FAIL') {
        var c = verifyResults[j].correction;
        msg += '\n  ' + (c.user_name || '') + ' ' + (c.date_to || c.date_from || '') + '日 ' + (c.action || '') + ' [' + (verifyResults[j].reason || '') + ']';
      }
    }
    msg += '\n\n詳細は「検証結果」シートを確認してください。';
  }

  return {
    success: true,
    message: msg,
    verifyResults: { total: verifyResults.length, ok: okCount, fail: failCount, skipped: skipCount }
  };
}

/**
 * [後方互換] 適用後検証メインフロー（ブロッキング版）
 * 新しいコードでは startExportForVerification → pollExportResult → finalizePostApplyVerification を使用
 * @deprecated クライアントサイドポーリング版を使用してください
 */
function runPostApplyVerification(month) {
  var targetMonth = month || getCurrentMonth();

  // 1) 保存済みの修正データを取得
  var corrections = getStoredCorrections_();
  if (!corrections || corrections.length === 0) {
    return { success: false, message: 'エラー: 修正データが見つかりません。' };
  }

  // 2) /api/export を呼び出して適用後CSVを取得
  console.log('[postApplyVerify] Exporting post-apply CSV for month=' + targetMonth);
  var url = API_BASE_URL + "/api/export";
  var payload = { "month": targetMonth };
  var options = {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify(payload),
    "muteHttpExceptions": true
  };

  var csvContent;
  try {
    var response = UrlFetchApp.fetch(url, options);
    var statusCode = response.getResponseCode();
    var result = JSON.parse(response.getContentText());

    if (statusCode !== 200 || !result.success) {
      return { success: false, message: 'CSV再出力エラー: ' + (result.error || result.message || '不明なエラー') };
    }
    csvContent = result.result.csv_content;
    if (!csvContent) {
      return { success: false, message: 'CSV再出力エラー: csv_contentが空です' };
    }
  } catch (e) {
    return { success: false, message: 'サーバー接続エラー: ' + e.message };
  }

  // 3) Google Driveに保存
  try {
    var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
    var monthStr = targetMonth.replace("-", "");
    var fileName = "kaipoke_current_" + monthStr + "_post_apply.csv";

    var existingFiles = folder.getFilesByName(fileName);
    while (existingFiles.hasNext()) {
      existingFiles.next().setTrashed(true);
    }
    var bom = "\uFEFF";
    var blob = Utilities.newBlob(bom + csvContent, "text/csv", fileName);
    folder.createFile(blob);
    console.log('[postApplyVerify] Saved post-apply CSV: ' + fileName);
  } catch (e) {
    console.error('[postApplyVerify] Drive save error:', e);
  }

  // 4) 適用結果を取得（_apply_result_cacheから）
  var applyResult = getStoredApplyResult_();

  // 5) 照合
  var verifyResults = verifyApplyResult(corrections, csvContent, applyResult);

  // 6) 検証結果シートに書き込み
  try {
    writeVerificationResultToSheet(verifyResults);
  } catch (e) {
    console.error('[postApplyVerify] writeVerificationResultToSheet error:', e);
  }

  // 7) サマリーメッセージ作成
  var okCount = 0, failCount = 0, skipCount = 0;
  for (var i = 0; i < verifyResults.length; i++) {
    var v = verifyResults[i].verification;
    if (v === 'OK') okCount++;
    else if (v === 'FAIL') failCount++;
    else skipCount++;
  }

  var msg = '【適用後検証完了】\n\n' +
            '合計: ' + verifyResults.length + '件\n' +
            'OK: ' + okCount + '件\n' +
            'FAIL: ' + failCount + '件\n' +
            'スキップ: ' + skipCount + '件';

  if (failCount > 0) {
    msg += '\n\n--- 不一致 ---';
    for (var j = 0; j < verifyResults.length; j++) {
      if (verifyResults[j].verification === 'FAIL') {
        var c = verifyResults[j].correction;
        msg += '\n  ' + (c.user_name || '') + ' ' + (c.date_to || c.date_from || '') + '日 ' + (c.action || '') + ' [' + (verifyResults[j].reason || '') + ']';
      }
    }
    msg += '\n\n詳細は「検証結果」シートを確認してください。';
  }

  return {
    success: true,
    message: msg,
    verifyResults: { total: verifyResults.length, ok: okCount, fail: failCount, skipped: skipCount }
  };
}

/**
 * 修正データ配列と適用後CSVを照合
 * @param {Array} corrections - 修正データ配列
 * @param {string} postApplyCsvContent - 適用後CSVテキスト
 * @param {Object} applyResult - 適用結果（details含む）
 * @returns {Array} 検証結果配列
 */
function verifyApplyResult(corrections, postApplyCsvContent, applyResult) {
  var csvRows = Utilities.parseCsv(postApplyCsvContent);
  // ヘッダー行を除外してデータ行のみ
  var postData = [];
  for (var i = 1; i < csvRows.length; i++) {
    if (csvRows[i].length >= 16) {
      postData.push(csvRows[i]);
    }
  }

  // applyResult.details をマップ化（user+date+action → status）
  var detailMap = {};
  var details = (applyResult && applyResult.details) ? applyResult.details : [];
  for (var d = 0; d < details.length; d++) {
    var det = details[d];
    var key = (det.user || det.staff || '') + '|' + (det.date || '') + '|' + (det.action || '');
    detailMap[key] = det.status || '';
  }

  var results = [];
  for (var c = 0; c < corrections.length; c++) {
    var corr = corrections[c];
    var action = corr.action || '';

    // event_add はCSV照合不可 → 適用ステータスを信頼
    if (action === 'event_add') {
      var evKey = (corr.staff_name || corr.user_name || '') + '|' + (corr.date_to || '') + '|event_add';
      var evStatus = detailMap[evKey] || 'unknown';
      results.push({
        correction: corr,
        verification: (evStatus === 'success') ? 'OK' : 'skipped',
        reason: 'イベント: 適用ステータス=' + evStatus
      });
      continue;
    }

    // 適用時に失敗/スキップだったものは検証もスキップ
    var corrKey = (corr.user_name || '') + '|' + (corr.date_to || corr.date_from || '') + '|' + action;
    var applyStatus = detailMap[corrKey] || '';
    if (applyStatus === 'failed' || applyStatus === 'error' || applyStatus === 'skipped') {
      results.push({
        correction: corr,
        verification: 'skipped',
        reason: '適用時ステータス: ' + applyStatus
      });
      continue;
    }

    // CSV照合
    var vResult = verifySingleCorrection(corr, postData);
    results.push(vResult);
  }

  return results;
}

/**
 * 1件の修正がCSVに反映されているか検証
 * CSVカラム: 職員名１(0), 職種１(1), 職員名２(2), 職種２(3), 同行２(4),
 *            職員名３(5), 職種３(6), 同行３(7), 事業所名(8),
 *            日付(9), 曜日(10), 利用者(11), 業務種別(12), サービス内容(13),
 *            開始時間(14), 終了時間(15), 提供時間（分）(16), 備考(17)
 * @param {Object} correction - 修正データ1件
 * @param {Array} postData - CSVデータ行配列（ヘッダー除外済み）
 * @returns {Object} { correction, verification, reason }
 */
function verifySingleCorrection(correction, postData) {
  var action = correction.action || '';
  var userName = (correction.user_name || '').trim();
  var dateTo = String(correction.date_to || '').trim();
  var dateFrom = String(correction.date_from || '').trim();
  var startTo = (correction.start_time_to || '').trim();
  var endTo = (correction.end_time_to || '').trim();
  var startFrom = (correction.start_time_from || '').trim();
  var staff1To = (correction.staff1_to || '').trim();
  var staff1From = (correction.staff1_from || '').trim();

  // user_name が「なし」または空欄の場合 → イベント/個別業務でCSV照合不可
  if ((action === 'add' || action === 'edit') && (userName === 'なし' || userName === '')) {
    return {
      correction: correction,
      verification: 'FAIL',
      reason: '利用者名「' + (userName || '空欄') + '」はカイポケ上に存在しないユーザーです。' +
              'イベント・個別業務は利用者なしの登録のため、CSV利用者欄での照合ができません。',
      failCategory: 'user_not_found'
    };
  }

  if (action === 'delete') {
    // 削除対象がCSVに存在しないことを確認
    var found = false;
    for (var i = 0; i < postData.length; i++) {
      var row = postData[i];
      var csvUser = (row[11] || '').trim();
      var csvDate = (row[9] || '').trim();
      var csvStart = (row[14] || '').trim();
      if (csvUser === userName && csvDate === dateFrom && csvStart === startFrom) {
        found = true;
        break;
      }
    }
    if (!found) {
      return {
        correction: correction,
        verification: 'OK',
        reason: '削除確認OK'
      };
    } else {
      return {
        correction: correction,
        verification: 'FAIL',
        reason: '削除対象がまだCSVに存在しています。' + userName + 'の' + dateFrom + '日 ' + startFrom + ' のエントリが残っています。',
        failCategory: 'entry_mismatch'
      };
    }
  }

  if (action === 'add' || action === 'edit') {
    var matched = false;
    for (var j = 0; j < postData.length; j++) {
      var row2 = postData[j];
      var csvUser2 = (row2[11] || '').trim();
      var csvDate2 = (row2[9] || '').trim();
      var csvStart2 = (row2[14] || '').trim();
      var csvEnd2 = (row2[15] || '').trim();
      if (csvUser2 === userName && csvDate2 === dateTo && csvStart2 === startTo && csvEnd2 === endTo) {
        matched = true;
        break;
      }
    }
    if (matched) {
      return {
        correction: correction,
        verification: 'OK',
        reason: (action === 'add' ? '追加確認OK' : '編集確認OK')
      };
    }

    // 未割当の検出
    if (staff1To === '未割当') {
      return {
        correction: correction,
        verification: 'FAIL',
        reason: '【未割当】最適化の結果、' + userName + 'さんの' + dateTo + '日 ' + startTo + '〜' + endTo +
                ' に割当可能なスタッフが存在しませんでした。' +
                '職員欄は変更前の「' + (staff1From || '不明') + '」のままです。',
        failCategory: 'unassigned_staff'
      };
    }

    // 同日・同ユーザーで別時間のエントリがあるか検索
    var actualEntries = [];
    for (var a = 0; a < postData.length; a++) {
      var rowA = postData[a];
      if ((rowA[11] || '').trim() === userName && (rowA[9] || '').trim() === dateTo) {
        actualEntries.push((rowA[14] || '').trim() + '-' + (rowA[15] || '').trim() +
                           '（' + (rowA[0] || '').trim() + '）');
      }
    }
    var actualInfo = actualEntries.length > 0
      ? ' 同日のCSV実績: ' + actualEntries.join(', ')
      : ' 同日のCSVにこの利用者のエントリはありません。';

    return {
      correction: correction,
      verification: 'FAIL',
      reason: (action === 'add' ? '追加' : '編集後') + 'エントリがCSVに未反映。' +
              '期待: ' + dateTo + '日 ' + startTo + '-' + endTo + '。' + actualInfo,
      failCategory: 'entry_mismatch'
    };
  }

  if (action === 'date_change') {
    var dcStartFrom = startFrom || startTo;
    var dcStartTo = startTo;
    var oldExists = false;
    var newExists = false;
    for (var k = 0; k < postData.length; k++) {
      var row3 = postData[k];
      var csvUser3 = (row3[11] || '').trim();
      var csvDate3 = (row3[9] || '').trim();
      var csvStart3 = (row3[14] || '').trim();
      var csvEnd3 = (row3[15] || '').trim();
      if (csvUser3 === userName) {
        if (csvDate3 === dateFrom && csvStart3 === dcStartFrom) oldExists = true;
        if (csvDate3 === dateTo && csvStart3 === dcStartTo && csvEnd3 === endTo) newExists = true;
      }
    }
    if (newExists && !oldExists) {
      return {
        correction: correction,
        verification: 'OK',
        reason: '日付変更確認OK: 旧日付(' + dateFrom + '日)から消え、新日付(' + dateTo + '日)に存在'
      };
    } else if (newExists && oldExists) {
      return {
        correction: correction,
        verification: 'FAIL',
        reason: '日付移動不完全: 新日付に存在するが旧日付(' + dateFrom + '日)のエントリが残っている',
        failCategory: 'entry_mismatch'
      };
    } else {
      // 新日付のCSVエントリを検索して表示
      var dcActualEntries = [];
      for (var m = 0; m < postData.length; m++) {
        var rowM = postData[m];
        if ((rowM[11] || '').trim() === userName && (rowM[9] || '').trim() === dateTo) {
          dcActualEntries.push((rowM[14] || '').trim() + '-' + (rowM[15] || '').trim() +
                               '（' + (rowM[0] || '').trim() + '）');
        }
      }
      var dcActualInfo = dcActualEntries.length > 0
        ? ' 同日のCSV実績: ' + dcActualEntries.join(', ')
        : ' 同日のCSVにこの利用者のエントリはありません。';

      return {
        correction: correction,
        verification: 'FAIL',
        reason: '日付移動失敗: 新日付(' + dateTo + '日)にエントリが見つからない。' +
                '期待: ' + dateTo + '日 ' + startTo + '-' + endTo + '。' + dcActualInfo,
        failCategory: 'entry_mismatch'
      };
    }
  }

  // 未知のアクション
  return {
    correction: correction,
    verification: 'skipped',
    reason: '未対応アクション: ' + action
  };
}

/**
 * 検証結果を「検証結果」シートに書き込み
 * @param {Array} verifyResults - 検証結果配列
 */
function writeVerificationResultToSheet(verifyResults) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName('検証結果');
  if (!sheet) {
    sheet = ss.insertSheet('検証結果');
  }
  sheet.clear();

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
  sheet.getRange(1, 1, 1, headers.length).setFontWeight('bold').setBackground('#4a86c8').setFontColor('#ffffff');

  // サマリー行（2行目）
  var okCount = 0, failCount = 0, skipCount = 0;
  for (var s = 0; s < verifyResults.length; s++) {
    var v = verifyResults[s].verification;
    if (v === 'OK') okCount++;
    else if (v === 'FAIL') failCount++;
    else skipCount++;
  }
  var timestamp = Utilities.formatDate(new Date(), 'JST', 'yyyy-MM-dd HH:mm:ss');
  var summaryRow = [
    '検証合計: ' + verifyResults.length + '件', '',
    'OK: ' + okCount, '', 'FAIL: ' + failCount,
    '', '', 'スキップ: ' + skipCount, '', '', '', timestamp
  ];
  sheet.getRange(2, 1, 1, headers.length).setValues([summaryRow]);
  sheet.getRange(2, 1, 1, headers.length).setFontWeight('bold').setBackground('#e2e3e5');

  // 詳細データ（3行目～）
  if (verifyResults.length > 0) {
    var rows = [];
    for (var i = 0; i < verifyResults.length; i++) {
      var vr = verifyResults[i];
      var c = vr.correction || {};
      var expectedTime = '';
      if (c.start_time_to || c.end_time_to) {
        expectedTime = (c.start_time_to || '') + ' - ' + (c.end_time_to || '');
      }
      var expectedStaff = c.staff1_to || '';
      var failCategoryLabel = vr.failCategory ? (categoryLabels[vr.failCategory] || vr.failCategory) : '';

      rows.push([
        c.user_name || '',
        c.date_from || '',
        c.date_to || '',
        c.action || '',
        c.business_type || '',
        expectedTime,
        expectedStaff,
        c.service_content || '',
        vr.verification || '',
        failCategoryLabel,
        vr.reason || '',
        timestamp
      ]);
    }
    sheet.getRange(3, 1, rows.length, headers.length).setValues(rows);

    // 色分け + 未割当スタッフ強調
    var colorMap = { 'OK': '#d4edda', 'FAIL': '#f8d7da', 'skipped': '#fff3cd' };
    for (var j = 0; j < rows.length; j++) {
      var verResult = rows[j][8];  // 検証結果は9列目（インデックス8）
      var color = colorMap[verResult] || '#ffffff';
      sheet.getRange(3 + j, 1, 1, headers.length).setBackground(color);

      // 未割当スタッフは赤色太字で強調
      if (rows[j][6] === '未割当') {
        sheet.getRange(3 + j, 7).setFontColor('#cc0000').setFontWeight('bold');
      }
    }
  }

  // 列幅自動調整
  for (var col = 1; col <= headers.length; col++) {
    sheet.autoResizeColumn(col);
  }
}

/**
 * 適用結果を隠しシートに保存（検証時に参照するため）
 * @param {Object} applyResult - 適用結果オブジェクト
 */
function storeApplyResult_(applyResult) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName('_apply_result_cache');
  if (!sheet) {
    sheet = ss.insertSheet('_apply_result_cache');
    sheet.hideSheet();
  }
  sheet.clear();
  sheet.getRange(1, 1).setValue(JSON.stringify(applyResult));
}

/**
 * 保存済みの適用結果を取得
 * @returns {Object} 適用結果オブジェクト
 */
function getStoredApplyResult_() {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName('_apply_result_cache');
  if (!sheet) return {};
  var json = sheet.getRange(1, 1).getValue();
  return json ? JSON.parse(json) : {};
}

/**
 * 検証結果をHTMLレポートとしてGoogle Driveに出力する。
 * - サマリー
 * - FAIL原因分析セクション（カテゴリ別集計）
 * - FAIL一覧テーブル（期待内容・FAIL原因・詳細列付き）
 * - 全件一覧
 *
 * @param {Array} verifyResults - verifyApplyResult() の結果
 * @param {string} month - 対象月 "YYYY-MM"
 * @returns {Object} { success: boolean, fileUrl: string, message: string }
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
    if (r.verification === 'OK') {
      okCount++;
    } else if (r.verification === 'FAIL') {
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
  html += '<p>生成日時: ' + Utilities.formatDate(new Date(), 'JST', 'yyyy-MM-dd HH:mm:ss') + '</p>';

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
      var fc = fi.correction || {};
      var expectedContent = '';
      if (fc.start_time_to || fc.end_time_to) {
        expectedContent = (fc.start_time_to || '') + ' - ' + (fc.end_time_to || '');
      }
      if (fc.staff1_to) {
        expectedContent += (expectedContent ? ' ' : '') + '(' + fc.staff1_to + ')';
      }
      var fCatLabel = fi.failCategory ? (categoryLabels[fi.failCategory] || fi.failCategory) : '';
      var fCssClass = '';
      if (fi.failCategory === 'unassigned_staff') fCssClass = 'cat-unassigned';
      else if (fi.failCategory === 'user_not_found') fCssClass = 'cat-user-not-found';
      else if (fi.failCategory === 'entry_mismatch') fCssClass = 'cat-entry-mismatch';

      html += '<tr class="fail">';
      html += '<td>' + (f + 1) + '</td>';
      html += '<td>' + (fc.user_name || '') + '</td>';
      html += '<td>' + (fc.date_to || fc.date_from || '') + '日</td>';
      html += '<td>' + (fc.action || '') + '</td>';
      html += '<td>' + expectedContent + '</td>';
      html += '<td class="' + fCssClass + '">' + fCatLabel + '</td>';
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
    var vc = vr.correction || {};
    var rowClass = '';
    if (vr.verification === 'OK') rowClass = 'ok';
    else if (vr.verification === 'FAIL') rowClass = 'fail';
    else rowClass = 'skip';

    html += '<tr class="' + rowClass + '">';
    html += '<td>' + (j + 1) + '</td>';
    html += '<td>' + (vc.user_name || '') + '</td>';
    html += '<td>' + (vc.date_to || vc.date_from || '') + '日</td>';
    html += '<td>' + (vc.action || '') + '</td>';
    html += '<td>' + (vc.business_type || '') + '</td>';
    html += '<td>' + (vr.verification || '') + '</td>';
    html += '<td>' + (vr.reason || '') + '</td>';
    html += '</tr>';
  }
  html += '</table>';
  html += '</body></html>';

  // Google Driveに保存
  try {
    var htmlFilename = '検証レポート_' + targetMonth + '.html';
    var blob = Utilities.newBlob(html, 'text/html', htmlFilename);
    var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);

    // 既存ファイルがあれば削除して再作成
    var existing = folder.getFilesByName(htmlFilename);
    while (existing.hasNext()) {
      existing.next().setTrashed(true);
    }
    var file = folder.createFile(blob);
    var fileUrl = file.getUrl();

    console.log('[exportVerificationToDoc] HTMLレポート保存完了: ' + htmlFilename);

    return {
      success: true,
      fileUrl: fileUrl,
      message: '検証レポートを生成しました: ' + htmlFilename
    };
  } catch (e) {
    console.error('[exportVerificationToDoc] エラー:', e);
    return {
      success: false,
      fileUrl: '',
      message: 'レポート生成エラー: ' + e.message
    };
  }
}
