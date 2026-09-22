import time
import math
import re
import threading
import os
import numpy as np
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import io
import json
from decimal import Decimal
import psycopg2
from flask import Flask, jsonify, render_template, request, Response, redirect, make_response, send_file

import config
import database
import filter_engine
import calendar_provider
import nightscout_api
import data_processor
import agp_renderer
import profile_history
import cgm_health
import log_manager
import periodicity
from scipy import stats as scipy_stats
import collections
import queue

# STATUS SERVER
app = Flask(__name__)


@app.after_request
def add_no_cache_headers(response):
    """Prevent browsers from serving a stale copy of a dynamically
    rendered page. Confirmed 28 Aug 2026: Safari Private Browsing
    isolates cookies/localStorage/sessionStorage, but does NOT disable
    HTTP caching within a session -- with no Cache-Control header at all
    (the default for a plain Flask/Werkzeug response), a private window
    reused a cached pre-fix copy of /trends across reloads, which looked
    exactly like a server-side bug for a while. Only sets the header when
    nothing already has -- static assets already get correct ETag-based
    revalidation from Flask's own static file handler, this must not
    clobber that."""
    if 'Cache-Control' not in response.headers:
        response.headers['Cache-Control'] = 'no-store'
    return response

# CENTRAL LOGGING SETUP (Rotates daily at local midnight, retains 7 days, in-memory SSE ring buffer)
def _sync_process_tz():
    tz_name = getattr(config, "TIMEZONE", "Australia/Perth")
    tz_name = os.environ.get("TIMEZONE", tz_name)
    if hasattr(time, "tzset"):
        os.environ["TZ"] = tz_name
        time.tzset()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")

def _get_configured_tz():
    return _sync_process_tz()

_sync_process_tz()
log_manager.setup_logging(get_tz_fn=_get_configured_tz)



status_data = {
    "daemon_heartbeat": True,
    "last_poll_utc": None,
    "last_sync_entries": None,
    "last_sync_treatments": None,
    "last_sync_devicestatus": None,
    "lag_seconds": None,
    "latest_entry_ts_utc": None,
    "service_health": {
        "sync": "ok"
    },
    "risk_flags": [],
    "errors_last": None,
    "backfill": {
        "enabled": False, 
        "running": False,
        "done": False,
        "progress": None,
        "errors_last": None,
        "manual_range": None, 
    },
    "last_maintenance_date": None,
    "feature_healing": {"running": False, "last_chunk_at": None, "gaps_repaired": 0}
}

# LOW-2: Lock that guards all writes/compound-reads of status_data.
# The GIL provides accidental safety for simple dict reads, but iterating
# status_data while another thread writes is not safe without this.
status_data_lock = threading.Lock()
metrics_rebuild_lock = threading.Lock() # Guard rebuilding alerts metrics concurrently with backfills


# ---------------- THREAD CONTROL ----------------
backfill_stop_event = threading.Event()

# ---------------- CONFIG LOADER ----------------
def load_settings_from_db():
    conn = None
    try:
        conn = database.get_conn()
        settings = database.get_all_config(conn)
        database.return_conn(conn)
        conn = None
        
        def _int(key, default): return int(settings[key]) if key in settings else default
        def _float(key, default): return float(settings[key]) if key in settings else default
        def _str(key, default): return settings[key] if key in settings else default

        config.POLL_PERIOD_SECONDS  = _int(  'POLL_PERIOD_SECONDS',  config.POLL_PERIOD_SECONDS)
        config.POLL_OFFSET_SECONDS  = _int(  'POLL_OFFSET_SECONDS',  config.POLL_OFFSET_SECONDS)
        config.RETENTION_DAYS       = _int(  'RETENTION_DAYS',       config.RETENTION_DAYS)
        config.BACKFILL_DELAY_SECONDS = _float('BACKFILL_DELAY_SECONDS', config.BACKFILL_DELAY_SECONDS)
        config.HEALTHCHECK_URL      = _str(  'HEALTHCHECK_URL',      config.HEALTHCHECK_URL)
        config.TIMEZONE             = _str(  'TIMEZONE',             config.TIMEZONE)
        _sync_process_tz()
        config.BF_CHUNK_ENTRIES     = _int(  'BACKFILL_CHUNK_DAYS_ENTRIES',       config.BF_CHUNK_ENTRIES)
        config.BF_CHUNK_TREAT       = _int(  'BACKFILL_CHUNK_DAYS_TREATMENTS',    config.BF_CHUNK_TREAT)
        config.BF_CHUNK_DEV         = _int(  'BACKFILL_CHUNK_DAYS_DEVICESTATUS',  config.BF_CHUNK_DEV)
        config.BF_PAGE_SIZE         = _int(  'BACKFILL_PAGE_SIZE',                config.BF_PAGE_SIZE)
        config.BF_SLEEP_PAGES       = _float('BACKFILL_SLEEP_BETWEEN_PAGES_SEC',  config.BF_SLEEP_PAGES)
        config.BF_SLEEP_WINDOWS     = _float('BACKFILL_SLEEP_BETWEEN_WINDOWS_SEC',config.BF_SLEEP_WINDOWS)
        
        config.RETENTION_DAYS_RAW      = _int(  'retention_days_raw',      config.RETENTION_DAYS_RAW)
        config.RETENTION_DAYS_CLINICAL = _int(  'retention_days_clinical', config.RETENTION_DAYS_CLINICAL)
        config.RETENTION_ENABLED       = _str(  'retention_enabled',       str(config.RETENTION_ENABLED)).lower() == 'true'
        config.LOG_LEVEL               = _str(  'LOG_LEVEL',               getattr(config, 'LOG_LEVEL', 'INFO')).upper()
        log_manager.set_runtime_log_level(config.LOG_LEVEL)

        print("[config] Settings reloaded from DB.")
    except Exception as e:
        print(f"[config] Failed to load settings: {e}")
    finally:
        if conn:
            database.return_conn(conn)


# ---------------- REFRESH WORKER & DASHBOARD BROADCASTER ----------------
refresh_lock = threading.Lock()
refresh_running = False

# Dashboard SSE Broadcaster
dashboard_sse_subscribers = []
dashboard_sse_lock = threading.Lock()


def _dashboard_event_json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dashboard_event_json(data):
    """Encode dashboard events while preserving numeric values for the UI."""
    return json.dumps(data, default=_dashboard_event_json_default)


def broadcast_dashboard_event(event_type: str, data: dict):
    """Broadcast an event to all active dashboard SSE connections."""
    with dashboard_sse_lock:
        dead = []
        for q in dashboard_sse_subscribers:
            try:
                q.put_nowait((event_type, data))
            except queue.Full:
                dead.append(q)
            except Exception:
                dead.append(q)
        for d in dead:
            if d in dashboard_sse_subscribers:
                dashboard_sse_subscribers.remove(d)

def broadcast_dashboard_refresh():
    """Query fresh scorecard data and push to all dashboard clients."""
    conn = None
    try:
        conn = database.get_conn()
        scorecard = database.get_dashboard_scorecard_data(conn)
        broadcast_dashboard_event("scorecard", scorecard)
    except Exception as e:
        print(f"[dashboard] Broadcast scorecard refresh failed: {e}")
    finally:
        if conn is not None:
            database.return_conn(conn)

def refresh_worker(start_date=None, end_date=None):
    global refresh_running
    print("[background] View refresh started...")
    conn = None
    try:
        conn = database.get_conn()
        # Use the app's configured local timezone (Perth) for "today", not the
        # container's system date (UTC). date.today() previously returned the
        # UTC calendar date, which lags Perth's actual local date by one day
        # during the first ~8 hours of each Perth day (Perth is UTC+8). That
        # meant the default effective_end below was a day too early during
        # that window, so compute_basal_for_date_range's live window never
        # covered "this morning" until the UTC date caught up -- leaving
        # layer2_basal_5min without rows for those early-morning buckets at
        # the moment populate_5min_aggregate ran for them, which permanently
        # baked a NULL scheduled_basal into layer2_five_minute_aggregate for
        # that stretch (nothing re-visits an already-aggregated bucket later).
        # Fixed 23 Aug 2026 -- see STATUS.md.
        tz_name = getattr(config, "TIMEZONE", "UTC")
        try:
            db_config = database.get_system_config(conn)
            tz_name = db_config.get("TIMEZONE", tz_name)
        except Exception:
            pass
        today = datetime.now(ZoneInfo(tz_name)).date()
        # If a specific start date is provided (e.g. from a backfill segment), use it.
        # Otherwise fall back to the last 7 days (correct for the normal poll cycle path).
        effective_start = start_date if start_date is not None else (today - timedelta(days=7))
        # Likewise, an explicit end_date scopes the basal recompute to just the
        # caller's own window (e.g. a single backfill day) instead of always
        # reaching through to today. Backfill segments already compute their own
        # day's basal directly (see run_backfill) -- without this, every
        # backfill-triggered refresh redundantly recomputed basal for the ENTIRE
        # history from that segment through to today on every call, which was the
        # dominant cost of a historical backfill (~20-27 min per call). Fixed 21
        # Aug 2026. Normal poll-cycle calls never pass end_date, so this has no
        # effect on the live 5-minute heartbeat.
        effective_end = end_date if end_date is not None else today

        # 1. Refresh profile schedule BEFORE computing basal
        # (basal computation depends on layer2_profile_schedule)
        database.refresh_analysis_views(conn, specific_views=['layer2_profile_schedule'])

        # 2. Compute basal for the effective window
        database.compute_basal_for_date_range(conn, effective_start, effective_end)

        # 3. Refresh ALL analysis views (including those that depend on basal)
        database.refresh_analysis_views(conn)

        database.return_conn(conn)
        conn = None
        print("[background] View refresh complete.")

        # Broadcast fresh scorecard to connected dashboards
        try:
            broadcast_dashboard_refresh()
        except Exception as be:
            print(f"[dashboard] SSE broadcast error: {be}")
    except Exception as e:
        print(f"[background] Refresh failed: {e}")
    finally:
        if conn:
            database.return_conn(conn)
        with refresh_lock:
            refresh_running = False

def trigger_background_refresh(start_date=None, end_date=None):
    global refresh_running
    with refresh_lock:
        if refresh_running:
            print("[background] Refresh already active. Skipping duplicate trigger.")
            return
        refresh_running = True
    
    t = threading.Thread(target=refresh_worker, kwargs={"start_date": start_date, "end_date": end_date})
    t.start()

# Lock to prevent concurrent maintenance tasks
maintenance_lock = threading.Lock()

def maintenance_task_wrapper():
    if not maintenance_lock.acquire(blocking=False):
        print("[maintenance] Maintenance already in progress. Skipping duplicate.")
        return
    
    print("[maintenance] Starting scheduled maintenance...")
    conn = None
    try:
        conn = database.get_conn()
        database.run_maintenance(conn)
        # Also trigger a view refresh after maintenance to ensure charts are current
        database.refresh_analysis_views(conn)
        print("[maintenance] Scheduled maintenance complete.")
    except Exception as e:
        print(f"[maintenance] Scheduled maintenance failed: {e}")
    finally:
        maintenance_lock.release()
        if conn:
            database.return_conn(conn)

# ---------------- BACKFILL LOGIC ----------------

def to_local_str(dt: datetime):
    if not dt: return "-"
    tz_name = getattr(config, "TIMEZONE", "UTC")
    try:
        conn = database.get_conn()
        db_config = database.get_system_config(conn)
        database.return_conn(conn)
        tz_name = db_config.get("TIMEZONE", tz_name)
    except: pass
    tz = ZoneInfo(tz_name)
    # If naive, assume UTC then convert to local
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%m-%d %H:%M")

def backfill_treatments_windowed(conn, start: datetime, end: datetime, url: str = None, secret: str = None):
    cur_start = start
    total = 0
    while cur_start < end:
        if backfill_stop_event.is_set():
            break
        cur_end = min(cur_start + timedelta(days=config.BF_CHUNK_TREAT), end)
        msg = f"treatments {to_local_str(cur_start)} -> {to_local_str(cur_end)}"
        status_data["backfill"]["progress"] = msg
        print(f"[backfill] {msg}")
        
        page = nightscout_api.fetch_treatments_range(cur_start, cur_end, url=url, secret=secret)
        
        if len(page) >= config.BF_PAGE_SIZE:
             if (cur_end - cur_start) > timedelta(hours=6):
                 cur_end = cur_start + timedelta(hours=6)
                 page = nightscout_api.fetch_treatments_range(cur_start, cur_end, url=url, secret=secret)
        
        n = data_processor.process_treatments(conn, page)
        n_sc = data_processor.process_site_changes(conn, page)
        database.record_coverage(conn, "treatments", cur_start, cur_end, "backfill")
        conn.commit()
        total += n
        cur_start = cur_end

def backfill_devicestatus_windowed(conn, start: datetime, end: datetime, url: str = None, secret: str = None):
    cur_start = start
    while cur_start < end:
        if backfill_stop_event.is_set():
            break
        cur_end = min(cur_start + timedelta(days=config.BF_CHUNK_DEV), end)
        msg = f"devicestatus {to_local_str(cur_start)} -> {to_local_str(cur_end)}"
        status_data["backfill"]["progress"] = msg
        print(f"[backfill] {msg}")
        
        page = nightscout_api.fetch_devicestatus_range(cur_start, cur_end, url=url, secret=secret)
        if len(page) >= config.BF_PAGE_SIZE:
             if (cur_end - cur_start) > timedelta(hours=6):
                 cur_end = cur_start + timedelta(hours=6)
                 page = nightscout_api.fetch_devicestatus_range(cur_start, cur_end, url=url, secret=secret)

        data_processor.process_devicestatus(conn, page)
        database.record_coverage(conn, "devicestatus", cur_start, cur_end, "backfill")
        conn.commit()
        cur_start = cur_end

def get_daily_segments(start_dt: datetime, end_dt: datetime):
    # Ensure they are aware
    if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=timezone.utc)
    
    tz_name = getattr(config, "TIMEZONE", "UTC")
    try:
        conn = database.get_conn()
        db_config = database.get_system_config(conn)
        database.return_conn(conn)
        tz_name = db_config.get("TIMEZONE", tz_name)
    except: pass
    
    tz = ZoneInfo(tz_name)
    
    # Iterate in Local Time
    current_local = start_dt.astimezone(tz)
    end_local = end_dt.astimezone(tz)
    
    while current_local < end_local:
        # End of current local day
        next_day_local = (current_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seg_end_local = min(next_day_local, end_local)
        
        # Convert bounds back to UTC for the API/DB
        seg_start_utc = current_local.astimezone(timezone.utc)
        seg_end_utc = seg_end_local.astimezone(timezone.utc)
        
        is_full = (current_local.hour == 0 and current_local.minute == 0) and (seg_end_local == next_day_local)
        
        yield (current_local.year, current_local.month, current_local.day, seg_start_utc, seg_end_utc, is_full)
        current_local = next_day_local

def run_backfill():
    is_manual = status_data["backfill"]["manual_range"] is not None
    if not is_manual:
        return
    
    with status_data_lock:
        status_data["backfill"]["running"] = True
    print("[backfill] starting backfill thread...")
    
    conn = None
    try:
        conn = database.get_conn()
        conn.autocommit = False

        # Fixed 19 Aug 2026: this previously never read db_config at all, so
        # every backfill call fell through to nightscout_api's static
        # config.NIGHTSCOUT_HOST / config.NS_SECRET defaults (whatever was in
        # .env at container startup) -- completely ignoring any URL/secret
        # since changed via the Settings page. poll_cycle() already did this
        # correctly; backfill just never matched that pattern.
        db_config = database.get_system_config(conn)
        final_url = db_config.get('NIGHTSCOUT_URL', config.NIGHTSCOUT_HOST)
        final_secret = db_config.get('API_SECRET', config.NS_SECRET)
        
        if status_data["backfill"]["manual_range"]:
             start_bound = status_data["backfill"]["manual_range"]["start"]
             now = status_data["backfill"]["manual_range"]["end"]
             if start_bound == now:
                  now = datetime.now(timezone.utc)
             print(f"[backfill] MANUAL RANGE: {start_bound} -> {now}")
        else:
             print("[backfill] No manual range specified. Aborting.")
             return
        
        segments = list(get_daily_segments(start_bound, now))
        if not segments:
            print("[backfill] No segments to process.")
            status_data["backfill"]["progress"] = "No data needed for this range."
        
        for (year, month, day, seg_start, seg_end, is_full_day) in segments:
            # Check stop event or config
            if backfill_stop_event.is_set():
                print("[backfill] Cancelled by user.")
                break

            seg_key = f"backfill_{year}_{month:02d}_{day:02d}"

            # NOTE: We intentionally do NOT skip days that have a prior sentinel in
            # ingestion_state during a MANUAL backfill.  Skipping was causing the
            # post-reset scenario where every day showed "Synced" with 0 records
            # because the old user's sentinels survived the pristine reset.
            # The sentinel is still written at the end so incremental auto-recovery
            # backfills remain efficient when triggered from poll_cycle.
            msg = f"Processing {seg_key} ({to_local_str(seg_start)} -> {to_local_str(seg_end)})"
            print(f"[backfill] {msg}")
            
            e_rows = nightscout_api.fetch_entries_range_paged(seg_start, seg_end, url=final_url, secret=final_secret)
            n_e = data_processor.process_entries(conn, e_rows)
            database.record_coverage(conn, "entries", seg_start, seg_end, "backfill")
            conn.commit()
            print(f"[backfill] {seg_key}: {n_e} CGM rows upserted.")
            
            backfill_treatments_windowed(conn, seg_start, seg_end, url=final_url, secret=final_secret)
            
            if config.BF_CHUNK_DEV > 0:
                backfill_devicestatus_windowed(conn, seg_start, seg_end, url=final_url, secret=final_secret)
                
            # Compute basal for this backfilled segment (permanent storage).
            # Use the segment's own local calendar date (year, month, day, from
            # get_daily_segments) rather than seg_start.date()/seg_end.date() --
            # those are UTC-aware timestamps, so .date() returns the UTC
            # calendar date, not the local one. For Perth (UTC+8), seg_start's
            # UTC date is one day *earlier* than the local day this segment
            # actually represents, silently expanding every segment's basal
            # recompute to cover the previous local day too. Harmless in that
            # it's over-inclusive rather than missing data, but it doubled the
            # work of every backfill segment. Fixed 23 Aug 2026 -- see STATUS.md.
            seg_local_date = datetime(year, month, day).date()
            database.compute_basal_for_date_range(conn, seg_local_date, seg_local_date)
            # Generate 5-minute aggregate timeline for the segment
            database.populate_5min_aggregate(conn, seg_start, seg_end)

            # Trigger a lightweight refresh scoped to this segment's date, not a
            # hardcoded last-7-days window (which was Bug 4 — views not rebuilt for
            # older segments).
            trigger_background_refresh(start_date=seg_local_date, end_date=seg_local_date)

            if is_full_day:
                database.upsert_ingestion_state(conn, seg_key, now)
                conn.commit()
                
            if config.BACKFILL_DELAY_SECONDS > 0:
                print(f"[backfill] Sleeping {config.BACKFILL_DELAY_SECONDS}s (Soft Backfill)...")
                time.sleep(config.BACKFILL_DELAY_SECONDS)
                
        print("[backfill] Finished processing segments.")
        
        with status_data_lock:
            status_data["backfill"]["done"] = True
        print("[backfill] done")
        
    except Exception as e:
        print(f"[backfill] error: {e}")
        with status_data_lock:
            status_data["backfill"]["errors_last"] = str(e)
    finally:
        with status_data_lock:
            status_data["backfill"]["running"] = False
            status_data["backfill"]["manual_range"] = None
        backfill_stop_event.clear()
        if conn:
            database.return_conn(conn)

# ---------------- POLLING LOGIC ----------------

def poll_cycle(target_url=None, api_secret=None):
    conn = None
    max_ts = None
    n1 = 0
    try:
        conn = database.get_conn()
        conn.autocommit = False
        db_config = database.get_system_config(conn)
        now = datetime.now(timezone.utc)
        
        # Determine target and secret (prio: args > DB > config)
        final_url = target_url or db_config.get('NIGHTSCOUT_URL', config.NIGHTSCOUT_HOST)
        final_secret = api_secret or db_config.get('API_SECRET', config.NS_SECRET)
        health_url = db_config.get('HEALTHCHECK_URL', config.HEALTHCHECK_URL)
        
        last_entries = database.get_last_ts(conn, "entries") or (now - timedelta(days=1))
        last_treat = database.get_last_ts(conn, "treatments") or (now - timedelta(minutes=30))
        last_dev = database.get_last_ts(conn, "devicestatus") or (now - timedelta(minutes=30))
        
        overlap = timedelta(minutes=config.FETCH_OVERLAP_MINUTES)
        
        # Use dynamic credentials for API calls
        entries_start, entries_end = last_entries - overlap, now + timedelta(minutes=1)
        entries = nightscout_api.fetch_entries_range_paged(entries_start, entries_end, url=final_url, secret=final_secret)
        database.record_coverage(conn, "entries", entries_start, entries_end, "polling")

        treat_start, treat_end = last_treat - overlap, now + timedelta(minutes=1)
        treatments = nightscout_api.fetch_treatments_range(treat_start, treat_end, url=final_url, secret=final_secret)
        database.record_coverage(conn, "treatments", treat_start, treat_end, "polling")

        dev_start, dev_end = last_dev - overlap, now + timedelta(minutes=1)
        devs = nightscout_api.fetch_devicestatus_range(dev_start, dev_end, url=final_url, secret=final_secret)
        database.record_coverage(conn, "devicestatus", dev_start, dev_end, "polling")
        
        # Profile Sync
        n_prof = 0
        try:
            profile_state = nightscout_api.fetch_profiles(url=final_url, secret=final_secret)
            n_prof = data_processor.process_profiles(conn, profile_state)
        except Exception as pe:
            print(f"[poll] profile sync error: {pe}")

        n1 = data_processor.process_entries(conn, entries)
        n2 = data_processor.process_treatments(conn, treatments)
        n_sc = data_processor.process_site_changes(conn, treatments)
        n3 = data_processor.process_devicestatus(conn, devs)

        # --- State-Driven Auto-Recovery Logic ---
        recovery_marker = db_config.get('AUTO_RECOVERY_MARKER')
        if n1 == 0:
            # Successfully polled but 0 records. If we were healthy, mark the potential gap start.
            if not recovery_marker:
                last_good = database.get_last_ts(conn, "entries")
                if last_good and (now - last_good) < timedelta(minutes=15):
                    database.set_system_config(conn, 'AUTO_RECOVERY_MARKER', last_good.isoformat())
                    print(f"[recovery] No new data. Gap detected starting at {last_good}")
        else:
            # Data is back!
            if recovery_marker:
                print(f"[recovery] Signal restored. Triggering auto-backfill from {recovery_marker}")
                start_ts = datetime.fromisoformat(recovery_marker).replace(tzinfo=timezone.utc)
                database.delete_system_config(conn, 'AUTO_RECOVERY_MARKER')
                
                def run_auto_backfill_task():
                    with status_data_lock:
                        if status_data["backfill"]["running"]:
                            print("[recovery] Backfill already in progress. Skipping auto-trigger.")
                            return
                        status_data["backfill"]["manual_range"] = {"start": start_ts, "end": now}
                    backfill_stop_event.clear()
                    run_backfill()

                threading.Thread(target=run_auto_backfill_task, daemon=True).start()

        if n1 > 0:
            print(f"[sync] New CGM records (n={n1}). Triggering chart refresh.")
            trigger_background_refresh()
        elif n2 > 0 or n_prof > 0:
            print(f"[sync] New treatments/profile detected (n={n2+n_prof}). Triggering chart refresh.")
            trigger_background_refresh()
        
        if entries:
             raw_max = max((e.get("dateString") or e.get("created_at") or "") for e in entries)
             if raw_max: 
                 max_ts = data_processor.parse_iso(raw_max)
                 database.upsert_ingestion_state(conn, "entries", max_ts)
             
        if treatments:
             t_max = max((t.get("created_at") or "") for t in treatments)
             if t_max: database.upsert_ingestion_state(conn, "treatments", data_processor.parse_iso(t_max))

        if n1 > 0 or n2 > 0 or n3 > 0:
            # Sync 5-minute aggregate table for the recent window
            # We cover last 2 hours to capture any late-arriving devicestatus or treatments
            database.populate_5min_aggregate(conn, now - timedelta(hours=2), now + timedelta(minutes=5))

        conn.commit()
        
        if max_ts:
            delta = now - max_ts
            status_data["lag_seconds"] = int(delta.total_seconds())
            status_data["latest_entry_ts_utc"] = nightscout_api.iso_z(max_ts)

        # State-of-Health (SoH) Scan & Watchdog
        # Decision: ping_healthcheck logic now handles internal Healthy check + suppression
        has_gaps = database.has_recent_gaps(conn)
        health_meta = nightscout_api.ping_healthcheck(status_data["lag_seconds"], health_url=health_url, has_gaps=has_gaps)
        
        with status_data_lock:
            status_data["service_health"] = health_meta
            status_data["has_gaps"] = has_gaps
            status_data["last_poll_utc"] = nightscout_api.iso_z(now)

        # Removed redundant verbose log

    except Exception as e:
        print(f"Poll Error: {e}")
        with status_data_lock:
            status_data["errors_last"] = str(e)
    finally:
        if conn:
            database.return_conn(conn)
    return max_ts if n1 > 0 else None

_main_loop_started = False
def main_loop():
    global _main_loop_started
    if _main_loop_started: return
    _main_loop_started = True
    print("[daemon] Entering background maintenance loop.")
    
    # Startup Configuration Sync
    try:
         conn = database.get_conn()
         # Seed system_config from the environment first. Standard mode
         # (force_overwrite=False) only fills in keys that don't already
         # exist -- it will NOT clobber values previously set via the
         # Settings UI. FORCE_CONFIG_OVERWRITE=true is the documented
         # emergency-reset escape hatch (.env.example), forcing every key
         # back to its .env value on this one restart.
         force_overwrite = os.environ.get("FORCE_CONFIG_OVERWRITE", "false").lower() == "true"
         database.sync_system_config(conn, force_overwrite=force_overwrite)

         # Load settings from DB at startup to update config.TIMEZONE etc.
         if 'load_settings_from_db' in globals():
             load_settings_from_db()
         
         database.return_conn(conn)
    except Exception as e:
         print(f"[daemon] Startup sync failed: {e}")
         
    while True:
        try:
            conn = database.get_conn()
            db_config = database.get_system_config(conn)
            database.return_conn(conn)
            
            # 3AM Maintenance Check (Local Time)
            tz_name = db_config.get('TIMEZONE', getattr(config, "TIMEZONE", "UTC"))
            now_local = datetime.now(ZoneInfo(tz_name))
            
            with status_data_lock:
                lmd = status_data["last_maintenance_date"]
                if now_local.hour == 3 and (lmd is None or lmd != now_local.date()):
                    print(f"[main] 3AM Maintenance Triggered for {now_local.date()} (TZ: {tz_name})")
                    status_data["last_maintenance_date"] = now_local.date()
                    threading.Thread(target=maintenance_task_wrapper, daemon=True).start()

            time.sleep(60)

        except Exception as e:
            print(f"[main_loop] Internal Error: {e}")
            time.sleep(60)

# ---------------- WEB ROUTES ----------------
@app.route("/config")
@app.route("/settings", methods=["GET"])
def config_page():
    return render_template("index.html")

@app.route("/sanity_check")
@app.route("/maintenance")
def sanity_check_page():
    return render_template("sanity_check.html")

@app.route("/api/stats")
def api_stats():
    db_config = {}
    db_stats = []
    include_db_stats = request.args.get("db_stats", "false").lower() == "true"
    try:
        conn = database.get_conn()
        if include_db_stats:
            db_stats = database.get_dbsize_stats(conn)
        db_config = database.get_system_config(conn)
        database.return_conn(conn)
    except Exception as e:
        print(f"Stats Error: {e}")

    current_config = {
        "NIGHTSCOUT_URL":                    db_config.get("NIGHTSCOUT_URL", config.NIGHTSCOUT_HOST),
        "API_SECRET":                        "********", # Redacted for UI
        "POLLING_ENABLED":                   db_config.get("POLLING_ENABLED", "true"),
        "POLL_PERIOD_SECONDS":               int(db_config.get("POLL_PERIOD_SECONDS", config.POLL_PERIOD_SECONDS)),
        "POLL_OFFSET_SECONDS":               int(db_config.get("POLL_OFFSET_SECONDS", config.POLL_OFFSET_SECONDS)),
        "TIMEZONE":                          db_config.get("TIMEZONE", getattr(config, "TIMEZONE", "UTC")),
        "HEALTHCHECK_URL":                   db_config.get("HEALTHCHECK_URL", config.HEALTHCHECK_URL),
        "RETENTION_DAYS":                    int(db_config.get("RETENTION_DAYS", getattr(config, "RETENTION_DAYS", 365))),
        "BACKFILL_DELAY_SECONDS":            float(db_config.get("BACKFILL_DELAY_SECONDS", config.BACKFILL_DELAY_SECONDS)),
        "BACKFILL_CHUNK_DAYS_ENTRIES":       int(db_config.get("BACKFILL_CHUNK_DAYS_ENTRIES", config.BF_CHUNK_ENTRIES)),
        "BACKFILL_CHUNK_DAYS_TREATMENTS":    int(db_config.get("BACKFILL_CHUNK_DAYS_TREATMENTS", config.BF_CHUNK_TREAT)),
        "BACKFILL_CHUNK_DAYS_DEVICESTATUS":  int(db_config.get("BACKFILL_CHUNK_DAYS_DEVICESTATUS", config.BF_CHUNK_DEV)),
        "BACKFILL_PAGE_SIZE":                int(db_config.get("BACKFILL_PAGE_SIZE", config.BF_PAGE_SIZE)),
        "BACKFILL_SLEEP_BETWEEN_PAGES_SEC":  float(db_config.get("BACKFILL_SLEEP_BETWEEN_PAGES_SEC", config.BF_SLEEP_PAGES)),
        "BACKFILL_SLEEP_BETWEEN_WINDOWS_SEC": float(db_config.get("BACKFILL_SLEEP_BETWEEN_WINDOWS_SEC", config.BF_SLEEP_WINDOWS)),
        "BACKFILL_ON_START":                 False,
        "BACKFILL_DAYS":                     0,
    }
    
    return jsonify({
        "status": status_data,
        "db_stats": db_stats,
        "config": current_config
    })

@app.route("/api/latest")
def get_latest():
    try:
        conn = database.get_conn()
        metrics = database.get_latest_metrics(conn)
        database.return_conn(conn)
        return jsonify(metrics)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

from flask import request

_earliest_cgm_cache = {"date": None, "ts": 0}

def _get_cached_earliest_cgm_date():
    now_sec = time.time()
    if _earliest_cgm_cache["date"] and (now_sec - _earliest_cgm_cache["ts"] < 3600):
        return _earliest_cgm_cache["date"]
    try:
        conn = database.get_conn()
        d = database.get_earliest_cgm_date(conn)
        database.return_conn(conn)
        if d:
            _earliest_cgm_cache["date"] = d
            _earliest_cgm_cache["ts"] = now_sec
            return d
    except Exception as e:
        print(f"[main] Error fetching earliest CGM date: {e}")
    return _earliest_cgm_cache.get("date")

@app.context_processor
def inject_nav_variables():
    # Extracts the client-side hostname addressing your server (e.g. 192.168.1.5 or localhost)
    try:
        ns_host = request.host.split(':')[0]
    except Exception:
        ns_host = 'localhost'

    # Plain site root for the user's own Nightscout instance (strip the
    # /api/v1 suffix used for the authenticated API calls) - this link is
    # for browsing/logging into the site directly, not the read-only token.
    try:
        conn = database.get_conn()
        db_config_for_nav = database.get_system_config(conn)
        database.return_conn(conn)
    except Exception:
        db_config_for_nav = {}
    raw_ns_url = (db_config_for_nav.get("NIGHTSCOUT_URL") or config.NIGHTSCOUT_HOST or "").strip()
    nightscout_site_url = raw_ns_url.rstrip("/")
    if nightscout_site_url.endswith("/api/v1"):
        nightscout_site_url = nightscout_site_url[: -len("/api/v1")]
    if not nightscout_site_url:
        nightscout_site_url = "#"

    footer_text = "Generated by ns-monitor v1.0"
    earliest_cgm_date = _get_cached_earliest_cgm_date()

    return dict(ns_host=ns_host,
                nightscout_site_url=nightscout_site_url,
                footer_text=footer_text,
                earliest_cgm_date=earliest_cgm_date)

def _trends_date_range():
    # Shared by AGP and Timeline: both pages take the same start_date/
    # end_date query params and default to the same trailing 7-day window.
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    today_local = datetime.now(_tz).strftime("%Y-%m-%d")
    yesterday_local = (datetime.now(_tz) - timedelta(days=1)).strftime("%Y-%m-%d")
    end_date_str = request.args.get('end_date', yesterday_local)
    start_date_str = request.args.get('start_date')

    if not start_date_str:
        end_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
        start_date_str = (end_dt - timedelta(days=6)).strftime('%Y-%m-%d')

    with status_data_lock:
        has_errors = status_data["errors_last"] is not None
        has_gaps = status_data.get("has_gaps", False)

    return today_local, start_date_str, end_date_str, has_errors, has_gaps

@app.route("/")
@app.route("/dashboard")
@app.route("/dashboard/<int:slot_number>")
def dashboard_home_page(slot_number=None):
    # Modular Clinical Dashboard home page (Phase 1 & Kiosk)
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    today_local = datetime.now(_tz).strftime("%Y-%m-%d")
    is_kiosk = request.args.get("kiosk") in ("1", "true", "yes") or request.args.get("fullscreen") in ("1", "true", "yes")
    resp = make_response(render_template("dashboard_home.html",
                                         today_local=today_local,
                                         initial_slot=slot_number,
                                         is_kiosk=is_kiosk))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/v1/dashboard/slots", methods=["GET"])
def api_dashboard_slots():
    conn = None
    try:
        conn = database.get_conn()
        slots = database.get_dashboard_slots(conn)
        return jsonify(slots)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

@app.route("/api/v1/dashboard/slots/active", methods=["POST"])
def api_dashboard_set_active_slot():
    data = request.get_json() or {}
    slot_number = data.get("slot_number")
    if not isinstance(slot_number, int) or slot_number < 1 or slot_number > 6:
        return jsonify({"error": "slot_number must be an integer between 1 and 6"}), 400

    conn = None
    try:
        conn = database.get_conn()
        updated = database.set_active_dashboard_slot(conn, int(slot_number))
        layout = database.get_dashboard_layout(conn, int(slot_number))
        return jsonify({"success": updated, "layout": layout})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

@app.route("/api/v1/dashboard/slots/reorder", methods=["POST"])
def api_dashboard_reorder_slots():
    data = request.get_json() or {}
    slots = data.get("slots", [])
    if not isinstance(slots, list):
        return jsonify({"error": "Field 'slots' must be a list"}), 400

    conn = None
    try:
        conn = database.get_conn()
        updated_slots = database.reorder_dashboard_slots(conn, slots)
        return jsonify({"success": True, "slots": updated_slots})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

@app.route("/api/v1/dashboard/layout", methods=["GET"])
def api_dashboard_get_layout():
    slot_number = request.args.get("slot", type=int)
    conn = None
    try:
        conn = database.get_conn()
        layout = database.get_dashboard_layout(conn, slot_number)
        if not layout:
            return jsonify({"error": "Layout not found"}), 404
        resp = jsonify(layout)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

@app.route("/api/v1/dashboard/layout", methods=["POST"])
def api_dashboard_save_layout():
    data = request.get_json() or {}
    slot_number = data.get("slot_number")
    if not isinstance(slot_number, int) or slot_number < 1 or slot_number > 6:
        return jsonify({"error": "slot_number must be an integer between 1 and 6"}), 400

    name = data.get("name")
    widgets = data.get("widgets", [])
    edited_profile = data.get("edited_profile", "computer")
    if not isinstance(widgets, list):
        return jsonify({"error": "widgets must be a list"}), 400
    if edited_profile not in database.DEVICE_PROFILES:
        return jsonify({"error": "edited_profile must be a supported device profile"}), 400
    if slot_number == 1 and edited_profile != "computer":
        return jsonify({"error": "Dashboard slot 1 must use the computer profile"}), 400

    conn = None
    try:
        conn = database.get_conn()
        saved = database.save_dashboard_layout(conn, int(slot_number), name, widgets, edited_profile)
        return jsonify({"success": True, "layout": saved})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

@app.route("/api/v1/dashboard/scorecard", methods=["GET"])
def api_dashboard_scorecard():
    conn = None
    try:
        conn = database.get_conn()
        scorecard = database.get_dashboard_scorecard_data(conn)
        resp = jsonify(scorecard)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

@app.route("/api/v1/dashboard/stream", methods=["GET"])
def api_dashboard_stream():
    """Server-Sent Events stream for real-time telemetry, scorecard updates, and display control."""
    q = queue.Queue(maxsize=50)
    with dashboard_sse_lock:
        dashboard_sse_subscribers.append(q)

    def event_stream():
        # Immediate welcome event
        yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"
        try:
            while True:
                try:
                    event_type, data = q.get(timeout=25.0)
                    yield f"event: {event_type}\ndata: {dashboard_event_json(data)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            with dashboard_sse_lock:
                if q in dashboard_sse_subscribers:
                    dashboard_sse_subscribers.remove(q)

    resp = Response(event_stream(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp

@app.route("/api/v1/dashboard/display", methods=["POST", "GET"])
def api_dashboard_display():
    """Endpoint for Home Assistant or remote presence automations to control tablet display sleep/wake."""
    data = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
    state = data.get("state") or request.args.get("state") or ""
    state = str(state).strip().lower()
    if state not in ("sleep", "wake", "toggle"):
        return jsonify({"error": "Query param or JSON 'state' must be 'sleep', 'wake', or 'toggle'"}), 400

    target_slot = data.get("slot") or request.args.get("slot")
    payload = {
        "state": state,
        "slot": int(target_slot) if (target_slot and str(target_slot).isdigit()) else None,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    broadcast_dashboard_event("display", payload)
    return jsonify({"success": True, "broadcasted": payload})

@app.route("/patterns")
@app.route("/interactive")
def agp_page():
    # 24h Modal Patterns (AGP)
    today_local, start_date_str, end_date_str, has_errors, has_gaps = _trends_date_range()
    return render_template("agp.html",
                         start_date=start_date_str, end_date=end_date_str,
                         has_errors=has_errors,
                         has_gaps=has_gaps,
                         today_local=today_local)

@app.route("/trends")
@app.route("/timeline")
def timeline_page():
    today_local, start_date_str, end_date_str, has_errors, has_gaps = _trends_date_range()
    return render_template("timeline.html",
                         start_date=start_date_str, end_date=end_date_str,
                         has_errors=has_errors,
                         has_gaps=has_gaps,
                         today_local=today_local)

@app.route("/profiles")
def profiles_page():
    today_local, start_date_str, end_date_str, has_errors, has_gaps = _trends_date_range()
    return render_template("profiles.html",
                         start_date=start_date_str, end_date=end_date_str,
                         has_errors=has_errors,
                         has_gaps=has_gaps,
                         today_local=today_local)

@app.route("/api/v1/profiles/history", methods=["GET"])
def api_profiles_history():
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    conn = None
    try:
        conn = database.get_conn()
        eras = profile_history.get_profile_history(conn, start_date, end_date)
        return jsonify({"eras": eras})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

def _summary_table_data():
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    today_local = datetime.now(_tz).strftime("%Y-%m-%d")

    anchor_date_str = request.args.get("anchor_date", today_local)

    try:
        conn = database.get_conn()
        metrics = database.get_dashboard_metrics(conn, anchor_date_str)
        database.return_conn(conn)
    except Exception as e:
        metrics = []
        print(f"Error loading metrics: {e}")

    with status_data_lock:
        has_errors = status_data["errors_last"] is not None
        has_gaps = status_data.get("has_gaps", False)

    return today_local, anchor_date_str, metrics, has_errors, has_gaps


@app.route("/carpet_plot")
@app.route("/carpet-plot")
def carpet_plot_page():
    return render_template("carpet_plot.html")


@app.route("/api/v1/carpet_plot")
@app.route("/api/carpet_plot")
def api_carpet_plot():
    conn = database.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE((SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'Australia/Perth')")
        tz_name = cur.fetchone()[0]

        cur.execute("""
            SELECT 
                (ts AT TIME ZONE %s)::date AS local_day,
                (EXTRACT(hour FROM (ts AT TIME ZONE %s))::int * 12 + (EXTRACT(minute FROM (ts AT TIME ZONE %s))::int / 5)) AS slot,
                ROUND(bg::numeric, 1) AS bg
            FROM layer2_five_minute_aggregate
            WHERE bg IS NOT NULL
            ORDER BY local_day, slot
        """, (tz_name, tz_name, tz_name))

        rows = cur.fetchall()
        if not rows:
            return jsonify({"days": [], "matrix": [], "min_date": None, "max_date": None})

        min_date = rows[0][0]
        max_date = rows[-1][0]

        day_count = (max_date - min_date).days + 1
        days = [(min_date + timedelta(days=i)).isoformat() for i in range(day_count)]
        day_index_map = {day_str: i for i, day_str in enumerate(days)}

        matrix = [[None] * 288 for _ in range(day_count)]
        for r_day, r_slot, r_bg in rows:
            day_str = r_day.isoformat()
            if day_str in day_index_map and 0 <= r_slot < 288:
                matrix[day_index_map[day_str]][r_slot] = float(r_bg)

        return jsonify({
            "days": days,
            "min_date": min_date.isoformat(),
            "max_date": max_date.isoformat(),
            "matrix": matrix
        })
    finally:
        database.return_conn(conn)


@app.route("/summary_table")
def summary_table_page():
    today_local, anchor_date_str, metrics, has_errors, has_gaps = _summary_table_data()
    return render_template("summary_table.html",
                         anchor_date=anchor_date_str,
                         metrics=metrics,
                         has_errors=has_errors,
                         has_gaps=has_gaps,
                         today_local=today_local)

@app.route("/static")
def dashboard_standard():
    # Kept as an alias for old bookmarks - the page now lives at /summary_table
    today_local, anchor_date_str, metrics, has_errors, has_gaps = _summary_table_data()
    return render_template("dashboard.html",
                         anchor_date=anchor_date_str,
                         metrics=metrics,
                         has_errors=has_errors,
                         has_gaps=has_gaps,
                         today_local=today_local)


@app.route("/trace")
@app.route("/daily")
def dashboard_daily():
    # Continuous-scroll chart (promoted from the Sandbox prototype, 23 Aug
    # 2026) -- replaces the old single-day/jump-a-day-at-a-time version.
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    today_local = datetime.now(_tz).strftime("%Y-%m-%d")
    target_date = request.args.get('date', today_local)
    return render_template("dashboard_daily.html", target_date=target_date, today_local=today_local)

@app.route("/calendar")
def calendar_page():
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    today_local = datetime.now(_tz).strftime("%Y-%m-%d")
    return render_template("calendar.html", today_local=today_local)

@app.route("/diary")
def diary_page():
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    today_local = datetime.now(_tz).strftime("%Y-%m-%d")
    return render_template("diary.html", today_local=today_local)

@app.route("/sandbox")
def sandbox_page():
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    today_local = datetime.now(_tz).strftime("%Y-%m-%d")
    today = datetime.now()
    start_date = (today - timedelta(days=730)).strftime('%Y-%m-%d')
    end_date = today_local
    return render_template("sandbox.html", start_date=start_date, end_date=end_date, today_local=today_local)

@app.route('/api/v1/periodicity')
def api_periodicity():
    start_date, end_date = request.args.get('start_date'), request.args.get('end_date')
    metric = request.args.get('metric', 'cv')
    if metric not in {'cv','titr','tir','tdd','mean','sd'} or not start_date or not end_date:
        return jsonify({'error': 'Valid start_date, end_date, and metric are required.'}), 400
    try:
        period = float(request.args['period']) if request.args.get('period') else None
    except ValueError:
        return jsonify({'error': 'Period must be between 2.0 and 400.0 days.'}), 400
    if period is not None and not periodicity.MIN_PERIOD <= period <= periodicity.MAX_PERIOD:
        return jsonify({'error': 'Period must be between 2.0 and 400.0 days.'}), 400
    try:
        conn = database.get_conn()
        try:
            records = database.get_periodicity_daily_records(conn, start_date, end_date)
        finally:
            database.return_conn(conn)
        res = jsonify(periodicity.analyse(records, metric, request.args.get('detrend', 'true').lower() != 'false', period))
        res.headers['Cache-Control'] = 'no-store'
        return res
    except Exception as e:
        app.logger.exception('Periodicity analysis failed')
        return jsonify({'error': str(e)}), 500


@app.route("/cgm")
def cgm_page():
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    today_local = datetime.now(_tz).strftime("%Y-%m-%d")
    today = datetime.now()
    start_date = (today - timedelta(days=7)).strftime('%Y-%m-%d')
    end_date = today_local
    return render_template("cgm.html", start_date=start_date, end_date=end_date, today_local=today_local)


@app.route("/hba1c")
def hba1c_page():
    conn = None
    try:
        conn = database.get_conn()
        records = database.get_all_hba1c_records(conn)
        custom_gmi_constants = _get_hba1c_custom_gmi_constants(conn)
        tz = _get_configured_tz()
        today = datetime.now(tz).date()
        end_date = request.args.get("end_date", "").strip() or today.strftime("%Y-%m-%d")
        start_date = request.args.get("start_date", "").strip() or (today - timedelta(days=365)).strftime("%Y-%m-%d")
        return render_template(
            "hba1c.html",
            hba1c_records=records,
            custom_gmi_constants=custom_gmi_constants,
            start_date=start_date,
            end_date=end_date,
            today_local=today.strftime("%Y-%m-%d"),
            earliest_cgm_date=_get_cached_earliest_cgm_date(),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


def _get_hba1c_custom_gmi_constants(conn):
    """Return the one persistent personalised HbA1c model for this user."""
    config_values = database.get_system_config(conn)
    try:
        const_a = float(config_values.get("HBA1C_CUSTOM_GMI_CONST_A", "3.31"))
        const_b = float(config_values.get("HBA1C_CUSTOM_GMI_CONST_B", "0.4306"))
    except (TypeError, ValueError):
        const_a, const_b = 3.31, 0.4306
    if not math.isfinite(const_a) or not math.isfinite(const_b):
        const_a, const_b = 3.31, 0.4306
    # Coefficients saved before the mmol/L model used mg/dL as x. Preserve
    # their predicted line while presenting the new mmol/L coefficient.
    if (config_values.get("HBA1C_CUSTOM_GMI_UNIT") != "mmol/L"
            and "HBA1C_CUSTOM_GMI_CONST_B" in config_values):
        const_b *= 18.0182
    return {"const_a": const_a, "const_b": const_b}


@app.route("/api/v1/hba1c_results", methods=["GET"])
def api_hba1c_results():
    conn = None
    try:
        conn = database.get_conn()
        records = database.get_all_hba1c_records(conn)
        return jsonify(records)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/hba1c_custom_gmi", methods=["GET", "PUT"])
def api_hba1c_custom_gmi():
    conn = None
    try:
        conn = database.get_conn()
        if request.method == "GET":
            return jsonify(_get_hba1c_custom_gmi_constants(conn))

        payload = request.get_json(silent=True) or {}
        const_a = payload.get("const_a")
        const_b = payload.get("const_b")
        try:
            const_a = float(const_a)
            const_b = float(const_b)
        except (TypeError, ValueError):
            return jsonify({"error": "Both Custom GMI constants must be numeric."}), 400
        if not math.isfinite(const_a) or not math.isfinite(const_b):
            return jsonify({"error": "Both Custom GMI constants must be finite numbers."}), 400

        database.set_system_config(conn, "HBA1C_CUSTOM_GMI_CONST_A", const_a)
        database.set_system_config(conn, "HBA1C_CUSTOM_GMI_CONST_B", const_b)
        database.set_system_config(conn, "HBA1C_CUSTOM_GMI_UNIT", "mmol/L")
        return jsonify({"const_a": const_a, "const_b": const_b})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/hba1c_custom_gmi/pairs", methods=["GET"])
def api_hba1c_custom_gmi_pairs():
    conn = None
    try:
        start_date = (request.args.get("start_date") or request.args.get("start") or "").strip()
        end_date = (request.args.get("end_date") or request.args.get("end") or "").strip()
        if not start_date or not end_date:
            return jsonify({"error": "start_date and end_date are required."}), 400
        conn = database.get_conn()
        return jsonify({"data": database.get_hba1c_custom_gmi_pairs(conn, start_date, end_date)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

@app.route("/mockup/<int:opt>")
def menu_mockup_page(opt):
    if opt < 1 or opt > 5:
        opt = 1
    return render_template("menu_mockups.html", opt=opt)

@app.route("/api/v1/cgm_health", methods=["GET"])
def api_cgm_health():
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    conn = None
    try:
        conn = database.get_conn()
        return jsonify({
            "coverage": cgm_health.get_coverage_stats(conn, start_date, end_date),
            "staleness": cgm_health.get_loop_staleness(conn, start_date, end_date),
            "gaps": cgm_health.get_gap_breakdown(conn, start_date, end_date),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

@app.route("/best_worst")
@app.route("/best-worst")
def best_worst_page():
    return render_template("best_worst.html")

@app.route("/api/v1/earliest_cgm_date", methods=["GET"])
def api_earliest_cgm_date():
    d = _get_cached_earliest_cgm_date()
    return jsonify({"earliest_date": d})

@app.route("/api/v1/daily_chart_data_range")
def api_daily_chart_data_range():
    # Range variant backing Daily's continuous-scroll chart -- returns a
    # multi-day buffer in one call so the frontend can pan across it without
    # re-fetching on every pixel of drag. See database.get_daily_chart_data_range().
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    if not start_date or not end_date:
        return jsonify({"error": "Missing start/end"}), 400

    try:
        conn = database.get_conn()
        data = database.get_daily_chart_data_range(conn, start_date, end_date)
        basal_events = database.get_exact_basal_events_range(conn, start_date, end_date)
        database.return_conn(conn)
        return jsonify({"data": data, "basal_events": basal_events})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/v1/sandbox_chart_data_range")
def api_sandbox_chart_data_range():
    # Range variant backing the continuous-scroll prototype (23 Aug 2026) --
    # returns a multi-day buffer in one call so the frontend can pan across
    # it without re-fetching on every pixel of drag. See
    # database.get_sandbox_chart_data_range().
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    if not start_date or not end_date:
        return jsonify({"error": "Missing start/end"}), 400

    try:
        conn = database.get_conn()
        data = database.get_sandbox_chart_data_range(conn, start_date, end_date)
        database.return_conn(conn)
        return jsonify({"data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/calendar/sparkline_data")
def api_calendar_sparkline_data():
    # Backs Calendar's BG Sparkline overlay (31 Aug 2026) -- lean per-day
    # [minute_of_day, bg] series, see database.get_calendar_sparkline_data().
    # Deliberately its own endpoint, not reusing /api/v1/daily_chart_data_range
    # (that one carries iob/cob/isf/basal/etc per point, unneeded weight
    # multiplied across dozens of buffered days for a sparkline).
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    if not start_date or not end_date:
        return jsonify({"error": "Missing start/end"}), 400

    conn = None
    try:
        conn = database.get_conn()
        data = database.get_calendar_sparkline_data(conn, start_date, end_date)
        return jsonify({"data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/sandbox/best_worst_period")
def api_sandbox_best_worst_period():
    """Sandbox best/worst/longest period search."""
    direction = request.args.get("direction", "highest").lower()
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    if not start_date or not end_date:
        return jsonify({"error": "Missing start/end date"}), 400
    # Boundary check: from must not be after to. ISO dates compare correctly
    # as strings, but parse anyway so a malformed date fails loudly here
    # rather than as an opaque database error further down.
    try:
        s_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        e_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Dates must be YYYY-MM-DD"}), 400
    if s_dt > e_dt:
        return jsonify({"error": "'From' date must not be after the 'To' date"}), 400

    statistic = request.args.get("stat", "titr" if direction == "longest" else "tir").lower()

    if direction == "longest":
        operator = request.args.get("op", ">=")
        try:
            threshold = float(request.args.get("threshold", "100" if statistic == "titr" else "7.0"))
        except ValueError:
            return jsonify({"error": "threshold must be a number"}), 400

        conn = None
        try:
            conn = database.get_conn()
            payload = database.find_longest_streak_period(
                conn, statistic, operator, threshold, start_date, end_date, limit=10
            )
            return jsonify(payload)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            if conn is not None:
                database.return_conn(conn)

    try:
        period_days = int(request.args.get("period", "7"))
    except ValueError:
        return jsonify({"error": "period must be a whole number of days"}), 400

    try:
        min_coverage = float(request.args.get("min_coverage", "80"))
    except ValueError:
        return jsonify({"error": "min_coverage must be a number"}), 400

    max_overlap_pct = None
    raw_overlap = request.args.get("max_overlap_pct")
    if raw_overlap is not None and raw_overlap != "":
        try:
            val = float(raw_overlap)
            if 0.0 <= val <= 100.0:
                max_overlap_pct = val
        except ValueError:
            pass

    conn = None
    try:
        conn = database.get_conn()
        payload = database.find_best_worst_period(
            conn, statistic, period_days, start_date, end_date,
            direction=direction, min_coverage=min_coverage, limit=10,
            max_overlap_pct=max_overlap_pct
        )
        return jsonify(payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


# ---------------------------------------------------------------------------
# Phase 5: Filter & Target Engine Routes (§6)
# ---------------------------------------------------------------------------
@app.route("/filter_test")
def filter_test_page():
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    today_local = datetime.now(_tz).strftime("%Y-%m-%d")
    default_start = (datetime.now(_tz) - timedelta(days=29)).strftime("%Y-%m-%d")
    return render_template("filter_test.html", today_local=today_local, default_start=default_start)

@app.route("/api/v1/filters/metrics", methods=["GET"])
def api_filters_metrics():
    return jsonify(filter_engine.FILTER_METRICS)

@app.route("/api/v1/filters/evaluate", methods=["POST"])
def api_filters_evaluate():
    payload = request.get_json()
    if not payload:
        return jsonify({"error": "Missing JSON request body"}), 400

    try:
        filter_engine.validate_filter_payload(payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    conn = None
    try:
        conn = database.get_conn()
        result = filter_engine.evaluate_filter_tree(conn, payload["filter"], payload["dateRange"])
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/calendar/metric_values", methods=["GET"])
def api_calendar_metric_values():
    metric = request.args.get("metric", "").strip()
    window = request.args.get("window", "single_day").strip()
    rolling_days_str = request.args.get("rollingDays", None)
    rolling_days = None
    if rolling_days_str is not None:
        try:
            rolling_days = int(rolling_days_str)
        except ValueError:
            return jsonify({"error": f"Invalid rollingDays '{rolling_days_str}', must be an integer"}), 400

    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()

    time_scope_start = request.args.get("timeScopeStart", None)
    time_scope_end = request.args.get("timeScopeEnd", None)

    time_scope = None
    if time_scope_start and time_scope_end:
        time_scope = {"start": time_scope_start.strip(), "end": time_scope_end.strip()}
    elif request.args.get("timeScope"):
        ts_val = request.args.get("timeScope", "").strip()
        if "-" in ts_val:
            parts = ts_val.split("-", 1)
            time_scope = {"start": parts[0].strip(), "end": parts[1].strip()}

    conn = None
    try:
        conn = database.get_conn()
        result = calendar_provider.get_calendar_metric_values(
            conn=conn,
            metric_id=metric,
            window_type=window,
            rolling_days=rolling_days,
            start_date=start,
            end_date=end,
            time_scope=time_scope,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


# ---------------------------------------------------------------------------
# Phase 8: Targets CRUD API
# ---------------------------------------------------------------------------
@app.route("/api/v1/targets", methods=["GET"])
def api_list_targets():
    conn = None
    try:
        conn = database.get_conn()
        targets = database.list_targets(conn)
        return jsonify(targets)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/targets", methods=["POST"])
def api_create_target():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    name = data.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        return jsonify({"error": "Target name is required and cannot be empty"}), 400
    name = name.strip()

    rule = data.get("rule")
    if not rule or not isinstance(rule, dict):
        return jsonify({"error": "Target rule object is required"}), 400

    try:
        filter_engine.validate_target_rule(rule)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    target_kind = data.get("target_kind", "evaluative")
    if target_kind not in ("evaluative", "reference"):
        return jsonify({"error": f"Invalid target_kind '{target_kind}'. Allowed: evaluative, reference"}), 400

    display_defaults = data.get("display_defaults")
    if display_defaults is not None and not isinstance(display_defaults, dict):
        return jsonify({"error": "display_defaults must be a JSON object"}), 400

    conn = None
    try:
        conn = database.get_conn()
        target = database.create_target(conn, name, rule, target_kind, display_defaults)
        return jsonify(target), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/targets/<int:target_id>", methods=["GET"])
def api_get_target(target_id: int):
    conn = None
    try:
        conn = database.get_conn()
        target = database.get_target(conn, target_id)
        if not target:
            return jsonify({"error": f"Target {target_id} not found"}), 404
        return jsonify(target)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/targets/<int:target_id>", methods=["PUT"])
def api_update_target(target_id: int):
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    name = data.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            return jsonify({"error": "Target name cannot be empty"}), 400
        name = name.strip()

    rule = data.get("rule")
    if rule is not None:
        if not isinstance(rule, dict):
            return jsonify({"error": "Target rule must be a JSON object"}), 400
        try:
            filter_engine.validate_target_rule(rule)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    target_kind = data.get("target_kind")
    if target_kind is not None and target_kind not in ("evaluative", "reference"):
        return jsonify({"error": f"Invalid target_kind '{target_kind}'. Allowed: evaluative, reference"}), 400

    display_defaults = data.get("display_defaults")
    if display_defaults is not None and not isinstance(display_defaults, dict):
        return jsonify({"error": "display_defaults must be a JSON object"}), 400

    conn = None
    try:
        conn = database.get_conn()
        target = database.update_target(conn, target_id, name, rule, target_kind, display_defaults)
        if not target:
            return jsonify({"error": f"Target {target_id} not found"}), 404
        return jsonify(target)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/targets/<int:target_id>", methods=["DELETE"])
def api_delete_target(target_id: int):
    conn = None
    try:
        conn = database.get_conn()
        blocking = database.check_target_deletion_guard(conn, target_id)
        if blocking:
            return jsonify({
                "status": "error",
                "error": "Cannot delete: referenced by saved views",
                "blocking_views": blocking
            }), 409
        deleted = database.delete_target(conn, target_id)
        if not deleted:
            return jsonify({"error": f"Target {target_id} not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


# ---------------------------------------------------------------------------
# Phase 8: Saved Views CRUD API
# ---------------------------------------------------------------------------
@app.route("/api/v1/saved_views", methods=["GET"])
def api_list_saved_views():
    page = request.args.get("page", "").strip()
    if not page or page not in ("trace", "trends", "patterns", "calendar", "hba1c"):
        return jsonify({"error": "Missing or invalid page parameter. Allowed: 'trace', 'trends', 'patterns', 'calendar', 'hba1c'"}), 400

    conn = None
    try:
        conn = database.get_conn()
        views = database.list_saved_views(conn, page)
        return jsonify(views)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/saved_views", methods=["POST"])
def api_create_saved_view():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # --- Required fields ---------------------------------------------------
    page = data.get("page", "").strip()
    if not page or page not in ("trace", "trends", "patterns", "calendar", "hba1c"):
        return jsonify({"error": "Missing or invalid page parameter. Allowed: 'trace', 'trends', 'patterns', 'calendar', 'hba1c'"}), 400

    name = data.get("name", "")
    if not isinstance(name, str):
        return jsonify({"error": "View name must be a string"}), 400
    name = name.strip()

    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        return jsonify({"error": "payload must be a JSON object"}), 400

    # Optional flags (default false/null)
    is_builtin = bool(data.get("is_builtin", False))
    source_default_id = data.get("source_default_id")
    if source_default_id is not None and not isinstance(source_default_id, str):
        return jsonify({"error": "source_default_id must be a string"}), 400

    is_deleted = bool(data.get("is_deleted", False))

    conn = None
    try:
        conn = database.get_conn()
        view = database.create_saved_view(conn, page, name, payload, is_builtin, source_default_id, is_deleted)
        return jsonify(view), 201
    except Exception as e:
        # Unique‑name conflict (PostgreSQL error code 23505)
        if hasattr(e, "pgcode") and e.pgcode == "23505":
            return jsonify({"error": "A saved view with this name already exists"}), 409
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/saved_views/<int:view_id>", methods=["GET"])
def api_get_saved_view(view_id: int):
    conn = None
    try:
        conn = database.get_conn()
        view = database.get_saved_view(conn, view_id)
        if not view:
            return jsonify({"error": f"Saved view {view_id} not found"}), 404
        return jsonify(view)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/saved_views/<int:view_id>", methods=["PUT"])
def api_update_saved_view(view_id: int):
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    name = data.get("name")
    if name is not None:
        if not isinstance(name, str):
            return jsonify({"error": "View name must be a string"}), 400
        name = name.strip()

    payload = data.get("payload")
    if payload is not None and not isinstance(payload, dict):
        return jsonify({"error": "payload must be a JSON object"}), 400

    is_deleted = data.get("is_deleted")
    if is_deleted is not None:
        is_deleted = bool(is_deleted)

    conn = None
    try:
        conn = database.get_conn()
        view = database.update_saved_view(conn, view_id, name, payload, is_deleted)
        if not view:
            return jsonify({"error": f"Saved view {view_id} not found"}), 404
        return jsonify(view)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/saved_views/<int:view_id>", methods=["DELETE"])
def api_delete_saved_view(view_id: int):
    conn = None
    try:
        conn = database.get_conn()
        deleted = database.delete_saved_view(conn, view_id)
        if not deleted:
            return jsonify({"error": f"Saved view {view_id} not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


# ---------------------------------------------------------------------------
# Phase 7b: Filters CRUD API
# ---------------------------------------------------------------------------
@app.route("/api/v1/filters", methods=["GET"])
def api_list_filters():
    conn = None
    try:
        conn = database.get_conn()
        filters_list = database.get_filters(conn)
        return jsonify(filters_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


# ---------------------------------------------------------------------------
# Filter form validation constants/helpers (Phase 0, 31 Aug 2026)
# Name is kept short deliberately -- it has to fit as a legend label, a
# popover-list row, and a saved-view pill, not carry the filter's full
# purpose (that's what `notes` is for). Notes has a generous cap since it's
# free-form explanation text, not a display label.
# ---------------------------------------------------------------------------
FILTER_NAME_MAX_LEN = 30
FILTER_NOTES_MAX_LEN = 200
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

ALLOWED_DISPLAY_TYPES = (
    "fill", "badge", "circle", "square", "triangle", "tick", "cross", "value",
    "num_badge", "custom_text",
    # Legacy aliases
    "fill-color", "corner-badge", "status-icon", "number"
)

ALLOWED_DISPLAY_LOCATIONS = (
    "top-left", "top-center", "top-right",
    "mid-left", "center", "mid-right",
    "bottom-left", "bottom-center", "bottom-right"
)


def _validate_filter_name(name):
    """Returns (cleaned_name, error_message_or_None)."""
    if not isinstance(name, str):
        return None, "Filter name must be a string"
    name = name.strip()
    if not name:
        return None, "Filter name cannot be empty"
    if len(name) > FILTER_NAME_MAX_LEN:
        return None, f"Filter name must be {FILTER_NAME_MAX_LEN} characters or fewer"
    return name, None


def _validate_filter_notes(notes):
    """Returns (cleaned_notes_or_None, error_message_or_None). notes is optional."""
    if notes is None:
        return None, None
    if not isinstance(notes, str):
        return None, "Filter notes must be a string"
    notes = notes.strip()
    if len(notes) > FILTER_NOTES_MAX_LEN:
        return None, f"Filter notes must be {FILTER_NOTES_MAX_LEN} characters or fewer"
    return (notes or None), None


def _validate_display_defaults(display_defaults):
    """Validate display_defaults dictionary containing type, location, value_metric, color, custom_text, show_frequency."""
    if display_defaults is None:
        return None
    if not isinstance(display_defaults, dict):
        return "display_defaults must be a JSON object"
    dtype = display_defaults.get("type") or display_defaults.get("style")
    if dtype is not None and dtype not in ALLOWED_DISPLAY_TYPES:
        return f"Invalid display type '{dtype}'. Allowed: {list(ALLOWED_DISPLAY_TYPES)}"
    loc = display_defaults.get("location")
    if loc is not None and loc not in ALLOWED_DISPLAY_LOCATIONS:
        return f"Invalid location '{loc}'. Allowed: {list(ALLOWED_DISPLAY_LOCATIONS)}"
    val_metric = display_defaults.get("value_metric")
    if val_metric is not None and val_metric not in filter_engine.FILTER_METRICS:
        return f"Invalid value_metric '{val_metric}'"
    color = display_defaults.get("color")
    if color is not None and not _HEX_COLOR_RE.match(str(color)):
        return "display_defaults.color must be a 6-digit hex colour, e.g. #e74c3c"
    ctext = display_defaults.get("custom_text")
    if ctext is not None:
        if not isinstance(ctext, str) or len(ctext.strip()) > 20:
            return "display_defaults.custom_text must be a string up to 20 characters"
    freq = display_defaults.get("show_frequency")
    if freq is not None and freq not in ("matching", "always"):
        return "display_defaults.show_frequency must be 'matching' or 'always'"
    return None


FILTER_APPLIES_TO_CHOICES = ("calendar", "patterns", "trends", "trace")


def _validate_applies_to(applies_to):
    """Which page(s) a filter is authored for -- Calendar, Patterns, Trends, or Trace.
    Required (not optional) on both create and update: at least one must be
    selected, matching the DB CHECK constraint (filters_applies_to_chk) as a
    defense-in-depth pair. Returns (cleaned_list_or_None, error_message_or_None)."""
    if not isinstance(applies_to, list) or not applies_to:
        return None, "applies_to must select at least one of: calendar, patterns, trends, trace"
    cleaned = []
    for item in applies_to:
        if item not in FILTER_APPLIES_TO_CHOICES:
            return None, f"applies_to contains an unknown value: {item!r}"
        if item not in cleaned:
            cleaned.append(item)
    return cleaned, None


@app.route("/api/v1/filters", methods=["POST"])
def api_create_filter():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    name, name_err = _validate_filter_name(data.get("name", ""))
    if name_err:
        return jsonify({"error": name_err}), 400

    notes, notes_err = _validate_filter_notes(data.get("notes"))
    if notes_err:
        return jsonify({"error": notes_err}), 400

    filter_tree = data.get("filter_tree")
    if not filter_tree or not isinstance(filter_tree, dict):
        return jsonify({"error": "filter_tree must be a JSON object"}), 400

    try:
        filter_engine.validate_filter_tree(filter_tree)
    except ValueError as e:
        return jsonify({"error": f"Invalid filter tree: {e}"}), 400

    display_defaults = data.get("display_defaults")
    dd_err = _validate_display_defaults(display_defaults)
    if dd_err:
        return jsonify({"error": dd_err}), 400

    applies_to, applies_to_err = _validate_applies_to(data.get("applies_to"))
    if applies_to_err:
        return jsonify({"error": applies_to_err}), 400

    conn = None
    try:
        conn = database.get_conn()
        created = database.create_filter(conn, name, filter_tree, display_defaults, notes, applies_to)
        return jsonify(created), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/filters/<int:filter_id>", methods=["GET"])
def api_get_filter(filter_id: int):
    conn = None
    try:
        conn = database.get_conn()
        flt = database.get_filter(conn, filter_id)
        if not flt:
            return jsonify({"error": f"Filter {filter_id} not found"}), 404
        return jsonify(flt)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/filters/<int:filter_id>", methods=["PUT"])
def api_update_filter(filter_id: int):
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    name = data.get("name")
    if name is not None:
        name, name_err = _validate_filter_name(name)
        if name_err:
            return jsonify({"error": name_err}), 400

    notes = None
    notes_provided = "notes" in data
    if notes_provided:
        notes, notes_err = _validate_filter_notes(data.get("notes"))
        if notes_err:
            return jsonify({"error": notes_err}), 400
        if notes is None:
            notes = ""  # explicit clear: distinguish "field omitted" (None) from "field cleared" (empty string)

    filter_tree = data.get("filter_tree")
    if filter_tree is not None:
        if not isinstance(filter_tree, dict):
            return jsonify({"error": "filter_tree must be a JSON object"}), 400
        try:
            filter_engine.validate_filter_tree(filter_tree)
        except ValueError as e:
            return jsonify({"error": f"Invalid filter tree: {e}"}), 400

    display_defaults = data.get("display_defaults")
    dd_err = _validate_display_defaults(display_defaults)
    if dd_err:
        return jsonify({"error": dd_err}), 400

    applies_to, applies_to_err = _validate_applies_to(data.get("applies_to"))
    if applies_to_err:
        return jsonify({"error": applies_to_err}), 400

    conn = None
    try:
        conn = database.get_conn()
        updated = database.update_filter(conn, filter_id, name, filter_tree, display_defaults, notes, applies_to)
        if not updated:
            return jsonify({"error": f"Filter {filter_id} not found"}), 404
        return jsonify(updated)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/filters/<int:filter_id>", methods=["DELETE"])
def api_delete_filter(filter_id: int):
    conn = None
    try:
        conn = database.get_conn()
        blocking = database.check_filter_deletion_guard(conn, filter_id)
        if blocking:
            return jsonify({
                "status": "error",
                "error": "Cannot delete: referenced by saved views",
                "blocking_views": blocking
            }), 409
        deleted = database.delete_filter(conn, filter_id)
        if not deleted:
            return jsonify({"error": f"Filter {filter_id} not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


# ---------------------------------------------------------------------------
# Phase 7b: Chart Display Bands API
# ---------------------------------------------------------------------------
@app.route("/api/v1/chart_bands", methods=["GET"])
def api_get_chart_bands():
    conn = None
    try:
        conn = database.get_conn()
        bands = database.get_chart_bands(conn)
        return jsonify(bands)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/chart_bands", methods=["POST"])
def api_update_chart_bands():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    for band_key in ("titr", "tir"):
        if band_key in data:
            band = data[band_key]
            if not isinstance(band, dict) or "low" not in band or "high" not in band:
                return jsonify({"error": f"Band '{band_key}' must have 'low' and 'high' numbers"}), 400
            try:
                low = float(band["low"])
                high = float(band["high"])
            except (ValueError, TypeError):
                return jsonify({"error": f"Band '{band_key}' bounds must be numeric"}), 400
            if low <= 0 or high <= 0:
                return jsonify({"error": f"Band '{band_key}' bounds must be positive"}), 400
            if low >= high:
                return jsonify({"error": f"Band '{band_key}': low ({low}) must be less than high ({high})"}), 400

    conn = None
    try:
        conn = database.get_conn()
        updated_bands = database.update_chart_bands(conn, data)
        return jsonify(updated_bands)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


# ---------------------------------------------------------------------------
# Chart Metric Settings & View Cascading API
# ---------------------------------------------------------------------------
@app.route("/api/v1/chart_settings", methods=["GET"])
def api_get_chart_settings():
    page = request.args.get("page")
    if page and page not in ("patterns", "trends", "trace", "calendar", "hba1c"):
        return jsonify({"error": f"Invalid page '{page}'."}), 400
    conn = None
    try:
        conn = database.get_conn()
        settings = database.get_chart_metric_settings(conn, page)
        return jsonify(settings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/chart_settings", methods=["POST"])
def api_update_chart_settings():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    page = data.get("page")
    metric_id = data.get("metric_id")
    settings = data.get("settings")
    cascade_view_ids = data.get("cascade_view_ids", [])

    if not page or page not in ("patterns", "trends", "trace", "calendar", "hba1c"):
        return jsonify({"error": "Invalid field 'page'."}), 400
    if not metric_id or not isinstance(metric_id, str):
        return jsonify({"error": "Field 'metric_id' must be a non-empty string"}), 400
    if not isinstance(settings, dict):
        return jsonify({"error": "Field 'settings' must be a JSON object"}), 400

    conn = None
    try:
        conn = database.get_conn()
        saved = database.upsert_chart_metric_settings(conn, page, metric_id, settings)
        cascaded_count = 0
        if cascade_view_ids and isinstance(cascade_view_ids, list):
            cascaded_count = database.cascade_metric_style_to_views(conn, page, metric_id, settings, cascade_view_ids)
        return jsonify({
            "status": "success",
            "page": page,
            "metric_id": metric_id,
            "settings": saved.get("settings", settings) if saved else settings,
            "cascaded_views_count": cascaded_count
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/chart_settings", methods=["DELETE"])
def api_delete_chart_settings():
    """
    Removes a metric's global default override, reverting it to the
    registry's factory-default style. Used by the style panel's
    "Restore default" button (Targets page, and the on-graph popover).
    """
    page = request.args.get("page")
    metric_id = request.args.get("metric_id")

    if not page or page not in ("patterns", "trends", "trace", "calendar", "hba1c"):
        return jsonify({"error": "Invalid query param 'page'"}), 400
    if not metric_id:
        return jsonify({"error": "Query param 'metric_id' is required"}), 400

    conn = None
    try:
        conn = database.get_conn()
        deleted = database.delete_chart_metric_settings(conn, page, metric_id)
        return jsonify({
            "status": "success",
            "page": page,
            "metric_id": metric_id,
            "deleted": deleted
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/chart_settings/affected_views", methods=["GET"])
def api_get_affected_views():
    page = request.args.get("page")
    metric_id = request.args.get("metric_id")
    if not page or page not in ("patterns", "trends", "trace", "calendar", "hba1c"):
        return jsonify({"error": "Invalid query param 'page'"}), 400

    conn = None
    try:
        conn = database.get_conn()
        views = database.get_views_for_metric_cascade(conn, page, metric_id)
        return jsonify({"page": page, "metric_id": metric_id, "views": views})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


# ---------------------------------------------------------------------------
# Phase 7b: Targets Management Route (Redirected to /settings)
# ---------------------------------------------------------------------------
@app.route("/targets")
def targets_page():
    return redirect("/settings", code=302)



@app.route("/api/v1/agp")
def get_agp_image():
    start_date = request.args.get("start_date", None)
    end_date = request.args.get("end_date", None)
    
    # Parse overlay toggles
    show_basal = request.args.get("show_basal", "1") == "1"
    show_iob = request.args.get("show_iob", "0") == "1"
    show_carbs = request.args.get("show_carbs", "0") == "1"

    try:
        conn = database.get_conn()
        img_buf = agp_renderer.generate_agp_image(
            conn, start_date, end_date, 
            show_basal=show_basal,
            show_iob=show_iob,
            show_carbs=show_carbs
        )
        database.return_conn(conn)
        conn = None
        
        if not img_buf:
            return jsonify({"error": "No data for selected period"}), 404
            
        return Response(img_buf.getvalue(), mimetype="image/png")
    except Exception as e:
        if conn:
            database.return_conn(conn)
        return jsonify({"error": str(e)}), 500

@app.route("/api/v1/trends_data")
def api_trends_data():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    grain = request.args.get("grain", "daily") # "daily" or "monthly"

    if not start_date or not end_date:
        return jsonify({"error": "Missing dates"}), 400

    conn = database.get_conn()
    try:
        data = database.get_trends_data(conn, start_date, end_date, grain=grain)
        if not data:
            return jsonify([]), 200 # Return empty list if no data
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        database.return_conn(conn)

@app.route("/api/v1/cohorts", methods=["GET"])
def api_list_cohorts():
    conn = None
    try:
        conn = database.get_conn()
        filters_list = database.get_filters(conn)
        cohorts = [f for f in filters_list if "patterns" in f.get("applies_to", [])]
        return jsonify(cohorts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

@app.route("/api/v1/agp_data")
def api_agp_data():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    grain = request.args.get("grain", "daily")
    cohort_id = request.args.get("cohort_id")

    if not start_date or not end_date:
        return jsonify({"error": "Missing dates"}), 400

    conn = database.get_conn()
    try:
        import agp_renderer
        cohort_days = None
        cohort_meta = None

        if cohort_id:
            try:
                cid = int(cohort_id)
                filter_row = database.get_filter(conn, cid)
                if filter_row:
                    filter_def = filter_row.get("filter_tree") or filter_row.get("filter_definition")
                    if isinstance(filter_def, str):
                        filter_def = json.loads(filter_def)
                    import filter_engine
                    eval_result = filter_engine.evaluate_filter_tree(
                        conn, filter_def, {"start": start_date, "end": end_date}
                    )
                    cohort_days = eval_result.get("matchingDates", [])
                    cohort_meta = {
                        "cohort_id": cid,
                        "cohort_name": filter_row.get("name"),
                        "matching_days": len(cohort_days),
                        "total_days": eval_result.get("meta", {}).get("totalDays", 0),
                        "matching_dates": cohort_days
                    }
            except Exception as ce:
                print(f"[agp] Failed to evaluate cohort filter {cohort_id}: {ce}")

        data = agp_renderer.get_agp_json_data(conn, start_date, end_date, cohort_days=cohort_days)
        if not data:
            if cohort_meta and cohort_meta["matching_days"] == 0:
                data = {
                    "percentiles": [],
                    "basal": [],
                    "overlays": [],
                    "ce": [],
                    "cohort": cohort_meta
                }
            else:
                return jsonify({"error": "No data for AGP"}), 404
        else:
            if cohort_meta:
                data["cohort"] = cohort_meta

        # Reuses cgm_health's coverage calc (same one backing Sandbox's CGM
        # Data Quality panel) rather than a second endpoint/round-trip --
        # guarantees the footnote% is computed over the exact same range as
        # the rest of this response, not a separately-fetched one that could
        # drift if the two requests raced.
        try:
            data["cgm_coverage_pct"] = cgm_health.get_coverage_stats(conn, start_date, end_date)["coverage_pct"]
        except Exception:
            data["cgm_coverage_pct"] = None
        return jsonify(data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        database.return_conn(conn)

@app.route("/logs")
def logs_page():
    current_level = log_manager.get_runtime_log_level()
    log_files = log_manager.get_log_files()
    return render_template("logs.html", current_level=current_level, log_files=log_files)

@app.route("/api/v1/system/logs/stream")
def stream_logs():
    def generate():
        # First, send existing ring buffer items as a JSON batch
        initial_items = log_manager.get_ring_buffer()
        if initial_items:
            yield f"event: batch\ndata: {json.dumps(initial_items)}\n\n"
        
        # Then, wait for new logs with keepalive ping
        q = log_manager.subscribe_stream()
        try:
            while True:
                try:
                    item = q.get(timeout=15.0)
                    yield f"data: {json.dumps(item)}\n\n"
                except queue.Empty:
                    # Keepalive comment to maintain TCP state and detect broken pipes immediately
                    yield ": ping\n\n"
        except (GeneratorExit, OSError):
            pass
        finally:
            log_manager.unsubscribe_stream(q)

    return Response(generate(), mimetype="text/event-stream")

@app.route("/api/v1/system/log_level", methods=["GET", "POST"])
def manage_log_level():
    if request.method == "POST":
        data = request.json or {}
        level = data.get("level", "INFO").upper()
        if level not in log_manager.LEVEL_MAP:
            return jsonify({"error": f"Invalid level. Must be one of {list(log_manager.LEVEL_MAP.keys())}"}), 400
        conn = database.get_conn()
        try:
            database.set_system_config(conn, "LOG_LEVEL", level)
        finally:
            database.return_conn(conn)
        applied = log_manager.set_runtime_log_level(level)
        return jsonify({"status": "ok", "level": applied})
    return jsonify({"level": log_manager.get_runtime_log_level()})

@app.route("/api/v1/system/logs/download")
def download_log_file():
    filename = request.args.get("file", "daemon.log")
    path = log_manager.get_log_file_path(filename)
    if not path:
        return jsonify({"error": "Log file not found"}), 404
    return send_file(path, mimetype="text/plain", as_attachment=True, download_name=os.path.basename(path))

@app.route("/api/v1/system/logs/files")
def get_log_files_list():
    return jsonify({"files": log_manager.get_log_files()})

@app.route("/api/v1/system/restart", methods=["POST"])
def restart_daemon_api():
    def _delayed_exit():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return jsonify({"status": "ok", "message": "Daemon restarting..."})


@app.route("/settings", methods=["POST"])
def update_settings():
    data = request.json or {}
    if 'TIMEZONE' in data:
        timezone_name = str(data['TIMEZONE']).strip()
        if not timezone_name:
            return jsonify({"status": "error", "message": "Timezone is required."}), 400
        try:
            ZoneInfo(timezone_name)
        except Exception:
            return jsonify({"status": "error", "message": "Timezone must be a valid IANA name, such as Australia/Perth."}), 400
        data['TIMEZONE'] = timezone_name
    try:
        conn = database.get_conn()
        for k, v in data.items():
            database.set_system_config(conn, k, str(v))
        database.return_conn(conn)
        
        load_settings_from_db()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/v1/spider_thresholds", methods=["GET"])
def get_spider_thresholds_route():
    try:
        conn = database.get_conn()
        thresholds = database.get_spider_thresholds(conn)
        database.return_conn(conn)
        return jsonify({"status": "ok", "thresholds": thresholds})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/v1/spider_thresholds", methods=["POST"])
def update_spider_thresholds_route():
    data = request.json
    try:
        # Validate before writing anything: t_ideal must differ from
        # t_critical (formulas divide by their difference) for every metric
        # supplied.
        for metric, vals in data.items():
            if metric not in SPIDER_METRICS:
                return jsonify({"status": "error", "message": f"Unknown metric: {metric}"}), 400
            t_ideal = float(vals["t_ideal"])
            t_critical = float(vals["t_critical"])
            if t_ideal == t_critical:
                return jsonify({"status": "error", "message": f"{metric.upper()}: ideal and critical values can't be equal"}), 400

        conn = database.get_conn()
        database.update_spider_thresholds(conn, data)
        database.return_conn(conn)
        return jsonify({"status": "ok"})
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"status": "error", "message": f"Invalid threshold data: {e}"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/backfill/start", methods=["POST"])
def start_manual_backfill():
    data = request.json
    start = data.get("start")
    end = data.get("end")
    if not start or not end:
        return jsonify({"status": "error", "message": "Missing dates"}), 400
    
    if status_data["backfill"]["running"]:
        return jsonify({"status": "error", "message": "Backfill already running"}), 400

    try:
        tz_name = getattr(config, "TIMEZONE", "UTC")
        try:
            conn = database.get_conn()
            db_config = database.get_system_config(conn)
            database.return_conn(conn)
            tz_name = db_config.get("TIMEZONE", tz_name)
        except: pass
        tz = ZoneInfo(tz_name)

        if len(start) == 10: start += "T00:00:00"
        if len(end) == 10: end += "T23:59:59"

        dt_start = datetime.fromisoformat(start).replace(tzinfo=tz).astimezone(timezone.utc)
        dt_end = datetime.fromisoformat(end).replace(tzinfo=tz).astimezone(timezone.utc)
        
        status_data["backfill"]["manual_range"] = {"start": dt_start, "end": dt_end}
        backfill_stop_event.clear()
        
        t = threading.Thread(target=run_backfill)
        t.start()
        
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route("/backfill/cancel", methods=["POST"])
def cancel_backfill():
    if not status_data["backfill"]["running"]:
        return jsonify({"status": "error", "message": "Backfill not running"}), 400
    
    backfill_stop_event.set()
    return jsonify({"status": "ok"})

# ---------------- BASAL REBUILD ----------------
basal_rebuild_status = {
    "running": False,
    "progress": "",
    "last_completed": None,
    "total_rows": 0,
    "error": None
}

def run_basal_rebuild():
    """Background thread: compute basal for ALL historical data."""
    basal_rebuild_status["running"] = True
    basal_rebuild_status["progress"] = "Starting..."
    basal_rebuild_status["error"] = None
    basal_rebuild_status["total_rows"] = 0
    
    conn = None
    try:
        conn = database.get_conn()
        
        # Get the full date range of data
        with conn.cursor() as cur:
            cur.execute("""
                SELECT LEAST(
                    COALESCE((SELECT MIN(ts)::date FROM cgm_readings), CURRENT_DATE),
                    COALESCE((SELECT MIN(ts)::date FROM treatments), CURRENT_DATE)
                ) AS oldest,
                CURRENT_DATE AS newest
            """)
            row = cur.fetchone()
            oldest, newest = row[0], row[1]
        
        print(f"[basal-rebuild] Full range: {oldest} to {newest}")
        
        # Process in monthly chunks for progress visibility
        from datetime import date, timedelta
        from dateutil.relativedelta import relativedelta
        
        chunk_start = oldest
        while chunk_start <= newest:
            chunk_end = min(chunk_start + relativedelta(months=1) - timedelta(days=1), newest)
            
            msg = f"Computing {chunk_start.strftime('%b %Y')}..."
            basal_rebuild_status["progress"] = msg
            print(f"[basal-rebuild] {msg}")
            
            rows = database.compute_basal_for_date_range(conn, chunk_start, chunk_end)
            basal_rebuild_status["total_rows"] += rows
            
            chunk_start = chunk_end + timedelta(days=1)
        
        basal_rebuild_status["progress"] = "Complete"
        basal_rebuild_status["last_completed"] = datetime.now(timezone.utc).isoformat()
        print(f"[basal-rebuild] Complete. Total rows: {basal_rebuild_status['total_rows']}")
        
        # Trigger a view refresh to pick up the new basal data
        database.refresh_analysis_views(conn)
        
    except Exception as e:
        basal_rebuild_status["error"] = str(e)
        basal_rebuild_status["progress"] = f"Failed: {e}"
        print(f"[basal-rebuild] Error: {e}")
    finally:
        basal_rebuild_status["running"] = False
        if conn:
            database.return_conn(conn)

@app.route("/api/v1/rebuild_basal", methods=["POST"])
def api_rebuild_basal_start():
    if basal_rebuild_status["running"]:
        return jsonify({"status": "error", "message": "Rebuild already in progress"}), 400
    
    t = threading.Thread(target=run_basal_rebuild)
    t.start()
    return jsonify({"status": "ok", "message": "Basal rebuild started"})

@app.route("/api/v1/rebuild_basal", methods=["GET"])
def api_rebuild_basal_status():
    return jsonify(basal_rebuild_status)

@app.route('/api/sanity_check')
def api_sanity_check():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    if not start_date or not end_date:
        # Default to last 30 days
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=30)
        start_date = start_dt.strftime("%Y-%m-%d")
        end_date = end_dt.strftime("%Y-%m-%d")
        
    try:
        conn = database.get_conn()
        stats = database.get_daily_counts(conn, start_date, end_date)
        database.return_conn(conn)
        
        # Calculate Averages (Simple algorithm)
        # Exclude first and last day from average if we have enough data (>2 days)
        # to avoid partial days skewing the result.
        calc_stats = [s for s in stats]
        if len(calc_stats) > 2:
            calc_subset = calc_stats[1:-1]
        else:
            calc_subset = calc_stats
            
        avg = {"cgm": 0, "treatments": 0, "device": 0}
        
        # Helper to calc average of non-zero values
        def calc_avg(key):
            values = [d[key] for d in calc_subset if d[key] > 0]
            if not values: return 0
            return sum(values) / len(values)

        if calc_subset:
            try:
                avg["cgm"] = calc_avg("cgm")
                avg["treatments"] = calc_avg("treatments")
                avg["device"] = calc_avg("device")
            except Exception: pass
            
        return jsonify({
            "range": {"start": start_date, "end": end_date},
            "averages": {k: int(v) for k, v in avg.items()},
            "daily_stats": stats
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/retention/stats')
def api_retention_stats():
    try:
        conn = database.get_conn()
        stats = database.get_retention_stats(conn)
        database.return_conn(conn)
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/export")
def export_page():
    return render_template("export.html")

@app.route("/import")
def import_page():
    return render_template("import.html")

@app.route("/api/export")
def export_data():
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    file_format = request.args.get('format', 'xlsx')
    config_str = request.args.get('config')
    
    if not start_date or not end_date:
        return jsonify({"error": "Missing date range"}), 400
    
    try:
        # Load selection config
        export_config = json.loads(config_str) if config_str else {}
        
        def mgdl_to_mmol(val):
            if val is None or val == 0: return val
            try:
                return round(float(val) / 18.0, 1)
            except: return val

        conn = database.get_conn()
        try:
            # 1. Daily Stats
            sheet_daily = export_config.get('daily', {"enabled": True, "cols": []})
            df_daily = pd.DataFrame()
            if sheet_daily.get('enabled'):
                # Repointed to consolidated schema 19 Aug 2026 (was
                # layer2_daily_stats + experimental_advanced_metrics_daily).
                q_daily = """
                    SELECT 
                        d."date" AS "Date",
                        ROUND((d.bg_sum / NULLIF(d.bg_readings, 0) / 18.0182)::numeric, 2) AS "Avg_BG",
                        d.total_basal AS "Total_Basal",
                        (d.smb + d.meal_bolus + d.correction_bolus) AS "Total_Bolus",
                        d.tdd AS "TDD", d.scheduled_basal AS "Scheduled_Basal", d.temp_basal_impact AS "Temp_Basal_Impact",
                        d.smb AS "SMB", d.meal_bolus AS "Meal Bolus", d.correction_bolus AS "Correction Bolus", d.carbs AS "Carbs",
                        d.pct_tir AS "TIR", d.pct_titr AS "TITR",
                        d.pct_tikr AS "TIKR", d.pct_vlow AS "VLow", d.pct_low AS "Low", d.pct_high AS "High", d.pct_vhigh AS "VHigh",
                        ROUND(r.gvi::numeric, 3) as "GVI",
                        ROUND(d.day_gmi_percent::numeric, 1) as "GMI",
                        ROUND(r.lbgi::numeric, 2) as "LBGI",
                        ROUND(r.hbgi::numeric, 2) as "HBGI",
                        ROUND(r.sd_mmol::numeric, 2) as "SD (mmol/L)",
                        d.bg_readings as "Data_Points"
                    FROM layer2_daily_band_stats d
                    LEFT JOIN layer2_daily_risk_stats r ON d."date" = r."date"
                    WHERE d."date" BETWEEN %s AND %s 
                    ORDER BY d."date" ASC
                """
                df_daily = pd.read_sql(q_daily, conn, params=[start_date, end_date])
                
                # Avg_BG is already in mmol/L from the SQL calculation
                if "Avg_BG" in df_daily.columns:
                    df_daily.rename(columns={"Avg_BG": "Avg_BG (mmol/L)"}, inplace=True)
                    
                # Subset columns if specified
                if sheet_daily.get('cols'):
                    cols_to_select = []
                    for c in sheet_daily['cols']:
                        if c == "Avg_BG": cols_to_select.append("Avg_BG (mmol/L)")
                        elif c in df_daily.columns: cols_to_select.append(c)
                    df_daily = df_daily[cols_to_select]
            
            # 2. Hourly Trends
            sheet_hourly = export_config.get('hourly', {"enabled": True, "cols": []})
            df_hourly = pd.DataFrame()
            if sheet_hourly.get('enabled'):
                # Correct query: layer2_hourly_stats only has Hour/Date/Basal/Bolus/Carbs.
                # Avg_BG, IOB, COB are sourced via LEFT JOIN aggregations on raw tables.
                q_hourly = """
                    SELECT 
                        h."Hour" AS "Timestamp",
                        ROUND((AVG(c.sg) / 18.0182)::numeric, 2) AS "Avg_BG (mmol/L)",
                        h."Basal",
                        h."Bolus",
                        (h."Basal" + h."Bolus") AS "Total_Insulin",
                        h."Carbs",
                        ROUND(AVG(d.iob)::numeric, 2) AS "Avg_IOB",
                        ROUND(AVG(d.cob)::numeric, 2) AS "Avg_COB",
                        ROUND(AVG(d.deviation), 2) AS "Avg_Deviation (mmol/L)",
                        ROUND(AVG(d.isf), 2) AS "Avg_ISF (mmol/L/U)",
                        ROUND(AVG(d.autosens_ratio), 2) AS "Avg_Sensitivity"
                    FROM layer2_hourly_stats h
                    LEFT JOIN cgm_readings c ON date_trunc('hour', c.ts) = h."Hour"
                    LEFT JOIN devicestatus d ON date_trunc('hour', d.ts) = h."Hour"
                    WHERE h."Date" BETWEEN %s AND %s
                    GROUP BY h."Hour", h."Basal", h."Bolus", h."Carbs"
                    ORDER BY h."Hour" ASC
                """
                df_hourly = pd.read_sql(q_hourly, conn, params=[start_date, end_date])

                if sheet_hourly.get('cols'):
                    cols_to_select = []
                    for c in sheet_hourly['cols']:
                        if c == "Avg_BG": cols_to_select.append("Avg_BG (mmol/L)")
                        elif c == "Avg_Deviation": cols_to_select.append("Avg_Deviation (mmol/L)")
                        elif c == "Avg_ISF": cols_to_select.append("Avg_ISF (mmol/L/U)")
                        elif c in df_hourly.columns: cols_to_select.append(c)
                    df_hourly = df_hourly[cols_to_select]

            # 3. Integrated Event Timeline
            sheet_timeline = export_config.get('timeline', {"enabled": True, "cols": []})
            df_timeline = pd.DataFrame()
            if sheet_timeline.get('enabled'):
                # Proper Local Date Boundary: From 00:00 of Start to 23:59:59 of End
                # This ensures we get exactly the calendar days requested in local time.
                tz = getattr(config, "TIMEZONE", "UTC")
                
                q_treatments = """
                    SELECT ts AT TIME ZONE %s as time, event_type, insulin, carbs
                    FROM treatments
                    WHERE ts >= %s::timestamp AT TIME ZONE %s
                      AND ts <= %s::timestamp AT TIME ZONE %s
                """
                df_t = pd.read_sql(q_treatments, conn, params=[tz, f"{start_date} 00:00:00", tz, f"{end_date} 23:59:59", tz])
                
                q_cgm = """
                    SELECT ts AT TIME ZONE %s as time, sg as "BG"
                    FROM cgm_readings
                    WHERE ts >= %s::timestamp AT TIME ZONE %s
                      AND ts <= %s::timestamp AT TIME ZONE %s
                """
                df_c = pd.read_sql(q_cgm, conn, params=[tz, f"{start_date} 00:00:00", tz, f"{end_date} 23:59:59", tz])
                
                q_meta = """
                    SELECT 
                        ts AT TIME ZONE %s as time,
                        iob as "IOB",
                        cob as "COB",
                        d.isf as "ISF",
                        d.deviation as "Deviation"
                    FROM devicestatus d
                    WHERE ts >= %s::timestamp AT TIME ZONE %s
                      AND ts <= %s::timestamp AT TIME ZONE %s
                """
                df_m = pd.read_sql(q_meta, conn, params=[tz, f"{start_date} 00:00:00", tz, f"{end_date} 23:59:59", tz])
                
                # Merge Timeline
                df_timeline = pd.merge(df_t, df_c, on='time', how='outer')
                df_timeline = pd.merge(df_timeline, df_m, on='time', how='outer')
                df_timeline = df_timeline.sort_values('time').reset_index(drop=True)
                df_timeline.rename(columns={'time': 'Time', 'event_type': 'Event Type', 'insulin': 'Insulin', 'carbs': 'Carbs'}, inplace=True)
                
                # Conversions
                # Fixed 19 Aug 2026: Deviation and ISF are already stored in
                # mmol/L (Deviation always; ISF as of the ingestion fix on
                # this same date), so re-converting them here was corrupting
                # nearly every row in the XLSX Event Timeline sheet. Only BG
                # (raw mg/dL from cgm_readings) genuinely needs this.
                if "BG" in df_timeline.columns:
                    df_timeline["BG"] = df_timeline["BG"].apply(mgdl_to_mmol)

                # Dynamic Precision Filtering: Drop rows where ALL user-selected "data" columns are empty.
                data_cols = [c for c in ['Insulin', 'Carbs', 'BG', 'IOB', 'COB', 'ISF', 'Deviation'] if c in df_timeline.columns]
                
                requested_cols = sheet_timeline.get('cols', [])
                if requested_cols:
                    filter_subset = [c for c in requested_cols if c in data_cols]
                    if filter_subset:
                        df_timeline.dropna(subset=filter_subset, how='all', inplace=True)
                    
                    # Finally, subset and RENAME for display
                    available = []
                    rename_map = {}
                    for c in requested_cols:
                        if c == "BG": 
                            available.append("BG")
                            rename_map["BG"] = "BG (mmol/L)"
                        elif c == "Deviation":
                            available.append("Deviation")
                            rename_map["Deviation"] = "Deviation (mmol/L)"
                        elif c == "ISF":
                            available.append("ISF")
                            rename_map["ISF"] = "ISF (mmol/L/U)"
                        elif c in df_timeline.columns:
                            available.append(c)
                    
                    if 'Time' not in available and 'Time' in df_timeline.columns:
                        available.insert(0, 'Time')
                    
                    df_timeline = df_timeline[available]
                    df_timeline.rename(columns=rename_map, inplace=True)
                else:
                    df_timeline.dropna(subset=data_cols, how='all', inplace=True)
                    df_timeline.rename(columns={
                        "BG": "BG (mmol/L)",
                        "Deviation": "Deviation (mmol/L)",
                        "ISF": "ISF (mmol/L/U)"
                    }, inplace=True)
        finally:
            if conn:
                database.return_conn(conn)
            conn = None

        # Excel compatibility: strip timezones
        for df in [df_daily, df_hourly, df_timeline]:
            if df.empty: continue
            for col in df.columns:
                if pd.api.types.is_datetime64_any_dtype(df[col]):
                    df[col] = df[col].dt.tz_localize(None)
                elif df[col].dtype == object and "Date" in col:
                    try:
                        df[col] = pd.to_datetime(df[col]).dt.tz_localize(None)
                    except: pass
        
        output = io.BytesIO()
        if file_format == 'xlsx':
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                if not df_daily.empty:
                    df_daily.to_excel(writer, sheet_name='Daily Summary', index=False)
                if not df_hourly.empty:
                    df_hourly.to_excel(writer, sheet_name='Hourly Trends', index=False)
                if not df_timeline.empty:
                    df_timeline.to_excel(writer, sheet_name='Event Timeline', index=False)
                
                # Auto-adjust columns
                for sheetname in writer.sheets:
                    worksheet = writer.sheets[sheetname]
                    for col in worksheet.columns:
                        max_length = 0
                        column = col[0].column_letter
                        for cell in col:
                            try:
                                if len(str(cell.value)) > max_length:
                                    max_length = len(str(cell.value))
                            except: pass
                        worksheet.column_dimensions[column].width = (max_length + 2)
            
            filename = f"NS_Export_{start_date}_{end_date}.xlsx"
            mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif file_format == 'csv_daily':
            if df_daily.empty:
                return jsonify({"error": "Daily data empty, cannot generate CSV"}), 404
            df_daily.to_csv(output, index=False)
            filename = f"NS_Daily_{start_date}_{end_date}.csv"
            mimetype = "text/csv"
        elif file_format == 'csv_hourly':
            if df_hourly.empty:
                return jsonify({"error": "Hourly data empty, cannot generate CSV"}), 404
            df_hourly.to_csv(output, index=False)
            filename = f"NS_Hourly_{start_date}_{end_date}.csv"
            mimetype = "text/csv"
        elif file_format == 'csv_timeline':
            if df_timeline.empty:
                return jsonify({"error": "Timeline data empty, cannot generate CSV"}), 404
            df_timeline.to_csv(output, index=False)
            filename = f"NS_Timeline_{start_date}_{end_date}.csv"
            mimetype = "text/csv"
        else:
            # Fallback/Legacy
            if df_daily.empty:
                return jsonify({"error": "Daily data empty, cannot generate CSV"}), 404
            df_daily.to_csv(output, index=False)
            filename = f"NS_Daily_{start_date}_{end_date}.csv"
            mimetype = "text/csv"

        output.seek(0)
        return Response(
            output,
            mimetype=mimetype,
            headers={"Content-disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# =====================================================================
# ADVANCED ANALYTICS ROUTES
# =====================================================================
from datetime import datetime, timedelta

# =====================================================================
# OMNIPOD ROUTES (20 Aug 2026)
# =====================================================================
@app.route("/omnipod")
def omnipod():
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')
    if not start_date or not end_date:
        # 30-day default (vs. the 7-day default elsewhere on this app) --
        # at ~3-4 day pod life this gives ~7-8 pods to populate the
        # histograms/BG curve with on first load, rather than ~2.
        end_date = today_str
        start_date = (today - timedelta(days=30)).strftime('%Y-%m-%d')
    return render_template("omnipod.html", start_date=start_date, end_date=end_date, today_local=today_str)


# Shared by both the BG-delta curve (get_pod_bg_relative_curve) and the
# BG rate-of-change curve (get_pod_bg_roc_curve) -- same bucket-grid /
# CI / milestone-t-test math, only the underlying column names differ
# (mean_delta/sd_delta/p10_delta/p90_delta vs mean_roc/sd_roc/p10_roc/
# p90_roc). Kept as one parametrized helper rather than duplicating this
# ~50-line block, so a fix to the stats logic can't drift between the two.
MILESTONE_HOURS = [-24, -12, -6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, 12, 24]

def _build_bucket_series_and_milestones(curve_rows, mean_field, sd_field, p10_field, p90_field):
    by_bucket = {row['bucket']: row for row in curve_rows}

    # 95% CI on the mean at each bucket (SEM = SD/sqrt(n), CI = mean +/-
    # 1.96*SEM) -- distinct from the 10th/90th percentile band, which
    # describes spread *between pods*, not uncertainty in the mean itself.
    # Needs n>=2 for a defined SD; single-pod buckets get no CI.
    def bucket_ci(row):
        if row is None or row[mean_field] is None or row[sd_field] is None or row['pod_n'] < 2:
            return None, None
        sem = float(row[sd_field]) / math.sqrt(row['pod_n'])
        mean = float(row[mean_field])
        return mean - 1.96 * sem, mean + 1.96 * sem

    # -288..288 buckets = -24h..+24h around pod start, at 5-min resolution.
    series = []
    for b in range(-288, 289):
        row = by_bucket.get(b)
        ci_lo, ci_hi = bucket_ci(row)
        series.append({
            "minute": b * 5,
            "n": row['pod_n'] if row else 0,
            "mean": float(row[mean_field]) if row and row[mean_field] is not None else None,
            "p10": float(row[p10_field]) if row and row[p10_field] is not None else None,
            "p90": float(row[p90_field]) if row and row[p90_field] is not None else None,
            "ci_lo": ci_lo,
            "ci_hi": ci_hi,
        })

    # Milestone one-sample t-tests against 0 (paired by construction, since
    # every value is already relative to that pod's own start reading, or
    # is itself a rate that should be zero absent any real drift).
    # Deliberately a fixed, pre-chosen set of hours -- both sides of pod
    # start, finer resolution near 0 where a "blip" at swap-over is
    # plausible -- rather than scanning all 577 buckets, which would rack
    # up false positives from multiple comparisons with no correction.
    milestones = []
    for h in MILESTONE_HOURS:
        bucket_idx = h * 12  # 12 five-minute buckets per hour
        row = by_bucket.get(bucket_idx)
        if row is None or row[mean_field] is None or row[sd_field] is None or row['pod_n'] < 2 or row[sd_field] == 0:
            milestones.append({
                "hour": h, "n": row['pod_n'] if row else 0,
                "mean": float(row[mean_field]) if row and row[mean_field] is not None else None,
                "ci_lo": None, "ci_hi": None, "p_value": None, "significant": None,
            })
            continue
        n = row['pod_n']
        mean = float(row[mean_field])
        sd = float(row[sd_field])
        sem = sd / math.sqrt(n)
        t_stat = mean / sem
        df = n - 1
        p_value = float(scipy_stats.t.sf(abs(t_stat), df) * 2)
        milestones.append({
            "hour": h, "n": n, "mean": round(mean, 3),
            "ci_lo": round(mean - 1.96 * sem, 3), "ci_hi": round(mean + 1.96 * sem, 3),
            "p_value": round(p_value, 4), "significant": p_value < 0.05,
        })

    return series, milestones


@app.route("/api/v1/omnipod/data")
def omnipod_data():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    start_time = request.args.get('start_time')
    end_time = request.args.get('end_time')
    try:
        datetime.strptime(start_date, '%Y-%m-%d')
        datetime.strptime(end_date, '%Y-%m-%d')
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "invalid date range"}), 400

    conn = database.get_conn()
    try:
        summary = database.get_pod_summary_stats(conn, start_date, end_date, start_time, end_time)
        hour_hist = database.get_pod_start_hour_histogram(conn, start_date, end_date, start_time, end_time)
        lifespan_hist = database.get_pod_lifespan_histogram(conn, start_date, end_date, start_time, end_time)
        bg_curve = database.get_pod_bg_relative_curve(conn, start_date, end_date, start_time, end_time)
        bg_roc_curve = database.get_pod_bg_roc_curve(conn, start_date, end_date, start_time, end_time)
        bg_window_counts = database.get_pod_bg_window_counts(conn, start_date, end_date, start_time, end_time)
    finally:
        database.return_conn(conn)

    hour_counts = [0] * 24
    for row in hour_hist:
        hour_counts[int(row['hour_of_day'])] = row['starts']

    # 16 buckets: 0=[0,5)h ... 15=[75,80]h
    lifespan_buckets = [0] * 16
    for row in lifespan_hist:
        idx = row['bucket_index']
        if 0 <= idx < 16:
            lifespan_buckets[idx] = row['pod_count']

    bg_series, milestones = _build_bucket_series_and_milestones(
        bg_curve, 'mean_delta', 'sd_delta', 'p10_delta', 'p90_delta')
    bg_roc_series, bg_roc_milestones = _build_bucket_series_and_milestones(
        bg_roc_curve, 'mean_roc', 'sd_roc', 'p10_roc', 'p90_roc')

    return jsonify({
        "status": "success",
        "pod_count": summary["pod_count"],
        "avg_duration_hours": round(summary["avg_duration_hours"], 1) if summary["avg_duration_hours"] is not None else None,
        "hour_of_day_counts": hour_counts,
        "lifespan_buckets": lifespan_buckets,
        "bg_curve": bg_series,
        "bg_milestones": milestones,
        "bg_roc_curve": bg_roc_series,
        "bg_roc_milestones": bg_roc_milestones,
        "bg_window_counts": bg_window_counts,
    })


def _advanced_analytics_date_range():
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')
    if not start_date or not end_date:
        end_date = today_str
        start_date = (today - timedelta(days=7)).strftime('%Y-%m-%d')
    return start_date, end_date, today_str

@app.route("/advanced_analytics")
def advanced_analytics():
    # Legacy alias redirecting to Clinical Reports (split into /clinical_reports and /novel_reports)
    return redirect("/clinical_reports", code=302)

@app.route("/clinical_reports")
def clinical_reports():
    start_date, end_date, today_str = _advanced_analytics_date_range()
    return render_template("clinical_reports.html", start_date=start_date, end_date=end_date, today_local=today_str)

@app.route("/novel_reports")
def novel_reports():
    start_date, end_date, today_str = _advanced_analytics_date_range()
    return render_template("novel_reports.html", start_date=start_date, end_date=end_date, today_local=today_str)

def get_col_for_span(span):
    col = '7 Day Avg'
    if span <= 1: col = 'Selected Day'
    elif span <= 14: col = '14 Day Avg'
    elif span <= 30: col = '30 Day Avg'
    elif span > 30: col = '90 Day Avg'
    return col

def get_val_global(rows, key, col):
    for r in rows:
        if key in r['Metric'] and r.get(col) is not None:
            val_str = str(r[col]).replace('%', '').split(' ')[0].strip()
            try: return float(val_str)
            except: return 0.0
    return 0.0

# ---------------- SPIDER CHART NORMALIZATION (19 Aug 2026) ----------------
# 10-metric spider chart, thresholds supplied and seeded into
# metric_thresholds (spider_* keys) same day. Three formula types:
SPIDER_METRICS = ["tir", "titr", "tbr", "tar", "gmi", "cv", "mag", "lbgi", "hbgi", "gri"]
SPIDER_ZONES = {1: "RANGE", 2: "STABILITY", 3: "RISK"}
SPIDER_ZONES_BY_METRIC = {
    "tir": 1, "titr": 1, "tbr": 1, "tar": 1,
    "gmi": 2, "cv": 2, "mag": 2,
    "lbgi": 3, "hbgi": 3, "gri": 3,
}
SPIDER_LABELS = {
    "tir": "TIR", "titr": "TITR", "tbr": "TBR", "tar": "TAR",
    "gmi": "GMI", "cv": "CV", "mag": "MAG",
    "lbgi": "LBGI", "hbgi": "HBGI", "gri": "GRI",
}

def normalize_spider_value(v, t_ideal, t_critical, formula):
    """Formulas A/B/C exactly as specified 19 Aug 2026."""
    if v is None:
        return 0.0
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if formula == "A":  # higher-is-better, linear
        s = 100.0 * (v - t_critical) / (t_ideal - t_critical)
    elif formula == "B":  # lower-is-better, linear
        s = 100.0 * (t_critical - v) / (t_critical - t_ideal)
    elif formula == "C":  # lower-is-better, exponential decay (k=2)
        v_clamped = max(min(v, t_critical), t_ideal) if t_critical >= t_ideal else min(max(v, t_critical), t_ideal)
        s = 100.0 * ((t_critical - v_clamped) / (t_critical - t_ideal)) ** 2
    else:
        return 0.0
    return max(0.0, min(100.0, s))

def _compute_spider_dataset(start_date, end_date, prev_start_date=None, prev_end_date=None):
    """
    Shared by spider_data (JSON) and spider_chart (PNG) so both stay in
    sync. Returns raw values, normalized 0-100 scores, and thresholds for
    both the selected period and the comparison period (immediately
    preceding period of the same length by default, or an explicit
    prev_start_date/prev_end_date override from the Last Period bar).
    """
    try:
        s_dt = datetime.strptime(start_date, '%Y-%m-%d')
        e_dt = datetime.strptime(end_date, '%Y-%m-%d')
    except Exception:
        e_dt = datetime.now()
        s_dt = e_dt - timedelta(days=6)
        start_date = s_dt.strftime('%Y-%m-%d')
        end_date = e_dt.strftime('%Y-%m-%d')

    prev_start_str, prev_end_str = database.resolve_comparison_period(
        start_date, end_date, prev_start_date, prev_end_date
    )

    conn = database.get_conn()
    try:
        current_raw = database.get_spider_raw_metrics(conn, start_date, end_date)
        previous_raw = database.get_spider_raw_metrics(conn, prev_start_str, prev_end_str)
        thresholds = database.get_spider_thresholds(conn)
    finally:
        database.return_conn(conn)

    current_scores = {}
    previous_scores = {}
    for m in SPIDER_METRICS:
        t = thresholds.get(m)
        if not t:
            current_scores[m] = 0.0
            previous_scores[m] = 0.0
            continue
        current_scores[m] = round(normalize_spider_value(current_raw.get(m), t["t_ideal"], t["t_critical"], t["formula"]), 1)
        previous_scores[m] = round(normalize_spider_value(previous_raw.get(m), t["t_ideal"], t["t_critical"], t["formula"]), 1)

    def calc_area(scores_dict):
        vals = [scores_dict[m] for m in SPIDER_METRICS]
        N = len(vals)
        sin_base = math.sin(2 * math.pi / N)
        sum_val = sum(vals[i] * vals[(i + 1) % N] for i in range(N))
        return 0.5 * sin_base * sum_val

    area_curr = calc_area(current_scores)
    area_prev = calc_area(previous_scores)
    pct_change = ((area_curr - area_prev) / area_prev * 100) if area_prev > 0 else 0.0

    return {
        "current_raw": current_raw, "previous_raw": previous_raw,
        "current_scores": current_scores, "previous_scores": previous_scores,
        "thresholds": thresholds,
        "area_curr": area_curr, "area_prev": area_prev, "pct_change": pct_change,
    }


@app.route("/api/v1/advanced_metrics/spider_data")
def advanced_metrics_spider_data():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    prev_start_date = request.args.get('prev_start_date')
    prev_end_date = request.args.get('prev_end_date')
    d = _compute_spider_dataset(start_date, end_date, prev_start_date, prev_end_date)

    return jsonify({
        "status": "success",
        "metrics": SPIDER_METRICS,
        "labels": [SPIDER_LABELS[m] for m in SPIDER_METRICS],
        "zones": {m: d["thresholds"].get(m, {}).get("zone") for m in SPIDER_METRICS},
        "current_raw": [d["current_raw"].get(m) for m in SPIDER_METRICS],
        "previous_raw": [d["previous_raw"].get(m) for m in SPIDER_METRICS],
        "current_score": [d["current_scores"][m] for m in SPIDER_METRICS],
        "previous_score": [d["previous_scores"][m] for m in SPIDER_METRICS],
        "thresholds": d["thresholds"],
        "area_curr": round(d["area_curr"], 1),
        "area_prev": round(d["area_prev"], 1),
        "area_change": round(d["pct_change"], 1),
        # GRI Risk Grid (19 Aug 2026): x/y components + combined score for both
        # periods, piggybacking on the existing spider_data current/previous_raw
        # fetch rather than a new endpoint.
        "current_gri_grid": {
            "x": d["current_raw"].get("gri_x"),
            "y": d["current_raw"].get("gri_y"),
            "gri": d["current_raw"].get("gri"),
        },
        "previous_gri_grid": {
            "x": d["previous_raw"].get("gri_x"),
            "y": d["previous_raw"].get("gri_y"),
            "gri": d["previous_raw"].get("gri"),
        },
    })
@app.route("/api/v1/advanced_metrics/poincare_data")
def advanced_metrics_poincare_data():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    import pandas as pd
    import numpy as np

    conn2 = database.get_conn()
    try:
        df = pd.read_sql_query("""
            SELECT bg as bg_n, LEAD(bg) OVER (ORDER BY ts) as bg_next
            FROM layer2_five_minute_aggregate
            WHERE ts >= %s AND ts <= %s AND bg IS NOT NULL
        """, conn2, params=(start_date + " 00:00:00", end_date + " 23:59:59"))
    finally:
        database.return_conn(conn2)

    df_clean = df.dropna()
    if df_clean.empty or len(df_clean) < 10:
        return jsonify({"status": "error", "message": "Insufficient data", "sd1": 0, "sd2": 0, "ratio": 0})
    
    X = df_clean['bg_n']
    Y = df_clean['bg_next']
    SD1 = float(np.std(X - Y) / np.sqrt(2))
    SD2 = float(np.std(X + Y) / np.sqrt(2))
    ratio = SD1 / SD2 if SD2 > 0 else 0.0

    return jsonify({
        "status": "success",
        "sd1": round(SD1, 2),
        "sd2": round(SD2, 2),
        "ratio": round(ratio, 2)
    })

@app.route("/api/v1/experimental/kinematics")
def experimental_kinematics():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    prev_start_date = request.args.get('prev_start_date')
    prev_end_date = request.args.get('prev_end_date')
    from scipy.stats import kurtosis

    conn = database.get_conn()
    try:
        # Comparison period: shared resolver, same one spider/GRI use. Fixes a
        # prior drift where J-Index independently hardcoded a 14-day-back
        # window instead of matching the selected span (20 Aug 2026).
        prev_start, prev_end_str = database.resolve_comparison_period(
            start_date, end_date, prev_start_date, prev_end_date
        )

        # 1. J-Index (pooled)
        # Sourced from get_spider_raw_metrics (same arbitrary-[start,end]-range
        # query the spider chart uses) rather than the old get_dashboard_metrics
        # fixed-window lookup -- that previously meant J-Index silently ignored
        # whatever range was actually selected on the page and always showed a
        # 14-day-trailing figure. Formula (0.324*(Mean+SD)^2) is unchanged.
        current_jraw = database.get_spider_raw_metrics(conn, start_date, end_date)
        previous_jraw = database.get_spider_raw_metrics(conn, prev_start, prev_end_str)

        MGDL_PER_MMOL = 18.0182

        def _mean_sd(raw):
            # mean_bg_mgdl is mg/dL (matches the GMI formula it's computed
            # alongside); convert to mmol/L to match this app's display
            # convention and the original J-Index formula's expected inputs.
            mean_val = (raw.get('mean_bg_mgdl') or 0.0) / MGDL_PER_MMOL
            cv_val = raw.get('cv') or 0.0
            sd_val = (cv_val / 100.0) * mean_val
            return mean_val, sd_val

        mean_val, sd_val = _mean_sd(current_jraw)
        j_index = 0.324 * ((mean_val + sd_val) ** 2)

        m_p, sd_p = _mean_sd(previous_jraw)
        j_prev = 0.324 * ((m_p + sd_p) ** 2)
        j_change = ((j_index - j_prev) / j_prev * 100.0) if j_prev > 0 else 0.0

        # 2. Kurtosis
        import numpy as np

        with conn.cursor() as cur:
            # Current Period ROCs
            cur.execute("""
                SELECT bg_roc FROM layer2_five_minute_aggregate 
                WHERE ts >= %s AND ts <= %s AND bg_roc IS NOT NULL
            """, (start_date + " 00:00:00", end_date + " 23:59:59"))
            rocs = [float(row[0]) for row in cur.fetchall()]
            
            # Previous Period ROCs
            cur.execute("""
                SELECT bg_roc FROM layer2_five_minute_aggregate 
                WHERE ts >= %s AND ts <= %s AND bg_roc IS NOT NULL
            """, (prev_start + " 00:00:00", prev_end_str + " 23:59:59"))
            rocs_prev = [float(row[0]) for row in cur.fetchall()]
        
        def get_kurt_robust(rocs):
            if not rocs: return 0.0, 0.0
            # 0.6 filter
            f = [r for r in rocs if abs(r) <= 0.6]
            if not f: return 0.0, 0.0
            # 0.02 fuzzy
            z = [0.0 if abs(r) <= 0.02 else r for r in f]
            k = float(kurtosis(z, fisher=True)) if len(z) > 10 else 0.0
            s = (len([r for r in f if abs(r) <= 0.02]) / len(f) * 100.0)
            return k, s

        kurt, stasis_curr = get_kurt_robust(rocs)
        k_prev_val, stasis_prev = get_kurt_robust(rocs_prev)

        # Fixed Bin Histogram for ROC Velocity (0.1 width, centered on values)
        # Bins centered on -0.6, -0.5, ..., 0.0, ..., 0.6
        # So edges should be -0.65, -0.55, ..., -0.05, 0.05, ..., 0.65
        bins_edges = np.arange(-0.65, 0.75, 0.1)
        
        def get_hist_pct(data):
            if not data: return [0.0] * (len(bins_edges) - 1)
            counts, _ = np.histogram(data, bins=bins_edges)
            return (counts / len(data) * 100.0).tolist()

        # Labels as midpoints of the bins (-0.6, -0.5, ..., 0.0, ..., 0.6)
        midpoints = (bins_edges[:-1] + bins_edges[1:]) / 2.0

        hist_data = {
            "current": get_hist_pct(rocs),
            "previous": get_hist_pct(rocs_prev),
            "bins": midpoints.tolist()
        }

        # 2b. BG Frequency Distribution (bins centered in 1.0 mg/dL increments from 36.0 to 450.0)
        bg_bins_edges = np.arange(35.5, 451.5, 1.0)
        
        with conn.cursor() as cur:
            # Current Period BGs (raw mmol/L)
            cur.execute("""
                SELECT bg FROM layer2_five_minute_aggregate 
                WHERE ts >= %s AND ts <= %s AND bg IS NOT NULL
            """, (start_date + " 00:00:00", end_date + " 23:59:59"))
            bgs_raw = [float(row[0]) for row in cur.fetchall()]
            
            # Previous Period BGs (raw mmol/L)
            cur.execute("""
                SELECT bg FROM layer2_five_minute_aggregate 
                WHERE ts >= %s AND ts <= %s AND bg IS NOT NULL
            """, (prev_start + " 00:00:00", prev_end_str + " 23:59:59"))
            bgs_prev_raw = [float(row[0]) for row in cur.fetchall()]

        # Convert to mg/dL and clip for histogram
        bgs = np.clip([x * 18.0182 for x in bgs_raw], 36.0, 450.0).tolist() if bgs_raw else []
        bgs_prev = np.clip([x * 18.0182 for x in bgs_prev_raw], 36.0, 450.0).tolist() if bgs_prev_raw else []

        # Calculate stats for current period in mmol/L
        if bgs_raw:
            stats = {
                "mean": float(np.mean(bgs_raw)),
                "median": float(np.median(bgs_raw)),
                "q1": float(np.percentile(bgs_raw, 25)),
                "q3": float(np.percentile(bgs_raw, 75))
            }
        else:
            stats = {"mean": 0.0, "median": 0.0, "q1": 0.0, "q3": 0.0}

        def get_bg_hist_pct(data):
            if not data: return [0.0] * (len(bg_bins_edges) - 1)
            counts, _ = np.histogram(data, bins=bg_bins_edges)
            return (counts / len(data) * 100.0).tolist()

        bg_midpoints = (bg_bins_edges[:-1] + bg_bins_edges[1:]) / 2.0
        # Convert midpoints back to mmol/L for the axis scale
        bg_midpoints_mmol = [float(b / 18.0182) for b in bg_midpoints]

        bg_hist_data = {
            "current": get_bg_hist_pct(bgs),
            "previous": get_bg_hist_pct(bgs_prev),
            "bins": bg_midpoints_mmol,
            "stats": stats
        }

        # 3. CONGA-2 Clock Profile
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    EXTRACT(HOUR FROM ts) as hr,
                    EXTRACT(MINUTE FROM ts) as mn,
                    stddev(bg_diff_2h) as val_2h,
                    stddev(bg_diff_1h) as val_1h,
                    stddev(bg_diff_4h) as val_4h
                FROM layer2_five_minute_aggregate
                WHERE ts >= %s AND ts <= %s
                GROUP BY 1, 2
                ORDER BY 1, 2
            """, (start_date + " 00:00:00", end_date + " 23:59:59"))
            conga_rows = cur.fetchall()

        # NOTE: stddev() is SQL NULL (Python None) for any time-of-day bucket
        # with fewer than 2 samples -- most commonly when "today" is a
        # still-in-progress day included in the range, so slots after the
        # current time only have one prior day's worth of data. That's a
        # genuinely undefined value, not a real zero -- coercing it to 0
        # (the previous behaviour) made a "no data yet" bucket
        # indistinguishable from "measured and found perfectly stable",
        # and rendered as a misleading flatline-to-zero on the chart. Pass
        # None through as JSON null instead; ApexCharts renders null as a
        # gap in the line rather than a false zero.
        conga_profile = {
            "labels": [f"{int(r[0]):02d}:{int(r[1]):02d}" for r in conga_rows],
            "values_2h": [float(r[2]) if r[2] is not None else None for r in conga_rows],
            "values_1h": [float(r[3]) if r[3] is not None else None for r in conga_rows],
            "values_4h": [float(r[4]) if r[4] is not None else None for r in conga_rows]
        }
        
        # Summary numbers (14-day aggregate)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT stddev(bg_diff_1h), stddev(bg_diff_2h), stddev(bg_diff_4h)
                FROM layer2_five_minute_aggregate
                WHERE ts >= %s AND ts <= %s
            """, (start_date + " 00:00:00", end_date + " 23:59:59"))
            c_summaries = cur.fetchone()

        return jsonify({
            "status": "success",
            "j_index": round(j_index, 2),
            "j_prev": round(j_prev, 2),
            "j_change": round(j_change, 1),
            "kurtosis": round(kurt, 2),
            "kurtosis_prev": round(k_prev_val, 2),
            "stasis": round(stasis_curr, 1),
            "stasis_prev": round(stasis_prev, 1),
            "velocity_hist": hist_data,
            "bg_hist": bg_hist_data,
            "conga_profile": conga_profile,
            "conga_summaries": {
                "1h": round(float(c_summaries[0] or 0), 2),
                "2h": round(float(c_summaries[1] or 0), 2),
                "4h": round(float(c_summaries[2] or 0), 2)
            }
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        database.return_conn(conn)

@app.route("/api/v1/advanced_metrics/spider_chart")
def advanced_metrics_spider():
    try:
        import numpy as np
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from io import BytesIO
        from matplotlib.figure import Figure
        from flask import request, Response

        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        prev_start_date = request.args.get('prev_start_date')
        prev_end_date = request.args.get('prev_end_date')
        d = _compute_spider_dataset(start_date, end_date, prev_start_date, prev_end_date)

        labels = [SPIDER_LABELS[m] for m in SPIDER_METRICS]
        current = [d["current_scores"][m] for m in SPIDER_METRICS]
        previous = [d["previous_scores"][m] for m in SPIDER_METRICS]

        num_vars = len(labels)
        angles = np.linspace(0, 2 * np.pi, num_vars, endpoint=False).tolist()
        angles += angles[:1]
        curr_plot = current + [current[0]]
        prev_plot = previous + [previous[0]]

        fig = Figure(figsize=(6, 6))
        fig.patch.set_facecolor('#1e1e1e')
        ax = fig.add_subplot(111, polar=True)
        ax.set_facecolor('#1a1a1a')
        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(labels, size=9, color='#aaaaaa')

        # 10-metric / 3-zone layout (19 Aug 2026):
        # Zone 1 RANGE (green): TIR, TITR, TBR, TAR (indices 0-3)
        # Zone 2 STABILITY (blue): GMI, CV, MAG (indices 4-6)
        # Zone 3 RISK (red): LBGI, HBGI, GRI (indices 7-9)
        zone_colors = {1: '#00e676', 2: '#3498db', 3: '#e74c3c'}
        colors = [zone_colors[SPIDER_ZONES_BY_METRIC[m]] for m in SPIDER_METRICS]
        for tick, color in zip(ax.get_xticklabels(), colors):
            tick.set_color(color)
            tick.set_fontweight('bold')

        ax.spines['polar'].set_color('#444444')
        ax.grid(color='#555555', linestyle='--', linewidth=0.8, alpha=0.4)
        ax.scatter([0], [0], color='#ffffff', s=30, zorder=10)
        ax.set_ylim(0, 100)

        # Zone labels, positioned at the angular midpoint of each zone's span.
        # Pushed well beyond the metric tick labels (which matplotlib places
        # at ~r=110-115 by default) -- at r=112 these sat almost exactly on
        # top of them, and for the two 3-metric zones the geometric midpoint
        # angle lands exactly on the *middle* metric's own ray (CV for
        # STABILITY, HBGI for RISK), so no radius short of a large jump
        # would have cleared it.
        # 19 Aug 2026 v2: 150 was too far for RISK specifically -- its
        # angle (pointing down, same ray as HBGI) reached into the legend
        # below the plot. Pulled back to 128 and gave the legend more
        # clearance (bbox_to_anchor y-offset) rather than just shrinking
        # the radius further, since RANGE/STABILITY were fine at 150.
        zones = [
            (SPIDER_ZONES[1], (angles[0] + angles[3]) / 2, 128, zone_colors[1]),
            (SPIDER_ZONES[2], (angles[4] + angles[6]) / 2, 128, zone_colors[2]),
            (SPIDER_ZONES[3], (angles[7] + angles[9]) / 2, 128, zone_colors[3]),
        ]
        for txt, ang, rad, col in zones:
            ax.text(ang, rad, txt, color=col, weight='bold', size=10, ha='center', va='center', clip_on=False)

        ax.plot(angles, curr_plot, color='#00e676', linewidth=2, label='Current')
        ax.fill(angles, curr_plot, color='#00e676', alpha=0.15)
        ax.plot(angles, prev_plot, color='#aaaaaa', linewidth=1.5, linestyle='--', label='Previous Period')
        ax.fill(angles, prev_plot, color='#aaaaaa', alpha=0.05)
        ax.legend(loc='lower center', bbox_to_anchor=(0.5, -0.32), frameon=False, ncol=2, labelcolor='#e0e0e0')

        img_io = BytesIO()
        fig.savefig(img_io, format='png', bbox_inches='tight', dpi=100, facecolor='#1e1e1e')
        img_io.seek(0)
        content = img_io.getvalue()
        plt.close(fig)
        return Response(content, mimetype='image/png')
    except Exception as e:
        import traceback
        with open("/tmp/spider_error.log", "w") as f:
            f.write(traceback.format_exc())
        return Response("Backend error in Spider Chart", status=500)

@app.route("/api/v1/advanced_metrics/poincare_chart")
def advanced_metrics_poincare():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    import pandas as pd
    import numpy as np
    import io
    from io import BytesIO
    from matplotlib.patches import Ellipse
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    conn2 = database.get_conn()
    try:
        df = pd.read_sql_query("""
            SELECT 
                bg as bg_n,
                LEAD(bg) OVER (ORDER BY ts) as bg_next
            FROM layer2_five_minute_aggregate
            WHERE ts >= %s AND ts <= %s
              AND bg IS NOT NULL
        """, conn2, params=(start_date + " 00:00:00", end_date + " 23:59:59"))
    finally:
        database.return_conn(conn2)

    df_clean = df.dropna()
    from matplotlib.figure import Figure
    from datetime import datetime as _dt
    fig = Figure(figsize=(6, 6))
    fig.patch.set_facecolor('#1e1e1e')
    ax = fig.add_subplot(111)
    ax.set_facecolor('#1a1a1a')

    # Baked into the image itself (not an HTML overlay) so this chart is a
    # true standalone printout -- per Harry, if the date range changes the
    # chart gets regenerated anyway, so there's no separate "keep it in sync"
    # concern the way there would be with an HTML-side date span.
    _date_range_str = f"{_dt.strptime(start_date, '%Y-%m-%d').strftime('%d/%m/%Y')} \u2013 {_dt.strptime(end_date, '%Y-%m-%d').strftime('%d/%m/%Y')}"
    fig.suptitle("The Poincaré Plot & Geometric Fit (mmol/L)", color='#ffffff', fontsize=13, y=0.98)
    fig.text(0.5, 0.935, _date_range_str, color='#aaaaaa', fontsize=9.5, ha='center')

    if df_clean.empty or len(df_clean) < 10:
        ax.text(0.5, 0.5, 'Insufficient Data', color='#888', ha='center', va='center')
        ax.axis('off')
    else:
        X = df_clean['bg_n']
        Y = df_clean['bg_next']
        SD1 = np.std(X - Y) / np.sqrt(2)
        SD2 = np.std(X + Y) / np.sqrt(2)
        
        # Plot points
        ax.scatter(X, Y, s=4, color='#3498db', alpha=0.4, label='Readings $BG_n$ vs $BG_{n+1}$')
        
        # Identity line
        min_val = min(X.min(), Y.min())
        max_val = max(X.max(), Y.max())
        ax.plot([min_val, max_val], [min_val, max_val], color='#ffadad', linestyle='--', alpha=0.5, label='Line of Identity')
        
        # Create Ellipse 
        ellipse = Ellipse(xy=(np.mean(X), np.mean(Y)), width=2*SD2, height=2*SD1, angle=45, 
                         edgecolor='#00e676', fill=False, linewidth=2, label=f'Ellipse fit (SD1={SD1:.1f}, SD2={SD2:.1f})')
        ax.add_patch(ellipse)
        
        ax.set_xlabel("$BG_n$ (mmol/L)", color='#aaaaaa')
        ax.set_ylabel("$BG_{n+1}$ (mmol/L)", color='#aaaaaa')
        ax.tick_params(colors='#aaaaaa')
        ax.xaxis.grid(color='#333333', linestyle=':')
        ax.yaxis.grid(color='#333333', linestyle=':')
        ax.legend(frameon=False, labelcolor='#e0e0e0', prop={'size': 9})

    # Reserve headroom for the two-line suptitle+date block above the axes,
    # now that it replaced the single-line ax.set_title().
    fig.subplots_adjust(top=0.88)

    img_io = BytesIO()
    fig.savefig(img_io, format='png', bbox_inches='tight', dpi=100, facecolor='#1e1e1e')
    img_io.seek(0)
    
    return Response(img_io, mimetype='image/png')


# ---------------------------------------------------------------------------
# Clinical Notes: free-text notes (user + synced AAPS), Weight, HbA1c.
# See docs/analysis/notes-weight-hba1c-implementation-plan.md (v3).
# ---------------------------------------------------------------------------

def _today_local_str():
    _tz = ZoneInfo(getattr(config, 'TIMEZONE', 'UTC'))
    return datetime.now(_tz).strftime("%Y-%m-%d")


def _reject_future_date(date_str):
    """Returns a (response, status) tuple to return immediately if date_str
    is after today (local), else None. Mirrors the future-date guard
    already applied client-side in ns-date-selector.js for the shared
    date selector -- this plan deliberately doesn't reintroduce that class
    of bug in a new surface."""
    if date_str and date_str > _today_local_str():
        return jsonify({"error": f"Date {date_str} is in the future"}), 400
    return None


def _fill_hba1c_pair(data):
    """If only one of hba1c_percent/hba1c_mmol_mol was supplied, derive the
    other so both stay populated and independently queryable."""
    pct = data.get("hba1c_percent")
    mmol = data.get("hba1c_mmol_mol")
    if pct is not None and mmol is None:
        data["hba1c_mmol_mol"] = database.hba1c_percent_to_mmol_mol(pct)
    elif mmol is not None and pct is None:
        data["hba1c_percent"] = database.hba1c_mmol_mol_to_percent(mmol)
    return data


@app.route("/api/v1/notes", methods=["GET"])
def api_get_notes():
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "date is required"}), 400
    conn = None
    try:
        conn = database.get_conn()
        notes = database.get_notes_for_date(conn, date_str)
        return jsonify(notes)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/notes/range", methods=["GET"])
def api_get_notes_range():
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return jsonify({"error": "start and end are required"}), 400
    conn = None
    try:
        conn = database.get_conn()
        dates = database.get_note_dates_range(conn, start, end)
        return jsonify(dates)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/notes", methods=["POST"])
def api_save_note():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    date_str = data.get("date")
    if not date_str:
        return jsonify({"error": "date is required"}), 400
    future_err = _reject_future_date(date_str)
    if future_err:
        return future_err

    note_type = data.get("note_type")
    if note_type not in ("user", "aaps", "weight", "hba1c"):
        return jsonify({"error": f"Invalid note_type '{note_type}'"}), 400

    if note_type in ("user", "aaps") and not (data.get("text_content") or "").strip():
        return jsonify({"error": "text_content is required"}), 400

    if note_type == "hba1c":
        data = _fill_hba1c_pair(data)

    conn = None
    try:
        conn = database.get_conn()
        note = database.save_note(conn, data)
        return jsonify(note), 201 if not data.get("id") else 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/notes/<int:note_id>", methods=["DELETE"])
def api_delete_note(note_id: int):
    conn = None
    try:
        conn = database.get_conn()
        deleted = database.delete_note(conn, note_id)
        if not deleted:
            return jsonify({"error": f"Note {note_id} not found"}), 404
        return jsonify({"deleted": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/metrics/<metric_type>", methods=["GET"])
def api_get_metrics(metric_type: str):
    if metric_type not in ("weight", "hba1c"):
        return jsonify({"error": f"Invalid metric_type '{metric_type}'"}), 400
    start = request.args.get("start") or "2000-01-01"
    end = request.args.get("end") or _today_local_str()
    conn = None
    try:
        conn = database.get_conn()
        records = database.get_metric_records(conn, metric_type, start, end)
        return jsonify(records)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/metrics/<metric_type>", methods=["POST"])
def api_save_metric(metric_type: str):
    if metric_type not in ("weight", "hba1c"):
        return jsonify({"error": f"Invalid metric_type '{metric_type}'"}), 400
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    date_str = data.get("date")
    if not date_str:
        return jsonify({"error": "date is required"}), 400
    future_err = _reject_future_date(date_str)
    if future_err:
        return future_err

    data["note_type"] = metric_type
    if metric_type == "weight" and data.get("weight_kg") is None:
        return jsonify({"error": "weight_kg is required"}), 400
    if metric_type == "hba1c" and data.get("hba1c_percent") is None and data.get("hba1c_mmol_mol") is None:
        return jsonify({"error": "hba1c_percent or hba1c_mmol_mol is required"}), 400
    if metric_type == "hba1c":
        data = _fill_hba1c_pair(data)

    conn = None
    try:
        conn = database.get_conn()
        record = database.save_note(conn, data)
        return jsonify(record), 201 if not data.get("id") else 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/metrics")
def metrics_page():
    return render_template("notes_metrics.html", today_local=_today_local_str())


# ---------------------------------------------------------------------------
# CLINICAL DIARY REST API
# ---------------------------------------------------------------------------

@app.route("/api/v1/diary/categories", methods=["GET"])
def api_diary_get_categories():
    conn = None
    try:
        conn = database.get_conn()
        cats = database.get_diary_categories(conn)
        return jsonify(cats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/diary/categories", methods=["POST"])
def api_diary_save_category():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    conn = None
    try:
        conn = database.get_conn()
        saved = database.save_diary_category(conn, data)
        return jsonify(saved), 201 if not data.get("id") else 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/diary/categories/<int:cat_id>", methods=["DELETE"])
def api_diary_delete_category(cat_id: int):
    conn = None
    try:
        conn = database.get_conn()
        deleted = database.delete_diary_category(conn, cat_id)
        if not deleted:
            return jsonify({"error": f"Category {cat_id} not found"}), 404
        return jsonify({"deleted": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/diary/notes", methods=["GET"])
def api_diary_get_notes():
    category_id = request.args.get("category")
    search = request.args.get("search")
    sort = request.args.get("sort", "date_desc")
    limit = int(request.args.get("limit", 300))
    conn = None
    try:
        conn = database.get_conn()
        notes = database.get_diary_notes(conn, category_id=category_id, search=search, sort=sort, limit=limit)
        return jsonify(notes)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/diary/notes/<int:note_id>", methods=["GET"])
def api_diary_get_note(note_id: int):
    conn = None
    try:
        conn = database.get_conn()
        note = database.get_diary_note(conn, note_id)
        if not note:
            return jsonify({"error": f"Note {note_id} not found"}), 404
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/diary/notes", methods=["POST"])
def api_diary_save_note():
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    date_str = data.get("date")
    if not date_str:
        return jsonify({"error": "date is required"}), 400
    future_err = _reject_future_date(date_str)
    if future_err:
        return future_err

    # Automatically calculate paired HbA1c if either is provided
    if data.get("hba1c_percent") is not None or data.get("hba1c_mmol_mol") is not None:
        data = _fill_hba1c_pair(data)

    conn = None
    try:
        conn = database.get_conn()
        note = database.save_diary_note(conn, data)
        return jsonify(note), 201 if not data.get("id") else 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@app.route("/api/v1/diary/notes/<int:note_id>", methods=["DELETE"])
def api_diary_delete_note(note_id: int):
    conn = None
    try:
        conn = database.get_conn()
        deleted = database.delete_diary_note(conn, note_id)
        if not deleted:
            return jsonify({"error": f"Note {note_id} not found"}), 404
        return jsonify({"deleted": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)



@app.route("/api/v1/hba1c_overlay", methods=["GET"])
def api_hba1c_overlay():
    start_date = request.args.get('start_date') or request.args.get('start')
    end_date = request.args.get('end_date') or request.args.get('end')
    if not start_date or not end_date:
        return jsonify({"error": "Missing start_date or end_date"}), 400

    conn = None
    try:
        conn = database.get_conn()
        overlay_data = database.get_hba1c_overlay_data(conn, start_date, end_date)
        return jsonify({"data": overlay_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

if __name__ == "__main__":
    # Debugging is opt-in: never expose Flask's debugger by default.
    default_debug = "false"
    is_debug = os.environ.get("FLASK_DEBUG", default_debug).lower() == "true"

    # Ensure background thread only starts once even with Flask reloader
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not is_debug:
        t_main = threading.Thread(target=main_loop)
        t_main.daemon = True
        t_main.start()
        print("[daemon] Background maintenance thread started.")

        if config.NIGHTSCOUT_HOST and config.NS_SECRET:
            try:
                nightscout_api.check_network_clock_skew(config.NIGHTSCOUT_HOST)
            except Exception as ce:
                print(f"[clock] Startup clock check error: {ce}")
            import cloud_poller
            t_cloud = threading.Thread(
                target=cloud_poller.run_loop,
                kwargs={
                    "refresh_callback": trigger_background_refresh,
                    "status_data": status_data,
                    "status_lock": status_data_lock,
                },
            )
            t_cloud.daemon = True
            t_cloud.start()
            print("[daemon] cloud polling thread started.")
    
    # Enable debug mode for hot-reloading (detects file changes)
    if is_debug:
        print("[daemon] Running in DEBUG mode with hot-reload.")
    app.run(host="0.0.0.0", port=8080, debug=is_debug, use_reloader=is_debug)
