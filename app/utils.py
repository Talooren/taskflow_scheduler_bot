"""Утилиты."""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import cfg


def utc_now_iso() -> str:
    """Текущее время UTC в ISO 8601 для Airtable."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
