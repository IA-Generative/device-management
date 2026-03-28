"""Keycloak client registry service."""

from __future__ import annotations
import json
import os


def get_defaults() -> dict:
    return {
        "realm": os.getenv("KEYCLOAK_REALM", ""),
        "issuer_url": os.getenv("KEYCLOAK_ISSUER_URL", ""),
        "default_client_id": os.getenv("KEYCLOAK_CLIENT_ID", ""),
        "default_redirect_uri": os.getenv("KEYCLOAK_REDIRECT_URI", "http://localhost:28443/callback"),
    }


def list_clients(cur, realm: str = None) -> list[dict]:
    conditions, params = [], []
    if realm:
        conditions.append("realm = %s")
        params.append(realm)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    cur.execute(f"SELECT * FROM keycloak_clients {where} ORDER BY client_id", params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_client(cur, client_db_id: int) -> dict | None:
    cur.execute("SELECT * FROM keycloak_clients WHERE id = %s", (client_db_id,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def create_client(cur, *, client_id: str, realm: str, description: str = "",
                  client_type: str = "public", redirect_uris: list = None,
                  web_origins: list = None, pkce_enabled: bool = True,
                  direct_access_grants: bool = False) -> int:
    cur.execute("""
        INSERT INTO keycloak_clients
            (client_id, realm, description, client_type, redirect_uris,
             web_origins, pkce_enabled, direct_access_grants)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (client_id, realm, description, client_type,
          json.dumps(redirect_uris or []),
          json.dumps(web_origins or ["*"]),
          pkce_enabled, direct_access_grants))
    return cur.fetchone()[0]


def export_keycloak_json(client: dict, plugin_name: str = "") -> dict:
    """Generate Keycloak-compatible client import JSON."""
    redirect_uris = client.get("redirect_uris") or []
    if isinstance(redirect_uris, str):
        redirect_uris = json.loads(redirect_uris)
    web_origins = client.get("web_origins") or ["*"]
    if isinstance(web_origins, str):
        web_origins = json.loads(web_origins)

    return {
        "clientId": client["client_id"],
        "name": plugin_name or client.get("description") or client["client_id"],
        "description": "Client genere par Device Management",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": client.get("client_type") == "public",
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": client.get("direct_access_grants", False),
        "serviceAccountsEnabled": False,
        "redirectUris": redirect_uris,
        "webOrigins": web_origins,
        "attributes": {
            "pkce.code.challenge.method": "S256" if client.get("pkce_enabled") else "",
            "post.logout.redirect.uris": "+",
        },
        "defaultClientScopes": ["web-origins", "profile", "roles", "email"],
        "optionalClientScopes": ["offline_access", "groups"],
    }


def link_plugin_client(cur, plugin_id: int, keycloak_client_id: int,
                       environment: str = "prod"):
    """Associate a Keycloak client with a plugin for an environment."""
    cur.execute("""
        INSERT INTO plugin_keycloak_clients (plugin_id, keycloak_client_id, environment)
        VALUES (%s, %s, %s)
        ON CONFLICT (plugin_id, keycloak_client_id, environment) DO NOTHING
    """, (plugin_id, keycloak_client_id, environment))


def unlink_plugin_client(cur, plugin_id: int, environment: str):
    cur.execute("""
        DELETE FROM plugin_keycloak_clients
        WHERE plugin_id = %s AND environment = %s
    """, (plugin_id, environment))


def get_plugin_clients(cur, plugin_id: int) -> list[dict]:
    """Get all keycloak clients linked to a plugin, grouped by environment."""
    cur.execute("""
        SELECT pkc.environment, kc.*
        FROM plugin_keycloak_clients pkc
        JOIN keycloak_clients kc ON kc.id = pkc.keycloak_client_id
        WHERE pkc.plugin_id = %s
        ORDER BY pkc.environment
    """, (plugin_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
