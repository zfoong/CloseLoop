"""Central logging for CloseLoop.

One logger tree under "closeloop.*". Two sinks:
  - console at the configured level (default INFO) — readable operator view,
  - rotating file at DEBUG (data/closeloop.log) — the comprehensive record.

Configure once at startup via setup_logging(cfg). Every module does
`log = logsetup.get("<module>")` and logs at DEBUG/INFO/WARNING/ERROR.
"""
import logging
import logging.handlers
import os
import sys

from .config import ROOT

_configured = False


def setup_logging(cfg=None):
    """Idempotent. Reads cfg['logging'] = {console_level, file_level, file}."""
    global _configured
    logcfg = (cfg or {}).get("logging", {}) if cfg else {}
    console_level = str(logcfg.get("console_level", logcfg.get("level", "INFO"))).upper()
    file_level = str(logcfg.get("file_level", "DEBUG")).upper()
    logfile = logcfg.get("file", os.path.join(ROOT, "data", "closeloop.log"))

    root = logging.getLogger("closeloop")
    if _configured:
        return root
    os.makedirs(os.path.dirname(logfile), exist_ok=True)

    # Windows consoles default to cp1252; force UTF-8 so unicode in task text,
    # S2 output, or log messages doesn't turn into mojibake.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    console_fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
    file_fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, console_level, logging.INFO))
    ch.setFormatter(console_fmt)
    root.addHandler(ch)

    fh = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(getattr(logging, file_level, logging.DEBUG))
    fh.setFormatter(file_fmt)
    root.addHandler(fh)

    _configured = True
    root.info("logging initialised — console=%s file=%s path=%s", console_level, file_level, logfile)
    return root


def get(name):
    """Get a module logger, e.g. logsetup.get('jev') -> 'closeloop.jev'."""
    return logging.getLogger("closeloop." + name)


def preview(value, limit=200):
    """Single-line truncated preview for log messages."""
    s = value if isinstance(value, str) else repr(value)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"
