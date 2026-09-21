"""
profile_history.py -- Derives distinct, human-meaningful profile "eras" from
the Profile Switch treatment stream, for the Profiles page.

Background: AAPS logs a Profile Switch treatment on every quick-adjust
percentage change (e.g. "Tuned 29/4/26 (85%)") as well as on genuine
saved-profile activations/edits. Those percentage-only events reference the
exact same underlying basal/ISF/IC/target document as their parent profile --
only the runtime `percentage` field differs -- and the percentage is
sometimes baked into the display name, which is not a reliable signal either
way (a manually-created profile can legitimately be named with "(NN%)" in
it). So a new era is only started when the underlying *content* actually
changes (dia/basal/sens/carbratio/target_low/target_high), not on every
Profile Switch row and not by pattern-matching the name.

Eras shorter than MIN_ERA_SECONDS are dropped as manual-editing/tuning
noise (rapid consecutive saves while live-editing a profile). A dropped era
leaves a small gap in the timeline; it is not absorbed into a neighbour.
"""
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import config

MIN_ERA_SECONDS = 3600  # eras active for less than this are excluded

# Fields that define a genuinely different *saved* profile. percentage/
# duration/timeshift/notes/_id are deliberately excluded -- those vary on
# every quick-adjust switch even when the underlying profile is unchanged.
CONTENT_FIELDS = ("dia", "basal", "sens", "carbratio", "target_low", "target_high")


def _content_hash(profile_json_str: Optional[str]) -> Optional[str]:
    """Hash only the fields that define the saved profile itself, ignoring
    runtime percentage/duration/timeshift and any name/_id/notes noise."""
    if not profile_json_str:
        return None
    try:
        doc = json.loads(profile_json_str)
    except (TypeError, ValueError):
        return None
    subset = {k: doc.get(k) for k in CONTENT_FIELDS}
    canonical = json.dumps(subset, sort_keys=True, default=str)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def _field_key(value: Any) -> str:
    """Canonical string for equality-comparing one profile field between
    two eras (order/whitespace-insensitive)."""
    return json.dumps(value, sort_keys=True, default=str)


def _diff_fields(doc: Dict[str, Any], prev_doc: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """Per-field changed flags vs the chronologically-previous era. The very
    first era in the account's history (prev_doc is None) has nothing to
    compare against, so nothing is flagged as changed."""
    if prev_doc is None:
        return {"dia": False, "ic": False, "isf": False, "basal": False, "target": False}
    target_key = _field_key((doc.get("target_low"), doc.get("target_high")))
    prev_target_key = _field_key((prev_doc.get("target_low"), prev_doc.get("target_high")))
    return {
        "dia": doc.get("dia") != prev_doc.get("dia"),
        "ic": _field_key(doc.get("carbratio")) != _field_key(prev_doc.get("carbratio")),
        "isf": _field_key(doc.get("sens")) != _field_key(prev_doc.get("sens")),
        "basal": _field_key(doc.get("basal")) != _field_key(prev_doc.get("basal")),
        "target": target_key != prev_target_key,
    }


def _format_lines(entries: Any, suffix: str = "") -> List[str]:
    """Time-of-day array (basal/sens/carbratio) -> 'HH:MM  value<suffix>'
    lines, one per distinct authored entry, in time order. A profile with a
    single flat value for the whole day produces exactly one line."""
    if not entries:
        return []
    lines = []
    for e in sorted(entries, key=lambda x: x.get("timeAsSeconds", 0)):
        t = e.get("time", "")
        v = e.get("value")
        if v is None:
            continue
        lines.append(f"{t}  {v}{suffix}")
    return lines


def _format_target_lines(low_entries: Any, high_entries: Any, unit_suffix: str = "") -> List[str]:
    """target_low/target_high are stored as two parallel time-of-day arrays;
    pair them by timeAsSeconds into single 'HH:MM  low - high' lines."""
    if not low_entries and not high_entries:
        return []
    by_time: Dict[int, Dict[str, Any]] = {}
    for e in (low_entries or []):
        by_time.setdefault(e.get("timeAsSeconds", 0), {})["low"] = e
    for e in (high_entries or []):
        by_time.setdefault(e.get("timeAsSeconds", 0), {})["high"] = e
    lines = []
    for sec in sorted(by_time.keys()):
        pair = by_time[sec]
        t = (pair.get("low") or pair.get("high") or {}).get("time", "")
        low_v = (pair.get("low") or {}).get("value")
        high_v = (pair.get("high") or {}).get("value")
        lines.append(f"{t}  {low_v} - {high_v}{unit_suffix}")
    return lines


def _basal_series(entries: Any) -> List[Dict[str, Any]]:
    """basal array -> [{seconds, rate}, ...] sorted by time-of-day, for the
    24h step chart. A single-entry profile still yields one point; the
    frontend is responsible for extending it flat across the day."""
    if not entries:
        return []
    return [
        {"seconds": e.get("timeAsSeconds", 0), "rate": e.get("value")}
        for e in sorted(entries, key=lambda x: x.get("timeAsSeconds", 0))
    ]


def get_profile_history(conn, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Return distinct saved-profile eras overlapping [start_date, end_date],
    newest first.

    Pulls the ENTIRE Profile Switch history, not just up to end_date --
    deliberately not bounded by the requested window at all. Two separate
    things need history outside the window and would both break if the
    query were truncated at end_date:
      1. an era that began before start_date needs its earlier start_ts to
         identify it as one continuous era (not appear to start at the
         window boundary) -- same principle layer2_profile_schedule uses.
      2. the era active at end_date needs to see whatever switch happened
         AFTER end_date to correctly close out its own "to" time. Bounding
         the fetch at end_date made that switch invisible, so the last era
         in range always looked like it was still active ("to: present")
         even when a later real change was known to exist -- exactly the
         bug Harry hit querying July 2026 for a profile that in reality
         changed again in mid-August.
    The window-overlap filter (Pass 4 below) still limits what's actually
    *displayed* to eras overlapping [start_date, end_date]; only the
    fetch itself is unbounded.
    """
    query = """
        SELECT
            ts,
            raw_json->>'profile' AS profile_name,
            raw_json->>'profileJson' AS profile_json
        FROM treatments
        WHERE event_type = 'Profile Switch'
          AND raw_json->>'profileJson' IS NOT NULL
        ORDER BY ts ASC
    """
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    if not rows:
        return []

    # --- Pass 1: collapse consecutive rows sharing the same content hash ----
    # This is what absorbs every percentage-only quick-adjust switch,
    # regardless of what the display name looks like.
    raw_segments: List[Dict[str, Any]] = []
    prev_hash = None
    for ts, name, profile_json in rows:
        h = _content_hash(profile_json)
        if h is None:
            continue
        if h != prev_hash:
            raw_segments.append({
                "name": name,
                "start_ts": ts,
                "content_hash": h,
                "profile_json": profile_json,
            })
            prev_hash = h
        # else: identical content to the currently-running era (pure
        # percentage/duration noise, or an identical re-save) -- absorbed.

    if not raw_segments:
        return []

    for i in range(len(raw_segments) - 1):
        raw_segments[i]["end_ts"] = raw_segments[i + 1]["start_ts"]
    raw_segments[-1]["end_ts"] = None  # still active as of end_date

    # --- Pass 2: drop eras shorter than MIN_ERA_SECONDS ----------------------
    # Dropped eras leave a small gap; neighbouring eras' boundaries are left
    # exactly as they were (no absorption), per Harry's call.
    kept = []
    for seg in raw_segments:
        if seg["end_ts"] is not None:
            duration_s = (seg["end_ts"] - seg["start_ts"]).total_seconds()
            if duration_s < MIN_ERA_SECONDS:
                continue
        kept.append(seg)

    # --- Pass 3: per-field changed-vs-previous-era flags ----------------------
    # Computed on the full chronological `kept` list (not the window-filtered
    # one) so the oldest era visible in a given range still compares
    # correctly against whatever came immediately before it, even if that
    # prior era itself falls outside the requested window.
    prev_doc: Optional[Dict[str, Any]] = None
    for seg in kept:
        try:
            doc = json.loads(seg["profile_json"])
        except (TypeError, ValueError):
            doc = {}
        seg["doc"] = doc
        seg["changed"] = _diff_fields(doc, prev_doc)
        prev_doc = doc

    # --- Pass 4: keep only eras overlapping the requested window -------------
    tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
    window_start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
    window_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz)

    overlapping = [
        seg for seg in kept
        if seg["start_ts"] < window_end and (seg["end_ts"] is None or seg["end_ts"] > window_start)
    ]
    # Sort on the real timestamp, not the formatted display string below --
    # "01 Sep 2026" doesn't sort correctly as text (e.g. "05 Oct 2026" would
    # sort before "20 Jan 2027"), unlike the ISO format this replaced.
    overlapping.sort(key=lambda seg: seg["start_ts"], reverse=True)

    # --- Pass 5: format for display, newest first -----------------------------
    def fmt_local(dt) -> str:
        # "01 Sep 2026 14:30" -- date as requested (dd Mon yyyy), time stays 24h.
        return dt.astimezone(tz).strftime("%d %b %Y %H:%M")

    results = []
    for seg in overlapping:
        doc = seg["doc"]
        units = doc.get("units", "")
        isf_suffix = f" {units}/U" if units else ""
        target_suffix = f" {units}" if units else ""

        results.append({
            "name": seg["name"],
            "from": fmt_local(seg["start_ts"]),
            "to": fmt_local(seg["end_ts"]) if seg["end_ts"] is not None else None,
            "dia": doc.get("dia"),
            "ic_lines": _format_lines(doc.get("carbratio"), suffix=" g/U"),
            "isf_lines": _format_lines(doc.get("sens"), suffix=isf_suffix),
            "basal_lines": _format_lines(doc.get("basal"), suffix=" U/hr"),
            "target_lines": _format_target_lines(doc.get("target_low"), doc.get("target_high"), unit_suffix=target_suffix),
            "basal_series": _basal_series(doc.get("basal")),
            "changed": seg["changed"],
        })

    return results
