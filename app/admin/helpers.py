"""
Admin helpers: audit logging, DB connection, Jinja2 filters.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger("dm-admin")


def get_db_connection():
    """Get a psycopg2 connection from DATABASE_URL."""
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL", ""))


def _derive_audit_plugin(cur, *, resource_type: str, resource_id,
                         payload: dict | None) -> str | None:
    """Plugin concerné par l'entrée d'audit — dérivé À L'ÉCRITURE (persisté),
    pour que l'historique survive aux suppressions futures de plugins/flags.
    Sources, dans l'ordre : ressource plugin:<slug> ; payload.plugin_slug ;
    payload.plugin_id résolu via le catalogue ; flag:N → feature_flags."""
    if resource_type == "plugin" and resource_id:
        rid = str(resource_id)
        if rid.isdigit():
            # Certaines actions historiques référencent plugin:<id> numérique
            cur.execute("SELECT slug FROM plugins WHERE id = %s", (int(rid),))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
        elif rid != "*":  # '*' = purge globale, pas de plugin unique
            return rid
    if isinstance(payload, dict):
        slug = str(payload.get("plugin_slug") or "").strip()
        if slug:
            return slug
        plugin_id = payload.get("plugin_id")
        if plugin_id is not None and str(plugin_id).isdigit():
            cur.execute("SELECT slug FROM plugins WHERE id = %s", (int(plugin_id),))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    if resource_type == "flag" and resource_id and str(resource_id).isdigit():
        cur.execute("SELECT NULLIF(plugin_slug, '') FROM feature_flags WHERE id = %s",
                    (int(resource_id),))
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    return None


def audit_log(cur, *, actor: dict, action: str, resource_type: str,
              resource_id: str = None, payload: dict = None,
              ip: str = None, ua: str = None, plugin: str = None):
    """Insert an audit log entry. Must be called within the same transaction.

    `plugin` : slug explicite ; sinon dérivé automatiquement (ressource,
    payload, catalogue) — aucun call-site à modifier. Non-fatal : une
    dérivation qui échoue ne bloque jamais l'écriture de l'audit.
    """
    if plugin is None:
        try:
            plugin = _derive_audit_plugin(cur, resource_type=resource_type,
                                          resource_id=resource_id, payload=payload)
        except Exception:
            plugin = None
    cur.execute("""
        INSERT INTO admin_audit_log
          (actor_email, actor_sub, action, resource_type, resource_id, payload, ip_address, user_agent, plugin_slug)
        VALUES (%s, %s, %s, %s, %s, %s, %s::inet, %s, %s)
    """, (
        actor.get("email", "unknown"),
        actor.get("sub", "unknown"),
        action, resource_type, resource_id,
        json.dumps(payload) if payload else None,
        ip, ua, plugin
    ))
    # Émet aussi l'entrée d'audit sur stdout (visible via `kubectl logs` / SIEM).
    # La table admin_audit_log reste la source de vérité ; ce log assure qu'une
    # action laisse une trace même si elle n'est consultée qu'au fil de l'eau.
    # Aucune valeur sensible (payload) n'est journalisée ici.
    logger.info(
        "audit action=%s actor=%s sub=%s resource=%s id=%s ip=%s",
        action,
        actor.get("email", "unknown"),
        actor.get("sub", "unknown"),
        resource_type,
        resource_id,
        ip,
    )


def timeago(dt) -> str:
    """Human-readable relative time in French."""
    if dt is None:
        return "jamais"
    if isinstance(dt, (int, float)):
        dt = datetime.fromtimestamp(dt, tz=UTC)
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return str(dt)
    now = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
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
    # Lifecycle
    "ExtensionLoaded": ("Demarrage plugin", "🚀"),
    "ExtensionUpdated": ("Mise a jour", "⬆️"),
    "BootstrapConfig": ("Config bootstrap", "🔄"),
    "EnrollSuccess": ("Enrollment OK", "🔑"),
    "EnrollFailed": ("Enrollment echoue", "🔴"),
    # Writer actions
    "ExtendSelection": ("Generer la suite", "➕"),
    "EditSelection": ("Modifier selection", "✏️"),
    "ResizeSelection": ("Ajuster longueur", "📏"),
    "SummarizeSelection": ("Resumer", "📝"),
    "SimplifySelection": ("Reformuler", "💬"),
    # Calc actions
    "TransformToColumn": ("Transformer colonnes", "🔄"),
    "GenerateFormula": ("Formule IA", "🧮"),
    "AnalyzeRange": ("Analyser plage", "📊"),
    # Navigation
    "OpenmiraiWebsite": ("Site web", "🌐"),
    "OpenDocumentation": ("Documentation", "📚"),
    "OpenSettings": ("Parametres", "⚙️"),
    "AboutDialog": ("A propos", "ℹ️"),
    # Legacy aliases
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
        last_contact_at = last_contact_at.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - last_contact_at
    if last_error:
        return "error"
    if delta.total_seconds() > 86400:
        return "stale"
    return "ok"
