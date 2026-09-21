"""
Writes canonical records (already produced by an adapter -- see
daemon/adapters/) into this database. Source-agnostic: doesn't know or
care which adapter produced a record. SQL here is the same INSERT ...
ON CONFLICT shape as v1's data_processor.py, just taking canonical dicts
instead of raw source dicts as input. In-memory deduplication is performed
before batch insertion to prevent PostgreSQL CardinalityViolation (ON CONFLICT
DO UPDATE cannot affect row a second time).
"""
import json
import psycopg2.extras


def write_glucose_readings(conn, records: list[dict]) -> int:
    deduped = {}
    for r in records:
        deduped[r["ts"]] = r
    rows = [
        (r["ts"], r["sg"], r["direction"], r["device"], json.dumps(r["raw"]))
        for r in deduped.values()
    ]
    if not rows:
        return 0
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
            page_size=1000,
        )
        return len(rows)


def write_treatments(conn, records: list[dict]) -> int:
    deduped = {}
    for r in records:
        deduped[(r["ts"], r["raw_id"])] = r
    rows = [
        (
            r["ts"], r["event_type"], r["insulin"], r["carbs"], r["smb_flag"],
            r["notes"], r["raw_id"], r["duration"], r["rate"], r["absolute"],
            json.dumps(r["raw_json"]),
        )
        for r in deduped.values()
    ]
    if not rows:
        return 0
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
            page_size=1000,
        )
        return len(rows)


def write_site_changes(conn, records: list[dict]) -> int:
    seen = set()
    rows = []
    for r in records:
        if r["ts"] not in seen:
            seen.add(r["ts"])
            rows.append((r["ts"],))
    if not rows:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO site_changes(ts) VALUES %s ON CONFLICT (ts) DO NOTHING",
            rows,
            page_size=500,
        )
        return len(rows)


def write_clinical_notes_from_aaps(conn, records: list[dict]) -> int:
    """Idempotent insert of AAPS-sourced notes into clinical_notes. Keyed
    on (raw_treatment_id, raw_treatment_ts) -- NOT raw_treatment_id alone,
    since treatments.raw_id is not globally unique (see migration comment).
    Only inserts -- never overwrites -- so a user edit (which flips
    note_type to 'user' and is_edited to TRUE) is never clobbered by a
    later poll of the same underlying AAPS treatment."""
    seen = set()
    rows = []
    for r in records:
        key = (r["raw_id"], r["ts"]) if r.get("raw_id") else None
        if key:
            if key in seen:
                continue
            seen.add(key)
        rows.append((r["ts"].date(), r["ts"], "aaps", r["text_content"], r["raw_id"], r["ts"]))
    if not rows:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO clinical_notes
                (date, time_created, note_type, text_content, raw_treatment_id, raw_treatment_ts)
            VALUES %s
            ON CONFLICT (raw_treatment_id, raw_treatment_ts) WHERE raw_treatment_id IS NOT NULL
            DO NOTHING
            """,
            rows,
            page_size=500,
        )
        return len(rows)


def write_devicestatus(conn, records: list[dict]) -> int:
    deduped = {}
    for r in records:
        deduped[r["ts"]] = r
    rows = [
        (
            r["ts"], r["iob"], r["cob"], r["bgi"], r["deviation"], r["autosens_ratio"],
            r["loop_enacted"],
            json.dumps(r["predictions"]) if r["predictions"] else None,
            json.dumps(r["raw_json"]),
            r["isf"], r["cr"], r["target_bg"], r["insulin_req"], r["variable_sens"],
            r.get("bolus_iob"), r.get("basal_iob"), r.get("loop_iob"), r.get("loop_basal_iob"),
        )
        for r in deduped.values()
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO devicestatus(
                ts, iob, cob, bgi, deviation, autosens_ratio,
                loop_enacted, predictions, raw_json,
                isf, cr, target_bg, insulin_req, variable_sens,
                bolus_iob, basal_iob, loop_iob, loop_basal_iob
            )
            VALUES %s
            ON CONFLICT (ts) DO UPDATE SET
                iob = EXCLUDED.iob, cob = EXCLUDED.cob, bgi = EXCLUDED.bgi,
                deviation = EXCLUDED.deviation, autosens_ratio = EXCLUDED.autosens_ratio,
                loop_enacted = EXCLUDED.loop_enacted, predictions = EXCLUDED.predictions,
                raw_json = EXCLUDED.raw_json,
                isf = EXCLUDED.isf, cr = EXCLUDED.cr,
                target_bg = EXCLUDED.target_bg, insulin_req = EXCLUDED.insulin_req,
                variable_sens = EXCLUDED.variable_sens,
                bolus_iob = EXCLUDED.bolus_iob,
                basal_iob = COALESCE(EXCLUDED.basal_iob, devicestatus.basal_iob),
                loop_iob = EXCLUDED.loop_iob,
                loop_basal_iob = EXCLUDED.loop_basal_iob
            """,
            rows,
            page_size=500,
        )
        return len(rows)
