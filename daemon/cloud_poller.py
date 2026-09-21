"""
Polls the real Nightscout cloud API directly and routes raw records
through the adapter (daemon/adapters/nightscout.py) and canonical_store.py.
This is the active ingestion path for the daemon.

Deliberately does NOT re-backfill full history from the cloud API.
This starts from wherever data already ends (with the overlap window)
and polls forward from there.

Writes ingestion_coverage via database.record_coverage() on every pass,
so the Daily Record Counts / DB maintenance page's gap display works correctly.
Honours POLLING_ENABLED, NIGHTSCOUT_URL, and API_SECRET from system_config.
"""
import time
from datetime import datetime, timedelta, timezone

import config
import database
import nightscout_api
from adapters import nightscout as ns_adapter
import canonical_store


def _write_entries(v2_conn, entries):
    records = []
    for raw in entries:
        record, _ = ns_adapter.to_glucose_reading(raw)
        if record:
            records.append(record)
    return canonical_store.write_glucose_readings(v2_conn, records)


def _write_treatments(v2_conn, treatments):
    t_records = []
    sc_records = []
    note_records = []
    for raw in treatments:
        t_record, _ = ns_adapter.to_treatment(raw)
        if t_record:
            t_records.append(t_record)
        if ns_adapter.is_site_change(raw):
            sc_record, _ = ns_adapter.to_site_change(raw)
            if sc_record:
                sc_records.append(sc_record)
        if ns_adapter.is_user_note(raw):
            note_record, _ = ns_adapter.to_clinical_note(raw)
            if note_record:
                note_records.append(note_record)
    n_t = canonical_store.write_treatments(v2_conn, t_records)
    n_sc = canonical_store.write_site_changes(v2_conn, sc_records)
    canonical_store.write_clinical_notes_from_aaps(v2_conn, note_records)
    return n_t, n_sc


def _write_devicestatus(v2_conn, devs):
    records = []
    for raw in devs:
        record, _ = ns_adapter.to_devicestatus(raw)
        if record:
            records.append(record)
    return canonical_store.write_devicestatus(v2_conn, records)


def _fetch_and_write_window(v2_conn, url, secret, start, end, method):
    """One fetch+write+coverage pass over [start, end) for all three streams."""
    entries = nightscout_api.fetch_entries_range_paged(start, end, url=url, secret=secret)
    n1 = _write_entries(v2_conn, entries)
    database.record_coverage(v2_conn, "entries", start, end, method)

    treatments = nightscout_api.fetch_treatments_range(start, end, url=url, secret=secret)
    n2, n_sc = _write_treatments(v2_conn, treatments)
    database.record_coverage(v2_conn, "treatments", start, end, method)

    devs = nightscout_api.fetch_devicestatus_range(start, end, url=url, secret=secret)
    n3 = _write_devicestatus(v2_conn, devs)
    database.record_coverage(v2_conn, "devicestatus", start, end, method)

    v2_conn.commit()
    return n1, n2, n_sc, n3


def _update_status(status_data, status_lock, latest, note):
    if status_data is None or status_lock is None:
        return
    now = datetime.now(timezone.utc)
    lag_seconds = int((now - latest).total_seconds()) if latest else None
    with status_lock:
        status_data["service_health"]["sync"] = note
        status_data["last_poll_utc"] = now.isoformat().replace("+00:00", "Z")
        if latest:
            status_data["latest_entry_ts_utc"] = latest.isoformat().replace("+00:00", "Z")
            status_data["lag_seconds"] = lag_seconds
    nightscout_api.ping_healthcheck(lag_seconds=lag_seconds, sync_status=note)


def run_loop(refresh_callback=None, status_data=None, status_lock=None):
    v2_conn = database.get_conn()
    try:
        db_config = database.get_system_config(v2_conn)
        url = db_config.get("NIGHTSCOUT_URL") or config.NIGHTSCOUT_HOST
        secret = db_config.get("API_SECRET") or config.NS_SECRET
        if not url or not secret:
            print("[cloud] NIGHTSCOUT_URL/API_SECRET not configured -- cloud polling not started.")
            return

        overlap = timedelta(minutes=config.FETCH_OVERLAP_MINUTES)

        # Wait if polling is initially disabled
        while True:
            db_config = database.get_system_config(v2_conn)
            polling_enabled = db_config.get("POLLING_ENABLED", "true").lower() == "true"
            if polling_enabled:
                break
            if status_data is not None and status_lock is not None:
                with status_lock:
                    status_data["service_health"]["sync"] = "Disabled"
                    status_data["lag_seconds"] = None
            time.sleep(10)

        if status_data is not None and status_lock is not None:
            with status_lock:
                status_data["backfill"]["enabled"] = True
                status_data["backfill"]["running"] = True
                status_data["backfill"]["done"] = False
                status_data["service_health"]["sync"] = "Polling cloud (catch-up)"

        with v2_conn.cursor() as cur:
            cur.execute("SELECT max(ts) FROM cgm_readings")
            last_cgm = cur.fetchone()[0]
        v2_conn.commit()
        now = datetime.now(timezone.utc)
        catchup_start = (last_cgm - overlap) if last_cgm else (now - timedelta(days=1))

        print(f"[cloud] catching up from {catchup_start.isoformat()} -> now.")
        try:
            n1, n2, n_sc, n3 = _fetch_and_write_window(v2_conn, url, secret, catchup_start, now + timedelta(minutes=1), "polling")
            print(f"[cloud] catch-up: {n1} cgm / {n2} treatments / {n_sc} site_changes / {n3} devicestatus")

            if n1 or n2 or n3:
                database.populate_5min_aggregate(v2_conn, catchup_start, now + timedelta(minutes=5))
                v2_conn.commit()
                if refresh_callback:
                    refresh_callback()

            if status_data is not None and status_lock is not None:
                with status_lock:
                    status_data["backfill"]["running"] = False
                    status_data["backfill"]["done"] = True
                    status_data["backfill"]["progress"] = "Caught up to cloud's current tail."

            with v2_conn.cursor() as cur:
                cur.execute("SELECT max(ts) FROM cgm_readings")
                latest = cur.fetchone()[0]
            v2_conn.commit()
            _update_status(status_data, status_lock, latest, "ok (cloud)")
            last_entry_ts = latest
        except Exception as e:
            v2_conn.rollback()
            print(f"[cloud] catch-up pass failed, will retry in polling loop: {e}")
            last_entry_ts = None

        print(f"[cloud] switching to steady-state polling every {config.POLL_PERIOD_SECONDS}s.")

        while True:
            db_config = database.get_system_config(v2_conn)
            polling_enabled = db_config.get("POLLING_ENABLED", "true").lower() == "true"
            url = db_config.get("NIGHTSCOUT_URL") or config.NIGHTSCOUT_HOST
            secret = db_config.get("API_SECRET") or config.NS_SECRET

            if not polling_enabled:
                if status_data is not None and status_lock is not None:
                    with status_lock:
                        status_data["service_health"]["sync"] = "Disabled"
                        status_data["lag_seconds"] = None
                last_entry_ts = None
                time.sleep(5)
                continue

            sleep_sec = config.POLL_PERIOD_SECONDS

            if last_entry_ts:
                now = datetime.now(timezone.utc)
                if last_entry_ts.tzinfo is None:
                    last_entry_ts = last_entry_ts.replace(tzinfo=timezone.utc)
                target_time = last_entry_ts + timedelta(seconds=config.POLL_PERIOD_SECONDS) + timedelta(seconds=config.POLL_OFFSET_SECONDS)

                wait_delta = (target_time - now).total_seconds()

                if wait_delta > 5:
                    sleep_sec = wait_delta
                    print(f"[cloud] Phase-locked. Next sync in {int(sleep_sec)}s (Target: {nightscout_api.iso_z(target_time)})")
                else:
                    sleep_sec = 10
                    print(f"[cloud] Catching up. Next sync in {sleep_sec}s")
            else:
                print(f"[cloud] No data found. Falling back to standard {sleep_sec}s interval.")

            # Sleep responsively in chunks so toggling off in Settings takes effect promptly
            slept = 0.0
            interrupted = False
            while slept < sleep_sec:
                step = min(2.0, sleep_sec - slept)
                time.sleep(step)
                slept += step
                chk_cfg = database.get_system_config(v2_conn)
                if chk_cfg.get("POLLING_ENABLED", "true").lower() != "true":
                    interrupted = True
                    break

            if interrupted:
                continue

            now = datetime.now(timezone.utc)
            with v2_conn.cursor() as cur:
                cur.execute("SELECT max(ts) FROM cgm_readings")
                last_cgm = cur.fetchone()[0]
            v2_conn.commit()
            poll_start = (last_cgm - overlap) if last_cgm else (now - timedelta(days=1))
            try:
                n1, n2, n_sc, n3 = _fetch_and_write_window(v2_conn, url, secret, poll_start, now + timedelta(minutes=1), "polling")
                if n1 or n2 or n3:
                    print(f"[cloud] cgm={n1} treatments={n2} site_changes={n_sc} devicestatus={n3}")
                    database.populate_5min_aggregate(v2_conn, now - timedelta(hours=2), now + timedelta(minutes=5))
                    v2_conn.commit()
                    if refresh_callback:
                        refresh_callback()
                with v2_conn.cursor() as cur:
                    cur.execute("SELECT max(ts) FROM cgm_readings")
                    latest = cur.fetchone()[0]
                v2_conn.commit()
                last_entry_ts = latest if n1 > 0 else None
                _update_status(status_data, status_lock, latest, "ok (cloud)")
            except Exception as e:
                v2_conn.rollback()
                print(f"[cloud] pass failed, will retry next cycle: {e}")
                last_entry_ts = None
                err_note = f"error (cloud): {e}"
                if status_data is not None and status_lock is not None:
                    with status_lock:
                        status_data["service_health"]["sync"] = err_note
                        status_data["errors_last"] = str(e)
                nightscout_api.ping_healthcheck(sync_status=err_note)
    finally:
        database.return_conn(v2_conn)
