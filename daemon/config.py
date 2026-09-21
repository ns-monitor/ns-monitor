import os

# API
NS_SECRET = os.getenv("API_SECRET", "")
NIGHTSCOUT_HOST = (os.environ.get("NIGHTSCOUT_URL") or os.environ.get("NIGHTSCOUT_HOST", "")).rstrip("/")
NS_SSL_VERIFY = os.environ.get("NS_SSL_VERIFY", "true").lower() == "true"

# Database
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_NAME = os.getenv("DB_NAME", "nightscout")
DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD", "")

# Polling Settings
POLL_PERIOD_SECONDS = int(os.environ.get("POLL_PERIOD_SECONDS", "300"))
POLL_OFFSET_SECONDS = int(os.environ.get("POLL_OFFSET_SECONDS", "30"))
FETCH_OVERLAP_MINUTES = 120
STALE_THRESHOLD_SECONDS = 600
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "365"))
RETENTION_DAYS_RAW = int(os.environ.get("RETENTION_DAYS_RAW", "120"))
RETENTION_DAYS_CLINICAL = int(os.environ.get("RETENTION_DAYS_CLINICAL", "365"))
RETENTION_ENABLED = os.environ.get("RETENTION_ENABLED", "false").lower() == "true"

# Backfill Settings
BF_CHUNK_ENTRIES = int(os.getenv("BACKFILL_CHUNK_DAYS_ENTRIES", "2"))
BF_CHUNK_TREAT = int(os.getenv("BACKFILL_CHUNK_DAYS_TREATMENTS", "7"))
BF_CHUNK_DEV = int(os.getenv("BACKFILL_CHUNK_DAYS_DEVICESTATUS", "7"))
BF_PAGE_SIZE = int(os.getenv("BACKFILL_PAGE_SIZE", "5000"))
BF_SLEEP_PAGES = float(os.getenv("BACKFILL_SLEEP_BETWEEN_PAGES_SEC", "0.6"))
BF_SLEEP_WINDOWS = float(os.getenv("BACKFILL_SLEEP_BETWEEN_WINDOWS_SEC", "1.0"))
BACKFILL_DELAY_SECONDS = float(os.getenv("BACKFILL_DELAY_SECONDS", "0"))

# Monitoring
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

# Local Settings (Runtime)
# Note: this is only the process-start fallback. sync_system_config() (called
# at daemon startup) seeds this into system_config on first boot; from then on
# load_settings_from_db() overrides config.TIMEZONE from the DB on every
# startup, making the DB value the real source of truth (Settings UI).
TIMEZONE = os.environ.get("TIMEZONE", "UTC")

