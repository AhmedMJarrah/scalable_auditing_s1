"""
Central configuration for scalable_auditing_s1.

Design rule: every path, URL and secret comes from the environment (.env).
Nothing is hardcoded. Step 1 runs locally on Windows, but the identical code
must run on the Linux server later — moving machines has to be an .env edit,
never a code edit.

Usage:
    from src.core.config import get_settings
    settings = get_settings()
    print(settings.logs_dir)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/core/config.py -> src/core -> src -> <project root>
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Settings(BaseSettings):
    """All runtime settings. Field names map to UPPER_CASE keys in .env."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- environment -----------------------------------------------------
    app_env: str = "local"          # local | server | prod
    random_seed: int = 20260804     # fixed seed => reproducible sampling

    # --- paths (all optional in .env; derived from project_root if absent) -
    project_root: Path | None = None
    data_dir: Path | None = None
    logs_dir: Path | None = None
    configs_dir: Path | None = None

    # --- database ---------------------------------------------------------
    # SQLite locally, Postgres on the server. Same code either way.
    database_url: str | None = None

    # --- auth / assignment --------------------------------------------------
    # These were present in .env.example from step 1 but NOT declared here,
    # so extra="ignore" silently dropped them — reading settings.secret_key
    # would have raised AttributeError the first time auth code needed it.
    secret_key: str = ""
    session_max_age_minutes: int = 480
    assignment_lease_minutes: int = 45

    # --- logging ----------------------------------------------------------
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024   # 10 MB per file before rotation
    log_backup_count: int = 10
    log_console: bool = True

    # ----------------------------------------------------------------------
    @field_validator(
        "project_root", "data_dir", "logs_dir", "configs_dir", "database_url",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        """
        A blank key in .env (DATA_DIR=) arrives as "", not None. Without this,
        Path("") resolves to the project root and logs/data land in the wrong
        place — silently. Treat blank as "not set".
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_level(cls, v: Any) -> str:
        level = str(v).strip().upper()
        if level not in VALID_LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(VALID_LOG_LEVELS)}, got {v!r}"
            )
        return level

    @model_validator(mode="after")
    def _derive_paths(self) -> "Settings":
        root = (self.project_root or REPO_ROOT).expanduser().resolve()
        object.__setattr__(self, "project_root", root)

        if self.data_dir is None:
            object.__setattr__(self, "data_dir", root / "data")
        if self.logs_dir is None:
            object.__setattr__(self, "logs_dir", root / "logs")
        if self.configs_dir is None:
            object.__setattr__(self, "configs_dir", root / "configs")

        # Resolve any path that came from .env as a relative value.
        for name in ("data_dir", "logs_dir", "configs_dir"):
            p: Path = getattr(self, name)
            if not p.is_absolute():
                p = (root / p).resolve()
            object.__setattr__(self, name, p)

        if not self.database_url:
            # as_posix() matters on Windows: SQLAlchemy wants forward slashes,
            # otherwise C:\... backslashes break the URL.
            db_path = (self.data_dir / "s1.db").as_posix()
            object.__setattr__(self, "database_url", f"sqlite:///{db_path}")

        return self

    # ----------------------------------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        return (self.database_url or "").startswith("sqlite")

    def ensure_dirs(self) -> None:
        """Create the directories this process needs. Safe to call repeatedly."""
        for p in (self.data_dir, self.logs_dir, self.configs_dir):
            p.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, Any]:
        """Config summary safe to write into a log (secrets redacted)."""
        return {
            "app_env": self.app_env,
            "project_root": str(self.project_root),
            "data_dir": str(self.data_dir),
            "logs_dir": str(self.logs_dir),
            "configs_dir": str(self.configs_dir),
            "database_backend": "sqlite" if self.is_sqlite else "postgres",
            "database_url": self._redacted_db_url(),
            "log_level": self.log_level,
            "random_seed": self.random_seed,
            "python_utf8_mode": os.environ.get("PYTHONUTF8", "0"),
        }

    def _redacted_db_url(self) -> str:
        """Hide credentials in a Postgres URL before it reaches the log file."""
        url = self.database_url
        if "@" in url and "://" in url:
            scheme, rest = url.split("://", 1)
            _, host = rest.rsplit("@", 1)
            return f"{scheme}://***:***@{host}"
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Import this, never construct Settings() directly."""
    return Settings()


if __name__ == "__main__":
    import json

    print(json.dumps(get_settings().describe(), indent=2, ensure_ascii=False))
