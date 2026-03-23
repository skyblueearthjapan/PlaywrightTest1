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
        # Smart Allocation Engine (安定版)
        # Phase 1: 制約厳しい順ソートで初期割当
        # Phase 2: Level 1 全スタッフ再挿入
        # Phase 3: Ejection Chain
        # Phase 4: 段階的制約緩和
        # ==============================================================

        # Step 1 - events
        self._insert_events()

        # Step 2 - sort (制約が厳しいリクエストを先に処理)
        sorted_requests = self._sort_requests_smart(active_requests)

        # Step 3 - Level 0: 初期割当（即座に時間確定）
        logger.info("Level 0: Allocating %d requests...", len(sorted_requests))
        self._level0_allocate(sorted_requests)
        self._enforce_coupled_atomicity()
        self._sync_coupled_times()

        # Step 4 - GapPack (時間の詰め直し)
        logger.info("GapPack: Refining time assignments...")
        self._gap_pack()
        self._sync_coupled_times()

        # Step 5 - Level 1: 全スタッフ再挿入
        unassigned_indices = [
            i for i, r in enumerate(self.results)
            if not r.staff_id and not r.is_event
        ]
        if unassigned_indices:
            logger.info("Level 1: Reinsertion for %d visits...", len(unassigned_indices))
            self._level1_reinsertion(unassigned_indices)
            self._sync_coupled_times()

        # Step 6 - Ejection Chain（入替え挿入）
        remaining_unassigned = [
            i for i, r in enumerate(self.results)
            if not r.staff_id and not r.is_event
        ]
        if remaining_unassigned:
            logger.info("Ejection Chain: %d visits...", len(remaining_unassigned))
            ejected = self._ejection_chain(remaining_unassigned)
            if ejected > 0:
                self._sync_coupled_times()
                logger.info("Ejection Chain: resolved %d visits", ejected)

        # Step 7 - 段階的制約緩和
        still_unassigned = [
            i for i, r in enumerate(self.results)
            if not r.staff_id and not r.is_event
        ]
        if still_unassigned:
            logger.info("Relaxed Pass: %d visits...", len(still_unassigned))
            relaxed = self._relaxed_reinsertion(still_unassigned)
            if relaxed > 0:
                self._sync_coupled_times()
                logger.info("Relaxed Pass: resolved %d visits", relaxed)

        # Step 7.5 - 曜日シフト戦略（希望日以外への振替）
        day_shift_unassigned = [
            i for i, r in enumerate(self.results)
            if not r.staff_id and not r.is_event
        ]
        if day_shift_unassigned:
            logger.info("Day Shift: Attempting day redistribution for %d visits...",
                        len(day_shift_unassigned))
            shifted = self._day_shift_strategy(day_shift_unassigned, active_requests)
            if shifted > 0:
                self._enforce_coupled_atomicity()
                self._sync_coupled_times()
                logger.info("Day Shift: resolved %d visits", shifted)

        # Step 8 - route optimisation
        logger.info("Level 3: Optimizing routes...")
        self._level3_route_optimize()
        self._sync_coupled_times()

        # Step 9 - overlap fix
        self._fix_cross_patient_overlaps()
        self._sync_coupled_times()

        # Step 10 - mentor pair expansion (post-processing)
        self._apply_mentor_pairs()

        # Step 11 - 最終安全ネット
        self._final_overlap_sweep()
        self._enforce_coupled_atomicity(allow_partial=True)

        # Step 12 - 最終曜日シフト（overlap sweepで解除された訪問を救済）
        final_unassigned = [
            i for i, r in enumerate(self.results)
            if not r.staff_id and not r.is_event
        ]
        if final_unassigned:
            logger.info("Final Day Shift: %d visits after overlap sweep...", len(final_unassigned))
            shifted = self._day_shift_strategy(final_unassigned, active_requests)
            if shifted > 0:
                self._sync_coupled_times()
                self._enforce_coupled_atomicity(allow_partial=True)
                logger.info("Final Day Shift: resolved %d visits", shifted)

        # Step 13 - 2名体制の不足分を未割当として追加
        self._add_missing_coupled_entries(active_requests)

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

        # デバッグ: 未割当の内訳
        unassigned_detail = {}
        for r in self.results:
            if not r.staff_id and not r.is_event:
                key = f"{r.pid}({r.pname})|{r.date_str}"
                unassigned_detail[key] = unassigned_detail.get(key, 0) + 1
        debug_notes = []
        for r in self.results:
            if r.note and ("[曜日シフト" in r.note or "[DayShift" in r.note):
                debug_notes.append(f"{r.pid}: {r.note[:60]}")
        # 曜日シフト失敗ログも収集
        day_shift_failures = getattr(self, '_day_shift_failures', [])

        return {
            "results": self.results,
            "unassigned": self.unassigned,
            "summary": {
                "total": len(active_requests),
                "assigned": assigned_count,
                "unassigned": unassigned_count,
                "unassigned_detail": unassigned_detail,
                "day_shifts": debug_notes,
                "day_shift_failures": day_shift_failures[:30],
                "coupled_debug": getattr(self, '_coupled_debug', {}),
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

    def _sort_requests_smart(self, requests: List[VisitRequest]) -> List[VisitRequest]:
        """Sort by constraint tightness: hardest to place first."""
        def _key(r: VisitRequest) -> tuple:
            # 候補スタッフ数を事前計算（少ないほど先に処理）
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
            return (eligible, r.date_str, -r.need_staff, r.start_min or 9999)
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
        # GASはstart_time_minutesではなくearliest_time_minutesに固定時刻を送る場合がある
        fixed_start = req.start_min
        if fixed_start is None and req.time_type == "固定" and req.earliest_min is not None:
            fixed_start = req.earliest_min
        fixed_end = req.end_min
        if fixed_end is None and fixed_start is not None:
            fixed_end = fixed_start + (req.service_min or 30)

        if req.time_type == "固定" and fixed_start is not None and fixed_end is not None:
            for iv in blocked:
                if intervals_overlap(fixed_start, fixed_end, iv.start, iv.end):
                    return False
            if fixed_start < staff.shift_start_min or fixed_end > staff.shift_end_min:
                return False
            return not self._has_overlap(staff.sid, req.date_str, fixed_start, fixed_end)

        # Flexible time: check if service fits in any available gap
        eff_earliest, eff_latest = get_effective_window(
            req.time_type, req.earliest_min, req.latest_min,
        )
        eff_earliest = max(eff_earliest, staff.shift_start_min, 540)
        eff_latest = min(eff_latest, staff.shift_end_min)

        all_blocked = list(blocked)
        # Include existing visit times as blocked (重複防止の核心)
        # EXTRA_BUFFER_MINを含めて_can_insertと整合性を取る
        key = f"{staff.sid}|{req.date_str}"
        for idx in self.staff_day_visits.get(key, []):
            r = self.results[idx]
            if r.start_min is not None and r.end_min is not None:
                all_blocked.append(Interval(
                    r.start_min - EXTRA_BUFFER_MIN,
                    r.end_min + EXTRA_BUFFER_MIN,
                ))

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

    def _has_overlap(
        self, staff_id: str, date_str: str, start: int, end: int,
        exclude_indices: Optional[Set[int]] = None,
    ) -> bool:
        """Return True if ``[start, end)`` overlaps any existing visit for the staff.

        Args:
            exclude_indices: result indices to skip (e.g. the visit being moved).
        """
        key = f"{staff_id}|{date_str}"
        for idx in self.staff_day_visits.get(key, []):
            if exclude_indices and idx in exclude_indices:
                continue
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

            # 自分自身のペアindexを除外して重複チェック
            pair_indices = set(indices)

            if target_end <= common_latest:
                no_overlap_1 = not self._has_overlap(
                    r1.staff_id, r1.date_str, target, target_end,
                    exclude_indices=pair_indices,
                )
                no_overlap_2 = not self._has_overlap(
                    r2.staff_id, r2.date_str, target, target_end,
                    exclude_indices=pair_indices,
                )
                if no_overlap_1 and no_overlap_2:
                    r1.start_min = r2.start_min = target
                    r1.end_min = r2.end_min = target_end
                    continue

            # Fallback: force to common earliest (重複チェック付き)
            fb_end = common_earliest + svc
            fb_ok_1 = not self._has_overlap(
                r1.staff_id, r1.date_str, common_earliest, fb_end,
                exclude_indices=pair_indices,
            )
            fb_ok_2 = not self._has_overlap(
                r2.staff_id, r2.date_str, common_earliest, fb_end,
                exclude_indices=pair_indices,
            )
            if fb_ok_1 and fb_ok_2:
                r1.start_min = r2.start_min = common_earliest
                r1.end_min = r2.end_min = fb_end
                r1.note += " [時刻同期]"
                r2.note += " [時刻同期]"
            else:
                # 重複回避不可 → 時刻を変えず警告のみ
                logger.warning(
                    "CoupledSync: could not sync %s without overlap, skipping",
                    base_id,
                )

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
                # 固定時刻・イベント・2名体制(時刻確定)のみアンカー
                # 柔軟訪問(終日/午前/午後/時間帯)はflex → 時間調整可能
                is_anchor = (
                    r.is_event
                    or (r.time_type == "固定" and has_time)
                    or (r.is_coupled and has_time)
                )
                if is_anchor:
                    anchors.append((idx, r.start_min, r.end_min))  # type: ignore[arg-type]
                elif has_time:
                    flexes.append(idx)  # 時間確定済みだが柔軟 → 再配置可能
                # start_min=Noneの訪問もflex
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
                    # 同一ギャップ内でリトライ（重複箇所をスキップして再試行）
                    while True:
                        start_cand = max(gap_cursors[gi], eff_earliest)
                        end_cand = start_cand + svc

                        if end_cand > gap.end:
                            break  # このギャップには入らない

                        if eff_latest is not None and end_cand > eff_latest:
                            break

                        if start_cand >= gap.start and end_cand <= gap.end:
                            # 重複チェック（他の訪問との衝突を防止）
                            if self._has_overlap(
                                staff_id, r.date_str, start_cand, end_cand,
                                exclude_indices={f_idx},
                            ):
                                # この位置は使えない → カーソルを進めて同ギャップ内リトライ
                                gap_cursors[gi] = end_cand + EXTRA_BUFFER_MIN
                                continue
                            r.start_min = start_cand
                            r.end_min = end_cand
                            gap_cursors[gi] = end_cand + EXTRA_BUFFER_MIN
                            placed = True
                            break
                        else:
                            break
                    if placed:
                        break

                if not placed:
                    logger.debug(
                        "GapPack: visit %s could not fit, unassigning", r.visit_id,
                    )
                    if r.staff_id:
                        self._unregister_assignment(r.staff_id, r.date_str, f_idx)
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
            # 全スタッフを候補に（Top10制限を撤廃）

            for staff, _ in candidates:
                # 固定時刻の場合はその時刻を尊重
                if r.time_type == "固定" and r.earliest_min is not None:
                    fit_start = r.earliest_min
                    svc = r.service_min or 30
                    # シフト境界チェック
                    if fit_start < staff.shift_start_min or fit_start + svc > staff.shift_end_min:
                        continue
                    # blocked区間チェック（休み/午前休等）
                    blocked = self._get_blocked_intervals(staff.sid, r.date_str)
                    if any(intervals_overlap(fit_start, fit_start + svc, b.start, b.end) for b in blocked):
                        continue
                    # 重複チェック
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

        # 固定時刻: earliest_minからの直接配置を試みる
        fixed_start = None
        if r.time_type == "固定" and r.earliest_min is not None:
            fixed_start = r.earliest_min
        elif r.time_type == "固定" and r.start_min is not None:
            fixed_start = r.start_min
        if fixed_start is not None:
            fixed_end = fixed_start + svc
            if fixed_start >= staff.shift_start_min and fixed_end <= staff.shift_end_min:
                if not self._has_overlap(staff.sid, r.date_str, fixed_start, fixed_end):
                    blocked = self._get_blocked_intervals(staff.sid, r.date_str)
                    if not any(intervals_overlap(fixed_start, fixed_end, b.start, b.end) for b in blocked):
                        return fixed_start
            return None  # 固定時刻で入れなければ他の時間は試さない
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

    def _enforce_coupled_atomicity(self, allow_partial: bool = False) -> None:
        """If one half of a 2-staff pair is unassigned, handle based on mode.

        Args:
            allow_partial: If True, keep the assigned half with a warning.
                           If False (default), unassign both halves.
        """
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
                if allow_partial:
                    # 1名のみ割当を許可（警告付き）
                    assigned_r = None
                    has_unassigned_slot = False
                    for idx in indices:
                        r = self.results[idx]
                        if not r.staff_id:
                            r.note += " [2名体制:1名のみ割当]"
                            has_unassigned_slot = True
                        else:
                            r.note += " [2名体制:1名のみ割当(相方未確保)]"
                            assigned_r = r
                    # 未割当スロットがない場合（曜日シフト等で移動済み）
                    # → 割当済み側の日付に未割当エントリを追加
                    if not has_unassigned_slot and assigned_r:
                        placeholder = AssignmentResult(
                            visit_id=f"{assigned_r.visit_id}_NEED2",
                            date_str=assigned_r.date_str,
                            weekday=assigned_r.weekday,
                            pid=assigned_r.pid,
                            pname=assigned_r.pname,
                            area=assigned_r.area,
                            service_min=assigned_r.service_min,
                            time_type=assigned_r.time_type,
                            is_coupled=True,
                            note="[2名体制:2人目未割当]",
                        )
                        self.results.append(placeholder)
                else:
                    # 両方解除（厳格モード）
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
    # Day Shift Strategy (曜日シフト戦略)
    # ==================================================================
    def _day_shift_strategy(
        self, unassigned_indices: List[int], all_requests: List[VisitRequest],
    ) -> int:
        """Shift unassigned visits to less crowded days of the week.

        The patient's preferred days are soft constraints. When a visit
        can't be placed on the preferred day, try other weekdays that:
        - Are within the same week
        - Have lower staff load
        - Don't violate NG days or other hard constraints
        """
        from datetime import datetime, timedelta

        resolved = 0
        self._day_shift_failures = []

        # Build a map of ALL dates in this week (Mon-Sun)
        # 週の全7日を候補にする（リクエストにない日も含む）
        week_dates = set()
        sample_dates = set()
        for r in self.results:
            if r.date_str:
                sample_dates.add(r.date_str)
        for r in all_requests:
            if r.date_str:
                sample_dates.add(r.date_str)

        if not sample_dates:
            return 0

        # 最初の日付から週の月曜を計算し、7日分を生成
        first_date_str = min(sample_dates)
        try:
            parts = first_date_str.split("/")
            first_dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
            # 月曜に戻す
            monday = first_dt - timedelta(days=first_dt.weekday())
            for i in range(7):
                d = monday + timedelta(days=i)
                week_dates.add(d.strftime("%Y/%m/%d"))
        except (ValueError, IndexError):
            week_dates = sample_dates

        # Calculate load per date (how many visits are already assigned)
        date_load: Dict[str, int] = {}
        for d in sorted(week_dates):
            date_load[d] = sum(
                1 for r in self.results
                if r.staff_id and r.date_str == d and not r.is_event
            )

        # Parse dates for weekday calculation
        date_weekday: Dict[str, str] = {}
        weekday_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        for d in week_dates:
            try:
                parts = d.split("/")
                dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
                date_weekday[d] = weekday_names[dt.weekday()]
            except (ValueError, IndexError):
                pass

        # Group unassigned by patient+date
        # For coupled visits, include ALL slots (even if one was assigned)
        pid_date_unassigned: Dict[str, List[int]] = {}
        unassigned_set = set()
        for idx in unassigned_indices:
            r = self.results[idx]
            if r.staff_id or r.is_event:
                continue
            unassigned_set.add(idx)
            key = f"{r.pid}|{r.date_str}"
            pid_date_unassigned.setdefault(key, []).append(idx)

        # Coupled partners: don't modify existing assignments for now
        coupled_partners: Dict[str, List[int]] = {}  # empty - no partner gathering

        # Sort dates by load (least crowded first)
        sorted_dates = sorted(date_load.keys(), key=lambda d: date_load.get(d, 0))

        # 曜日優先度でソート（低→中→高: 低優先度を先にシフト）
        priority_order = {"低": 0, "中": 1, "高": 2, "": 0}
        sorted_groups = sorted(
            pid_date_unassigned.items(),
            key=lambda x: priority_order.get(
                getattr(self.patient_map.get(x[0].split("|")[0], None), "day_priority", "低"), 0
            ),
        )

        # Debug: P041が含まれているか確認
        p041_groups = [k for k in pid_date_unassigned.keys() if "P041" in k]
        p064_groups = [k for k in pid_date_unassigned.keys() if "P064" in k]

        # P041のresults状態を確認
        p041_results = [(i, r.visit_id, r.staff_id or "NONE", r.date_str, r.is_event)
                        for i, r in enumerate(self.results) if r.pid == "P041"]
        p041_in_unassigned = [i for i in unassigned_indices
                              if i < len(self.results) and self.results[i].pid == "P041"]
        self._day_shift_failures.append(
            f"GROUPS: total={len(sorted_groups)}, P041_groups={p041_groups}, "
            f"P041_results={p041_results[:6]}, P041_unassigned_idx={p041_in_unassigned}"
        )

        for pd_key, indices in sorted_groups:
            if not indices:
                continue

            pid = pd_key.split("|")[0]

            # Get patient info
            patient = self.patient_map.get(pid)
            if not patient:
                self._day_shift_failures.append(f"SKIP: {pd_key} - no patient master (pid={pid})")
                continue

            # Get the original date of the unassigned visit
            orig_r = self.results[indices[0]]
            orig_date = orig_r.date_str

            # Get constraints for this visit
            constraints = self._request_constraints.get(indices[0], {})
            ng_ids = constraints.get("ng_staff_ids", [])
            sex_lim = constraints.get("sex_limit", "")

            self._day_shift_failures.append(
                f"TRYING: {pid}|{orig_date} ({len(indices)} slots)"
            )

            # Try each date from least crowded
            for alt_date in sorted_dates:
                if alt_date == orig_date:
                    continue  # Already tried this date
                alt_weekday = date_weekday.get(alt_date, "")
                if not alt_weekday:
                    continue

                # Skip weekends (Sat/Sun)
                if alt_weekday in ("Sat", "Sun"):
                    continue

                # Check if this patient already has a visit on this date
                # (割当済み OR 曜日シフト済みの両方をチェック)
                already_on_date = any(
                    r.pid == pid and r.date_str == alt_date
                    for r in self.results
                    if r.staff_id  # 割当済みのみ
                )
                if already_on_date:
                    continue

                # For coupled visits, include partner slots
                is_coupled = orig_r.is_coupled
                all_slots = list(indices)
                partner_slots = coupled_partners.get(pd_key, [])
                if partner_slots:
                    all_slots.extend(partner_slots)
                need_staff = len(all_slots)

                # Find eligible staff for this date
                placed_staff = []
                all_placed = True
                fail_reasons = []

                for slot_idx in all_slots:
                    r = self.results[slot_idx]
                    found_staff = False

                    for staff in self.staff_list:
                        if staff.sid in ng_ids or staff.sid in [s.sid for s in placed_staff]:
                            continue
                        if sex_lim == "女性のみ" and staff.gender != "女性":
                            continue
                        if sex_lim == "男性のみ" and staff.gender != "男性":
                            continue
                        if staff.work_days and alt_weekday not in staff.work_days:
                            continue
                        # Check maxPerDay
                        key = f"{staff.sid}|{alt_date}"
                        if self.assign_count.get(key, 0) >= staff.soft_cap():
                            continue
                        # Check blocked (day off etc)
                        blocked = self._get_blocked_intervals(staff.sid, alt_date)
                        if any(iv.start <= 0 and iv.end >= 1440 for iv in blocked):
                            continue

                        # Create a temporary result for _can_insert
                        temp_r = AssignmentResult(
                            date_str=alt_date,
                            weekday=alt_weekday,
                            service_min=r.service_min,
                            time_type=r.time_type,
                            earliest_min=r.earliest_min,
                            latest_min=r.latest_min,
                        )
                        fit = self._can_insert(staff, temp_r)
                        if fit is not None:
                            placed_staff.append(staff)
                            found_staff = True
                            break

                    if not found_staff:
                        all_placed = False
                        fail_reasons.append(f"slot {slot_idx}: no staff on {alt_date}({alt_weekday})")
                        break

                if all_placed and len(placed_staff) >= need_staff:
                    # Unassign partner slots first (they were still assigned)
                    for pi in partner_slots:
                        pr = self.results[pi]
                        if pr.staff_id:
                            self._unregister_assignment(pr.staff_id, pr.date_str, pi)
                            pr.staff_id = ""
                            pr.staff_name = ""

                    # Assign all slots
                    for si, slot_idx in enumerate(all_slots):
                        r = self.results[slot_idx]
                        staff = placed_staff[si]
                        svc = r.service_min or 30

                        # Update date
                        r.date_str = alt_date
                        r.weekday = alt_weekday

                        # Find time slot
                        temp_r = AssignmentResult(
                            date_str=alt_date,
                            weekday=alt_weekday,
                            service_min=r.service_min,
                            time_type=r.time_type,
                            earliest_min=r.earliest_min,
                            latest_min=r.latest_min,
                        )
                        fit = self._can_insert(staff, temp_r)
                        if fit is not None:
                            r.staff_id = staff.sid
                            r.staff_name = staff.name
                            r.start_min = fit
                            r.end_min = fit + svc
                            self._register_assignment(staff.sid, alt_date, slot_idx, pid=pid)
                            r.note += f" [曜日シフト: {date_weekday.get(orig_date, '?')}→{alt_weekday}]"
                            resolved += 1

                    # Update date_load
                    date_load[alt_date] = date_load.get(alt_date, 0) + need_staff
                    logger.info("DayShift: SUCCESS %s %s→%s (%d staff)", pid, orig_date, alt_date, need_staff)
                    break  # Move to next patient
            else:
                # All dates tried, none worked
                msg = f"{pid} from {orig_date}: {'; '.join(fail_reasons[:3]) if fail_reasons else 'no alt dates'}"
                logger.info("DayShift: FAILED %s", msg)
                self._day_shift_failures.append(msg)

        return resolved

    # ==================================================================
    # Add missing coupled entries
    # ==================================================================
    def _add_missing_coupled_entries(self, requests: List[VisitRequest]) -> None:
        """For 2-staff visits with only 1 assigned, add unassigned entry.

        Scans by patient+date: if need_staff=2 but only 1 result has
        staff_id set, adds an explicit unassigned placeholder.
        """
        # Build need_staff map from original requests
        need_staff_map: Dict[str, int] = {}
        for req in requests:
            if req.need_staff >= 2:
                key = f"{req.pid}|{req.date_str}"
                need_staff_map[key] = req.need_staff

        # Count assigned per patient+date
        assigned_count_map: Dict[str, int] = {}
        assigned_info: Dict[str, AssignmentResult] = {}
        for r in self.results:
            if r.is_event or not r.is_coupled:
                continue
            key = f"{r.pid}|{r.date_str}"
            if r.staff_id:
                assigned_count_map[key] = assigned_count_map.get(key, 0) + 1
                assigned_info[key] = r

        # Also check day-shifted visits (they have different dates from original)
        for r in self.results:
            if r.is_event or not r.is_coupled or not r.staff_id:
                continue
            key = f"{r.pid}|{r.date_str}"
            if key not in need_staff_map:
                # This visit was day-shifted to a date not in original requests
                need_staff_map[key] = 2  # Assume 2-staff need

        # Add missing entries
        added = 0
        for key, need in need_staff_map.items():
            actual = assigned_count_map.get(key, 0)
            if 0 < actual < need:
                # Has some assigned but not enough
                ref = assigned_info.get(key)
                if not ref:
                    continue
                for _ in range(need - actual):
                    placeholder = AssignmentResult(
                        visit_id=f"{ref.visit_id}_NEED",
                        date_str=ref.date_str,
                        weekday=ref.weekday,
                        pid=ref.pid,
                        pname=ref.pname,
                        area=ref.area,
                        start_min=ref.start_min,
                        end_min=ref.end_min,
                        service_min=ref.service_min,
                        time_type=ref.time_type,
                        earliest_min=ref.earliest_min,
                        latest_min=ref.latest_min,
                        is_coupled=True,
                        note="[2名体制:2人目未割当]",
                    )
                    self.results.append(placeholder)
                    added += 1

        self._coupled_debug = {
            "need_staff_map_count": len(need_staff_map),
            "partial_found": [(k, assigned_count_map.get(k, 0), v)
                              for k, v in need_staff_map.items()
                              if 0 < assigned_count_map.get(k, 0) < v],
            "added": added,
        }
        if added:
            logger.info("Added %d missing coupled entries", added)

    # ==================================================================
    # Final Overlap Sweep (最終重複チェック＆修正)
    # ==================================================================
    def _final_overlap_sweep(self) -> None:
        """Final safety net: detect and fix any remaining time overlaps.

        Scans all staff-date combinations and removes overlapping visits
        (keeping the one with earlier visit_id).
        """
        # Build staff-date groups from results (not from staff_day_visits which may be stale)
        groups: Dict[str, List[int]] = {}
        for i, r in enumerate(self.results):
            if not r.staff_id or r.start_min is None or r.end_min is None:
                continue
            key = f"{r.staff_id}|{r.date_str}"
            groups.setdefault(key, []).append(i)

        fixed_count = 0
        for key, indices in groups.items():
            if len(indices) <= 1:
                continue
            # Sort by start time
            sorted_idx = sorted(indices, key=lambda i: self.results[i].start_min)
            occupied_end = 0
            for idx in sorted_idx:
                r = self.results[idx]
                if r.start_min < occupied_end:
                    # Overlap detected! Unassign this visit
                    logger.warning(
                        "FinalSweep: overlap detected for %s staff=%s %s-%s, unassigning",
                        r.visit_id, r.staff_id,
                        fmt_min(r.start_min), fmt_min(r.end_min),
                    )
                    r.staff_id = ""
                    r.staff_name = ""
                    r.note += " [最終重複チェック: 未割当]"
                    fixed_count += 1
                else:
                    occupied_end = r.end_min

        if fixed_count:
            logger.info("FinalSweep: %d overlapping visits unassigned", fixed_count)

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
                and ar.time_type != "固定"  # 固定時刻訪問は入替え対象外
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
                    # 固定時刻のvictimは元の時刻でのみ再配置可能
                    if victim.time_type == "固定" and saved_start is not None:
                        svc_v = victim.service_min or 30
                        if self._has_overlap(alt_staff.sid, victim.date_str, saved_start, saved_start + svc_v):
                            continue
                        alt_fit = saved_start
                    else:
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
            # 固定時刻・時間帯指定は絶対に緩和しない
            if r.time_type in ("固定", "時間帯"):
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
