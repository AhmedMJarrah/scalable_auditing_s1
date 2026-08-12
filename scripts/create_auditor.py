"""
Create an auditor account.

    python scripts\\create_auditor.py <username> [--role admin] [--name "الاسم بالعربي"]

Prompts for the password interactively (never on the command line — shell
history and process lists both leak it otherwise).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.assignment.auth import AuthError, create_auditor          # noqa: E402
from src.core.config import get_settings                           # noqa: E402
from src.core.logging_setup import get_logger, setup_logging       # noqa: E402
from src.db.session import session_scope                           # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an auditor account.")
    parser.add_argument("username")
    parser.add_argument("--role", choices=["auditor", "admin"], default="auditor")
    parser.add_argument("--name", dest="display_name_ar", default=None,
                         help="Display name in Arabic (optional)")
    args = parser.parse_args()

    setup_logging(get_settings())
    log = get_logger("s1.create_auditor")

    password = getpass.getpass("Password (min 8 chars, never echoed): ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        return 1

    try:
        with session_scope() as db:
            auditor = create_auditor(
                db, args.username, password, role=args.role,
                display_name_ar=args.display_name_ar,
            )
            username, role, auditor_id = auditor.username, auditor.role, auditor.id
    except AuthError as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"\nCreated auditor {username!r} (role={role}, id={auditor_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
