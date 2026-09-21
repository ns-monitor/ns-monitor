"""
nightscout-monitor -- Central Logging & Rotation Manager

Provides:
- Python standard library logging configuration with TimedRotatingFileHandler (daily midnight rotation, 7-day retention).
- Docker stdout streaming via StreamHandler(sys.__stdout__).
- In-memory circular buffer (RingBufferHandler, max 2000 entries) for real-time SSE web streaming.
- Dynamic runtime log-level modification (DEBUG, INFO, WARNING, ERROR) without restarting the daemon.
- Transparent stdout bridge for legacy print() statements in poller/background threads.
"""

import collections
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import re
import sys
import threading
import queue
from datetime import datetime
from zoneinfo import ZoneInfo

MAX_BUFFER_LINES = 2000
DEFAULT_LOG_LEVEL = "INFO"

LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

REVERSE_LEVEL_MAP = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


class RingBufferHandler(logging.Handler):
    """
    In-memory bounded circular buffer handler that captures log records
    as structured dictionaries and broadcasts them to active SSE listener queues.
    """
    def __init__(self, maxlen=MAX_BUFFER_LINES, get_tz_fn=None):
        super().__init__()
        self.buffer = collections.deque(maxlen=maxlen)
        self.listeners = []
        self.get_tz_fn = get_tz_fn

    def _get_tz(self):
        if self.get_tz_fn:
            try:
                return self.get_tz_fn()
            except Exception:
                pass
        return ZoneInfo("UTC")

    def emit(self, record):
        try:
            tz = self._get_tz()
            ts_str = datetime.fromtimestamp(record.created, tz).strftime("%H:%M:%S")
            date_str = datetime.fromtimestamp(record.created, tz).strftime("%Y-%m-%d")
            level_str = REVERSE_LEVEL_MAP.get(record.levelno, "INFO")
            component = record.name if record.name and record.name != "root" else "daemon"
            msg = record.getMessage()

            # Clean up component if formatted like [poller] in message
            if msg.startswith("[") and "]" in msg:
                bracket_end = msg.find("]")
                tag = msg[1:bracket_end].strip()
                if tag and not component.startswith(tag):
                    component = tag
                    msg = msg[bracket_end + 1:].strip()

            entry = {
                "ts": ts_str,
                "date": date_str,
                "level": level_str,
                "component": component,
                "msg": msg,
                "raw": f"[{ts_str}] [{level_str}] [{component}] {msg}"
            }

            with self.lock:
                self.buffer.append(entry)
                # Broadcast to SSE listeners
                dead_listeners = []
                for q in self.listeners:
                    try:
                        q.put_nowait(entry)
                    except queue.Full:
                        dead_listeners.append(q)
                for q in dead_listeners:
                    if q in self.listeners:
                        self.listeners.remove(q)
        except Exception:
            self.handleError(record)

    def get_snapshot(self, level=None, search=None, limit=None):
        with self.lock:
            items = list(self.buffer)

        if level and level.upper() != "ALL":
            target_lvl = level.upper()
            if target_lvl == "WARNING":
                target_lvl = "WARN"
            items = [item for item in items if item["level"] == target_lvl]

        if search:
            query = search.lower()
            items = [item for item in items if query in item["msg"].lower() or query in item["component"].lower()]

        if limit and len(items) > limit:
            items = items[-limit:]

        return items

    def add_listener(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.listeners.append(q)
        return q

    def remove_listener(self, q):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)


_tls = threading.local()


class StdoutToLoggerBridge:
    """
    Redirects sys.stdout writes cleanly into Docker stdout, the rotating file,
    and the in-memory SSE ring buffer without invoking logger.handle() recursively,
    preventing any potential lock contention between sys.stdout and logging internals.
    """
    def __init__(self, original_stdout, file_handler_getter, ring_buffer_handler_getter):
        self.original_stdout = original_stdout
        self.file_handler_getter = file_handler_getter
        self.ring_buffer_handler_getter = ring_buffer_handler_getter
        self.line_buffer = ""
        self.lock = threading.Lock()

    def write(self, message):
        if getattr(_tls, "in_write", False):
            self.original_stdout.write(message)
            return len(message)

        _tls.in_write = True
        try:
            with self.lock:
                self.line_buffer += message
                if "\n" in self.line_buffer:
                    lines = self.line_buffer.split("\n")
                    for line in lines[:-1]:
                        stripped = line.strip()
                        if not stripped:
                            continue

                        # 1. Output to real stdout for Docker logs
                        self.original_stdout.write(stripped + "\n")
                        self.original_stdout.flush()

                        # Deduce level and component if present
                        level_str = "INFO"
                        comp = "daemon"
                        msg = stripped

                        upper_strip = stripped.upper()
                        if "ERROR" in upper_strip or "FAIL" in upper_strip or "EXCEPTION" in upper_strip:
                            level_str = "ERROR"
                        elif "WARN" in upper_strip:
                            level_str = "WARN"
                        elif "DEBUG" in upper_strip:
                            level_str = "DEBUG"

                        if stripped.startswith("[") and "]" in stripped:
                            bracket_end = stripped.find("]")
                            comp_candidate = stripped[1:bracket_end].strip()
                            if not any(char.isdigit() for char in comp_candidate):
                                comp = comp_candidate
                                msg = stripped[bracket_end + 1:].strip()

                        # 2. Output to rotating file
                        fh = self.file_handler_getter()
                        if fh and getattr(fh, "stream", None):
                            try:
                                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                fh.stream.write(f"[{now_str}] [{level_str}] [{comp}] {msg}\n")
                                fh.stream.flush()
                            except Exception:
                                pass

                        # 3. Add to ring buffer for web UI SSE stream
                        rb = self.ring_buffer_handler_getter()
                        if rb:
                            try:
                                tz = rb._get_tz()
                                now_dt = datetime.now(tz)
                                ts_str = now_dt.strftime("%H:%M:%S")
                                date_str = now_dt.strftime("%Y-%m-%d")
                                entry = {
                                    "ts": ts_str,
                                    "date": date_str,
                                    "level": level_str,
                                    "component": comp,
                                    "msg": msg,
                                    "raw": f"[{ts_str}] [{level_str}] [{comp}] {msg}"
                                }
                                with rb.lock:
                                    rb.buffer.append(entry)
                                    for q in list(rb.listeners):
                                        try:
                                            q.put_nowait(entry)
                                        except Exception:
                                            pass
                            except Exception:
                                pass

                    self.line_buffer = lines[-1]
            return len(message)
        finally:
            _tls.in_write = False

    def flush(self):
        self.original_stdout.flush()


# Module-level singletons
_ring_buffer_handler = None
_file_handler = None
_console_handler = None
_log_dir = None
_original_stdout = sys.__stdout__


def setup_logging(log_dir=None, default_level="INFO", get_tz_fn=None):
    """
    Configures root logging with:
    1. TimedRotatingFileHandler (daily midnight rotation, 7-day retention).
    2. StreamHandler(sys.__stdout__) for Docker logs.
    3. RingBufferHandler for web UI SSE stream.
    4. Stdout bridge for legacy print() statements.
    """
    global _ring_buffer_handler, _file_handler, _console_handler, _log_dir

    if log_dir is None:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    _log_dir = log_dir
    os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger()
    target_level = LEVEL_MAP.get(default_level.upper(), logging.INFO)
    root_logger.setLevel(target_level)

    # Avoid duplicate handlers on re-init
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    # 1. Console handler (Docker container logs)
    _console_handler = logging.StreamHandler(_original_stdout)
    _console_handler.setLevel(logging.DEBUG)
    console_fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s", datefmt="%H:%M:%S")
    _console_handler.setFormatter(console_fmt)
    root_logger.addHandler(_console_handler)

    # 2. TimedRotatingFileHandler (midnight daily rotation, 7-day retention)
    log_file_path = os.path.join(log_dir, "daemon.log")
    _file_handler = TimedRotatingFileHandler(
        log_file_path,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8"
    )
    _file_handler.suffix = "%Y-%m-%d"
    _file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    _file_handler.setFormatter(file_fmt)
    root_logger.addHandler(_file_handler)

    # 3. In-memory ring buffer handler (web UI SSE stream)
    _ring_buffer_handler = RingBufferHandler(maxlen=MAX_BUFFER_LINES, get_tz_fn=get_tz_fn)
    _ring_buffer_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(_ring_buffer_handler)

    # 4. Stdout bridge
    sys.stdout = StdoutToLoggerBridge(
        _original_stdout,
        file_handler_getter=lambda: _file_handler,
        ring_buffer_handler_getter=lambda: _ring_buffer_handler
    )

    # Silence routine Werkzeug HTTP 200 access logs from flooding the ring buffer and terminal
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    root_logger.info(f"[log_manager] Standard logging initialized (level={default_level}, rotation=daily, retention=7d).")


def set_runtime_log_level(level_name):
    """Updates root logger level in-memory immediately."""
    lvl = LEVEL_MAP.get(level_name.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(lvl)
    # Keep werkzeug access logger quiet unless explicitly switched to DEBUG
    if lvl > logging.DEBUG:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
    else:
        logging.getLogger("werkzeug").setLevel(logging.DEBUG)
    root_logger.info(f"[log_manager] Runtime log level changed to {level_name.upper()}")
    return level_name.upper()


def get_runtime_log_level():
    """Returns current active level string on root logger."""
    root_logger = logging.getLogger()
    return REVERSE_LEVEL_MAP.get(root_logger.level, "INFO")


def get_ring_buffer(level=None, search=None, limit=None):
    if _ring_buffer_handler:
        return _ring_buffer_handler.get_snapshot(level=level, search=search, limit=limit)
    return []


def subscribe_stream():
    if _ring_buffer_handler:
        return _ring_buffer_handler.add_listener()
    return queue.Queue()


def unsubscribe_stream(q):
    if _ring_buffer_handler:
        _ring_buffer_handler.remove_listener(q)


def get_log_files():
    """
    Returns a list of rotated log files with sizes and dates for the 7-day retention pool.
    """
    if not _log_dir or not os.path.exists(_log_dir):
        return []

    files = []
    for fname in os.listdir(_log_dir):
        if fname.startswith("daemon.log"):
            full_path = os.path.join(_log_dir, fname)
            try:
                st = os.stat(full_path)
                files.append({
                    "filename": fname,
                    "size_bytes": st.st_size,
                    "size_formatted": f"{st.st_size / 1024:.1f} KB" if st.st_size < 1024 * 1024 else f"{st.st_size / (1024 * 1024):.2f} MB",
                    "modified_ts": st.st_mtime,
                    "is_active": (fname == "daemon.log")
                })
            except OSError:
                continue

    files.sort(key=lambda x: x["modified_ts"], reverse=True)
    return files


def get_log_file_path(filename="daemon.log"):
    """
    Safely resolves log file path within the log directory, rejecting directory traversal.
    """
    if not _log_dir:
        return None
    # Strip directory components
    safe_name = os.path.basename(filename)
    full_path = os.path.join(_log_dir, safe_name)
    if os.path.exists(full_path) and os.path.isfile(full_path):
        return full_path
    return None
