# G1 Codex Code Review — `54ff3c5`

**Reviewer**: OpenAI Codex CLI v0.128.0 (gpt-5.5)
**Branch**: `fix/critical-bugs-2026-05`
**Date**: 2026-05-05

> 注: pytest 再実行は OS のシェル policy で拒否されたため、本レビューは静的解析 + ソース読込のみに基づく。

## VERDICT: REVISE

## Concise summary

The commit fixes several initial-selection bugs, but later rescue/reinsertion phases can still violate the same hard constraints. The diff tests are useful, but C-9/C-10 coverage misses important real cases. I would not deploy this as a critical-bug fix without tightening those paths.

## Major Findings

### 1. Required staff constraints are not preserved after Level 0

- 場所: `lib/allocation_engine.py:608` / `lib/allocation_engine.py:1209`
- 証拠: `_request_constraints` stores only `ng_staff_ids` and `sex_limit`. A request with `specified_type == "必須"` can be rejected by `_find_best_staff`, then later assigned to **any other eligible staff** by Level1, ejection_chain, relaxed reinsertion, day shift, or coupled rescue.
- **Fix**: store `specified_type` and `specified_staff_ids` in `_request_constraints`, and enforce an allowed-staff set in every post-Level0 insertion path.
- Severity: **HIGH** — silently violates a hard business rule (必須スタッフ指定)

### 2. Same-patient/same-day/same-staff exclusion is still missing from reinsertion paths

- 場所: `lib/allocation_engine.py:1267` / `lib/allocation_engine.py:2554`
- 証拠: `_is_staff_available_for_reinsertion` checks NG, gender, area, weekday, capacity, and full-day blocks, but **not** `pid_date_staff`. `_relaxed_reinsertion` duplicates eligibility logic and also skips this invariant.
- **Fix**: add `staff.sid in pid_date_staff[f"{r.pid}|{r.date_str}"]` rejection to reinsertion eligibility, with a safe exception only when re-placing the same unregistered result.
- Severity: **HIGH** — same patient assigned to same staff twice on same day after rescue

### 3. C-10 still fails true month-spanning week ranges

- 場所: `lib/diff_engine.py:362`
- 証拠: Extracting a day number and checking `target_week_start <= day <= target_week_end` **rejects every entry for ranges like `29..5`** (e.g., week 29 May → 4 June).
- **Fix**: parse comparable full dates when source values include year/month; for day-only fallback, handle wrap ranges with `day >= start or day <= end`.
- Severity: **HIGH** — month boundary weeks silently drop all entries

### 4. Date normalization is used for filtering/sorting but not equality

- 場所: `lib/diff_engine.py:457` / `lib/diff_engine.py:611`
- 証拠: A current row dated `2026/05/04` and optimized row dated `4` are treated as different dates and can produce a **false `date_change`**.
- **Fix**: introduce a canonical date key for grouping/matching, not only `_extract_day_of_month`.
- Severity: **MEDIUM** — false-positive corrections in diff output

## Minor Findings

- `lib/allocation_engine.py:531` and `lib/allocation_engine.py:2344` do not include area restrictions in eligibility counts, so "hardest first" ordering can be misleading.
- `lib/diff_engine.py:467` calls `_extract_day_of_month` twice per sort key; cache once for clarity.
- `lib/test_allocation_engine.py:668` claims C-9 would match unrelated users, but comparison is per user, so that test does not exercise the vulnerable branch.

## Test coverage gaps

- Missing tests for required-staff requests with an alternate valid staff available in later rescue phases
- Missing same-patient/same-day reinsertion regression
- Missing C-10 wrap ranges such as `target_week_start=29, target_week_end=5`
- Missing mixed date-format equivalence tests

I attempted to rerun pytest, but the shell policy rejected the command, so this is static review plus source inspection.

## Performance risk

Moderate. Five allocation trials plus post-sweep rescue already multiply work; adding more constraints is cheap, but date parsing should be centralized/cached if diffing large CSVs.

## Production deployment risk

**High** until the hard-constraint leaks are fixed. The engine can output schedules that appear successful while violating "必須" staff or same-patient/day staff rules, and diff output can create false date-change corrections around month boundaries or mixed date formats.

## Open questions

- Is `specified_type == "必須"` intended to remain hard through all rescue phases?
- Should empty request `area` bypass staff `areas`, or should it be treated as missing required data?
- Can `target_week_start/end` be changed to full dates instead of day numbers?
