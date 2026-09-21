import requests
import time
import random
from datetime import datetime, timezone
import config

# Time Helper
def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _headers():
    return {"User-Agent": "nightscout-monitor/1.0", "Accept": "application/json"}

def fetch_ns_page(endpoint: str, params: dict, retries: int = 3, url: str = None, secret: str = None) -> list:
    base_url = (url or config.NIGHTSCOUT_HOST).rstrip("/")
    target_url = f"{base_url}/{endpoint}"
    
    final_secret = secret or config.NS_SECRET
    if final_secret:
        params = dict(params)
        params["token"] = final_secret
    
    verify_ssl = getattr(config, "NS_SSL_VERIFY", True)
    
    for i in range(retries + 1):
        try:
            r = requests.get(target_url, headers=_headers(), params=params, timeout=30, verify=verify_ssl)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise RuntimeError(f"Non-list response from {endpoint}")
            return data
        except Exception as e:
            if i < retries:
                wait = (2 ** i) + random.uniform(0.1, 1.0)
                print(f"[api] Error fetching {endpoint} (try {i+1}/{retries+1}): {e}. Retrying in {wait:.2f}s...")
                time.sleep(wait)
            else:
                print(f"[api] Final failure for {endpoint} after {retries+1} attempts: {e}")
                return []

def check_network_clock_skew(url: str = None) -> bool:
    """
    Checks if the local system clock has drifted significantly (> 120s)
    from the HTTP network time (e.g. host rebooted without RTC sync).
    Returns True if healthy, False if significant skew detected.
    """
    base_url = (url or config.NIGHTSCOUT_HOST).rstrip("/")
    try:
        verify_ssl = getattr(config, "NS_SSL_VERIFY", True)
        r = requests.head(base_url, timeout=5, verify=verify_ssl)
        server_date_str = r.headers.get("Date")
        if server_date_str:
            from email.utils import parsedate_to_datetime
            server_dt = parsedate_to_datetime(server_date_str)
            local_dt = datetime.now(timezone.utc)
            skew = abs((local_dt - server_dt).total_seconds())
            if skew > 120:
                print(f"[clock] WARNING: Clock skew detected! Local={local_dt.isoformat()}, Server={server_dt.isoformat()} (delta: {skew:.1f}s)")
                return False
            else:
                print(f"[clock] System time healthy (skew: {skew:.1f}s vs network).")
                return True
    except Exception as e:
        print(f"[clock] Notice: Could not perform network clock check: {e}")
    return True


def fetch_entries_range_paged(start_ts: datetime, end_ts: datetime, url: str = None, secret: str = None) -> list[dict]:
    all_rows = []
    skip = 0
    max_pages = 50
    pages = 0
    last_first_marker = None

    while True:
        params = {
            "count": config.BF_PAGE_SIZE,
            "skip": skip,
            "find[date][$gte]": dt_to_ms(start_ts),
            "find[date][$lt]": dt_to_ms(end_ts),
        }
        page = fetch_ns_page("entries.json", params, url=url, secret=secret)
        if not page:
            break

        first_marker = page[0].get("_id") or page[0].get("date") or page[0].get("dateString")
        if last_first_marker is not None and first_marker == last_first_marker:
            print("[entries] paging stalled. Breaking.")
            break
        last_first_marker = first_marker

        all_rows.extend(page)
        if len(page) < config.BF_PAGE_SIZE:
            break

        # Sleep between pages to protect Nightscout server
        if config.BF_SLEEP_PAGES > 0:
             time.sleep(config.BF_SLEEP_PAGES)

        skip += config.BF_PAGE_SIZE
        pages += 1
        if pages >= max_pages:
            print("[entries] WARN: Hit max_pages limit, data may be truncated")
            break
            
    return all_rows

def fetch_treatments_range(start_ts: datetime, end_ts: datetime, url: str = None, secret: str = None) -> list[dict]:
    params = {
        "count": config.BF_PAGE_SIZE,
        "find[created_at][$gte]": iso_z(start_ts),
        "find[created_at][$lt]": iso_z(end_ts),
    }
    return fetch_ns_page("treatments.json", params, url=url, secret=secret)

def fetch_devicestatus_range(start_ts: datetime, end_ts: datetime, url: str = None, secret: str = None) -> list[dict]:
    params = {
        "count": config.BF_PAGE_SIZE,
        "find[created_at][$gte]": iso_z(start_ts),
        "find[created_at][$lt]": iso_z(end_ts),
    }
    return fetch_ns_page("devicestatus.json", params, url=url, secret=secret)

def fetch_profiles(url: str = None, secret: str = None) -> list[dict]:
    return fetch_ns_page("profile.json", {}, url=url, secret=secret)

def ping_healthcheck(lag_seconds: int = None, health_url: str = None, has_gaps: bool = False, sync_status: str = "ok"):
    """
    State-of-Health (SoH) prober. 
    1. Enforces a Lag Guard (20 min threshold).
    2. Checks for Ingestion Gaps.
    3. Pings the configured healthcheck URL if sync is healthy (green status-ok).
    4. Suppresses the outgoing ping if any health check fails.
    """
    health = {
        "sync": sync_status,
        "integrity": "ok",
    }

    # 1. Evaluate Sync (Lag Guard)
    if lag_seconds is not None and lag_seconds > getattr(config, 'STALE_THRESHOLD_SECONDS', 600):
        health["sync"] = f"stale ({lag_seconds}s)"
    
    # 2. Evaluate Integrity
    if has_gaps:
        health["integrity"] = "gaps_detected"

    # 3. Decision: Should we ping?
    # Healthy when sync is green ('ok', 'ok (cloud)', etc.) and integrity is ok
    s = (health["sync"] or "").lower()
    is_sync_ok = s.startswith("ok") or "polling" in s or "replaying" in s
    is_healthy = is_sync_ok and health["integrity"] == "ok"

    target_health_url = health_url or getattr(config, 'HEALTHCHECK_URL', '')

    if target_health_url and is_healthy:
        try:
            requests.get(target_health_url, timeout=10)
            print(f"[watchdog] SoH Healthy ({health['sync']}). Ping sent to {target_health_url[:35]}...")
        except Exception as e:
            print(f"[watchdog] Warning: failed to ping healthcheck: {e}")
    elif target_health_url and not is_healthy:
        print(f"[watchdog] SoH DEGRADED ({health['sync']}). Ping suppressed.")

    return health
