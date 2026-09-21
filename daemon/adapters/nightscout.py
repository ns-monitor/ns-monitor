"""
Source adapter for Nightscout's REST API JSON shape.

Translates a raw Nightscout record into the canonical shape the rest of
the app understands. This is a behavior-preserving port of the parsing
logic that used to live inline in data_processor.py on v1 (see git log
on master for the original) -- output must match exactly; any behavior
change here should be a deliberate, reviewed decision, not a side effect
of restructuring. Verified against real historical data via
replay_poller.py's regression check (docs/analysis/phase1-ingestion-replay-poller-spec.md).

Each to_*() function follows: classify -> extract & validate -> normalize
-> map to canonical, and returns (record, reject_reason) -- record is
None when reject_reason is set, never both. Callers count/log rejections;
this module stays a pure function set so it's easy to test against saved
fixture payloads without a database.
"""
import json
import re
from datetime import datetime, timezone

SOURCE = "nightscout"


def parse_iso(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def ms_to_dt_utc(ms_val):
    return datetime.fromtimestamp(ms_val / 1000.0, tz=timezone.utc)


def treatment_event_dt(t):
    d = t.get("date")
    if isinstance(d, (int, float)):
        return ms_to_dt_utc(int(d))
    s = t.get("dateString") or t.get("created_at")
    if isinstance(s, str) and s:
        return parse_iso(s)
    return None


# ---------------------------------------------------------------- entries

def to_glucose_reading(raw: dict):
    """Nightscout 'entries' record -> canonical glucose reading.
    Classification: caller already knows this came from the entries
    endpoint -- Nightscout entries don't self-describe a record type."""
    ts_str = raw.get("dateString") or raw.get("created_at")
    if not ts_str:
        return None, "missing timestamp (dateString/created_at)"
    ts = parse_iso(ts_str)
    if not ts:
        return None, f"unparseable timestamp: {ts_str!r}"

    return {
        "ts": ts,
        "sg": raw.get("sgv"),
        "direction": raw.get("direction"),
        "device": raw.get("device"),
        "raw": raw,
        "source": SOURCE,
    }, None


# -------------------------------------------------------------- treatments

def to_treatment(raw: dict):
    """Nightscout 'treatments' record -> canonical treatment."""
    raw_id = raw.get("_id")
    created_at = raw.get("created_at")
    if not raw_id or not created_at:
        return None, "missing _id or created_at"
    ts = parse_iso(created_at)
    if not ts:
        return None, f"unparseable timestamp: {created_at!r}"

    notes = raw.get("notes") or raw.get("note")
    smb_flag = bool(raw.get("isSMB", False))
    if not smb_flag and isinstance(notes, str) and "smb" in notes.lower():
        smb_flag = True

    return {
        "ts": ts,
        "event_type": raw.get("eventType"),
        "insulin": raw.get("insulin"),
        "carbs": raw.get("carbs"),
        "smb_flag": smb_flag,
        "notes": notes,
        "raw_id": raw_id,
        "duration": raw.get("duration"),
        "rate": raw.get("rate"),
        "absolute": raw.get("absolute"),
        "raw_json": raw,
        "source": SOURCE,
    }, None


def is_user_note(raw: dict) -> bool:
    """Classifier: is this a genuine, human-typed AAPS note?

    v3 decision (see docs/analysis/notes-weight-hba1c-implementation-plan.md):
    AAPS sets enteredBy='AAPS' only for notes typed by a human in its own
    UI -- profile-switch noise carries 'openaps://AndroidAPS' and app-
    lifecycle messages carry no enteredBy at all. This is a plain allowlist
    on purpose: no regex, no denylist of known-noise text patterns. If a
    future AAPS version changes what it puts in enteredBy, new notes stop
    matching here until that's noticed and fixed -- no historical data is
    at risk either way, since this only governs live ingestion.
    """
    et = (raw.get("eventType") or "").strip().lower()
    entered_by = (raw.get("enteredBy") or "").strip()
    return et == "note" and entered_by == "AAPS"


def to_clinical_note(raw: dict):
    """A treatment already classified via is_user_note() -> canonical
    clinical-note record. Doesn't re-check eventType/enteredBy itself,
    stays a pure mapping step -- call is_user_note() first."""
    raw_id = raw.get("_id")
    created_at = raw.get("created_at")
    if not raw_id or not created_at:
        return None, "missing _id or created_at"
    ts = parse_iso(created_at)
    if not ts:
        return None, f"unparseable timestamp: {created_at!r}"
    text = raw.get("notes") or raw.get("note")
    if not text:
        return None, "empty note text"
    return {
        "ts": ts,
        "raw_id": raw_id,
        "text_content": text,
        "source": SOURCE,
    }, None


def is_site_change(raw: dict) -> bool:
    """Classifier: does this treatment represent a site change? Kept as
    its own function (rather than re-checked ad hoc per caller) since two
    different canonical outputs both need this same classification."""
    et = (raw.get("eventType") or "").strip().lower()
    return et == "site change"


def to_site_change(raw: dict):
    """A treatment already classified via is_site_change() -> canonical
    site-change event. Doesn't re-check eventType itself, stays a pure
    mapping step -- call is_site_change() first."""
    ts = treatment_event_dt(raw)
    if not ts:
        return None, "missing/unparseable event timestamp"
    return {"ts": ts, "source": SOURCE}, None


# ------------------------------------------------------------ devicestatus

def _regex_fallback(structured_val, reason, pattern):
    if structured_val is not None:
        return structured_val
    match = re.search(pattern, reason)
    return float(match.group(1)) if match else None


def to_devicestatus(raw: dict):
    """Nightscout 'devicestatus' record -> canonical devicestatus.

    Preserves the regex-fallback extraction for fields AAPS sometimes
    only reports inside the human-readable `reason` string rather than as
    a structured field.
    """
    created_at = raw.get("created_at")
    if not created_at:
        return None, "missing created_at"
    ts = parse_iso(created_at)
    if not ts:
        return None, f"unparseable timestamp: {created_at!r}"

    openaps = raw.get("openaps") or {}
    suggested = openaps.get("suggested") or {}
    iob_obj = openaps.get("iob") or {}
    reason = suggested.get("reason", "")

    bgi = _regex_fallback(suggested.get("BGI"), reason, r"BGI: ([-+]?\d*\.\d+|\d+)")
    dev = _regex_fallback(suggested.get("deviation"), reason, r"Dev: ([-+]?\d*\.\d+|\d+)")

    # NOTE (inherited from the 19 Aug 2026 fix on v1, do not re-derive):
    # isfMgdlForCarbs is genuinely mg/dL and needs conversion; the regex
    # fallback parses reason text that on THIS source is already mmol/L,
    # so it must NOT be converted again. Assumes this AAPS instance
    # displays in mmol/L -- revisit if a second AAPS-based source is ever
    # adapted and reports in mg/dL, since that assumption is source-
    # specific and belongs in an adapter, not shared logic.
    isf_structured = suggested.get("isfMgdlForCarbs")
    if isf_structured is not None:
        isf = isf_structured / 18.0182
    else:
        match = re.search(r"ISF: ([-+]?\d*\.\d+|\d+)", reason)
        isf = float(match.group(1)) if match else None

    cr = _regex_fallback(suggested.get("CR"), reason, r"CR: ([-+]?\d*\.\d+|\d+)")
    target_bg = _regex_fallback(suggested.get("targetBG"), reason, r"Target: ([-+]?\d*\.\d+|\d+)")
    ins_req = _regex_fallback(suggested.get("insulinReq"), reason, r"insulinReq ([-+]?\d*\.\d+|\d+)")
    sens_ratio_structured = suggested.get("sensitivityRatio") or suggested.get("ratio")
    sens_ratio = _regex_fallback(sens_ratio_structured, reason, r"Autosens ratio: ([-+]?\d*\.\d+|\d+)")
    var_sens = _regex_fallback(suggested.get("variable_sens"), reason, r"variable_sens: ([-+]?\d*\.\d+|\d+)")

    loop_iob = iob_obj.get("iob")
    loop_basal_iob = iob_obj.get("basaliob")
    if loop_iob is not None and loop_basal_iob is not None:
        try:
            bolus_iob = round(float(loop_iob) - float(loop_basal_iob), 3)
        except (ValueError, TypeError):
            bolus_iob = None
    elif loop_iob is not None:
        try:
            bolus_iob = float(loop_iob)
        except (ValueError, TypeError):
            bolus_iob = None
    else:
        bolus_iob = None

    return {
        "ts": ts,
        "iob": bolus_iob if bolus_iob is not None else loop_iob,
        "cob": suggested.get("COB"),
        "bgi": bgi,
        "deviation": dev,
        "autosens_ratio": sens_ratio,
        "loop_enacted": True if suggested else False,
        "predictions": suggested.get("predBGs"),
        "raw_json": raw,
        "isf": isf,
        "cr": cr,
        "target_bg": target_bg,
        "insulin_req": ins_req,
        "variable_sens": var_sens,
        "source": SOURCE,
        "bolus_iob": bolus_iob,
        "basal_iob": None,
        "loop_iob": loop_iob,
        "loop_basal_iob": loop_basal_iob,
    }, None


# ----------------------------------------------------------------- profiles

def to_profile_switch(raw_profile_list: list):
    """Nightscout 'profile' response (a list; first entry is active) ->
    a synthetic canonical treatment representing a profile switch. Mirrors
    the original's trick of piggybacking on the treatment shape rather
    than a separate table."""
    if not raw_profile_list:
        return None, "empty profile list"
    p = raw_profile_list[0]
    raw_id = p.get("_id")

    dt_val = p.get("date")
    if isinstance(dt_val, (int, float)):
        ts = ms_to_dt_utc(int(dt_val))
    else:
        created_at = p.get("created_at")
        if not created_at:
            return None, "missing date/created_at"
        ts = parse_iso(created_at)
        if not ts:
            return None, f"unparseable timestamp: {created_at!r}"

    default_profile_name = p.get("defaultProfile")
    if not default_profile_name or default_profile_name not in p.get("store", {}):
        return None, "defaultProfile missing or not present in store"

    profile_data = p.get("store", {}).get(default_profile_name)
    synthetic = {
        "eventType": "Profile Switch",
        "_id": f"sync_{raw_id}",
        "created_at": ts.isoformat().replace("+00:00", "Z"),
        "profile": default_profile_name,
        "profileJson": json.dumps(profile_data),
        "percentage": 100,
        "duration": 0,
        "notes": f"Synced active profile: {default_profile_name}",
    }
    return to_treatment(synthetic)
