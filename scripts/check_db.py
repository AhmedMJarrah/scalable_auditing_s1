"""
Verify the database is migrated and healthy.

    python scripts\\check_db.py

Checks: alembic is at head, every table from models.py exists, and a full
insert/query round trip with Arabic text and a foreign key succeeds. Run
this after every `alembic upgrade head`, and before pointing the ingestion
adapter at it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, select                                # noqa: E402

from src.core.config import get_settings                              # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage   # noqa: E402
from src.db.base import Base                                          # noqa: E402
from src.db import models                                             # noqa: E402
from src.db.session import get_engine, session_scope                  # noqa: E402

ARABIC_PROBE = "قانون ضريبة الدخل رقم 34 لسنة 2014"


def check_migration_head(engine) -> tuple[bool, str]:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()

    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()

    if current is None:
        return False, "no migration applied yet — run: alembic upgrade head"
    if current != head:
        return False, f"database is at {current}, latest is {head} — run: alembic upgrade head"
    return True, f"at head ({current})"


def check_tables_exist(engine) -> tuple[bool, str]:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    expected = set(Base.metadata.tables.keys()) - {"alembic_version"}
    missing = expected - existing
    if missing:
        return False, f"missing tables: {sorted(missing)}"
    return True, f"{len(expected)} tables present"


def check_arabic_roundtrip() -> tuple[bool, str]:
    try:
        with session_scope() as db:
            leg = models.Legislation(
                id="__doctor_check__/9999", leg_name=ARABIC_PROBE,
                leg_number="__doctor__", year="9999", leg_type="law",
                source_meta={"note": ARABIC_PROBE},
            )
            db.add(leg)
            db.flush()

            auditor = models.Auditor(
                username="__doctor_check__", password_hash="x",
                display_name_ar=ARABIC_PROBE, role="auditor",
            )
            db.add(auditor)
            db.flush()

            item = models.AuditItem(
                spec_key="__doctor__", unit="legislation", legislation_id=leg.id,
            )
            db.add(item)
            db.flush()

            assignment = models.Assignment(
                item_id=item.id, auditor_id=auditor.id,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
            db.add(assignment)
            db.flush()

            response = models.Response(
                item_id=item.id, auditor_id=auditor.id, spec_key="__doctor__",
                spec_version=1, answers={"note": ARABIC_PROBE},
            )
            db.add(response)
            db.flush()

            back = db.execute(
                select(models.Legislation).where(models.Legislation.id == leg.id)
            ).scalar_one()
            ok = back.leg_name == ARABIC_PROBE and back.source_meta["note"] == ARABIC_PROBE

            # clean up — this is a check, not real data
            db.delete(response)
            db.delete(assignment)
            db.delete(item)
            db.delete(auditor)
            db.delete(leg)

        if not ok:
            return False, "Arabic text did not round-trip correctly"
        return True, "insert/query round trip verified, including FKs and JSON"
    except Exception as exc:                                          # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    log = get_logger("s1.check_db")
    engine = get_engine(settings)

    checks = []
    with stage("check_db", logger=log) as counters:
        for name, fn in (
            ("migration_head", lambda: check_migration_head(engine)),
            ("tables_exist", lambda: check_tables_exist(engine)),
            ("arabic_roundtrip", check_arabic_roundtrip),
        ):
            ok, detail = fn()
            checks.append((name, ok, detail))
            level = log.info if ok else log.error
            level("db.check", extra={"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

        failures = sum(1 for _, ok, _ in checks if not ok)
        counters.update(checks=len(checks), failures=failures)

    print()
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL':<5} {name:<20} {detail}")
    print()
    if failures:
        print(f"{failures} check(s) failed.")
        return 1
    print("All database checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
