import json
import re
from datetime import datetime, timezone, timedelta
import psycopg2.extras

# Time Helpers
def parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None

def ms_to_dt_utc(ms_val: int) -> datetime:
    return datetime.fromtimestamp(ms_val / 1000.0, tz=timezone.utc)

def treatment_event_dt(t: dict) -> datetime | None:
    d = t.get("date")
    if isinstance(d, (int, float)):
        return ms_to_dt_utc(int(d))
    s = t.get("dateString") or t.get("created_at")
    if isinstance(s, str) and s:
        try:
            return parse_iso(s)
        except Exception:
            return None
    return None

def process_entries(conn, entries):
    if not entries:
        return 0
    rows = []
    seen_ts = set()
    
    for e in entries:
        ts_str = e.get("dateString") or e.get("created_at")
        if not ts_str: continue
        
        ts = parse_iso(ts_str)
        if not ts: continue
        
        if ts in seen_ts:
            continue
        seen_ts.add(ts)

        rows.append((ts, e.get("sgv"), e.get("direction"), e.get("device"), json.dumps(e)))
    
    if not rows: return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO cgm_readings(ts, sg, direction, device, raw)
               VALUES %s
               ON CONFLICT (ts) DO UPDATE SET
                   sg        = EXCLUDED.sg,
                   direction = EXCLUDED.direction,
                   device    = EXCLUDED.device,
                   raw       = EXCLUDED.raw
            """,
            rows,
            page_size=1000
        )
        return cur.rowcount

def process_treatments(conn, treatments):
    if not treatments:
        return 0
    rows = []
    seen_keys = set()
    
    for t in treatments:
        raw_id = t.get("_id")
        created_at = t.get("created_at")
        if not raw_id or not created_at:
            continue
        
        ts = parse_iso(created_at)
        # Dedup within batch based on (ts, raw_id) which is the PK
        key = (ts, raw_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        notes = t.get("notes") or t.get("note")
        smb_flag = bool(t.get("isSMB", False))
        if not smb_flag and isinstance(notes, str) and "smb" in notes.lower():
            smb_flag = True
            
        rows.append((
            ts, t.get("eventType"), t.get("insulin"), t.get("carbs"), smb_flag, notes,
            raw_id, t.get("duration"), t.get("rate"), t.get("absolute"), json.dumps(t)
        ))
        
    if not rows: return 0
    
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO treatments(ts, event_type, insulin, carbs, smb_flag, notes, raw_id, duration, rate, absolute, raw_json)
            VALUES %s
            ON CONFLICT (ts, raw_id) DO UPDATE SET
              event_type = EXCLUDED.event_type,
              insulin    = EXCLUDED.insulin,
              carbs      = EXCLUDED.carbs,
              smb_flag   = EXCLUDED.smb_flag,
              notes      = EXCLUDED.notes,
              duration   = EXCLUDED.duration,
              rate       = EXCLUDED.rate,
              absolute   = EXCLUDED.absolute,
              raw_json   = EXCLUDED.raw_json
            """,
            rows,
            page_size=1000
        )
        return cur.rowcount

def process_site_changes(conn, treatments):
    if not treatments: return 0
    rows = []
    seen_ts = set()
    
    for t in treatments:
        et = (t.get("eventType") or "").strip().lower()
        if et == "site change":
            ts_dt = treatment_event_dt(t)
            if ts_dt and ts_dt not in seen_ts:
                rows.append((ts_dt,))
                seen_ts.add(ts_dt)
            
    if not rows: return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO site_changes(ts) VALUES %s ON CONFLICT (ts) DO NOTHING",
            rows,
            page_size=500
        )
        return cur.rowcount

def process_devicestatus(conn, devs):
    if not devs: return 0
    rows = []
    seen_ts = set()
    
    for d in devs:
        created_at = d.get("created_at")
        if not created_at: continue
        ts = parse_iso(created_at)
        
        if ts in seen_ts: continue
        seen_ts.add(ts)
        
        openaps = d.get("openaps") or {}
        suggested = openaps.get("suggested") or {}
        iob_obj = openaps.get("iob") or {}
        
        # Regex extraction for nested fields in reason
        reason = suggested.get("reason", "")
        bgi = suggested.get("BGI")
        if bgi is None:
            match = re.search(r"BGI: ([-+]?\d*\.\d+|\d+)", reason)
            if match: bgi = float(match.group(1))
            
        dev = suggested.get("deviation")
        if dev is None:
            match = re.search(r"Dev: ([-+]?\d*\.\d+|\d+)", reason)
            if match: dev = float(match.group(1))
        
        # New Extractions (ISF, CR, Target, Req, Sens)
        # Fixed 19 Aug 2026: conversion is now conditional on source. The
        # structured field is genuinely mg/dL and needs the conversion; the
        # regex fallback parses AAPS's human-readable reason text, which on
        # this system is already rendered in mmol/L (confirmed via real
        # devicestatus data), so converting it again was corrupting ~0.5%
        # of records with no structured field present. NOTE: this assumes
        # the source AAPS instance displays in mmol/L -- not a safe
        # assumption for other deployments if this app is ever distributed.
        isf_structured = suggested.get("isfMgdlForCarbs")
        if isf_structured is not None:
            isf = isf_structured / 18.0182
        else:
            match = re.search(r"ISF: ([-+]?\d*\.\d+|\d+)", reason)
            isf = float(match.group(1)) if match else None
        
        cr = suggested.get("CR")
        if cr is None:
            match = re.search(r"CR: ([-+]?\d*\.\d+|\d+)", reason)
            if match: cr = float(match.group(1))
            
        target_bg = suggested.get("targetBG")
        if target_bg is None:
            match = re.search(r"Target: ([-+]?\d*\.\d+|\d+)", reason)
            if match: target_bg = float(match.group(1))
            
        ins_req = suggested.get("insulinReq")
        if ins_req is None:
            match = re.search(r"insulinReq ([-+]?\d*\.\d+|\d+)", reason)
            if match: ins_req = float(match.group(1))

        sens_ratio = suggested.get("sensitivityRatio") or suggested.get("ratio")
        if sens_ratio is None:
            match = re.search(r"Autosens ratio: ([-+]?\d*\.\d+|\d+)", reason)
            if match: sens_ratio = float(match.group(1))

        var_sens = suggested.get("variable_sens")
        if var_sens is None:
            match = re.search(r"variable_sens: ([-+]?\d*\.\d+|\d+)", reason)
            if match: var_sens = float(match.group(1))

        rows.append((
            ts, iob_obj.get("iob"), suggested.get("COB"), bgi,
            dev, sens_ratio,
            True if suggested else False,
            json.dumps(suggested.get("predBGs")) if suggested.get("predBGs") else None,
            json.dumps(d),
            isf, cr, target_bg, ins_req, var_sens
        ))
        
    if not rows: return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO devicestatus(
                ts, iob, cob, bgi, deviation, autosens_ratio, 
                loop_enacted, predictions, raw_json,
                isf, cr, target_bg, insulin_req, variable_sens
            )
            VALUES %s
            ON CONFLICT (ts) DO UPDATE SET
                iob = EXCLUDED.iob, cob = EXCLUDED.cob, bgi = EXCLUDED.bgi,
                deviation = EXCLUDED.deviation, autosens_ratio = EXCLUDED.autosens_ratio,
                loop_enacted = EXCLUDED.loop_enacted, predictions = EXCLUDED.predictions, 
                raw_json = EXCLUDED.raw_json,
                isf = EXCLUDED.isf, cr = EXCLUDED.cr, 
                target_bg = EXCLUDED.target_bg, insulin_req = EXCLUDED.insulin_req,
                variable_sens = EXCLUDED.variable_sens
            """,
            rows,
            page_size=500
        )
        return cur.rowcount


def process_profiles(conn, profile_list):
    """
    Syncs the latest active profile from Nightscout.
    We convert the current profile state into a 'Profile Switch' treatment 
    so it seamlessly integrates with our historical basal logic.
    """
    if not profile_list: return 0
    
    # Nightscout usually returns a list, the first one is the active state
    p = profile_list[0]
    raw_id = p.get("_id")
    # Use the 'date' field if available (timestamp ms), else 'created_at'
    dt_val = p.get("date")
    if isinstance(dt_val, (int, float)):
        ts = ms_to_dt_utc(int(dt_val))
    else:
        created_at = p.get("created_at")
        if not created_at: return 0
        ts = parse_iso(created_at)

    default_profile_name = p.get("defaultProfile")
    if not default_profile_name or default_profile_name not in p.get("store", {}):
        return 0
    
    # Construct a synthetic 'Profile Switch' event
    profile_data = p.get("store", {}).get(default_profile_name)
    synthetic_switch = {
        "eventType": "Profile Switch",
        "_id": f"sync_{raw_id}", # Prefix to avoid collision with real switch events
        "created_at": ts.isoformat().replace("+00:00", "Z"),
        "profile": default_profile_name,
        "profileJson": json.dumps(profile_data),
        "percentage": 100, # Active profiles are usually base 100%
        "duration": 0,
        "notes": f"Synced active profile: {default_profile_name}"
    }
    
    return process_treatments(conn, [synthetic_switch])
