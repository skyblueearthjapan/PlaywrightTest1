"""
allocation_engine.py - Core visit allocation engine for the careflow scheduler.

Mirrors and improves upon the GAS UnifiedCode.js allocation logic.
Allocates visit requests to staff members considering events, 2-staff paired
visits, staff constraints, patient preferences, and distance optimization.

Allocation pipeline:
  1. Insert events as fixed time anchors
  2. Sort requests by priority (date, need_staff, rotation, time)
  3. Level 0 - Initial greedy allocation with scoring
  4. Sync coupled (2-staff) visit times
  5. GapPack - Pack flexible visits into gaps between anchors
  6. Level 1 - Reinsertion of unassigned visits with relaxed criteria
  7. Level 3 - Route optimization via nearest-neighbor + 2-opt
  8. Fix cross-patient time overlaps for the same staff
"""
from __future__ import annotations

import re
import logging
from typing import List, Dict, Optional, Tuple, Set

from .allocation_models import (
    Patient, Staff, VisitRequest, Event, StaffChange,
    WeeklyPattern, ConfirmedHistory, AssignmentResult, Interval
)
from .allocation_utils import (
    EXTRA_BUFFER_MIN, ASSIGN_BUFFER_MIN,
    calc_distance_km, dist_to_score, intervals_overlap, merge_intervals,
    compute_gaps, intersect_gaps, get_effective_window,
    nearest_neighbor_route, two_opt, route_length,
    normalize_cont_pref, weekday_index, fmt_min
)

logger = logging.getLogger(__name__)

# Regex compiled once for coupled visit ID parsing
_COUPLED_RE = re.compile(r'^(V\d+)-(\d+)$')


class AllocationEngine:
    """Careflow visit allocation engine.

    Allocates visit requests to staff members considering:
    - Events (highest priority, block time slots)
    - 2-staff paired visits (allocated first, synchronized times)
    - Staff constraints (shift, days off, maxPerDay, gender)
    - Patient preferences (time, staff, rotation)
    - Distance optimization (route planning)

    Usage::

        engine = AllocationEngine(
            staff_list=staff_list,
            patient_map=patient_map,
            events=events,
            staff_changes=staff_changes,
        )
        result = engine.allocate(requests)
        print(result["summary"]["message"])
    """

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------
    def __init__(
        self,
        staff_list: List[Staff],
        patient_map: Dict[str, Patient],
        events: List[Event],
        staff_changes: List[StaffChange],
        weekly_patterns: Optional[List[WeeklyPattern]] = None,
        confirmed_history: Optional[List[ConfirmedHistory]] = None,
        patient_changes=None,
        special_week_headers=None,
        special_week_details=None,
        mentor_pairs=None,
    ):
        self.staff_list = staff_list
        self.staff_map: Dict[str, Staff] = {s.sid: s for s in staff_list}
        self.patient_map = patient_map
        self.events = events
        self.staff_changes = staff_changes
        self.weekly_patterns = weekly_patterns or []
        self.confirmed_history = confirmed_history or []
        self.patient_changes = patient_changes or []
        self.special_week_headers = special_week_headers or []
        self.special_week_details = special_week_details or []
        self.mentor_pairs = mentor_pairs or []

        # Build staff-change lookup: "staffId|dateStr" -> [StaffChange]
        self.staff_change_map: Dict[str, List[StaffChange]] = {}
        for sc in staff_changes:
            key = f"{sc.staff_id}|{sc.date_str}"
            self.staff_change_map.setdefault(key, []).append(sc)

        # Build event lookup: "staffId|dateStr" -> [Event]
        self.event_map: Dict[str, List[Event]] = {}
        for ev in events:
            key = f"{ev.staff_id}|{ev.date_str}"
            self.event_map.setdefault(key, []).append(ev)

        # Build rotation history: pid -> [ConfirmedHistory]
        self.rotation_history: Dict[str, List[ConfirmedHistory]] = {}
        for ch in self.confirmed_history:
            self.rotation_history.setdefault(ch.pid, []).append(ch)

        # ---- Mutable state (reset on every allocate() call) ----
        self.results: List[AssignmentResult] = []
        self.unassigned: List[dict] = []
        self.assign_count: Dict[str, int] = {}          # "staffId|dateStr" -> count
        self.staff_day_visits: Dict[str, List[int]] = {}  # "staffId|dateStr" -> [result idx]
        self.pid_date_staff: Dict[str, Set[str]] = {}     # "pid|dateStr" -> {staffId}
        self.last_assigned_by_patient: Dict[str, str] = {}  # pid -> staffId (dynamic)
        # Level1用: 結果indexからリクエスト制約情報を引くマップ
        self._request_constraints: Dict[int, Dict] = {}  # result_idx -> {ng_staff_ids, sex_limit}

    # ==================================================================
    # Public entry point
    # ==================================================================
    def allocate(self, requests: List[VisitRequest]) -> dict:
        """Run the full allocation pipeline.

        Args:
            requests: Visit requests for the scheduling period.

        Returns:
            Dictionary with keys ``results``, ``unassigned``, and ``summary``.
        """
        self._reset_state()

        # Filter cancelled
        active_requests = [r for r in requests if r.change_type != "キャンセル"]

        # Step 0.1 - apply special week (REPLACE/ADD)
        active_requests = self._apply_special_week(active_requests)

        # Step 0.2 - apply patient changes (キャンセル/追加/時間変更/スタッフ変更)
        active_requests = self._apply_patient_changes(active_requests)

        # Step 0.3 - enrich from weekly patterns (時間窓補完)
        active_requests = self._enrich_from_patterns(active_requests)

        # ==============================================================
        # Smart Multi-Strategy Allocation Engine
        # 戦略1: 複数の処理順序で最良結果を選択
        # 戦略2: 全スタッフ探索 + Ejection Chain
        # 戦略3: 段階的制約緩和で未割当をゼロに近づける
        # ==============================================================

        import copy
        import random

        # Step 1 - events
        self._insert_events()

        # ----------------------------------------------------------
        # 戦略1: Multi-Order Trial（3つの異なる順序で割当し最良を採用）
        # ----------------------------------------------------------
        orderings = self._generate_orderings(active_requests)
        best_result = None
        best_unassigned = 99999

        for trial_idx, ordered_reqs in enumerate(orderings):
            # 各トライアルで状態をリセット（イベントは保持）
            saved_events = [r for r in self.results if r.is_event]
            self._reset_state()
            self.results = list(saved_events)
            # イベントをstaffDateMapに再登録
            for ei, ev in enumerate(saved_events):
                if ev.staff_id:
                    self._register_assignment(ev.staff_id, ev.date_str, ei)

            # Level 0
            self._level0_allocate(ordered_reqs)
            self._enforce_coupled_atomicity()
            self._sync_coupled_times()

            # GapPack
            self._gap_pack()
            self._sync_coupled_times()

            # Level 1 (enhanced: 全スタッフ探索)
            unassigned_indices = [
                i for i, r in enumerate(self.results)
                if not r.staff_id and not r.is_event
            ]
            if unassigned_indices:
                self._level1_reinsertion(unassigned_indices)
                self._sync_coupled_times()

            # スコア計算
            trial_unassigned = sum(
                1 for r in self.results if not r.staff_id and not r.is_event
            )
            logger.info(
                "Trial %d/%d: %d unassigned (order: %s)",
                trial_idx + 1, len(orderings), trial_unassigned,
                ordered_reqs[0].date_str if ordered_reqs else "empty",
            )

            if trial_unassigned < best_unassigned:
                best_unassigned = trial_unassigned
                best_result = {
                    "results": list(self.results),
                    "unassigned": list(self.unassigned),
                    "assign_count": dict(self.assign_count),
                    "staff_day_visits": {k: list(v) for k, v in self.staff_day_visits.items()},
                    "pid_date_staff": {k: set(v) for k, v in self.pid_date_staff.items()},
                    "last_assigned": dict(self.last_assigned_by_patient),
                    "request_constraints": dict(self._request_constraints),
                }
                if trial_unassigned == 0:
                    break  # 完全割当達成 → 探索終了

        # 最良結果を復元
        if best_result:
            self.results = best_result["results"]
            self.unassigned = best_result["unassigned"]
            self.assign_count = best_result["assign_count"]
            self.staff_day_visits = best_result["staff_day_visits"]
            self.pid_date_staff = best_result["pid_date_staff"]
            self.last_assigned_by_patient = best_result["last_assigned"]
            self._request_constraints = best_result["request_constraints"]

        # ----------------------------------------------------------
        # 戦略2: Ejection Chain（入替え挿入で未割当をさらに削減）
        # ----------------------------------------------------------
        remaining_unassigned = [
            i for i, r in enumerate(self.results)
            if not r.staff_id and not r.is_event
        ]
        if remaining_unassigned:
            logger.info(
                "Ejection Chain: Attempting swap-based reinsertion for %d visits...",
                len(remaining_unassigned),
            )
            ejected = self._ejection_chain(remaining_unassigned)
            if ejected > 0:
                self._sync_coupled_times()
                logger.info("Ejection Chain: resolved %d visits", ejected)

        # ----------------------------------------------------------
        # 戦略3: 段階的制約緩和（それでも未割当があれば）
        # ----------------------------------------------------------
        still_unassigned = [
            i for i, r in enumerate(self.results)
            if not r.staff_id and not r.is_event
        ]
        if still_unassigned:
            logger.info(
                "Relaxed Pass: Attempting with relaxed constraints for %d visits...",
                len(still_unassigned),
            )
            relaxed = self._relaxed_reinsertion(still_unassigned)
            if relaxed > 0:
                self._sync_coupled_times()
                logger.info("Relaxed Pass: resolved %d visits", relaxed)

        # Step 8 - route optimisation
        logger.info("Level 3: Optimizing routes...")
        self._level3_route_optimize()
        self._sync_coupled_times()

        # Step 9 - overlap fix
        self._fix_cross_patient_overlaps()
        self._sync_coupled_times()

        # Step 10 - mentor pair expansion (post-processing)
        self._apply_mentor_pairs()

        # Summary
        assigned_count = sum(
            1 for r in self.results if r.staff_id and not r.is_event
        )
        unassigned_count = sum(
            1 for r in self.results if not r.staff_id and not r.is_event
        )
        logger.info(
            "Allocation complete: %d assigned, %d unassigned out of %d total",
            assigned_count, unassigned_count, len(active_requests),
        )

        return {
            "results": self.results,
            "unassigned": self.unassigned,
            "summary": {
                "total": len(active_requests),
                "assigned": assigned_count,
                "unassigned": unassigned_count,
                "message": (
                    f"割当結果を {assigned_count} 件作成しました。"
                    f"割当不可: {unassigned_count} 件"
                ),
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers - state management
    # ------------------------------------------------------------------
    def _reset_state(self) -> None:
        """Clear mutable state before a fresh allocation run."""
        self.results = []
        self.unassigned = []
        self.assign_count = {}
        self.staff_day_visits = {}
        self.pid_date_staff = {}
        self.last_assigned_by_patient = {}

    def _register_assignment(
        self, staff_id: str, date_str: str, result_idx: int,
        pid: Optional[str] = None,
    ) -> None:
        """Update internal tracking maps after an assignment."""
        key = f"{staff_id}|{date_str}"
        self.assign_count[key] = self.assign_count.get(key, 0) + 1
        self.staff_day_visits.setdefault(key, []).append(result_idx)
        if pid:
            pd_key = f"{pid}|{date_str}"
            self.pid_date_staff.setdefault(pd_key, set()).add(staff_id)
            self.last_assigned_by_patient[pid] = staff_id

    def _unregister_assignment(
        self, staff_id: str, date_str: str, result_idx: int,
    ) -> None:
        """Remove tracking for a previously-registered assignment."""
        key = f"{staff_id}|{date_str}"
        visits = self.staff_day_visits.get(key, [])
        if result_idx in visits:
            visits.remove(result_idx)
        self.assign_count[key] = max(0, self.assign_count.get(key, 0) - 1)

    # ==================================================================
    # Step 1 - Insert Events
    # ==================================================================
    def _insert_events(self) -> None:
        """Insert events as fixed time anchors in the result list."""
        for ev in self.events:
            start = ev.start_min
            end = ev.end_min

            if start is None and end is None:
                staff = self.staff_map.get(ev.staff_id)
                shift_s = staff.shift_start_min if staff else 540
                shift_e = staff.shift_end_min if staff else 1080
                start = shift_s
                end = min(start + ev.duration_min, shift_e)
            elif start is not None and end is None:
                end = start + ev.duration_min

            svc = (end - start) if (start is not None and end is not None) else ev.duration_min
            staff = self.staff_map.get(ev.staff_id)

            result = AssignmentResult(
                visit_id=f"EV_{ev.event_id}",
                date_str=ev.date_str,
                staff_id=ev.staff_id,
                staff_name=staff.name if staff else "",
                area="EV",
                start_min=start,
                end_min=end,
                service_min=svc,
                time_type="固定",
                earliest_min=start,
                latest_min=end,
                note=ev.title or f"[EV] {ev.event_type}",
                is_event=True,
            )
            idx = len(self.results)
            self.results.append(result)
            self._register_assignment(ev.staff_id, ev.date_str, idx)

    # ==================================================================
    # Step 2 - Sort Requests
    # ==================================================================
    @staticmethod
    def _sort_requests(requests: List[VisitRequest]) -> List[VisitRequest]:
        """Sort requests: date ASC, needStaff DESC, rotation first, time ASC."""
        def _key(r: VisitRequest) -> tuple:
            cont = normalize_cont_pref(r.cont_pref)
            rotation_priority = 0 if cont == "ローテーション優先" else 1
            return (r.date_str, -r.need_staff, rotation_priority, r.start_min or 9999)
        return sorted(requests, key=_key)

    # ==================================================================
    # Step 3 - Level 0 Initial Allocation
    # ==================================================================
    def _level0_allocate(self, requests: List[VisitRequest]) -> None:
        """Process each request and assign the best-scoring staff."""
        visit_counter = 0

        for req in requests:
            used_staff_ids: Set[str] = set()
            visit_counter += 1

            for slot in range(1, req.need_staff + 1):
                visit_id = f"V{visit_counter:03d}"
                if req.need_staff > 1:
                    visit_id += f"-{slot}"

                chosen = self._find_best_staff(req, used_staff_ids)

                # 固定時刻: earliest_minからstart_minを確定
                start_min = req.start_min
                if start_min is None and req.time_type == "固定" and req.earliest_min is not None:
                    start_min = req.earliest_min
                end_min = req.end_min
                if end_min is None and start_min is not None:
                    end_min = start_min + req.service_min

                result = AssignmentResult(
                    visit_id=visit_id,
                    date_str=req.date_str,
                    weekday=req.weekday,
                    pid=req.pid,
                    pname=req.pname,
                    area=req.area,
                    start_min=start_min,
                    end_min=end_min,
                    service_min=req.service_min,
                    time_type=req.time_type,
                    earliest_min=req.earliest_min,
                    latest_min=req.latest_min,
                    note=req.note,
                    is_coupled=(req.need_staff > 1),
                )

                if chosen:
                    result.staff_id = chosen.sid
                    result.staff_name = chosen.name
                    used_staff_ids.add(chosen.sid)

                    # 即座に配置時間を確定（GapPack任せにしない）
                    if result.start_min is None:
                        fit = self._can_insert(chosen, result)
                        if fit is not None:
                            svc = result.service_min or 30
                            result.start_min = fit
                            result.end_min = fit + svc

                    idx = len(self.results)
                    self.results.append(result)
                    self._register_assignment(chosen.sid, req.date_str, idx, pid=req.pid)
                    self._request_constraints[idx] = {
                        "ng_staff_ids": req.ng_staff_ids,
                        "sex_limit": req.sex_limit,
                    }

                    # Track movement distance
                    patient = self.patient_map.get(req.pid)
                    if patient:
                        result.movement_km = calc_distance_km(
                            chosen.lat, chosen.lng, patient.lat, patient.lng
                        )
                else:
                    result.note += " [未割当: 条件を満たすスタッフなし]"
                    uidx = len(self.results)
                    self.results.append(result)
                    self._request_constraints[uidx] = {
                        "ng_staff_ids": req.ng_staff_ids,
                        "sex_limit": req.sex_limit,
                    }
                    self.unassigned.append({
                        "date_str": req.date_str,
                        "pid": req.pid,
                        "pname": req.pname,
                        "need_staff": req.need_staff,
                        "slot": slot,
                        "reason": "条件を満たすスタッフなし",
                    })

    # ==================================================================
    # Staff Selection
    # ==================================================================
    def _find_best_staff(
        self, req: VisitRequest, used_staff_ids: Set[str],
    ) -> Optional[Staff]:
        """Find the best staff member for a visit request.

        Selection order:
          1. Required designated staff (``specified_type == "必須"``)
          2. Same-person preference (``cont_pref == "同じ人希望"``)
          3. Scored candidate list (preference, rotation, load, distance)
        """
        cont_pref = normalize_cont_pref(req.cont_pref)
        dynamic_prev = self.last_assigned_by_patient.get(req.pid)

        # ---- Required designated staff ----
        if req.specified_type == "必須" and req.specified_staff_ids:
            for sid in req.specified_staff_ids:
                if sid in used_staff_ids:
                    continue
                staff = self.staff_map.get(sid)
                if staff and self._is_staff_available(staff, req):
                    return staff
            return None  # required staff unavailable

        # ---- Same-person preference ----
        if cont_pref == "同じ人希望":
            # 1. prev_staff_idが指定されていればそれを優先
            if req.prev_staff_id:
                staff = self.staff_map.get(req.prev_staff_id)
                if (staff
                        and req.prev_staff_id not in used_staff_ids
                        and req.prev_staff_id not in req.ng_staff_ids
                        and self._is_staff_available(staff, req)):
                    return staff
            # 2. confirmed_historyから最頻担当スタッフを検索
            history_records = self.rotation_history.get(req.pid, [])
            if history_records:
                from collections import Counter
                staff_counts = Counter(h.staff_id for h in history_records)
                for sid, _ in staff_counts.most_common():
                    if sid in used_staff_ids or sid in req.ng_staff_ids:
                        continue
                    staff = self.staff_map.get(sid)
                    if staff and self._is_staff_available(staff, req):
                        return staff

        # ---- Build scored candidate list ----
        candidates = self._build_candidate_list(req, used_staff_ids, cont_pref, dynamic_prev)
        if not candidates:
            return None

        # ---- Rotation adjustments ----
        if cont_pref == "ローテーション優先" and len(candidates) > 1:
            candidates = self._apply_rotation_ranking(candidates, req, dynamic_prev)

        # ---- Final sort by composite score ----
        candidates.sort(key=self._candidate_sort_key)
        return candidates[0]["staff"]

    def _build_candidate_list(
        self,
        req: VisitRequest,
        used_staff_ids: Set[str],
        cont_pref: str,
        dynamic_prev: Optional[str],
    ) -> List[dict]:
        """Return a list of candidate dicts for the given request."""
        pd_key = f"{req.pid}|{req.date_str}"
        candidates: List[dict] = []

        for staff in self.staff_list:
            if staff.sid in used_staff_ids:
                continue
            if staff.sid in req.ng_staff_ids:
                continue
            if not self._is_staff_available(staff, req):
                continue

            # Capacity check
            key = f"{staff.sid}|{req.date_str}"
            day_count = self.assign_count.get(key, 0)
            if day_count >= staff.soft_cap():
                continue

            # Gender restriction
            if req.sex_limit == "女性のみ" and staff.gender != "女性":
                continue
            if req.sex_limit == "男性のみ" and staff.gender != "男性":
                continue

            # Avoid same patient + same day + same staff (for multi-visit days)
            already_today = self.pid_date_staff.get(pd_key, set())
            if staff.sid in already_today:
                continue

            # Distance
            patient = self.patient_map.get(req.pid)
            dist_km: Optional[float] = None
            if patient:
                dist_km = calc_distance_km(
                    staff.lat, staff.lng, patient.lat, patient.lng,
                )

            # How many times this staff already visited this patient this week
            patient_count = sum(
                1 for r in self.results
                if r.pid == req.pid and r.staff_id == staff.sid and not r.is_event
            )

            candidates.append({
                "staff": staff,
                "day_count": day_count,
                "patient_count": patient_count,
                "dist_km": dist_km,
                "dist_score": dist_to_score(dist_km),
                "same_patient_today": staff.sid in already_today,
                "is_dynamic_prev": (staff.sid == dynamic_prev) if dynamic_prev else False,
                "is_pref": 1 if staff.sid in req.specified_staff_ids else 0,
                "rotation_rank": 0,
            })

        return candidates

    def _apply_rotation_ranking(
        self,
        candidates: List[dict],
        req: VisitRequest,
        dynamic_prev: Optional[str],
    ) -> List[dict]:
        """Filter and rank candidates for rotation preference.

        Prefers staff who have not yet visited this patient this week,
        excludes recently assigned staff from confirmed_history,
        and applies a weekday-based shift to distribute evenly.
        """
        # Prefer staff not yet assigned to this patient this week
        unassigned_this_week = [c for c in candidates if c["patient_count"] == 0]
        if unassigned_this_week:
            candidates = unassigned_this_week

        # Exclude dynamic previous if alternatives exist
        if dynamic_prev and len(candidates) > 1:
            non_prev = [c for c in candidates if not c["is_dynamic_prev"]]
            if non_prev:
                candidates = non_prev

        # Exclude recent staff from confirmed_history (past 2 assignments)
        history_records = self.rotation_history.get(req.pid, [])
        if history_records and len(candidates) > 1:
            sorted_hist = sorted(history_records, key=lambda h: h.date_str, reverse=True)
            recent_sids = set(h.staff_id for h in sorted_hist[:2])
            non_recent = [c for c in candidates if c["staff"].sid not in recent_sids]
            if non_recent:
                candidates = non_recent

        # Weekday rotation shift
        day_idx = weekday_index(req.weekday)
        candidates.sort(key=lambda c: c["staff"].sid)
        shift = day_idx % len(candidates) if candidates else 0
        candidates = candidates[shift:] + candidates[:shift]
        for i, c in enumerate(candidates):
            c["rotation_rank"] = i

        return candidates

    @staticmethod
    def _candidate_sort_key(c: dict) -> tuple:
        """Composite sort key for candidate ranking (lower is better)."""
        return (
            -c["is_pref"],                                  # 1. preferred staff first
            1 if c["same_patient_today"] else 0,            # 2. avoid same patient today
            1 if c["is_dynamic_prev"] else 0,               # 3. avoid dynamic prev
            c["patient_count"],                              # 4. fewer patient visits
            c["rotation_rank"],                              # 5. rotation rank
            c["day_count"],                                  # 6. fewer day assignments
            c["dist_score"],                                 # 7. closer distance
        )

    # ==================================================================
    # Staff Availability
    # ==================================================================
    def _is_staff_available(self, staff: Staff, req: VisitRequest) -> bool:
        """Determine whether *staff* can handle *req* on that date.

        Checks work days, blocked intervals, shift bounds, and existing
        visit overlap.
        """
        # Work-day check
        if staff.work_days and req.weekday not in staff.work_days:
            return False

        blocked = self._get_blocked_intervals(staff.sid, req.date_str)

        # Full day off
        for iv in blocked:
            if iv.start <= 0 and iv.end >= 1440:
                return False

        # Fixed-time visit: check direct overlap
        if req.time_type == "固定" and req.start_min is not None and req.end_min is not None:
            for iv in blocked:
                if intervals_overlap(req.start_min, req.end_min, iv.start, iv.end):
                    return False
            if req.start_min < staff.shift_start_min or req.end_min > staff.shift_end_min:
                return False
            return not self._has_overlap(staff.sid, req.date_str, req.start_min, req.end_min)

        # Flexible time: check if service fits in any available gap
        # (既存訪問との重複も必ずチェック)
        eff_earliest, eff_latest = get_effective_window(
            req.time_type, req.earliest_min, req.latest_min,
        )
        eff_earliest = max(eff_earliest, staff.shift_start_min)
        eff_latest = min(eff_latest, staff.shift_end_min)

        all_blocked = list(blocked)
        # Include existing visit times as blocked (重複防止の核心)
        key = f"{staff.sid}|{req.date_str}"
        for idx in self.staff_day_visits.get(key, []):
            r = self.results[idx]
            if r.start_min is not None and r.end_min is not None:
                all_blocked.append(Interval(r.start_min, r.end_min))

        gaps = compute_gaps(all_blocked, eff_earliest, eff_latest)
        svc = req.service_min or 30
        return any(gap.end - gap.start >= svc for gap in gaps)

    def _get_blocked_intervals(self, staff_id: str, date_str: str) -> List[Interval]:
        """Return merged blocked-time intervals for a staff member on a date."""
        key = f"{staff_id}|{date_str}"
        changes = self.staff_change_map.get(key, [])
        if not changes:
            return []

        staff = self.staff_map.get(staff_id)
        shift_s = staff.shift_start_min if staff else 540
        shift_e = staff.shift_end_min if staff else 1080

        intervals: List[Interval] = []
        for sc in changes:
            rt = sc.restriction_type
            if rt in ("休み", "終日不可", "終日"):
                intervals.append(Interval(0, 1440))
            elif rt == "午前休":
                intervals.append(Interval(shift_s, 720))
            elif rt == "午後休":
                intervals.append(Interval(720, shift_e))
            elif rt == "遅刻" and sc.start_min is not None:
                intervals.append(Interval(shift_s, sc.start_min))
            elif rt == "早退" and sc.end_min is not None:
                intervals.append(Interval(sc.end_min, shift_e))
            elif rt in ("時間指定", "時間指定制限") and sc.start_min is not None and sc.end_min is not None:
                intervals.append(Interval(sc.start_min, sc.end_min))

        return merge_intervals(intervals)

    def _has_overlap(self, staff_id: str, date_str: str, start: int, end: int) -> bool:
        """Return True if ``[start, end)`` overlaps any existing visit for the staff."""
        key = f"{staff_id}|{date_str}"
        for idx in self.staff_day_visits.get(key, []):
            r = self.results[idx]
            if r.start_min is not None and r.end_min is not None:
                if intervals_overlap(start, end, r.start_min, r.end_min):
                    return True
        return False

    # ==================================================================
    # Coupled Visit Time Sync
    # ==================================================================
    def _sync_coupled_times(self) -> None:
        """Synchronize start/end times for 2-staff paired visits."""
        coupled_map: Dict[str, List[int]] = {}
        for i, r in enumerate(self.results):
            if not r.visit_id or r.is_event:
                continue
            m = _COUPLED_RE.match(r.visit_id)
            if m:
                coupled_map.setdefault(m.group(1), []).append(i)

        for base_id, indices in coupled_map.items():
            if len(indices) != 2:
                continue
            r1 = self.results[indices[0]]
            r2 = self.results[indices[1]]

            if not r1.staff_id or not r2.staff_id:
                continue

            # Already in sync
            if (r1.start_min == r2.start_min
                    and r1.end_min == r2.end_min
                    and r1.start_min is not None):
                continue

            svc = r1.service_min or r2.service_min or 60

            w1 = get_effective_window(r1.time_type, r1.earliest_min, r1.latest_min)
            w2 = get_effective_window(r2.time_type, r2.earliest_min, r2.latest_min)
            common_earliest = max(w1[0], w2[0])
            common_latest = min(w1[1], w2[1])

            s1 = r1.start_min if r1.start_min is not None else common_earliest
            s2 = r2.start_min if r2.start_min is not None else common_earliest
            target = max(s1, s2, common_earliest)
            target_end = target + svc

            if target_end <= common_latest:
                no_overlap_1 = not self._has_overlap(
                    r1.staff_id, r1.date_str, target, target_end,
                )
                no_overlap_2 = not self._has_overlap(
                    r2.staff_id, r2.date_str, target, target_end,
                )
                if no_overlap_1 and no_overlap_2:
                    r1.start_min = r2.start_min = target
                    r1.end_min = r2.end_min = target_end
                    continue

            # Fallback: force to common earliest
            r1.start_min = r2.start_min = common_earliest
            r1.end_min = r2.end_min = common_earliest + svc
            r1.note += " [時刻同期]"
            r2.note += " [時刻同期]"

    # ==================================================================
    # GapPack - Time Refinement
    # ==================================================================
    def _gap_pack(self) -> None:
        """Pack flexible visits into the gaps between fixed anchors."""
        groups: Dict[str, List[int]] = {}
        for i, r in enumerate(self.results):
            if not r.staff_id:
                continue
            key = f"{r.staff_id}|{r.date_str}"
            groups.setdefault(key, []).append(i)

        for key, indices in groups.items():
            staff_id = key.split("|")[0]
            staff = self.staff_map.get(staff_id)
            if not staff:
                continue

            anchors: List[Tuple[int, int, int]] = []   # (idx, start, end)
            flexes: List[int] = []

            for idx in indices:
                r = self.results[idx]
                has_time = r.start_min is not None and r.end_min is not None
                is_anchor = (
                    r.is_event
                    or (r.time_type == "固定" and has_time)
                    or (r.is_coupled and has_time)
                )
                if is_anchor:
                    anchors.append((idx, r.start_min, r.end_min))  # type: ignore[arg-type]
                else:
                    flexes.append(idx)

            if not flexes:
                continue

            anchors.sort(key=lambda x: x[1])
            day_start = max(staff.shift_start_min, 540)
            day_end = staff.shift_end_min

            # Compute gaps between anchors
            gaps: List[Interval] = []
            cursor = day_start
            for _, a_start, a_end in anchors:
                gap_end = a_start - EXTRA_BUFFER_MIN
                if cursor < gap_end:
                    gaps.append(Interval(cursor, gap_end))
                cursor = max(cursor, a_end + EXTRA_BUFFER_MIN)
            if cursor < day_end:
                gaps.append(Interval(cursor, day_end))

            # Sort flexes by earliest preference, then start time
            flexes.sort(
                key=lambda idx: (
                    self.results[idx].earliest_min
                    or self.results[idx].start_min
                    or 9999
                ),
            )

            # Place each flex into the first fitting gap
            gap_cursors = [g.start for g in gaps]
            for f_idx in flexes:
                r = self.results[f_idx]
                if not r.staff_id:
                    continue
                svc = r.service_min or 30
                eff_earliest, eff_latest = get_effective_window(
                    r.time_type, r.earliest_min, r.latest_min,
                )
                eff_earliest = max(eff_earliest, day_start)

                placed = False
                for gi, gap in enumerate(gaps):
                    start_cand = max(gap_cursors[gi], eff_earliest)
                    end_cand = start_cand + svc

                    if eff_latest is not None and end_cand > eff_latest:
                        continue

                    if start_cand >= gap.start and end_cand <= gap.end:
                        r.start_min = start_cand
                        r.end_min = end_cand
                        gap_cursors[gi] = end_cand + EXTRA_BUFFER_MIN
                        placed = True
                        break

                if not placed:
                    logger.debug(
                        "GapPack: visit %s could not fit, unassigning", r.visit_id,
                    )
                    if r.staff_id:
                        self._unregister_assignment(r.staff_id, r.date_str, idx)
                    r.staff_id = ""
                    r.staff_name = ""
                    r.note += " [GapPack: 隙間に入らず未割当]"

    # ==================================================================
    # Level 1 - Reinsertion
    # ==================================================================
    def _level1_reinsertion(self, unassigned_indices: List[int]) -> None:
        """Attempt to place unassigned visits with alternative staff."""
        self._enforce_coupled_atomicity()

        for idx in unassigned_indices:
            r = self.results[idx]
            if r.staff_id or r.is_event:
                continue

            patient = self.patient_map.get(r.pid)
            if not patient:
                continue

            # Build distance-sorted candidates (top 10)
            constraints = self._request_constraints.get(idx, {})
            ng_ids = list(constraints.get("ng_staff_ids", []))
            sex_lim = constraints.get("sex_limit", "")

            # Coupled pair: exclude the staff already assigned to the other slot
            if r.is_coupled and r.visit_id:
                m = _COUPLED_RE.match(r.visit_id)
                if m:
                    base_id = m.group(1)
                    for other_r in self.results:
                        if (other_r.visit_id and other_r.visit_id != r.visit_id
                                and other_r.visit_id.startswith(base_id + "-")
                                and other_r.staff_id):
                            ng_ids.append(other_r.staff_id)

            candidates: List[Tuple[Staff, float]] = []
            for staff in self.staff_list:
                if not self._is_staff_available_for_reinsertion(
                    staff, r, ng_staff_ids=ng_ids, sex_limit=sex_lim
                ):
                    continue
                dist = calc_distance_km(staff.lat, staff.lng, patient.lat, patient.lng)
                candidates.append((staff, dist if dist is not None else 99999.0))

            candidates.sort(key=lambda x: x[1])
            candidates = candidates[:10]

            for staff, _ in candidates:
                # 固定時刻の場合はその時刻を尊重
                if r.time_type == "固定" and r.earliest_min is not None:
                    fit_start = r.earliest_min
                    svc = r.service_min or 30
                    # この時刻でスタッフが空いているか確認
                    if self._has_overlap(staff.sid, r.date_str, fit_start, fit_start + svc):
                        continue
                else:
                    fit_start = self._can_insert(staff, r)
                if fit_start is not None:
                    svc = r.service_min or 30
                    r.staff_id = staff.sid
                    r.staff_name = staff.name
                    r.start_min = fit_start
                    r.end_min = fit_start + svc
                    self._register_assignment(staff.sid, r.date_str, idx, pid=r.pid)
                    r.note += f" [Level1再挿入: {staff.name}]"
                    logger.debug(
                        "Level1: reinserted visit %s -> %s at %s",
                        r.visit_id, staff.name, fmt_min(fit_start),
                    )
                    break

    def _is_staff_available_for_reinsertion(
        self, staff: Staff, r: AssignmentResult,
        ng_staff_ids: Optional[List[str]] = None,
        sex_limit: str = "",
    ) -> bool:
        """Quick eligibility check for Level-1 reinsertion.

        Now includes NG staff and gender checks (previously missing).
        """
        # H1: NGスタッフ除外
        if ng_staff_ids and staff.sid in ng_staff_ids:
            return False
        # H2: 性別制限
        if sex_limit == "女性のみ" and staff.gender != "女性":
            return False
        if sex_limit == "男性のみ" and staff.gender != "男性":
            return False
        # H3: 勤務曜日
        if staff.work_days and r.weekday not in staff.work_days:
            return False
        # H6: maxPerDay (soft_capで統一)
        key = f"{staff.sid}|{r.date_str}"
        if self.assign_count.get(key, 0) >= staff.soft_cap():
            return False
        # H5: スタッフ個別変更（終日不可チェック）
        blocked = self._get_blocked_intervals(staff.sid, r.date_str)
        for iv in blocked:
            if iv.start <= 0 and iv.end >= 1440:
                return False
        return True

    def _can_insert(self, staff: Staff, r: AssignmentResult) -> Optional[int]:
        """Find the earliest start minute where *r* fits in *staff*'s day.

        Returns:
            The start minute, or ``None`` if no slot is available.
        """
        svc = r.service_min or 30
        eff_earliest, eff_latest = get_effective_window(
            r.time_type, r.earliest_min, r.latest_min,
        )
        eff_earliest = max(eff_earliest, staff.shift_start_min, 540)
        eff_latest = min(eff_latest, staff.shift_end_min)

        # Aggregate all blocked intervals
        blocked = list(self._get_blocked_intervals(staff.sid, r.date_str))
        key = f"{staff.sid}|{r.date_str}"
        for idx in self.staff_day_visits.get(key, []):
            existing = self.results[idx]
            if existing.start_min is not None and existing.end_min is not None:
                blocked.append(Interval(
                    existing.start_min - EXTRA_BUFFER_MIN,
                    existing.end_min + EXTRA_BUFFER_MIN,
                ))

        gaps = compute_gaps(blocked, eff_earliest, eff_latest)
        for gap in gaps:
            gap_start = max(gap.start, eff_earliest)
            if gap.end - gap_start >= svc:
                return gap_start
        return None

    def _enforce_coupled_atomicity(self) -> None:
        """If one half of a 2-staff pair is unassigned, unassign both."""
        coupled_map: Dict[str, List[int]] = {}
        for i, r in enumerate(self.results):
            if not r.visit_id:
                continue
            m = _COUPLED_RE.match(r.visit_id)
            if m:
                coupled_map.setdefault(m.group(1), []).append(i)

        for base_id, indices in coupled_map.items():
            if len(indices) != 2:
                continue
            r1 = self.results[indices[0]]
            r2 = self.results[indices[1]]
            assigned1 = bool(r1.staff_id)
            assigned2 = bool(r2.staff_id)

            if assigned1 != assigned2:
                for idx in indices:
                    r = self.results[idx]
                    if r.staff_id:
                        self._unregister_assignment(r.staff_id, r.date_str, idx)
                    r.staff_id = ""
                    r.staff_name = ""
                    r.note += " [2名体制:ペア未確保]"

    # ==================================================================
    # Level 3 - Route Optimization
    # ==================================================================
    def _level3_route_optimize(self) -> None:
        """Reorder flexible visits using nearest-neighbor + 2-opt."""
        groups: Dict[str, List[int]] = {}
        for i, r in enumerate(self.results):
            if not r.staff_id:
                continue
            key = f"{r.staff_id}|{r.date_str}"
            groups.setdefault(key, []).append(i)

        for key, indices in groups.items():
            staff_id = key.split("|")[0]
            staff = self.staff_map.get(staff_id)
            if not staff:
                continue

            anchors: List[int] = []
            flexes: List[int] = []

            for idx in indices:
                r = self.results[idx]
                is_anchor = (
                    r.is_event
                    or r.time_type == "固定"
                    or r.is_coupled
                    or r.earliest_min is not None
                    or r.latest_min is not None
                )
                if is_anchor and r.start_min is not None:
                    anchors.append(idx)
                elif r.start_min is not None:
                    flexes.append(idx)

            if len(flexes) < 2:
                continue

            # Build node list for the optimizer
            nodes: List[dict] = []
            for idx in flexes:
                r = self.results[idx]
                patient = self.patient_map.get(r.pid)
                nodes.append({
                    "idx": idx,
                    "lat": patient.lat if patient else None,
                    "lng": patient.lng if patient else None,
                    "svc_min": r.service_min or 30,
                })

            optimized = two_opt(nearest_neighbor_route(nodes, staff.lat, staff.lng))

            # Compute gap intervals between anchors
            anchor_times = sorted(
                [
                    (self.results[i].start_min, self.results[i].end_min)
                    for i in anchors
                    if self.results[i].start_min is not None and self.results[i].end_min is not None
                ],
                key=lambda x: x[0],
            )

            day_start = max(staff.shift_start_min, 540)
            day_end = staff.shift_end_min

            gaps: List[Interval] = []
            cursor = day_start
            for a_s, a_e in anchor_times:
                gap_end = a_s - EXTRA_BUFFER_MIN
                if cursor < gap_end:
                    gaps.append(Interval(cursor, gap_end))
                cursor = max(cursor, a_e + EXTRA_BUFFER_MIN)
            if cursor < day_end:
                gaps.append(Interval(cursor, day_end))

            # Place optimised nodes into gaps
            opt_idx = 0
            for gap in gaps:
                t = gap.start
                while opt_idx < len(optimized) and t < gap.end:
                    node = optimized[opt_idx]
                    svc = node["svc_min"]
                    if t + svc <= gap.end:
                        r = self.results[node["idx"]]
                        # 時間窓制約チェック
                        eff_earliest, eff_latest = get_effective_window(
                            r.time_type, r.earliest_min, r.latest_min
                        )
                        if t >= eff_earliest and (eff_latest is None or t + svc <= eff_latest):
                            r.start_min = t
                            r.end_min = t + svc
                            t += svc + EXTRA_BUFFER_MIN
                            opt_idx += 1
                        else:
                            # 時間窓外: スキップして次の訪問を試す
                            opt_idx += 1
                    else:
                        break

    # ==================================================================
    # Fix Cross-Patient Overlaps
    # ==================================================================
    def _fix_cross_patient_overlaps(self) -> None:
        """Resolve time conflicts between different patients for the same staff."""
        groups: Dict[str, List[int]] = {}
        for i, r in enumerate(self.results):
            if not r.staff_id or r.is_event:
                continue
            key = f"{r.staff_id}|{r.date_str}"
            groups.setdefault(key, []).append(i)

        for key, indices in groups.items():
            staff_id = key.split("|")[0]
            staff = self.staff_map.get(staff_id)
            if not staff or len(indices) < 2:
                continue

            sorted_idx = sorted(
                indices, key=lambda i: self.results[i].start_min or 0,
            )
            placed: List[Tuple[int, int]] = []

            for idx in sorted_idx:
                r = self.results[idx]
                if r.start_min is None or r.end_min is None:
                    continue

                has_conflict = any(
                    intervals_overlap(r.start_min, r.end_min, p_s, p_e)
                    for p_s, p_e in placed
                )

                if not has_conflict:
                    placed.append((r.start_min, r.end_min))
                    continue

                # Don't shift coupled visits (they are synced separately)
                if r.is_coupled:
                    continue

                # Attempt to shift after each conflicting placement
                svc = r.service_min or 30
                new_start = r.start_min
                for p_s, p_e in sorted(placed):
                    if intervals_overlap(new_start, new_start + svc, p_s, p_e):
                        new_start = p_e + ASSIGN_BUFFER_MIN

                eff_latest = r.latest_min if r.latest_min is not None else staff.shift_end_min
                if new_start + svc <= eff_latest:
                    old = r.start_min
                    r.start_min = new_start
                    r.end_min = new_start + svc
                    r.note += f" [重複修正: {fmt_min(old)}->{fmt_min(new_start)}]"
                    placed.append((new_start, new_start + svc))
                else:
                    logger.warning(
                        "Could not resolve overlap for visit %s on %s; unassigning",
                        r.visit_id, r.date_str,
                    )
                    r.staff_id = ""
                    r.staff_name = ""
                    r.note += " [重複解消不可: 未割当]"

    # ==================================================================
    # Pre-processing: patient_changes, special_week, weekly_patterns
    # ==================================================================

    def _apply_patient_changes(self, requests: List[VisitRequest]) -> List[VisitRequest]:
        """Apply patient_changes to weekly requests."""
        if not self.patient_changes:
            return requests
        result = list(requests)
        for pc in self.patient_changes:
            if pc.operation == "キャンセル":
                result = [r for r in result
                          if not (r.pid == pc.pid and r.date_str == pc.date_str)]
                logger.debug("PatientChange: cancelled %s on %s", pc.pid, pc.date_str)
            elif pc.operation == "追加":
                new_req = VisitRequest(
                    request_id=f"PC_ADD_{pc.pid}_{pc.date_str}",
                    date_str=pc.date_str,
                    pid=pc.pid,
                    service_min=pc.service_min or 60,
                    need_staff=pc.need_staff or 1,
                    time_type=pc.time_type or "",
                    start_min=pc.start_min,
                    end_min=pc.end_min,
                    earliest_min=pc.earliest_min,
                    latest_min=pc.latest_min,
                    specified_staff_ids=pc.specified_staff_ids or [],
                    specified_type=pc.specified_type or "",
                    ng_staff_ids=pc.ng_staff_ids or [],
                    note=pc.note or "個別変更:追加",
                )
                patient = self.patient_map.get(pc.pid)
                if patient:
                    new_req.pname = patient.name
                    new_req.area = patient.area
                result.append(new_req)
                logger.debug("PatientChange: added %s on %s", pc.pid, pc.date_str)
            elif pc.operation in ("時間変更", "スタッフ変更"):
                for r in result:
                    if r.pid == pc.pid and r.date_str == pc.date_str:
                        if pc.operation == "時間変更":
                            if pc.start_min is not None:
                                r.start_min = pc.start_min
                            if pc.end_min is not None:
                                r.end_min = pc.end_min
                            if pc.earliest_min is not None:
                                r.earliest_min = pc.earliest_min
                            if pc.latest_min is not None:
                                r.latest_min = pc.latest_min
                            if pc.time_type:
                                r.time_type = pc.time_type
                            if pc.service_min is not None:
                                r.service_min = pc.service_min
                        elif pc.operation == "スタッフ変更":
                            if pc.specified_staff_ids:
                                r.specified_staff_ids = pc.specified_staff_ids
                            if pc.specified_type:
                                r.specified_type = pc.specified_type
                            if pc.ng_staff_ids:
                                r.ng_staff_ids = pc.ng_staff_ids
                        r.note += f" [個別変更:{pc.operation}]"
        return result

    def _apply_special_week(self, requests: List[VisitRequest]) -> List[VisitRequest]:
        """Apply special_week headers/details to requests."""
        if not self.special_week_headers:
            return requests
        result = list(requests)
        for header in self.special_week_headers:
            details = [d for d in self.special_week_details
                       if d.special_week_id == header.special_week_id]
            if header.mode == "REPLACE":
                result = [r for r in result if r.pid != header.pid]
                logger.debug("SpecialWeek REPLACE: removed requests for %s", header.pid)
            for i, detail in enumerate(details):
                new_req = VisitRequest(
                    request_id=f"SW_{header.special_week_id}_{i}",
                    date_str=detail.date_str,
                    pid=detail.pid or header.pid,
                    pname=header.pname,
                    service_min=detail.service_min,
                    need_staff=detail.need_staff,
                    time_type=detail.time_type,
                    start_min=detail.start_min,
                    end_min=detail.end_min,
                    earliest_min=detail.earliest_min,
                    latest_min=detail.latest_min,
                    note=f"特別訪問週間:{header.reason}" if header.reason else "特別訪問週間",
                )
                patient = self.patient_map.get(new_req.pid)
                if patient:
                    new_req.area = patient.area
                result.append(new_req)
            logger.debug("SpecialWeek %s: added %d details for %s",
                         header.mode, len(details), header.pid)
        return result

    def _enrich_from_patterns(self, requests: List[VisitRequest]) -> List[VisitRequest]:
        """Enrich requests with time window info from weekly_patterns."""
        if not self.weekly_patterns:
            return requests
        pattern_map: Dict[str, List] = {}
        for wp in self.weekly_patterns:
            key = f"{wp.pid}|{wp.day_code}"
            pattern_map.setdefault(key, []).append(wp)
        for req in requests:
            key = f"{req.pid}|{req.weekday}"
            patterns = pattern_map.get(key)
            if not patterns:
                continue
            pat = patterns[0]
            if req.start_min is None and pat.start_min is not None:
                req.start_min = pat.start_min
            if req.end_min is None and pat.end_min is not None:
                req.end_min = pat.end_min
            if not req.time_type and pat.start_min is not None:
                req.time_type = "固定"
        return requests

    # ==================================================================
    # Post-processing: mentor pairs
    # ==================================================================

    def _apply_mentor_pairs(self) -> None:
        """Generate trainee shadowing visits from mentor_pairs."""
        if not self.mentor_pairs:
            return
        new_results = []
        for mp in self.mentor_pairs:
            if not mp.trainee_staff_id or not mp.mentor_staff_id:
                continue
            mentor_visits = [r for r in self.results
                             if r.staff_id == mp.mentor_staff_id
                             and not r.is_event
                             and r.start_min is not None]
            if mp.start_date:
                mentor_visits = [v for v in mentor_visits if v.date_str >= mp.start_date]
            if mp.end_date:
                mentor_visits = [v for v in mentor_visits if v.date_str <= mp.end_date]
            if mp.band == "午前":
                mentor_visits = [v for v in mentor_visits if v.start_min < 720]
            elif mp.band == "午後":
                mentor_visits = [v for v in mentor_visits if v.start_min >= 720]
            if mp.day_condition:
                day_map = {"月": "Mon", "火": "Tue", "水": "Wed", "木": "Thu",
                           "金": "Fri", "土": "Sat", "日": "Sun"}
                allowed = set()
                for ch in mp.day_condition.replace(",", "").replace("、", ""):
                    if ch in day_map:
                        allowed.add(day_map[ch])
                if allowed:
                    mentor_visits = [v for v in mentor_visits if v.weekday in allowed]
            trainee_times: Dict[str, List[Tuple[int, int]]] = {}
            for r in self.results:
                if r.staff_id == mp.trainee_staff_id and r.start_min is not None:
                    trainee_times.setdefault(r.date_str, []).append(
                        (r.start_min, r.end_min))
            for mv in mentor_visits:
                existing = trainee_times.get(mv.date_str, [])
                has_conflict = any(
                    mv.start_min < e and mv.end_min > s for s, e in existing
                )
                if has_conflict:
                    continue
                copy = AssignmentResult(
                    visit_id=f"{mv.visit_id}_T_{mp.trainee_staff_id}",
                    date_str=mv.date_str,
                    weekday=mv.weekday,
                    staff_id=mp.trainee_staff_id,
                    staff_name="",
                    pid=mv.pid,
                    pname=mv.pname,
                    area=mv.area,
                    start_min=mv.start_min,
                    end_min=mv.end_min,
                    service_min=mv.service_min,
                    time_type=mv.time_type,
                    note=f"同行({mp.mentor_staff_id})",
                    is_event=False,
                    is_coupled=False,
                )
                trainee_staff = self.staff_map.get(mp.trainee_staff_id)
                if trainee_staff:
                    copy.staff_name = trainee_staff.name
                new_results.append(copy)
                trainee_times.setdefault(mv.date_str, []).append(
                    (mv.start_min, mv.end_min))
        self.results.extend(new_results)
        if new_results:
            logger.info("MentorPairs: %d shadowing visits added", len(new_results))

    # ==================================================================
    # Smart Strategy Methods
    # ==================================================================

    def _generate_orderings(self, requests: List[VisitRequest]) -> List[List[VisitRequest]]:
        """Generate multiple request orderings for multi-trial allocation.

        Strategy: try different sort keys to find the ordering that
        minimizes unassigned visits.
        """
        import random as _rnd

        orderings = []

        # Order 1: Default (date → needStaff desc → rotation → time)
        orderings.append(self._sort_requests(requests))

        # Order 2: Hardest-first (fewest eligible staff → need_staff desc → date)
        def _constraint_tightness(r: VisitRequest) -> tuple:
            eligible = 0
            for s in self.staff_list:
                if s.sid in r.ng_staff_ids:
                    continue
                if r.sex_limit == "女性のみ" and s.gender != "女性":
                    continue
                if r.sex_limit == "男性のみ" and s.gender != "男性":
                    continue
                if s.work_days and r.weekday and r.weekday not in s.work_days:
                    continue
                eligible += 1
            return (eligible, -r.need_staff, r.date_str, r.start_min or 9999)
        orderings.append(sorted(requests, key=_constraint_tightness))

        # Order 3: Reverse date (金→月) + need_staff desc
        def _reverse_date(r: VisitRequest) -> tuple:
            return (r.date_str, -r.need_staff, r.start_min or 9999)
        orderings.append(sorted(requests, key=_reverse_date, reverse=True))

        return orderings

    def _ejection_chain(self, unassigned_indices: List[int]) -> int:
        """Swap-based reinsertion: displace an assigned visit to make room.

        For each unassigned visit U:
          1. For each staff S (all staff, not just top 10):
             - If S has room → insert U directly
          2. If no room via direct insert:
             - For each assigned visit A on the same date:
               - Temporarily remove A
               - Try inserting U in A's staff slot
               - If U fits, try re-inserting A with another staff
               - If both succeed → commit the swap
               - If not → rollback
        """
        resolved = 0

        for idx in unassigned_indices:
            r = self.results[idx]
            if r.staff_id or r.is_event:
                continue

            constraints = self._request_constraints.get(idx, {})
            ng_ids = list(constraints.get("ng_staff_ids", []))
            sex_lim = constraints.get("sex_limit", "")

            # Coupled pair: also exclude the other slot's staff
            if r.is_coupled and r.visit_id:
                m = _COUPLED_RE.match(r.visit_id)
                if m:
                    base_id = m.group(1)
                    for other_r in self.results:
                        if (other_r.visit_id and other_r.visit_id != r.visit_id
                                and other_r.visit_id.startswith(base_id + "-")
                                and other_r.staff_id):
                            ng_ids.append(other_r.staff_id)

            patient = self.patient_map.get(r.pid)
            if not patient:
                continue

            # --- Direct insert (全スタッフ探索) ---
            placed = False
            for staff in self.staff_list:
                if not self._is_staff_available_for_reinsertion(
                    staff, r, ng_staff_ids=ng_ids, sex_limit=sex_lim
                ):
                    continue
                if r.time_type == "固定" and r.earliest_min is not None:
                    fit_start = r.earliest_min
                    svc = r.service_min or 30
                    if self._has_overlap(staff.sid, r.date_str, fit_start, fit_start + svc):
                        continue
                else:
                    fit_start = self._can_insert(staff, r)
                if fit_start is not None:
                    svc = r.service_min or 30
                    r.staff_id = staff.sid
                    r.staff_name = staff.name
                    r.start_min = fit_start
                    r.end_min = fit_start + svc
                    self._register_assignment(staff.sid, r.date_str, idx, pid=r.pid)
                    r.note += f" [EjectionChain直接挿入: {staff.name}]"
                    resolved += 1
                    placed = True
                    break

            if placed:
                continue

            # --- Ejection Chain (入替え挿入) ---
            # 同日の割当済み訪問を1つずつ試す
            same_day_assigned = [
                (ai, ar) for ai, ar in enumerate(self.results)
                if ar.staff_id and not ar.is_event
                and ar.date_str == r.date_str
                and not ar.is_coupled  # coupled訪問は入替え対象外
            ]

            for victim_idx, victim in same_day_assigned:
                victim_staff = self.staff_map.get(victim.staff_id)
                if not victim_staff:
                    continue

                # U をvictimのスタッフに入れられるか？
                if not self._is_staff_available_for_reinsertion(
                    victim_staff, r, ng_staff_ids=ng_ids, sex_limit=sex_lim
                ):
                    continue

                # victimを一時的に除去
                saved_staff = victim.staff_id
                saved_name = victim.staff_name
                saved_start = victim.start_min
                saved_end = victim.end_min
                self._unregister_assignment(victim.staff_id, victim.date_str, victim_idx)
                victim.staff_id = ""
                victim.staff_name = ""

                # U をvictimの元スタッフに挿入試行
                if r.time_type == "固定" and r.earliest_min is not None:
                    fit_u = r.earliest_min
                    svc_u = r.service_min or 30
                    if self._has_overlap(saved_staff, r.date_str, fit_u, fit_u + svc_u):
                        fit_u = None
                else:
                    fit_u = self._can_insert(victim_staff, r)

                if fit_u is None:
                    # U が入らない → ロールバック
                    victim.staff_id = saved_staff
                    victim.staff_name = saved_name
                    victim.start_min = saved_start
                    victim.end_min = saved_end
                    self._register_assignment(saved_staff, victim.date_str, victim_idx, pid=victim.pid)
                    continue

                # U を配置
                svc_u = r.service_min or 30
                r.staff_id = saved_staff
                r.staff_name = saved_name
                r.start_min = fit_u
                r.end_min = fit_u + svc_u
                self._register_assignment(saved_staff, r.date_str, idx, pid=r.pid)

                # victimを別スタッフに再配置
                victim_constraints = self._request_constraints.get(victim_idx, {})
                v_ng = list(victim_constraints.get("ng_staff_ids", []))
                v_sex = victim_constraints.get("sex_limit", "")
                victim_placed = False

                for alt_staff in self.staff_list:
                    if alt_staff.sid == saved_staff:
                        continue
                    if not self._is_staff_available_for_reinsertion(
                        alt_staff, victim, ng_staff_ids=v_ng, sex_limit=v_sex
                    ):
                        continue
                    alt_fit = self._can_insert(alt_staff, victim)
                    if alt_fit is not None:
                        svc_v = victim.service_min or 30
                        victim.staff_id = alt_staff.sid
                        victim.staff_name = alt_staff.name
                        victim.start_min = alt_fit
                        victim.end_min = alt_fit + svc_v
                        self._register_assignment(alt_staff.sid, victim.date_str, victim_idx, pid=victim.pid)
                        victim.note += f" [EjectionChain移動: {alt_staff.name}]"
                        victim_placed = True
                        break

                if victim_placed:
                    r.note += f" [EjectionChain入替: {saved_name}→移動]"
                    resolved += 1
                    break
                else:
                    # victimが再配置不可 → 全ロールバック
                    self._unregister_assignment(saved_staff, r.date_str, idx)
                    r.staff_id = ""
                    r.staff_name = ""
                    r.start_min = None
                    r.end_min = None
                    victim.staff_id = saved_staff
                    victim.staff_name = saved_name
                    victim.start_min = saved_start
                    victim.end_min = saved_end
                    self._register_assignment(saved_staff, victim.date_str, victim_idx, pid=victim.pid)
                    continue

        return resolved

    def _relaxed_reinsertion(self, unassigned_indices: List[int]) -> int:
        """Final pass with progressively relaxed constraints.

        Relaxation levels:
          Level 1: Use max_per_day instead of soft_cap
          Level 2: Expand time window (午前/午後 → 終日)
        """
        resolved = 0

        for idx in unassigned_indices:
            r = self.results[idx]
            if r.staff_id or r.is_event:
                continue

            constraints = self._request_constraints.get(idx, {})
            ng_ids = list(constraints.get("ng_staff_ids", []))
            sex_lim = constraints.get("sex_limit", "")

            if r.is_coupled and r.visit_id:
                m = _COUPLED_RE.match(r.visit_id)
                if m:
                    base_id = m.group(1)
                    for other_r in self.results:
                        if (other_r.visit_id and other_r.visit_id != r.visit_id
                                and other_r.visit_id.startswith(base_id + "-")
                                and other_r.staff_id):
                            ng_ids.append(other_r.staff_id)

            # --- Relaxation Level 1: max_per_day (no soft_cap) ---
            for staff in self.staff_list:
                if staff.sid in ng_ids:
                    continue
                if sex_lim == "女性のみ" and staff.gender != "女性":
                    continue
                if sex_lim == "男性のみ" and staff.gender != "男性":
                    continue
                if staff.work_days and r.weekday not in staff.work_days:
                    continue
                key = f"{staff.sid}|{r.date_str}"
                if self.assign_count.get(key, 0) >= staff.max_per_day:
                    continue
                blocked = self._get_blocked_intervals(staff.sid, r.date_str)
                if any(iv.start <= 0 and iv.end >= 1440 for iv in blocked):
                    continue

                fit_start = self._can_insert(staff, r)
                if fit_start is not None:
                    svc = r.service_min or 30
                    r.staff_id = staff.sid
                    r.staff_name = staff.name
                    r.start_min = fit_start
                    r.end_min = fit_start + svc
                    self._register_assignment(staff.sid, r.date_str, idx, pid=r.pid)
                    r.note += f" [緩和挿入L1: {staff.name}]"
                    resolved += 1
                    break

            if r.staff_id:
                continue

            # --- Relaxation Level 2: 時間窓拡大（午前/午後→終日） ---
            # 固定時刻は絶対に緩和しない
            if r.time_type == "固定":
                continue
            saved_tt = r.time_type
            saved_earliest = r.earliest_min
            saved_latest = r.latest_min
            r.time_type = "終日"
            r.earliest_min = None
            r.latest_min = None

            for staff in self.staff_list:
                if staff.sid in ng_ids:
                    continue
                if sex_lim == "女性のみ" and staff.gender != "女性":
                    continue
                if sex_lim == "男性のみ" and staff.gender != "男性":
                    continue
                if staff.work_days and r.weekday not in staff.work_days:
                    continue
                key = f"{staff.sid}|{r.date_str}"
                if self.assign_count.get(key, 0) >= staff.max_per_day:
                    continue
                blocked = self._get_blocked_intervals(staff.sid, r.date_str)
                if any(iv.start <= 0 and iv.end >= 1440 for iv in blocked):
                    continue

                fit_start = self._can_insert(staff, r)
                if fit_start is not None:
                    svc = r.service_min or 30
                    r.staff_id = staff.sid
                    r.staff_name = staff.name
                    r.start_min = fit_start
                    r.end_min = fit_start + svc
                    self._register_assignment(staff.sid, r.date_str, idx, pid=r.pid)
                    r.note += f" [緩和挿入L2: {staff.name}, 時間窓拡大({saved_tt}→終日)]"
                    resolved += 1
                    break

            if not r.staff_id:
                # 復元（割当できなかった場合）
                r.time_type = saved_tt
                r.earliest_min = saved_earliest
                r.latest_min = saved_latest

        return resolved
