"""
allocation_utils.py - Utility functions for the allocation engine.
"""
import math
from typing import List, Optional, Tuple
from .allocation_models import Interval


# ============================================================
# Constants
# ============================================================
EXTRA_BUFFER_MIN = 15     # Buffer between visits in gap packing
ASSIGN_BUFFER_MIN = 5     # Buffer within window capacity calc
EARTH_RADIUS_KM = 6371.0  # Earth radius for Haversine
MAX_TWO_OPT_ITER = 50     # Max 2-opt iterations
TWO_OPT_THRESHOLD = 1e-9  # Improvement threshold for 2-opt

# Default time windows by time type (in minutes from midnight)
TIME_TYPE_DEFAULTS = {
    "午前": (540, 720),     # 09:00 - 12:00
    "午後": (780, 1020),    # 13:00 - 17:00
    "終日": (540, 1080),    # 09:00 - 18:00
}
DEFAULT_WINDOW = (540, 1080)  # fallback: 09:00 - 18:00

# Distance scoring thresholds
DIST_SCORE_THRESHOLDS = [
    (2.0, 0),   # <= 2km: score 0
    (5.0, 1),   # <= 5km: score 1
    (10.0, 2),  # <= 10km: score 2
]
DIST_SCORE_FAR = 5         # > 10km
DIST_SCORE_UNKNOWN = 99    # no coordinates


# ============================================================
# Distance
# ============================================================
def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate great-circle distance between two points using Haversine formula."""
    to_rad = math.pi / 180.0
    d_lat = (lat2 - lat1) * to_rad
    d_lng = (lng2 - lng1) * to_rad
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(lat1 * to_rad) * math.cos(lat2 * to_rad) *
         math.sin(d_lng / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c


def calc_distance_km(lat1: Optional[float], lng1: Optional[float],
                     lat2: Optional[float], lng2: Optional[float]) -> Optional[float]:
    """Calculate distance, returns None if any coordinate is missing."""
    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return None
    return haversine_km(lat1, lng1, lat2, lng2)


def dist_to_score(km: Optional[float]) -> int:
    """Convert distance in km to integer score."""
    if km is None:
        return DIST_SCORE_UNKNOWN
    for threshold, score in DIST_SCORE_THRESHOLDS:
        if km <= threshold:
            return score
    return DIST_SCORE_FAR


# ============================================================
# Time Interval Operations
# ============================================================
def intervals_overlap(s1: int, e1: int, s2: int, e2: int) -> bool:
    """Check if two intervals overlap (strict, not touching)."""
    return s1 < e2 and s2 < e1


def merge_intervals(intervals: List[Interval]) -> List[Interval]:
    """Sort and merge overlapping/adjacent intervals."""
    if len(intervals) <= 1:
        return list(intervals)
    sorted_ivs = sorted(intervals, key=lambda iv: iv.start)
    merged = [Interval(sorted_ivs[0].start, sorted_ivs[0].end)]
    for iv in sorted_ivs[1:]:
        last = merged[-1]
        if iv.start <= last.end:  # overlapping or adjacent
            last.end = max(last.end, iv.end)
        else:
            merged.append(Interval(iv.start, iv.end))
    return merged


def compute_gaps(blocked: List[Interval], window_start: int, window_end: int) -> List[Interval]:
    """Compute free gaps in a window given blocked intervals."""
    merged = merge_intervals(blocked)
    gaps = []
    cursor = window_start
    for iv in merged:
        if cursor < iv.start:
            gaps.append(Interval(cursor, iv.start))
        cursor = max(cursor, iv.end)
    if cursor < window_end:
        gaps.append(Interval(cursor, window_end))
    return gaps


def intersect_gaps(gaps1: List[Interval], gaps2: List[Interval]) -> List[Interval]:
    """Find common free intervals between two gap lists."""
    result = []
    i, j = 0, 0
    while i < len(gaps1) and j < len(gaps2):
        start = max(gaps1[i].start, gaps2[j].start)
        end = min(gaps1[i].end, gaps2[j].end)
        if start < end:
            result.append(Interval(start, end))
        if gaps1[i].end < gaps2[j].end:
            i += 1
        else:
            j += 1
    return result


def get_effective_window(time_type: str, earliest: Optional[int],
                         latest: Optional[int]) -> Tuple[int, int]:
    """Get effective time window from time_type and explicit preferences."""
    eff_earliest = earliest
    eff_latest = latest
    if eff_earliest is None or eff_latest is None:
        defaults = TIME_TYPE_DEFAULTS.get(time_type, DEFAULT_WINDOW)
        if eff_earliest is None:
            eff_earliest = defaults[0]
        if eff_latest is None:
            eff_latest = defaults[1]
    return (eff_earliest, eff_latest)


# ============================================================
# Route Optimization
# ============================================================
def nearest_neighbor_route(nodes: List[dict], start_lat: Optional[float] = None,
                           start_lng: Optional[float] = None) -> List[dict]:
    """Greedy nearest-neighbor route construction.

    Each node: {"idx": int, "lat": float|None, "lng": float|None, ...}
    """
    if len(nodes) <= 1:
        return list(nodes)

    rest = list(nodes)
    route = []
    curr_lat = start_lat if start_lat is not None else (rest[0].get("lat") if rest else None)
    curr_lng = start_lng if start_lng is not None else (rest[0].get("lng") if rest else None)

    while rest:
        best_idx = 0
        best_dist = float("inf")
        for i, node in enumerate(rest):
            d = calc_distance_km(curr_lat, curr_lng, node.get("lat"), node.get("lng"))
            dd = d if d is not None else 99999.0
            if dd < best_dist:
                best_dist = dd
                best_idx = i
        pick = rest.pop(best_idx)
        route.append(pick)
        if pick.get("lat") is not None:
            curr_lat = pick["lat"]
            curr_lng = pick["lng"]

    return route


def route_length(nodes: List[dict]) -> float:
    """Sum of edge distances in a route."""
    total = 0.0
    for i in range(len(nodes) - 1):
        d = calc_distance_km(
            nodes[i].get("lat"), nodes[i].get("lng"),
            nodes[i+1].get("lat"), nodes[i+1].get("lng")
        )
        if d is not None:
            total += d
    return total


def two_opt(nodes: List[dict]) -> List[dict]:
    """Apply 2-opt local search to improve route."""
    if len(nodes) <= 3:
        return list(nodes)

    best = list(nodes)
    best_len = route_length(best)
    improved = True
    iterations = 0

    while improved and iterations < MAX_TWO_OPT_ITER:
        iterations += 1
        improved = False
        for i in range(1, len(best) - 1):
            for k in range(i + 1, len(best)):
                candidate = best[:i] + best[i:k+1][::-1] + best[k+1:]
                cand_len = route_length(candidate)
                if cand_len + TWO_OPT_THRESHOLD < best_len:
                    best = candidate
                    best_len = cand_len
                    improved = True

    return best


# ============================================================
# Normalization helpers
# ============================================================
def normalize_cont_pref(raw: str) -> str:
    """Normalize continuation preference string."""
    if not raw:
        return ""
    v = raw.strip()
    if v in ("同じ人", "同じ人希望"):
        return "同じ人希望"
    if v in ("ローテーション", "ローテーション優先"):
        return "ローテーション優先"
    if v == "どちらでも":
        return "どちらでも"
    return v


def normalize_weekday(raw: str) -> str:
    """Normalize weekday string to English 3-letter code."""
    mapping = {
        "月": "Mon", "火": "Tue", "水": "Wed", "木": "Thu",
        "金": "Fri", "土": "Sat", "日": "Sun",
        "Mon": "Mon", "Tue": "Tue", "Wed": "Wed", "Thu": "Thu",
        "Fri": "Fri", "Sat": "Sat", "Sun": "Sun",
        "Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed",
        "Thursday": "Thu", "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun",
    }
    return mapping.get(raw.strip(), raw.strip())


def weekday_index(day_code: str) -> int:
    """Convert day code to index (Sun=0, Mon=1, ..., Sat=6) matching JS getDay()."""
    idx_map = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
    return idx_map.get(day_code, 0)


def fmt_min(minutes: Optional[int]) -> str:
    """Format minutes as HH:MM string."""
    if minutes is None:
        return "--:--"
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}"
