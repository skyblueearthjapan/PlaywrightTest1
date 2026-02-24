/**
 * 監査（合否判定）ビュー - 定数・ユーティリティ
 *
 * 既存コードを変更せず、監査機能を追加するための基盤モジュール
 */

// ============================================================
// 監査設定（定数）
// ============================================================

const AUDIT_CONFIG = {
  TIME_BUFFER_MIN: 15,           // バッファ（分）：期待範囲外でもこの範囲内ならWARN
  CACHE_TTL_SEC: 900,            // キャッシュTTL（秒）= 15分
  CACHE_KEY_PREFIX: 'auditDataset|'
};

// 判定ステータス
const AUDIT_STATUS = {
  OK: 'OK',
  WARN: 'WARN',
  NG: 'NG'
};

// 期待ソース種別
const AUDIT_SOURCE = {
  MASTER: 'MASTER',
  CHANGE: 'CHANGE',
  SPECIAL_ADD: 'SPECIAL_ADD',
  SPECIAL_REPLACE: 'SPECIAL_REPLACE'
};

// 操作種別（個別変更）
const AUDIT_OP = {
  CANCEL: 'キャンセル',
  TIME_CHANGE: '時間変更',
  ADD: '追加'
};

// 時間タイプ
const AUDIT_TIME_TYPE = {
  FIXED: '固定',
  RANGE: '時間帯',
  MORNING: '午前',
  AFTERNOON: '午後',
  ALL_DAY: '終日'
};

// 時間タイプ別デフォルト範囲（分）
const AUDIT_TIME_DEFAULTS = {
  '午前': { earliestMin: 540, latestMin: 720 },    // 9:00-12:00
  '午後': { earliestMin: 780, latestMin: 1020 },   // 13:00-17:00
  '終日': { earliestMin: 540, latestMin: 1080 }    // 9:00-18:00
};

// 監査タグ（判定理由ラベル）
const AUDIT_TAGS = {
  // OK系
  MASTER: 'MASTER',
  CHANGE_TIME: 'CHANGE(時間変更)',
  CHANGE_ADD: 'CHANGE(追加)',
  SPECIAL_ADD: 'SPECIAL_ADD',
  SPECIAL_REPLACE: 'SPECIAL_REPLACE',
  // WARN系
  TIME_DIFF: 'TIME_DIFF',           // TIME_DIFF(+10m) のように使用
  MISSING_ACTUAL: 'MISSING_ACTUAL',
  EXTRA_ACTUAL: 'EXTRA_ACTUAL',
  // NG系
  UNASSIGNED: 'UNASSIGNED',
  OUT_OF_WINDOW: 'OUT_OF_WINDOW',
  EVENT_CONFLICT: 'EVENT_CONFLICT',
  CANCELLED_BUT_VISITED: 'CANCELLED_BUT_VISITED',
  TIME_PARSE_ERROR: 'TIME_PARSE_ERROR'
};

// ============================================================
// 日付ユーティリティ
// ============================================================

/**
 * 日付を yyyy/MM/dd 形式の文字列に変換
 * @param {Date|string|number} d - 日付
 * @param {string} tz - タイムゾーン
 * @return {string|null}
 */
function audit_formatDateStr_(d, tz) {
  if (!d) return null;
  var dateObj = audit_parseDate_(d);
  if (!dateObj) return null;
  return Utilities.formatDate(dateObj, tz, 'yyyy/MM/dd');
}

/**
 * 日付をパース（表記揺れ対応）
 * @param {Date|string|number} val - 日付値
 * @return {Date|null}
 */
function audit_parseDate_(val) {
  if (!val) return null;
  if (val instanceof Date) return val;

  // 数値（シリアル値）の場合
  if (typeof val === 'number') {
    // Excelシリアル値（1900年1月1日基準）
    if (val > 40000 && val < 60000) {
      var d = new Date((val - 25569) * 86400 * 1000);
      return d;
    }
    return null;
  }

  // 文字列の場合
  var s = String(val).trim();
  if (!s) return null;

  // yyyy/MM/dd or yyyy-MM-dd
  var match = s.match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})/);
  if (match) {
    return new Date(parseInt(match[1]), parseInt(match[2]) - 1, parseInt(match[3]));
  }

  // Date.parse を試す
  var parsed = Date.parse(s);
  if (!isNaN(parsed)) {
    return new Date(parsed);
  }

  return null;
}

/**
 * 週開始日（月曜）を取得
 * @param {Date} d - 基準日
 * @return {Date}
 */
function audit_getWeekStart_(d) {
  var date = new Date(d);
  var day = date.getDay();
  var diff = (day === 0) ? -6 : 1 - day;
  date.setDate(date.getDate() + diff);
  date.setHours(0, 0, 0, 0);
  return date;
}

/**
 * 週終了日（日曜）を取得
 * @param {Date} weekStart - 週開始日
 * @return {Date}
 */
function audit_getWeekEnd_(weekStart) {
  var d = new Date(weekStart);
  d.setDate(d.getDate() + 6);
  return d;
}

/**
 * 日付から曜日文字列を取得
 * @param {Date} d - 日付
 * @return {string} Mon〜Sun
 */
function audit_getYoubiEn_(d) {
  var days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  return days[d.getDay()];
}

/**
 * 曜日文字列を正規化（日本語→英語）
 * @param {string} youbi - 曜日文字列
 * @return {string} Mon〜Sun
 */
function audit_normalizeYoubi_(youbi) {
  var s = String(youbi || '').trim();
  var map = {
    '日': 'Sun', '日曜': 'Sun', '日曜日': 'Sun', 'Sunday': 'Sun',
    '月': 'Mon', '月曜': 'Mon', '月曜日': 'Mon', 'Monday': 'Mon',
    '火': 'Tue', '火曜': 'Tue', '火曜日': 'Tue', 'Tuesday': 'Tue',
    '水': 'Wed', '水曜': 'Wed', '水曜日': 'Wed', 'Wednesday': 'Wed',
    '木': 'Thu', '木曜': 'Thu', '木曜日': 'Thu', 'Thursday': 'Thu',
    '金': 'Fri', '金曜': 'Fri', '金曜日': 'Fri', 'Friday': 'Fri',
    '土': 'Sat', '土曜': 'Sat', '土曜日': 'Sat', 'Saturday': 'Sat'
  };
  return map[s] || s;
}

// ============================================================
// 時間ユーティリティ
// ============================================================

/**
 * 時刻を分（0〜1440）に変換
 * @param {Date|number|string} val - 時刻値
 * @return {number|null}
 */
function audit_parseTimeToMin_(val) {
  if (val === null || val === undefined || val === '') return null;

  // Date型の場合
  if (val instanceof Date) {
    return val.getHours() * 60 + val.getMinutes();
  }

  // 数値（シリアル値 0〜1）の場合
  if (typeof val === 'number') {
    if (val >= 0 && val < 1) {
      return Math.round(val * 1440);
    }
    // 既に分の場合（60〜1440程度）
    if (val >= 0 && val <= 1440) {
      return Math.round(val);
    }
    return null;
  }

  // 文字列の場合
  var s = String(val).trim();
  if (!s) return null;

  // HH:mm or H:mm
  var match = s.match(/^(\d{1,2}):(\d{2})$/);
  if (match) {
    return parseInt(match[1]) * 60 + parseInt(match[2]);
  }

  // HH時mm分
  match = s.match(/^(\d{1,2})時(\d{1,2})分?$/);
  if (match) {
    return parseInt(match[1]) * 60 + parseInt(match[2]);
  }

  // HH時
  match = s.match(/^(\d{1,2})時$/);
  if (match) {
    return parseInt(match[1]) * 60;
  }

  return null;
}

/**
 * 分を HH:mm 形式に変換
 * @param {number} min - 分（0〜1440）
 * @return {string}
 */
function audit_minToTimeStr_(min) {
  if (min === null || min === undefined) return '';
  var h = Math.floor(min / 60);
  var m = min % 60;
  return ('0' + h).slice(-2) + ':' + ('0' + m).slice(-2);
}

/**
 * 分をシリアル値に変換
 * @param {number} min - 分（0〜1440）
 * @return {number}
 */
function audit_minToSerial_(min) {
  if (min === null || min === undefined) return null;
  return min / 1440;
}

// ============================================================
// キー生成ユーティリティ
// ============================================================

/**
 * Patient-day キーを生成
 * @param {string} pid - 患者ID
 * @param {string} dateStr - 日付文字列 (yyyy/MM/dd)
 * @return {string}
 */
function audit_makePdKey_(pid, dateStr) {
  return String(pid || '').trim() + '|' + String(dateStr || '').trim();
}

/**
 * Staff-day キーを生成
 * @param {string} staffId - スタッフID
 * @param {string} dateStr - 日付文字列 (yyyy/MM/dd)
 * @return {string}
 */
function audit_makeSdKey_(staffId, dateStr) {
  return String(staffId || '').trim() + '|' + String(dateStr || '').trim();
}

/**
 * Patient-week キーを生成
 * @param {string} pid - 患者ID
 * @param {string} weekStartStr - 週開始日文字列 (yyyy/MM/dd)
 * @return {string}
 */
function audit_makePwKey_(pid, weekStartStr) {
  return String(pid || '').trim() + '|' + String(weekStartStr || '').trim();
}

// ============================================================
// ヘッダー検索ユーティリティ（表記揺れ対応）
// ============================================================

/**
 * ヘッダー配列から列インデックスを検索（部分一致対応）
 * @param {string[]} headers - ヘッダー配列
 * @param {string} target - 検索対象
 * @return {number} インデックス（見つからない場合は -1）
 */
function audit_findHeaderIdx_(headers, target) {
  // 完全一致
  var idx = headers.indexOf(target);
  if (idx >= 0) return idx;

  // 空白除去して完全一致
  for (var i = 0; i < headers.length; i++) {
    if (String(headers[i]).trim() === target) return i;
  }

  // 部分一致（targetを含む or targetが含む）
  for (var j = 0; j < headers.length; j++) {
    var h = String(headers[j]).trim();
    if (h.indexOf(target) >= 0 || target.indexOf(h) >= 0) return j;
  }

  return -1;
}

// ============================================================
// 曜日パースユーティリティ
// ============================================================

/**
 * 曜日文字列（カンマ/スラッシュ区切り）をパースして配列化
 * @param {string} val - 曜日文字列（例：「月,水,金」「Mon/Wed/Fri」）
 * @return {string[]} 英語曜日配列（例：['Mon','Wed','Fri']）
 */
function audit_parseDays_(val) {
  if (!val) return [];
  var s = String(val).trim();
  if (!s) return [];

  // カンマ、スラッシュ、スペースで分割
  var parts = s.split(/[,\/\s]+/);
  var result = [];

  for (var i = 0; i < parts.length; i++) {
    var p = parts[i].trim();
    if (p) {
      result.push(audit_normalizeYoubi_(p));
    }
  }

  return result;
}

// ============================================================
// 数値変換ユーティリティ
// ============================================================

/**
 * 全角数字を半角に変換し、数値として返す
 * @param {any} val - 値
 * @param {number} defaultVal - デフォルト値
 * @return {number}
 */
function audit_toNumber_(val, defaultVal) {
  if (val === null || val === undefined || val === '') return defaultVal;

  if (typeof val === 'number') return val;

  var s = String(val)
    .replace(/[０-９]/g, function(c) {
      return String.fromCharCode(c.charCodeAt(0) - 0xFEE0);
    })
    .trim();

  var n = parseFloat(s);
  return isNaN(n) ? defaultVal : n;
}
