"""Loading of config.yaml, profile.yaml and secrets from the environment / .env."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    config: dict
    profile: dict
    profile_path: Path
    db_path: Path

    # secrets (from env)
    anthropic_key_present: bool
    telegram_token: str | None
    telegram_chat_id: str | None
    imap_host: str | None
    imap_user: str | None
    imap_password: str | None

    def section(self, name: str) -> dict:
        return self.config.get(name) or {}


def load_settings(
    config_path: Path | None = None,
    profile_path: Path | None = None,
    root: Path = ROOT,
) -> Settings:
    load_dotenv(root / ".env")

    config_path = config_path or root / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    if profile_path is None:
        profile_path = root / "profile.yaml"
        if not profile_path.exists():
            log.warning("profile.yaml not found, falling back to profile.example.yaml")
            profile_path = root / "profile.example.yaml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}

    db_path = Path(os.environ.get("JOBRADAR_DB") or root / "data" / "jobradar.sqlite3")

    return Settings(
        config=config,
        profile=profile,
        profile_path=profile_path,
        db_path=db_path,
        anthropic_key_present=bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        imap_host=os.environ.get("IMAP_HOST") or None,
        imap_user=os.environ.get("IMAP_USER") or None,
        imap_password=os.environ.get("IMAP_PASSWORD") or None,
    )
