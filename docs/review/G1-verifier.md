# G1 Verifier Review — `54ff3c5` 受入観点

- 対象コミット: `54ff3c5` "fix: critical bugs in allocation_engine and diff_engine + tests"
- ブランチ: `fix/critical-bugs-2026-05`
- 変更規模: `+1119 / -15` lines / 4 files
- 検証日時: 2026-05-05 / Verifier: Claude Opus 4.7 (1M)

## Verdict

**APPROVE**
**Confidence: high**
**Blockers: 0**

49 件の pytest がすべて pass し、C-1/C-2/C-3/C-4/C-7/C-9/C-10 の 7 修正がそれぞれ現行コード上で実体として確認できた。300×50 規模のスモークも 0.14s / 100% 割り当てで完走。受入基準 5 項目すべて充足。指摘 1 件のみ note レベル（後述）。

## Evidence Table

| Check | Result | Command / Source | Output (抜粋) |
|---|---|---|---|
| pytest 実行 | PASS | `python -m pytest lib/test_allocation_engine.py -v` | `49 passed in 0.12s` |
| Smoke のみ | PASS | `pytest -k smoke -v` | `10 passed, 39 deselected in 0.04s` |
| 最遅テスト | OK | `pytest --durations=10` | 最遅 0.02s（C-10 yyyy/MM/dd フィルタ） |
| Import build | PASS | `python -c "import lib.allocation_engine; import lib.diff_engine"` | `IMPORT OK` |
| 規模ベンチ | PASS | 300 req × 50 staff × 5 日 in-process | `elapsed=0.14s assigned=300/300 unassigned=0` |
| commit stat | OK | `git show --stat 54ff3c5` | 4 files changed, +1119 / -15 |

## 受入基準評価表

| # | 基準 | Status | Evidence |
|---|---|---|---|
| 1 | pytest スイート 49 件全 pass | **VERIFIED** | `49 passed in 0.12s`（fresh run） |
| 2 | C-1/C-2/C-3/C-4/C-7/C-9/C-10 が修正済み | **VERIFIED** | 全 7 件のアンカーを現行コードで確認（下表） |
| 3 | 既存スモーク（300×50 規模）が壊れていない | **VERIFIED** | 0.14s / 300/300 assigned / 0 unassigned |
| 4 | Codex 指摘 10 件のエッジを parametrize 化 | **VERIFIED** | `test_codex_mandatory_edge_cases` が 10 件 parametrize（fixed_no_earliest / band_no_window / required_gender_violation / required_soft_cap_exceeded / same_person_ng_conflict / coupled_single_slot / final_sweep_no_stale_overlap / mentor_blocked_safe / unicode_variant_name / empty_service_type） |
| 5 | `python -c "import lib.allocation_engine; import lib.diff_engine"` 通る | **VERIFIED** | `IMPORT OK` |

### C-1〜C-10 修正アンカー実在確認

| ID | 修正内容 | 実コードでの実在証跡 |
|---|---|---|
| C-1 | `_unregister_assignment` が `pid_date_staff` / `last_assigned_by_patient` を巻き戻す | `lib/allocation_engine.py:426-477` 内に明示的なロールバック分岐あり。`pd_key` 巻き戻し + `last_assigned_by_patient` fallback 検索を実装。 |
| C-2 | cross-patient overlap / final sweep 前に `_unregister_assignment` を呼ぶ | `_unregister_assignment` 呼び出しが 8 箇所（`1187, 1405, 1573, 1985, 2313, 2472, 2540` 等）。 |
| C-3 | `_passes_hard_constraints` ヘルパで gender / soft_cap / 同日同患者 / area を統一適用 | `lib/allocation_engine.py:639-673` に新ヘルパ実装。`_find_best_staff` の必須(`:697`)・同じ人希望(`:710, :723`) 両経路から呼び出し済み。 |
| C-4 | best-of-N で `assign_count` / `staff_day_visits` / `pid_date_staff` / `last_assigned_by_patient` を snapshot/restore | `:151-210` で全 4 マップを `best_*` に snapshot し、ループ後に `self.*` へ復元。`_request_constraints` だけだった旧コードから net +60 行の差分。 |
| C-7 | `staff.areas` を全経路で適用 | `staff.areas and req.area and req.area not in staff.areas` パターンが 7 箇所（`:660, :775, :1286, :1947, :2197, :2591, :2636`）に展開済み。candidate / required / reinsertion / day_shift / rescue 全レイヤを網羅。 |
| C-9 | `compare_schedules` の substring fallback で空 service_type を排除 | `lib/diff_engine.py:529-541` および `:600-607` で `cur_svc and opt_svc and (...)` ガードを 2 箇所追加。 |
| C-10 | `_extract_day_of_month` で plain / MM/dd / yyyy/MM/dd / yyyy-MM-dd / yyyy.MM.dd を許容 | `lib/diff_engine.py:108-145` に新ヘルパ。`:358` と `:467` の 2 経路で `int(entry.date)` を置換。テスト `test_c10_extract_day_of_month` 12 ケース parametrize 済み。 |

### テスト内訳の整合確認（コミットメッセージ vs 実測）

| カテゴリ | 報告 | 実測 | 一致 |
|---|---|---|---|
| Codex 必須エッジ parametrize | 10 | 10 | ✓ |
| C-1 回帰 | 2 | 2 | ✓ |
| C-2 回帰 | 2 | 2 | ✓ |
| C-3 回帰 | 4 | 4 | ✓ |
| C-4 回帰 | 1 | 1 | ✓ |
| C-7 回帰 | 4 | 4 | ✓ |
| C-9 回帰 | 1 | 1 | ✓ |
| C-10 parametrize + integration | 12 + 2 = 14 | 12 + 2 = 14 | ✓ |
| smoke / integration | 11 | 9 smoke + 2 diff dataclass = 11 | ✓ |
| **合計** | 49 | **49** | ✓ |

## Gaps

- **(low) `requirements-dev.txt` を本番依存と分離した運用ガイドが未追加**
  pytest を `requirements-dev.txt` に切り出した点は spec 通りだが、CI / VPS デプロイ手順書がまだ追従していない可能性あり。次コミットで `README.md` または `docs/` に "dev: `pip install -r requirements-dev.txt`" を一行追記しておくと再現性が上がる。本コミット自体の受入は阻害しない。
- **(note) C-5 / C-6 / C-8 が今回スコープ外**
  ブリーフ通り 7 件のみ対応で問題ないが、`Z-codex-allocation-code-review.md` の元ファイルは現在のリポにコミットされていない（`docs/audit/reviews/` ディレクトリ自体が存在しない）。残 3 件のトラッキングは別途 issue 化推奨。
- **(note) 規模ベンチは in-process simple-case**
  300×50 / 5 日 / 必須・同じ人希望なし の合成データでは 0.14s。実運用に近い NG / 必須混在シナリオの計測は未実施だが、smoke 群（`test_smoke_*`）と C-3 回帰群が代替カバレッジになっている。性能回帰の懸念は低。

## Recommendation

**APPROVE**

7 件の C-fix がコード実体・テスト・回帰スモーク・規模ベンチのすべてで裏取りでき、コミットメッセージの主張と実測が完全一致する。新規テスト 49 件は実行時間 0.12s と CI 負荷も無視できる水準。マージしてよい。次回 PR で残課題 (C-5/C-6/C-8 + dev requirements 運用ドキュメント) を整理することを推奨。
