"""
Environment doctor for scalable_auditing_s1.

Checks the things that silently break an Arabic data pipeline on Windows,
before any real code is layered on top. Run it after creating the venv, and
again on the server before the first deployment.

    py -3.11 scripts\\doctor.py

Exit code 0 = all pass or warnings only. 1 = at least one FAIL.
"""

from __future__ import annotations

import locale
import os
import platform
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings              # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage  # noqa: E402

ARABIC_PROBE = "قانون ضريبة الدخل رقم 34 لسنة 2014 — المادة الأولى"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def check_python_version() -> Check:
    major, minor = sys.version_info[:2]
    version = platform.python_version()
    if (major, minor) == (3, 11):
        return Check("python_version", PASS, version)
    if major == 3 and minor >= 11:
        return Check("python_version", WARN, f"{version} (project targets 3.11)")
    return Check("python_version", FAIL, f"{version} — 3.11 required")


def check_virtualenv() -> Check:
    active = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if active:
        return Check("virtualenv", PASS, sys.prefix)
    return Check(
        "virtualenv", FAIL,
        "not running inside a venv — activate s1\\Scripts\\activate first",
    )


def check_utf8_mode() -> Check:
    """
    PYTHONUTF8=1 is the single most valuable Windows setting for this project.
    Without it, stdout defaults to the legacy console codepage.
    """
    utf8_mode = os.environ.get("PYTHONUTF8") == "1" or sys.flags.utf8_mode
    stdout_enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    preferred = locale.getpreferredencoding(False).lower()

    detail = f"utf8_mode={bool(utf8_mode)} stdout={stdout_enc} locale={preferred}"
    if utf8_mode and "utf-8" in stdout_enc:
        return Check("utf8_mode", PASS, detail)
    if "utf-8" in stdout_enc:
        return Check("utf8_mode", WARN, detail + " — set PYTHONUTF8=1 to be safe")
    return Check(
        "utf8_mode", FAIL,
        detail + " — set PYTHONUTF8=1 (setx PYTHONUTF8 1, then reopen VS Code)",
    )


def check_arabic_roundtrip() -> Check:
    """Write Arabic to disk and read it back. If this fails, nothing else matters."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.txt"
            probe.write_text(ARABIC_PROBE, encoding="utf-8")
            back = probe.read_text(encoding="utf-8")
        if back == ARABIC_PROBE:
            return Check("arabic_roundtrip", PASS, "utf-8 write/read verified")
        return Check("arabic_roundtrip", FAIL, "text changed on round-trip")
    except Exception as exc:                                  # noqa: BLE001
        return Check("arabic_roundtrip", FAIL, f"{type(exc).__name__}: {exc}")


def check_console_arabic() -> Check:
    """Can the console actually print Arabic? A warning, not a failure."""
    try:
        sys.stdout.write("")
        ARABIC_PROBE.encode(sys.stdout.encoding or "utf-8")
        return Check("console_arabic", PASS, "console can render Arabic")
    except (UnicodeEncodeError, LookupError):
        return Check(
            "console_arabic", WARN,
            "console cannot encode Arabic — log files are still correct",
        )


def check_cloud_sync(project_root: Path) -> Check:
    """
    OneDrive/Dropbox locking a SQLite file mid-write corrupts it silently.
    This is the highest-severity environment risk in the whole project.
    """
    parts = {p.lower() for p in project_root.parts}
    markers = {"onedrive", "dropbox", "google drive", "googledrive", "icloud"}
    hit = parts & markers
    if hit:
        return Check(
            "cloud_sync", FAIL,
            f"project sits inside a synced folder ({', '.join(sorted(hit))}) — "
            "move it (e.g. C:\\nicst) or the database will eventually corrupt",
        )
    if "onedrive" in str(os.environ.get("OneDrive", "")).lower() and \
            str(project_root).lower().startswith(
                str(os.environ.get("OneDrive", "")).lower()):
        return Check("cloud_sync", FAIL, "project is under the OneDrive root")
    return Check("cloud_sync", PASS, "not inside a known sync folder")


def check_path_length(project_root: Path) -> Check:
    """
    Windows MAX_PATH is 260 chars. Arabic folder names plus a nested venv
    eat that budget fast.
    """
    length = len(str(project_root))
    if length <= 60:
        return Check("path_length", PASS, f"{length} chars")
    if length <= 120:
        return Check("path_length", WARN, f"{length} chars — keep the tree shallow")
    return Check(
        "path_length", FAIL,
        f"{length} chars — too deep, move the project closer to C:\\",
    )


def check_writable(label: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Check(f"writable_{label}", PASS, str(path))
    except Exception as exc:                                  # noqa: BLE001
        return Check(f"writable_{label}", FAIL, f"{path}: {exc}")


def check_sqlite_wal(data_dir: Path) -> Check:
    """WAL mode is what makes SQLite usable with concurrent readers."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".wal_probe.db"
        conn = sqlite3.connect(probe)
        mode = conn.execute("PRAGMA journal_mode=WAL;").fetchone()[0]
        conn.execute("CREATE TABLE t (x TEXT);")
        conn.execute("INSERT INTO t VALUES (?);", (ARABIC_PROBE,))
        conn.commit()
        stored = conn.execute("SELECT x FROM t;").fetchone()[0]
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(probe) + suffix).unlink(missing_ok=True)

        if stored != ARABIC_PROBE:
            return Check("sqlite", FAIL, "Arabic text corrupted in SQLite")
        if mode.lower() != "wal":
            return Check("sqlite", WARN, f"journal_mode={mode}, expected wal")
        return Check("sqlite", PASS, f"sqlite {sqlite3.sqlite_version}, journal_mode=wal")
    except Exception as exc:                                  # noqa: BLE001
        return Check("sqlite", FAIL, f"{type(exc).__name__}: {exc}")


def check_env_file(project_root: Path) -> Check:
    if (project_root / ".env").exists():
        return Check("env_file", PASS, ".env present")
    return Check("env_file", WARN, ".env missing — copy .env.example to .env")


def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    log = get_logger("s1.doctor")

    with stage("doctor", logger=log) as counters:
        checks = [
            check_python_version(),
            check_virtualenv(),
            check_utf8_mode(),
            check_arabic_roundtrip(),
            check_console_arabic(),
            check_cloud_sync(settings.project_root),
            check_path_length(settings.project_root),
            check_writable("logs", settings.logs_dir),
            check_writable("data", settings.data_dir),
            check_sqlite_wal(settings.data_dir),
            check_env_file(settings.project_root),
        ]

        for c in checks:
            level = {PASS: log.info, WARN: log.warning, FAIL: log.error}[c.status]
            level("doctor.check", extra={"check": c.name, "status": c.status,
                                         "detail": c.detail})

        failures = sum(1 for c in checks if c.status == FAIL)
        warnings = sum(1 for c in checks if c.status == WARN)
        counters.update(checks=len(checks), failures=failures, warnings=warnings)

    width = max(len(c.name) for c in checks)
    print("\n" + "=" * (width + 12))
    for c in checks:
        print(f"{c.status:<5} {c.name:<{width}}  {c.detail}")
    print("=" * (width + 12))
    print(f"{len(checks)} checks | {failures} failed | {warnings} warnings\n")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
