"""
Admin helpers: audit logging, DB connection, Jinja2 filters.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("dm-admin")


def get_db_connection():
    """Get a psycopg2 connection from DATABASE_URL."""
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL", ""))


def audit_log(cur, *, actor: dict, action: str, resource_type: str,
              resource_id: str = None, payload: dict = None,
              ip: str = None, ua: str = None):
    """Insert an audit log entry. Must be called within the same transaction."""
    cur.execute("""
        INSERT INTO admin_audit_log
          (actor_email, actor_sub, action, resource_type, resource_id, payload, ip_address, user_agent)
        VALUES (%s, %s, %s, %s, %s, %s, %s::inet, %s)
    """, (
        actor.get("email", "unknown"),
        actor.get("sub", "unknown"),
        action, resource_type, resource_id,
        json.dumps(payload) if payload else None,
        ip, ua
    ))


def timeago(dt) -> str:
    """Human-readable relative time in French."""
    if dt is None:
        return "jamais"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return str(dt)
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "il y a quelques secondes"
    if seconds < 3600:
        m = seconds // 60
        return f"il y a {m} min"
    if seconds < 86400:
        h = seconds // 3600
        return f"il y a {h}h"
    days = seconds // 86400
    if days == 1:
        return "il y a 1 jour"
    if days < 30:
        return f"il y a {days} jours"
    return dt.strftime("%d/%m/%Y")


SPAN_LABELS = {
    "ExtensionLoaded": ("Demarrage plugin", "🚀"),
    "ExtensionUpdated": ("Mise a jour", "⬆️"),
    "EditSelection": ("Reecriture IA", "✏️"),
    "ExtendSelection": ("Extension IA", "➕"),
    "TranslateSelection": ("Traduction", "🌐"),
    "SummarizeDocument": ("Resume", "📝"),
    "LoginSuccess": ("Connexion SSO", "🔑"),
    "LoginError": ("Echec connexion", "🔴"),
    "ConfigFetched": ("Config rechargee", "🔄"),
    "TelemetryError": ("Erreur telemetrie", "⚠️"),
}


def span_label(span_name: str) -> str:
    """Return human-readable label for a telemetry span name."""
    label, icon = SPAN_LABELS.get(span_name, (span_name, "📌"))
    return f"{icon} {label}"


def compute_device_health(last_contact_at, enrollment_status=None, last_error=None) -> str:
    """
    Compute operational health of a device.
    Returns: "ok" | "stale" | "error" | "never"
    """
    if last_contact_at is None:
        return "never"
    if isinstance(last_contact_at, str):
        try:
            last_contact_at = datetime.fromisoformat(last_contact_at)
        except Exception:
            return "never"
    if last_contact_at.tzinfo is None:
        last_contact_at = last_contact_at.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - last_contact_at
    if last_error:
        return "error"
    if delta.total_seconds() > 86400:
        return "stale"
    return "ok"
