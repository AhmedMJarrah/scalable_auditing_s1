"""
Bootstrap the scalable_auditing_s1 project tree.

Creates the directory layout, package __init__.py files, and .gitkeep markers
so empty directories survive a git commit. Copies .env.example to .env if
.env does not exist yet.

    py -3.11 scripts\\bootstrap.py

Idempotent — nothing is overwritten, so run it as often as you like.

Note: .gitignore, .gitattributes, .env.example, requirements.txt and README.md
are tracked files in the repo, not generated here. If one is missing this
script reports it rather than recreating it, so a deleted file is noticed
instead of silently restored.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings                              # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage   # noqa: E402

DIRECTORIES: list[str] = [
    "configs",              # one YAML spec per audit type
    "data/raw",             # exactly as received; never edited in place
    "data/interim",
    "data/processed",
    "docs",
    "logs",
    "reports",
    "src/core",
    "src/db",
    "src/ingest",
    "src/sampling",
    "src/scoring",
    "src/sync",
    "src/web",
    "src/web/templates",
    "src/web/static",
    "tests",
]

PACKAGE_DIRS: list[str] = [
    "src", "src/core", "src/db", "src/ingest",
    "src/sampling", "src/scoring", "src/sync", "src/web", "tests",
]

# Directories that are gitignored but must still exist at runtime; a .gitkeep
# keeps the shape of the tree visible to anyone cloning the repo.
KEEP_DIRS: list[str] = [
    "data/raw", "data/interim", "data/processed", "logs", "reports",
]

EXPECTED_REPO_FILES: list[str] = [
    ".gitignore", ".gitattributes", ".env.example",
    "requirements.txt", "README.md",
]


def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    log = get_logger("s1.bootstrap")

    with stage("bootstrap", logger=log, project_root=str(ROOT)) as counters:
        dirs_created = 0
        for rel in DIRECTORIES:
            path = ROOT / rel
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                dirs_created += 1
                log.info("dir.created", extra={"path": rel})

        inits_created = 0
        for rel in PACKAGE_DIRS:
            init = ROOT / rel / "__init__.py"
            if not init.exists():
                init.write_text("", encoding="utf-8")
                inits_created += 1

        keeps_created = 0
        for rel in KEEP_DIRS:
            keep = ROOT / rel / ".gitkeep"
            if not keep.exists():
                keep.write_text("", encoding="utf-8")
                keeps_created += 1

        missing = [n for n in EXPECTED_REPO_FILES if not (ROOT / n).exists()]
        for name in missing:
            log.error("repo_file.missing", extra={"path": name})

        env_created = False
        env_path = ROOT / ".env"
        example = ROOT / ".env.example"
        if not env_path.exists() and example.exists():
            shutil.copyfile(example, env_path)
            env_created = True
            log.warning(
                "env.created_from_example",
                extra={"action": "open .env and fill in the blank values"},
            )

        counters.update(
            dirs_created=dirs_created,
            inits_created=inits_created,
            gitkeeps_created=keeps_created,
            repo_files_missing=len(missing),
            env_created=env_created,
        )

    if missing:
        print("\nMissing tracked files (restore from git, do not recreate by hand):")
        for name in missing:
            print(f"  - {name}")

    print("\nBootstrap complete. Next: py -3.11 scripts\\doctor.py")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
