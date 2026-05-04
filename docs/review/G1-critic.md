# レビュー: コミット `54ff3c5` — allocation_engine / diff_engine クリティカルバグ修正

**Reviewer**: oh-my-claudecode:critic (Opus)
**Branch**: `fix/critical-bugs-2026-05`
**Date**: 2026-05-05

## VERDICT: **ACCEPT-WITH-RESERVATIONS**

## 概要

C-1〜C-4, C-7, C-9, C-10 の 7 件はいずれも実装上の根拠が裏付け可能で、49 件のテストはローカルで再実行しても全て pass（0.09s）。狙ったバグ（pid_date_staff 復元漏れ、stale staff_day_visits、必須/同じ人パスの制約欠落、best 試行時の state 復元、areas 無効化、空文字 substring 暴走、yyyy/MM/dd ValueError）は具体的なユニットテストでカバーされている。ただし設計上の重複ロジックと、`_extract_day_of_month` の和暦/日本語フォーマット非対応、`last_assigned_by_patient` フォールバックの「最後」定義の曖昧さなど、後続コミットで処理すべき残課題がある。本コミット単体としてはマージ可能。

該当ブランチを保留にする理由には足りないが、C-5/C-6/C-8 と合わせた次回コミットでの整理を強く推奨する。

---

## Major Findings

### M-1. `_extract_day_of_month` が日本語日付フォーマットを silently 0 にフォールバックする
- 場所: `lib/diff_engine.py:108-145`、呼び出しは `lib/diff_engine.py:354`（`compare_schedules` の `in_range`）と `lib/diff_engine.py:466`（`sorted` の key）
- 証拠: `2026年5月4日` のような和暦/全角混じり文字列は `s.isdigit()` で false、separator ループで `/`,`-`,`.`,` ` のいずれにもマッチせず、最後の `return None` に落ちる。`in_range` は False を返すので「黙って週範囲外として捨てる」結果になり、`compare_schedules` の sorted key は `None` 時 0 に変換され（466 行: `if _extract_day_of_month(x) is not None else 0`）、想定外フォーマットがすべて月初として並ぶ。
- なぜ重要: カイポケ実 CSV はテンプレート設定により「2026/05/04」「2026-05-04」が混在し得るのは想定済みだが、ユーザが「2026年5月4日」「2026/5/4(月)」のような派生表記をコピーペーストする運用ケースは観測されている。テストは ASCII 区切りしか確認していない。
- Confidence: HIGH
- Fix:
  ```python
  # 数字以外を separator として正規化してから抽出
  import re
  digits = re.findall(r'\d+', s)
  if not digits:
      return None
  d = int(digits[-1])
  return d if 1 <= d <= 31 else None
  ```
  既存 `isdigit` 早期 return は維持しつつ、separator ループを `re.findall(r'\d+', s)` に置換すれば `2026年5月4日`, `2026/5/4(月)`, `5月4日` も網羅できる。
- Realist Check: 内部運用で発生していないため CRITICAL ではなく MAJOR。downgrade 理由: カイポケ標準出力は ASCII 区切り固定で、和暦文字列が混入するのはユーザーペースト経路のみ（< 5% のオペレーション）。

### M-2. `last_assigned_by_patient` フォールバックの「直前」定義が `self.results` のインデックス順に依存
- 場所: `lib/allocation_engine.py:466-477`
- 証拠: コメントは `# Find the most recent remaining assigned staff for the pid` だが、`for other_r in self.results:` で順次 `fallback = other_r.staff_id` を上書きしているだけで、確定するのは「`self.results` の最後に出現した同 pid の staff_id」。`self.results` への append 順は時系列ではなく Level0 → GapPack → Level1 等パイプライン順なので、「直前担当」の意味論を保証できない。
- なぜ重要: `last_assigned_by_patient` は次回トライアルおよび `_find_best_staff` の「同じ人希望」フォールバック (line 686 `dynamic_prev = self.last_assigned_by_patient.get(req.pid)`) で使われ、ローテーション優先のスコアにも影響する (`is_dynamic_prev` 列, `_candidate_sort_key`)。間違ったフォールバックが入ると次回の同じ患者の希望ロジックが破綻する。
- Confidence: HIGH
- Fix: 「最も新しい assignment」を担保したいなら `self.results` を逆順で走査して最初に見つかったものを採用、または `_register_assignment` 時に `(timestamp, staff_id)` を保持する補助マップを設ける。最低限、ループを `for other_r in reversed(self.results):` にして break すれば「最後に append された staff」を取れる（pipeline は再アサインで上書きするので結果として希望に近い）。
- Realist Check: テスト（test_c1_unregister_keeps_pid_when_other_visit_remains 含む）は単一ペアしか並べていないため、このバグは現行テストでは露呈しない。

### M-3. `_passes_hard_constraints` と `_build_candidate_list` で重複した制約チェック
- 場所: `lib/allocation_engine.py:639-673`（ヘルパ）と `lib/allocation_engine.py:751-781`（候補ビルド）
- 証拠: gender (`if req.sex_limit == "女性のみ" ...`)、areas (`if staff.areas and req.area and ...`)、soft_cap (`self.assign_count.get(...) >= staff.soft_cap()`)、same-patient-same-day (`already_today = self.pid_date_staff.get(...)`) が両者で平行実装されている。さらに `_relaxed_reinsertion` Level1/2、`_day_shift_strategy`、`_rescue_partial_coupled` でも個別にインライン展開されている (line 1942-1958, 2192-2210, 2586-2599, 2632-2645)。
- なぜ重要: C-7 のような追加フィルタを次回入れる際、6 箇所の修正漏れリスクが再発する（実際にこのコミットで該当の 6 箇所すべてに areas を追加することになった）。テストはあるが、forgetting-to-update-one-path は静的検査では見えない。
- Confidence: HIGH
- Fix: `_build_candidate_list` と関連パスを `_passes_hard_constraints(staff, req, allow_max_per_day=False)` 系の単一関数に集約。relax 時は `allow_max_per_day=True` を渡して soft_cap を緩める。コミット範囲を超えるので別 PR が妥当だが、本コミットの README/コメントで「次回ヘルパ統合」を明示することを推奨。
- Realist Check: 直接的な動作バグではないので MAJOR で正当。downgrade しない（将来の C-* 系バグの温床）。

---

## Minor Findings

- **m-1**: `_unregister_assignment` の `still_used` 判定 (lib/allocation_engine.py:451-457) は `self.staff_day_visits.get(key, [])` を引いているが、直前 (line 439) で `visits.remove(result_idx)` がリストを破壊的更新済み。`if other_idx == result_idx: continue` ガード (line 452-453) は冗長。動作上は問題ないが意図が読みにくい。
- **m-2**: `_extract_day_of_month` の `for sep in ("/", "-", ".", " "):` ループは複数 separator が同時に存在した場合 (例 `2026/05-04`) `if sep in s` で最初のヒットだけを使い、ありえない split になる可能性。実害は無いが M-1 の正規表現方式で同時に解消可能。
- **m-3**: `requirements-dev.txt` に `pytest>=7.0` が追加されたが、既存 CI/Makefile/README に「dev インストール手順」の更新がない（このコミットには `requirements-dev.txt | 10 +` のみ）。新規開発者が `pip install -r requirements.txt` だけ実行すると 49 テストを走らせられない。
- **m-4**: `test_c10_extract_day_of_month` は parametrize で 11 パターンだが、`32-None` と `0-None` の境界以外に「`-1`」「`/`」「`/-/`」「全角数字」など防御的入力が無い。

---

## Test Coverage Assessment

| 観点 | 状態 | 補足 |
|---|---|---|
| C-1 unregister 復元 | ◎ | 2 ケース (削除/兄弟あり) で last_assigned_by_patient と pid_date_staff 双方を assert |
| C-2 unregister 前置 | ◎ | `_final_overlap_sweep` と `_fix_cross_patient_overlaps` を直接呼んで stale state 不在を検証。GapPack 経路 (line 1187) や ejection_chain rollback (line 2540) は明示テストなし |
| C-3 hard constraints | ◎ | 必須ルートの gender/soft_cap/same-day を `_find_best_staff` 直叩きで断定。`同じ人希望` 経路は smoke のみで、prev_staff_id が areas 違反のケース未検証 |
| C-4 state snapshot | ○ | 1 つの allocate 後で assign_count/staff_day_visits/pid_date_staff が結果と一致するか検証。**多試行間で best が確実に異なるシナリオ**（trial 1 で 5 件 / trial 2 で 4 件 unassigned 等）の構築がなく、「最終 trial と best が偶然一致」しても緑になる脆さ |
| C-7 areas | ◎ | 4 ケース (除外/許容/空欄/必須ルート) で網羅。ただし **`_relaxed_reinsertion`, `_ejection_chain`, `_day_shift_strategy`, `_rescue_partial_coupled` のエリア違反シナリオの個別テストがない** — 6 箇所にコピペした事実に対するカバレッジが薄い |
| C-9 substring | ○ | 1 ケースで edit が出ないことを確認。Pass3 経路 (line 595-606) も理論的には同じ修正だが、Pass2 と Pass3 を区別するテストがない |
| C-10 date | ◎ | 12 parametrize + 範囲内/範囲外の 2 統合 |

**最大の懸念**: C-4 の test (`test_c4_state_maps_consistent_with_results_after_allocate`) は **多試行 best 復元の経路を本当に通っているか** を保証していない。`make_engine` で 3 staff × 5 patient の単純な構成では最初の trial で `unassigned=0` を達成し、line 195 で break するため、後続 trial が走らず `best_assign_count is not None` 分岐に入らない可能性が高い。**「2 trial 目に best 更新が起きるシナリオ」を 1 ケース追加すべき**（例: 1 trial 目で意図的に soft_cap オーバーになる順序、2 trial 目の smart sort で解消される構成）。

---

## What's Missing

- C-5 / C-6 / C-8 の対象外宣言は妥当だが、**これらが本コミットの修正と相互作用しないか** の確認テストが無い（特に C-5 が `_request_constraints` shallow copy 関連ならば C-4 の snapshot 範囲と被る）。
- `_request_constraints` は `dict(self._request_constraints)` で shallow copy（line 181）。値は `{ng_staff_ids, sex_limit}` の dict で、`ng_staff_ids` が list → 後続 trial で `ng_ids = list(constraints.get(...))` するので結果的にセーフだが、**この前提に対するコメント/テストが無い**。将来 ng_staff_ids を直接 mutate するコードが入ったら静かに壊れる。
- `requirements-dev.txt` 追加にもかかわらず CI 設定 (.github/workflows 等) の更新が同梱されていない — 49 テストが PR でも回ることが担保されない。
- 既知限界として報告された `_unregister_assignment` の O(n) 走査 (last_assigned_by_patient フォールバック) は規模がスタッフ数 × 訪問数の二乗オーダーで効くため、年度切替バッチで顕在化する可能性があるが、ベンチマーク/プロファイリング指標が無い。

---

## Open Questions

- C-2 修正で `_fix_cross_patient_overlaps` (line 1573) と `_final_overlap_sweep` (line 2313) は unregister するが、`_gap_pack` (line 1187) は元から unregister していた。`ejection_chain` rollback (line 2540) も同様。**他に `r.staff_id = ""` を直接実行している場所が漏れていないか** 全数確認したか? grep では上記 6 箇所のみだが「assignment を破棄するときは必ず unregister する」を invariant として強制するヘルパ (`_unassign(idx)`) を導入するほうが将来の C-* 防止に効く。
- `_passes_hard_constraints` の area チェック (line 660) は `req.area` が空文字の場合 `req.area not in staff.areas` が False になる（空文字は何にも含まれない… いや、`"" in ["A"]` は False、`"" in []` も False なので空文字ならパス）。**`req.area = ""` でも staff.areas が指定されていれば、areas チェックはパスする** という現挙動は意図通りか? test_c7_empty_areas_is_unrestricted は staff 側 areas=[] をテストしているが、req 側 area="" のケースはテストしていない。

---

## Multi-Perspective

**Executor 視点**: コミットメッセージとコードコメントが C-1〜C-10 のラベル付きで詳述されており、`requirements-dev.txt` も同梱。`pytest lib/test_allocation_engine.py -v` で 49/49 緑。再現環境の準備は明快。ただし「multi-trial best 復元経路」を実際に通すために何 trial 目に何が起きるかの説明はコードコメントにのみ存在し、**2 trial 連続で best が更新される再現シナリオ** はテストにない。

**Stakeholder 視点**: 報告された 7 件のクリティカルバグは全て対象患者・スタッフが本番割当で実際に苦しむ症状（割当不能, 重複, 性別違反, 月またぎでの差分消失）と一対一対応しており、修正の business impact は明確。areas が「ロード済みだが未使用」だった事実は監査観点では既に深刻で、speedy fix は妥当。

**Skeptic 視点**: 最も強い反論は「**C-4 の修正は本当に必要だったのか**」。`allocate()` の戻り値 (`results`/`unassigned`/`summary`) には state map は含まれず、外部呼び出し元が `engine.assign_count` を読むユースケースが明示されていない。コミットメッセージは「post-allocate consumer that calls back into the engine」と書くが、現行 `PythonAllocateBridge.js` 等の呼び出し元はそういう使い方をしていない可能性が高い（要確認）。**呼び出し元の検証無しで内部 state を整える修正が入っており、過剰修正のリスク**。ただし副作用で害のある修正ではない（むしろ defensive）。

別観点: 「**本当に C-1 の症状で本番が苦しんでいたのか?** = `_unregister_assignment` を呼ぶ箇所がそもそも修正前の `_fix_cross_patient_overlaps` と `_final_overlap_sweep` から呼ばれていなかった (C-2 で初めて追加された)」 → C-1 と C-2 はセットでないと症状が出ない可能性。逆に言うと、テストはセットで効くこと前提に書かれている。これ自体は問題ではないが「C-1 単独で報告された stale 問題」がどこで顕在化していたのか別パスで再現する責任が残る (GapPack 経路 line 1187 と ejection_chain rollback line 2540 は元から呼んでいたので、そこ起点)。

---

## 総評

コミット `54ff3c5` はラベル付きで対象を明確に絞った良質なバグフィックス群で、テスト網羅も同水準のコミット平均より明らかに厚い。マージ可能だが、上記 M-1〜M-3 を次回 (C-5/C-6/C-8 を含むコミット) で同時解消することを強く推奨する。特に **M-3 (重複ロジック) は次の C-* バグを必ず生む構造** であり、ヘルパ統合を後回しにするほどコストが上がる。

主な参照ファイル:
- `C:\Users\imaizumi.LINEWORKS-NET\Documents\PlaywrightTest1\lib\allocation_engine.py`
- `C:\Users\imaizumi.LINEWORKS-NET\Documents\PlaywrightTest1\lib\diff_engine.py`
- `C:\Users\imaizumi.LINEWORKS-NET\Documents\PlaywrightTest1\lib\test_allocation_engine.py`
- `C:\Users\imaizumi.LINEWORKS-NET\Documents\PlaywrightTest1\lib\allocation_models.py`
- `C:\Users\imaizumi.LINEWORKS-NET\Documents\PlaywrightTest1\requirements-dev.txt`
