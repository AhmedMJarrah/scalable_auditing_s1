"""
Structured logging for scalable_auditing_s1.

Every run writes two files:
    logs/s1.log        human-readable, rotating  -> for you, while working
    logs/s1.jsonl      one JSON object per line  -> for querying and reports

Both are UTF-8. This is not optional on Windows: the default console codepage
will turn Arabic into '????' and will eventually raise UnicodeEncodeError from
inside the logging handler itself, which is a miserable bug to trace.

Every record carries a run_id, so you can isolate a single execution later:
    findstr "<run_id>" logs\\s1.jsonl

Usage:
    from src.core.logging_setup import setup_logging, get_logger, stage

    run_id = setup_logging()
    log = get_logger(__name__)

    with stage("ingest.laws") as counters:
        counters["rows_in"] = 5879
        counters["rows_rejected"] = 12
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.core.config import Settings, get_settings

# One id per process. Stamped onto every record emitted by this run.
RUN_ID: str = uuid.uuid4().hex[:12]

_CONFIGURED = False

# Standard LogRecord attributes. Anything outside this set that appears on a
# record was passed by us via extra={...}, so it goes into the JSON payload.
_RESERVED_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
}


class RunIdFilter(logging.Filter):
    """Attach the process run_id to every record that lacks one."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = RUN_ID
        return True


class JsonlFormatter(logging.Formatter):
    """One JSON object per line. ensure_ascii=False keeps Arabic readable."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "run_id": getattr(record, "run_id", None),
            "logger": record.name,
            "event": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }

        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED_ATTRS and not k.startswith("_") and k != "run_id"
        }
        if extras:
            payload["ctx"] = extras

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # default=str so Path, datetime, Decimal etc. never break a log write.
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Readable single line, with any extra context appended compactly."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(run_id)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED_ATTRS and not k.startswith("_") and k != "run_id"
        }
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in extras.items())
            base = f"{base} [{rendered}]"
        return base


def _force_utf8_stream(stream: Any) -> Any:
    """
    Make stdout/stderr UTF-8 capable.

    On Windows the console is often cp1252/cp437. Without this, the first
    Arabic log line crashes the handler. errors='replace' is a deliberate
    fallback: a mangled character in the console is acceptable, a crashed
    pipeline is not. The file handlers below are always true UTF-8.
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    return stream


def setup_logging(settings: Settings | None = None) -> str:
    """
    Configure root logging. Idempotent — calling it twice is a no-op.

    Returns the run_id so callers can print or persist it.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return RUN_ID

    settings = settings or get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    run_filter = RunIdFilter()

    # 1. Human-readable rotating log.
    human = logging.handlers.RotatingFileHandler(
        filename=settings.logs_dir / "s1.log",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",          # non-negotiable
    )
    human.setFormatter(HumanFormatter())
    human.addFilter(run_filter)
    root.addHandler(human)

    # 2. Structured JSONL log.
    jsonl = logging.handlers.RotatingFileHandler(
        filename=settings.logs_dir / "s1.jsonl",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",          # non-negotiable
    )
    jsonl.setFormatter(JsonlFormatter())
    jsonl.addFilter(run_filter)
    root.addHandler(jsonl)

    # 3. Console.
    if settings.log_console:
        console = logging.StreamHandler(_force_utf8_stream(sys.stdout))
        console.setFormatter(HumanFormatter())
        console.addFilter(run_filter)
        root.addHandler(console)

    # Third-party libraries are noisy at DEBUG; keep our own output legible.
    for noisy in ("urllib3", "googleapiclient", "google_auth_httplib2", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True

    logging.getLogger("s1.boot").info(
        "run.start",
        extra={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
            **settings.describe(),
        },
    )
    return RUN_ID


def get_logger(name: str | None = None) -> logging.Logger:
    """Get a logger, configuring logging on first use."""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name or "s1")


@contextmanager
def stage(name: str, logger: logging.Logger | None = None, **context: Any) -> Iterator[dict]:
    """
    Wrap a pipeline stage so it always logs start, duration and outcome.

    Yields a mutable dict — put row counts and metrics in it, and they are
    written on the closing record:

        with stage("sampling.laws", audit_type="metadata") as c:
            c["population"] = 6113
            c["sampled"] = 100

    Note: keys passed here must not collide with reserved LogRecord attribute
    names (message, module, name, args, lineno, ...) or logging raises.
    """
    log = logger or get_logger("s1.stage")
    counters: dict[str, Any] = {}
    started = time.perf_counter()

    log.info("stage.start", extra={"stage": name, **context})
    try:
        yield counters
    except Exception:
        log.exception(
            "stage.failed",
            extra={
                "stage": name,
                "duration_s": round(time.perf_counter() - started, 3),
                **context,
                **counters,
            },
        )
        raise
    else:
        log.info(
            "stage.done",
            extra={
                "stage": name,
                "duration_s": round(time.perf_counter() - started, 3),
                **context,
                **counters,
            },
        )


if __name__ == "__main__":
    rid = setup_logging()
    log = get_logger("s1.demo")
    log.info("logging.selftest", extra={"note": "اختبار الترميز العربي"})
    with stage("demo.stage", sample="قانون ضريبة الدخل رقم 34 لسنة 2014") as c:
        c["rows"] = 3
        time.sleep(0.05)
    print(f"\nrun_id = {rid}")
    print(f"logs written to {get_settings().logs_dir}")
