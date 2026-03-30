"""
Admin UI router — all routes under /admin/*.
Security: every route (except login/callback) uses @require_admin.
Architecture: routes delegate to services, never run SQL directly.
"""

import csv
import io
import json
import logging
import os
import time

from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates

from .auth import (
    require_admin, _sign_session, _verify_session, _get_oidc_config,
    _get_token_endpoint, _has_admin_group, SESSION_COOKIE, SESSION_TTL,
    CLIENT_ID, CLIENT_SECRET, REDIRECT_URI,
)
from .helpers import audit_log, get_db_connection, timeago, span_label

from .services import (
    devices as devices_svc,
    campaigns as campaigns_svc,
    flags as flags_svc,
    cohorts as cohorts_svc,
    artifacts as artifacts_svc,
    audit as audit_svc,
    catalog as catalog_svc,
    communications as comms_svc,
    keycloak as keycloak_svc,
)

from ..settings import settings

logger = logging.getLogger("dm-admin-router")

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")


def _get_deploy_environments() -> list[dict]:
    """Return the list of deploy environments from settings."""
    try:
        return json.loads(settings.deploy_environments_json)
    except (json.JSONDecodeError, TypeError):
        return [
            {"name": "dev", "label": "Dev", "strategy": "patch_all", "confirm_name": False},
            {"name": "int", "label": "Integration", "strategy": "patch_all", "confirm_name": False},
            {"name": "beta", "label": "Beta", "strategy": "choice", "confirm_name": False},
            {"name": "preview", "label": "Preview", "strategy": "choice", "confirm_name": False},
            {"name": "prod", "label": "Production", "strategy": "progressive", "confirm_name": True},
        ]

# Register custom Jinja2 filters
templates.env.globals["timeago"] = timeago
templates.env.globals["span_label"] = span_label


# ─── dm-config.json helpers ──────────────────────────────────────────────

_PLATFORM_DEFAULTS = {
    "llm_base_urls": "${{LLM_BASE_URL}}",
    "llm_default_models": "${{DEFAULT_MODEL_NAME}}",
    "llm_api_tokens": "${{LLM_API_TOKEN}}",
    "keycloakIssuerUrl": "${{KEYCLOAK_ISSUER_URL}}",
    "keycloakRealm": "${{KEYCLOAK_REALM}}",
    "keycloakClientId": "${{KEYCLOAK_CLIENT_ID}}",
    "keycloak_redirect_uri": "${{KEYCLOAK_REDIRECT_URI}}",
    "keycloak_allowed_redirect_uri": "${{KEYCLOAK_ALLOWED_REDIRECT_URI}}",
}
_LOCAL_PROFILES = {"local"}


def _apply_platform_defaults(template: dict) -> dict:
    """Add ${{VAR}} placeholders to server profiles where keys are missing."""
    for section_name, section in template.items():
        if section_name in ("configVersion", "default") or section_name in _LOCAL_PROFILES:
            continue
        if not isinstance(section, dict):
            continue
        for key, placeholder in _PLATFORM_DEFAULTS.items():
            if section.get(key) is None or section.get(key) == "":
                section[key] = placeholder
    return template


def _strip_dm_metadata_from_zip(data: bytes) -> bytes:
    """Remove dm-config.json and dm-manifest.json from a ZIP archive (OXT/XPI/CRX) before storage."""
    import zipfile
    strip_names = {"dm-config.json", "dm_config.json", "dm-manifest.json", "dm_manifest.json"}
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename.lower() in strip_names:
                continue
            dst.writestr(item, src.read(item.filename))
    return buf.getvalue()


# ─── OIDC callback / logout ──────────────────────────────────────────────

@router.get("/callback")
async def oidc_callback(request: Request, code: str = "", state: str = ""):
    """Exchange authorization code for tokens, verify group, set session cookie."""
    import urllib.parse
    import urllib.request
    import base64

    stored_state = request.cookies.get("dm_oidc_state")
    if state != stored_state:
        raise HTTPException(400, "Invalid state")

    cfg = _get_oidc_config()
    if not cfg:
        raise HTTPException(503, "OIDC provider not configured")

    # Use internal token endpoint for server-side exchange (Docker-safe)
    token_url = _get_token_endpoint()
    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
    }
    if CLIENT_SECRET:
        token_params["client_secret"] = CLIENT_SECRET
    # PKCE: include code_verifier if present
    code_verifier = request.cookies.get("dm_pkce_verifier")
    if code_verifier:
        token_params["code_verifier"] = code_verifier
    data = urllib.parse.urlencode(token_params).encode()
    req = urllib.request.Request(
        token_url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            tokens = json.loads(r.read())
    except Exception:
        raise HTTPException(502, "Token exchange failed")

    # Decode id_token (HTTPS guarantees integrity from the issuer)
    payload_b64 = tokens["id_token"].split(".")[1] + "=="
    claims = json.loads(base64.urlsafe_b64decode(payload_b64))

    if not _has_admin_group(claims):
        raise HTTPException(403, "Acces refuse : groupe admin-dm requis")

    session = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "name": claims.get("name", claims.get("preferred_username")),
        "exp": int(time.time()) + SESSION_TTL,
    }
    resp = RedirectResponse("/admin/", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE, _sign_session(session),
        httponly=True, samesite="lax", max_age=SESSION_TTL,
    )
    resp.delete_cookie("dm_oidc_state")
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/admin/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ─── Dashboard ────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@require_admin
async def dashboard(request: Request):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Metrics
            cur.execute("""
                SELECT COUNT(DISTINCT client_uuid) FROM device_connections
                WHERE created_at > NOW() - INTERVAL '7 days'
            """)
            active_devices = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*) FROM provisioning WHERE status = 'ENROLLED'
            """)
            enrolled = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM provisioning")
            total_prov = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*) FROM campaigns WHERE status = 'active'
            """)
            active_campaigns_count = cur.fetchone()[0]

            metrics = {
                "active_devices": active_devices,
                "enrollment_rate": round(enrolled / total_prov * 100, 1) if total_prov else 0,
                "error_rate": 0,
                "active_campaigns": active_campaigns_count,
            }

            # Active campaigns with stats
            active_campaigns = campaigns_svc.list_campaigns(cur, status="active")
            for c in active_campaigns:
                stats = campaigns_svc.get_campaign_stats(cur, c["id"])
                c["progress_pct"] = stats["progress_pct"]
                c["error_pct"] = stats["error_pct"]

            # Recent audit
            recent_audit = audit_svc.list_audit_entries(cur, limit=10)

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "metrics": metrics,
            "active_campaigns": active_campaigns,
            "recent_audit": recent_audit,
        })
    finally:
        conn.close()


@router.get("/api/metrics", response_class=HTMLResponse)
@require_admin
async def api_metrics(request: Request):
    """HTMX fragment for dashboard metric tiles."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT client_uuid) FROM device_connections
                WHERE created_at > NOW() - INTERVAL '7 days'
            """)
            active_devices = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM provisioning WHERE status = 'ENROLLED'")
            enrolled = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM provisioning")
            total_prov = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM campaigns WHERE status = 'active'")
            active_campaigns_count = cur.fetchone()[0]

        metrics = {
            "active_devices": active_devices,
            "enrollment_rate": round(enrolled / total_prov * 100, 1) if total_prov else 0,
            "error_rate": 0,
            "active_campaigns": active_campaigns_count,
        }

        html = f"""
        <div class="dm-grid-4">
            <div class="dm-metric-tile">
                <div style="font-size:1.5rem;font-weight:bold;">{metrics['active_devices']}</div>
                <div style="font-size:0.875rem;color:#666;">Appareils actifs (7j)</div>
            </div>
            <div class="dm-metric-tile dm-metric-tile--success">
                <div style="font-size:1.5rem;font-weight:bold;">{metrics['enrollment_rate']}%</div>
                <div style="font-size:0.875rem;color:#666;">Taux d'enrolement</div>
            </div>
            <div class="dm-metric-tile">
                <div style="font-size:1.5rem;font-weight:bold;">{metrics['error_rate']}%</div>
                <div style="font-size:0.875rem;color:#666;">Taux d'erreur</div>
            </div>
            <div class="dm-metric-tile">
                <div style="font-size:1.5rem;font-weight:bold;">{metrics['active_campaigns']}</div>
                <div style="font-size:0.875rem;color:#666;">Campagnes actives</div>
            </div>
        </div>
        """
        return HTMLResponse(html)
    finally:
        conn.close()


# ─── Devices ──────────────────────────────────────────────────────────────

@router.get("/devices", response_class=HTMLResponse)
@require_admin
async def devices_list(request: Request, owner: str = "", platform: str = "",
                       health: str = "", page: int = 0):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            summary = devices_svc.health_summary(cur)
            device_list = devices_svc.list_devices(
                cur, owner=owner or None, platform=platform or None,
                health=health or None, limit=50, offset=page * 50,
            )
        filters = {"owner": owner, "platform": platform, "health": health}
        return templates.TemplateResponse("devices.html", {
            "request": request, "devices": device_list, "summary": summary,
            "filters": filters, "page": page, "timeago": timeago,
        })
    finally:
        conn.close()


@router.get("/api/devices/health-summary", response_class=HTMLResponse)
@require_admin
async def api_health_summary(request: Request):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            s = devices_svc.health_summary(cur)
        html = f"""
        <div class="dm-counter" onclick="window.location='/admin/devices?health=ok'">
            <span class="dm-counter__value" style="color:#18753C;">{s['ok_count']}</span>
            <span class="dm-counter__label">OK</span>
        </div>
        <div class="dm-counter" onclick="window.location='/admin/devices?health=stale'">
            <span class="dm-counter__value" style="color:#B34000;">{s['stale_count']}</span>
            <span class="dm-counter__label">Inactifs</span>
        </div>
        <div class="dm-counter" onclick="window.location='/admin/devices?health=error'">
            <span class="dm-counter__value" style="color:#CE0500;">{s['error_count']}</span>
            <span class="dm-counter__label">En erreur</span>
        </div>
        <div class="dm-counter" onclick="window.location='/admin/devices?health=never'">
            <span class="dm-counter__value" style="color:#929292;">{s['never_count']}</span>
            <span class="dm-counter__label">Jamais vus</span>
        </div>
        """
        return HTMLResponse(html)
    finally:
        conn.close()


@router.get("/devices/{client_uuid}", response_class=HTMLResponse)
@require_admin
async def device_detail(request: Request, client_uuid: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            device = devices_svc.get_device_detail(cur, client_uuid)
            if not device:
                raise HTTPException(404, "Appareil non trouve")
            connections = devices_svc.get_device_connections(cur, client_uuid)
            campaign_statuses = devices_svc.get_device_campaign_statuses(cur, client_uuid)
            device_flags = devices_svc.get_device_flags(cur, client_uuid, device.get("email"))
        return templates.TemplateResponse("device_detail.html", {
            "request": request, "device": device,
            "connections": connections, "campaign_statuses": campaign_statuses,
            "device_flags": device_flags, "timeago": timeago,
        })
    finally:
        conn.close()


@router.get("/api/devices/{client_uuid}/activity", response_class=HTMLResponse)
@require_admin
async def api_device_activity(request: Request, client_uuid: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            activity = devices_svc.get_device_activity(cur, client_uuid)
        rows = ""
        for a in activity:
            ts = a["span_ts"].strftime("%d/%m %H:%M") if a.get("span_ts") else "-"
            label = span_label(a["span_name"])
            version = a.get("plugin_version") or ""
            rows += f"<tr><td>{ts}</td><td>{label}</td><td>{version}</td></tr>"
        if not rows:
            return HTMLResponse("<p style='color:#666;margin-top:1rem;'>Aucune activite recente.</p>")
        return HTMLResponse(f"""
        <table class="dm-table-compact" style="margin-top:1rem;">
            <thead><tr><th>Horodatage</th><th>Action</th><th>Version</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
        """)
    finally:
        conn.close()


# ─── Cohorts ──────────────────────────────────────────────────────────────

@router.get("/cohorts", response_class=HTMLResponse)
@require_admin
async def cohorts_list(request: Request):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cohort_list = cohorts_svc.list_cohorts(cur)
        return templates.TemplateResponse("cohorts.html", {
            "request": request, "cohorts": cohort_list,
        })
    finally:
        conn.close()


@router.post("/cohorts")
@require_admin
async def cohorts_create(request: Request, name: str = Form(...),
                         type: str = Form(...), description: str = Form(""),
                         members: str = Form("")):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cohort_id = cohorts_svc.create_cohort(
                cur, name=name, description=description, type=type,
            )
            if type == "manual" and members.strip():
                member_list = []
                for line in members.strip().splitlines():
                    val = line.strip()
                    if not val:
                        continue
                    id_type = "client_uuid" if "-" in val and len(val) > 30 else "email"
                    member_list.append((id_type, val))
                cohorts_svc.add_members(cur, cohort_id, member_list)

            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="cohort.create",
                      resource_type="cohort", resource_id=str(cohort_id),
                      payload={"name": name, "type": type},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse("/admin/cohorts", status_code=303)
    except Exception as e:
        conn.rollback()
        logger.error("cohort create failed: %s", e)
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/cohorts/{cohort_id}", response_class=HTMLResponse)
@require_admin
async def cohort_detail(request: Request, cohort_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cohort = cohorts_svc.get_cohort(cur, cohort_id)
            if not cohort:
                raise HTTPException(404, "Cohorte non trouvee")
            members = cohorts_svc.get_cohort_members(cur, cohort_id)
        return templates.TemplateResponse("cohort_edit.html", {
            "request": request, "cohort": cohort, "members": members,
        })
    finally:
        conn.close()


@router.post("/cohorts/{cohort_id}/members")
@require_admin
async def cohort_add_members(request: Request, cohort_id: int,
                             members: str = Form("")):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            member_list = []
            for line in members.strip().splitlines():
                val = line.strip()
                if not val:
                    continue
                id_type = "client_uuid" if "-" in val and len(val) > 30 else "email"
                member_list.append((id_type, val))
            count = cohorts_svc.add_members(cur, cohort_id, member_list)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="cohort.add_members",
                      resource_type="cohort", resource_id=str(cohort_id),
                      payload={"added": count},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/cohorts/{cohort_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/cohorts/{cohort_id}")
@require_admin
async def cohort_delete(request: Request, cohort_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="cohort.delete",
                      resource_type="cohort", resource_id=str(cohort_id),
                      ip=request.client.host if request.client else None)
            cohorts_svc.delete_cohort(cur, cohort_id)
            conn.commit()
        return RedirectResponse("/admin/cohorts", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/api/cohorts/estimate")
@require_admin
async def api_cohort_estimate(request: Request, type: str = "", value: str = ""):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            count = cohorts_svc.estimate_device_count(cur, type, value)
        return JSONResponse({"count": count})
    finally:
        conn.close()


# ─── Feature Flags ────────────────────────────────────────────────────────

@router.get("/flags", response_class=HTMLResponse)
@require_admin
async def flags_list(request: Request):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            flag_list = flags_svc.list_flags(cur)
        return templates.TemplateResponse("feature_flags.html", {
            "request": request, "flags": flag_list,
        })
    finally:
        conn.close()


@router.post("/flags")
@require_admin
async def flags_create(request: Request, name: str = Form(...),
                       description: str = Form(""),
                       default_value: str = Form("true")):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            flag_id = flags_svc.create_flag(
                cur, name=name, description=description,
                default_value=default_value == "true",
            )
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="flag.create",
                      resource_type="flag", resource_id=str(flag_id),
                      payload={"name": name, "default_value": default_value},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse("/admin/flags", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/flags/{flag_id}", response_class=HTMLResponse)
@require_admin
async def flag_detail(request: Request, flag_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            flag = flags_svc.get_flag(cur, flag_id)
            if not flag:
                raise HTTPException(404, "Feature flag non trouve")
            overrides = flags_svc.get_flag_overrides(cur, flag_id)
            cohort_list = cohorts_svc.list_cohorts(cur)
        return templates.TemplateResponse("flag_detail.html", {
            "request": request, "flag": flag, "overrides": overrides,
            "cohorts": cohort_list,
        })
    finally:
        conn.close()


@router.post("/flags/{flag_id}/default")
@require_admin
async def flag_update_default(request: Request, flag_id: int,
                              value: str = Form(...)):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            old_flag = flags_svc.get_flag(cur, flag_id)
            flags_svc.update_flag_default(cur, flag_id, value == "true")
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="flag.update",
                      resource_type="flag", resource_id=str(flag_id),
                      payload={"before": old_flag.get("default_value"), "after": value == "true"},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/flags/{flag_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/flags/{flag_id}/overrides")
@require_admin
async def flag_add_override(request: Request, flag_id: int,
                            cohort_id: int = Form(...),
                            value: str = Form("true"),
                            min_plugin_version: str = Form("")):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            flags_svc.create_override(
                cur, feature_id=flag_id, cohort_id=cohort_id,
                value=value == "true",
                min_plugin_version=min_plugin_version or None,
            )
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="flag.override.create",
                      resource_type="flag", resource_id=str(flag_id),
                      payload={"cohort_id": cohort_id, "value": value == "true"},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/flags/{flag_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/flags/{flag_id}/overrides/{cohort_id}")
@require_admin
async def flag_delete_override(request: Request, flag_id: int, cohort_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            flags_svc.delete_override(cur, flag_id, cohort_id)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="flag.override.delete",
                      resource_type="flag", resource_id=str(flag_id),
                      payload={"cohort_id": cohort_id},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/flags/{flag_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


# ─── Artifacts ────────────────────────────────────────────────────────────

@router.get("/artifacts", response_class=HTMLResponse)
@require_admin
async def artifacts_list(request: Request):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            artifact_list = artifacts_svc.list_artifacts(cur)
        return templates.TemplateResponse("artifacts.html", {
            "request": request, "artifacts": artifact_list,
        })
    finally:
        conn.close()


@router.post("/artifacts/upload")
@require_admin
async def artifact_upload(request: Request,
                          device_type: str = Form(...),
                          platform_variant: str = Form(""),
                          version: str = Form(...),
                          changelog_url: str = Form(""),
                          binary: UploadFile = File(...)):
    # Validate extension
    error = artifacts_svc.validate_upload(binary.filename or "", binary.size or 0)
    if error:
        return HTMLResponse(f'<div class="dm-flash dm-flash--error">{error}</div>')

    data = await binary.read()
    if len(data) > artifacts_svc.MAX_UPLOAD_SIZE:
        return HTMLResponse('<div class="dm-flash dm-flash--error">Fichier trop volumineux (>100 Mo)</div>')

    checksum = artifacts_svc.compute_checksum(data)

    # Store locally + push to DM API pods
    binaries_dir = os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")
    rel_path = f"{device_type}/{version}_{binary.filename}"
    os.makedirs(f"{binaries_dir}/{device_type}", exist_ok=True)
    local_path = f"{binaries_dir}/{rel_path}"
    with open(local_path, "wb") as f:
        f.write(data)


    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            artifact_id = artifacts_svc.create_artifact(
                cur, device_type=device_type, platform_variant=platform_variant,
                version=version, s3_path=local_path, checksum=checksum,
                changelog_url=changelog_url or None,
            )
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="artifact.upload",
                      resource_type="artifact", resource_id=str(artifact_id),
                      payload={"device_type": device_type, "version": version, "checksum": checksum},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return HTMLResponse(
            f'<div class="dm-flash dm-flash--success">Artifact {device_type}/{version} uploade (ID: {artifact_id})</div>'
        )
    except Exception as e:
        conn.rollback()
        return HTMLResponse(f'<div class="dm-flash dm-flash--error">Erreur: {e}</div>')
    finally:
        conn.close()


@router.post("/artifacts/{artifact_id}/toggle")
@require_admin
async def artifact_toggle(request: Request, artifact_id: int,
                          is_active: str = Form("true")):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            artifacts_svc.toggle_artifact(cur, artifact_id, is_active == "true")
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="artifact.toggle",
                      resource_type="artifact", resource_id=str(artifact_id),
                      payload={"is_active": is_active == "true"},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse("/admin/artifacts", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


# ─── Campaigns ────────────────────────────────────────────────────────────

@router.get("/campaigns", response_class=HTMLResponse)
@require_admin
async def campaigns_list(request: Request, status: str = ""):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            campaign_list = campaigns_svc.list_campaigns(cur, status=status or None)
        return templates.TemplateResponse("campaigns.html", {
            "request": request, "campaigns": campaign_list,
            "filters": {"status": status},
        })
    finally:
        conn.close()


@router.get("/campaigns/new", response_class=HTMLResponse)
@require_admin
async def campaign_new_form(request: Request):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            artifact_list = artifacts_svc.list_artifacts(cur)
            cohort_list = cohorts_svc.list_cohorts(cur)
        return templates.TemplateResponse("campaign_new.html", {
            "request": request, "artifacts": artifact_list, "cohorts": cohort_list,
        })
    finally:
        conn.close()


@router.post("/campaigns")
@require_admin
async def campaign_create(request: Request,
                          name: str = Form(...),
                          description: str = Form(""),
                          artifact_id: str = Form(""),
                          rollback_artifact_id: str = Form(""),
                          target_cohort_id: str = Form(""),
                          urgency: str = Form("normal"),
                          deadline_at: str = Form(""),
                          start_status: str = Form("draft")):
    conn = get_db_connection()
    try:
        actor = getattr(request.state, "admin_session", {})
        with conn.cursor() as cur:
            campaign_id = campaigns_svc.create_campaign(
                cur,
                name=name, description=description, type="plugin_update",
                artifact_id=int(artifact_id) if artifact_id else None,
                rollback_artifact_id=int(rollback_artifact_id) if rollback_artifact_id else None,
                target_cohort_id=int(target_cohort_id) if target_cohort_id else None,
                urgency=urgency,
                deadline_at=deadline_at or None,
                status=start_status,
                created_by=actor.get("email"),
            )
            audit_log(cur, actor=actor, action="campaign.create",
                      resource_type="campaign", resource_id=str(campaign_id),
                      payload={"name": name, "status": start_status},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/campaigns/{campaign_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        logger.error("campaign create failed: %s", e)
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
@require_admin
async def campaign_detail(request: Request, campaign_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            campaign = campaigns_svc.get_campaign(cur, campaign_id)
            if not campaign:
                raise HTTPException(404, "Campagne non trouvee")
            stats = campaigns_svc.get_campaign_stats(cur, campaign_id)
            events = campaigns_svc.get_campaign_events(cur, campaign_id)
        return templates.TemplateResponse("campaign_detail.html", {
            "request": request, "campaign": campaign, "stats": stats, "events": events,
        })
    finally:
        conn.close()


@router.get("/api/campaigns/{campaign_id}/stats", response_class=HTMLResponse)
@require_admin
async def api_campaign_stats(request: Request, campaign_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            s = campaigns_svc.get_campaign_stats(cur, campaign_id)
        error_class = "dm-metric-tile--error" if s["error_pct"] >= 10 else (
            "dm-metric-tile--warning" if s["error_pct"] > 2 else "")
        return HTMLResponse(f"""
        <div class="dm-grid-4" style="margin-top:1rem;">
            <div class="dm-metric-tile dm-metric-tile--success">
                <div style="font-size:1.5rem;font-weight:bold;">{s['updated']}/{s['total']}</div>
                <div style="font-size:0.875rem;">Mis a jour</div>
                <div class="dm-progress-bar" style="margin-top:0.5rem;"><div class="dm-progress-bar__fill" style="width:{s['progress_pct']}%;"></div></div>
            </div>
            <div class="dm-metric-tile {error_class}">
                <div style="font-size:1.5rem;font-weight:bold;">{s['error_pct']}%</div>
                <div style="font-size:0.875rem;">Taux erreur</div>
            </div>
            <div class="dm-metric-tile">
                <div style="font-size:1.5rem;font-weight:bold;">{s['notified']}</div>
                <div style="font-size:0.875rem;">Notifies</div>
            </div>
            <div class="dm-metric-tile">
                <div style="font-size:1.5rem;font-weight:bold;">{s['pending']}</div>
                <div style="font-size:0.875rem;">En attente</div>
            </div>
        </div>
        """)
    finally:
        conn.close()


def _campaign_action(campaign_id: int, new_status: str, action_name: str,
                     request: Request, redirect_prefix: str = "campaigns"):
    """Helper for campaign lifecycle actions."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            campaigns_svc.update_campaign_status(cur, campaign_id, new_status)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action=f"campaign.{action_name}",
                      resource_type="campaign", resource_id=str(campaign_id),
                      payload={"new_status": new_status},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/{redirect_prefix}/{campaign_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/campaigns/{campaign_id}/activate")
@require_admin
async def campaign_activate(request: Request, campaign_id: int):
    return _campaign_action(campaign_id, "active", "activate", request)


@router.post("/campaigns/{campaign_id}/pause")
@require_admin
async def campaign_pause(request: Request, campaign_id: int):
    return _campaign_action(campaign_id, "paused", "pause", request)


@router.post("/campaigns/{campaign_id}/resume")
@require_admin
async def campaign_resume(request: Request, campaign_id: int):
    return _campaign_action(campaign_id, "active", "resume", request)


@router.post("/campaigns/{campaign_id}/complete")
@require_admin
async def campaign_complete(request: Request, campaign_id: int):
    return _campaign_action(campaign_id, "completed", "complete", request)


@router.post("/campaigns/{campaign_id}/rollback")
@require_admin
async def campaign_rollback(request: Request, campaign_id: int):
    return _campaign_action(campaign_id, "rolled_back", "rollback", request)


# ─── Deploy Wizard (Deploiement 1-2-3) ────────────────────────────────

DEVICE_TYPES = [
    {"id": "libreoffice", "label": "LibreOffice", "ext": ".oxt"},
    {"id": "matisse", "label": "Thunderbird (Matisse)", "ext": ".xpi"},
]


@router.get("/deploy", response_class=HTMLResponse)
@require_admin
async def deploy_wizard(request: Request):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cohort_list = cohorts_svc.list_cohorts(cur)
            artifact_list = artifacts_svc.list_artifacts(cur)
        return templates.TemplateResponse("deploy_wizard.html", {
            "request": request,
            "device_types": DEVICE_TYPES,
            "cohorts": cohort_list,
            "artifacts": artifact_list,
            "mode": "wizard",
        })
    finally:
        conn.close()


@router.post("/api/deploy/extract-version")
@require_admin
async def api_extract_version(request: Request, binary: UploadFile = File(...)):
    """Extract version from plugin package (ZIP: .xpi, .oxt, .crx)."""
    import zipfile
    import io
    import re

    data = await binary.read()
    version = None
    filename = binary.filename or ""

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()

            # .xpi / .crx — manifest.json
            if "manifest.json" in names:
                manifest = json.loads(zf.read("manifest.json"))
                version = manifest.get("version")

            # .oxt — description.xml
            if not version and "description.xml" in names:
                desc = zf.read("description.xml").decode("utf-8", errors="replace")
                m = re.search(r'value="(\d+\.\d+(?:\.\d+)*)"', desc)
                if m:
                    version = m.group(1)

            # .oxt — META-INF/manifest.xml fallback (rare)
            if not version and "META-INF/manifest.xml" in names:
                meta = zf.read("META-INF/manifest.xml").decode("utf-8", errors="replace")
                m = re.search(r'version="(\d+\.\d+(?:\.\d+)*)"', meta)
                if m:
                    version = m.group(1)
    except zipfile.BadZipFile:
        pass

    # Fallback: extract from filename
    if not version:
        m = re.search(r'(\d+\.\d+(?:\.\d+)*)', filename)
        if m:
            version = m.group(1)

    # Validate format
    errors = []
    warnings = []
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed_ext = {"oxt", "xpi", "crx"}
    file_size = len(data)
    is_valid_zip = False

    if ext not in allowed_ext:
        errors.append(f"Extension .{ext} non supportee (attendu: .oxt, .xpi, .crx)")
    if file_size == 0:
        errors.append("Fichier vide")
    elif file_size > 100 * 1024 * 1024:
        errors.append(f"Fichier trop volumineux ({file_size // (1024*1024)} Mo > 100 Mo)")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf_check:
            is_valid_zip = True
            names = zf_check.namelist()
            if ext == "xpi" and "manifest.json" not in names:
                warnings.append("manifest.json absent du XPI")
            if ext == "oxt" and "META-INF/manifest.xml" not in names and "description.xml" not in names:
                warnings.append("description.xml et META-INF/manifest.xml absents du OXT")
    except zipfile.BadZipFile:
        errors.append("Le fichier n'est pas une archive ZIP valide")

    if not version and not errors:
        warnings.append("Version non detectee dans le package — saisie manuelle requise")

    # Detect device type + extract dm-manifest.json release notes
    device_type = None
    release_notes = ""

    if ext == "oxt":
        device_type = "libreoffice"
    elif ext in ("xpi", "crx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf2:
                if "manifest.json" in zf2.namelist():
                    mf = json.loads(zf2.read("manifest.json"))
                    bss = mf.get("browser_specific_settings", mf.get("applications", {}))
                    if bss.get("thunderbird") or "messenger" in json.dumps(mf.get("permissions", [])).lower():
                        device_type = "matisse"
                    elif bss.get("gecko") or ext == "xpi":
                        device_type = "firefox"
                    elif mf.get("manifest_version") == 3:
                        device_type = "chrome"
                    else:
                        device_type = "chrome" if ext == "crx" else "firefox"
                else:
                    device_type = "firefox" if ext == "xpi" else "chrome"
        except zipfile.BadZipFile:
            device_type = "firefox" if ext == "xpi" else "chrome"

    # Extract release notes from dm-manifest.json for the detected version
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf3:
            for zname in zf3.namelist():
                if zname.rsplit("/", 1)[-1].lower() in ("dm-manifest.json", "dm_manifest.json"):
                    dm_m = json.loads(zf3.read(zname).decode("utf-8", errors="replace"))
                    for entry in dm_m.get("changelog", []):
                        if entry.get("version") == version:
                            release_notes = "\n".join(f"- {c}" for c in entry.get("changes", []))
                            break
                    break
    except Exception:
        pass

    return JSONResponse({
        "version": version or "",
        "source": "package" if version else "filename",
        "device_type": device_type,
        "valid": len(errors) == 0,
        "is_valid_zip": is_valid_zip,
        "file_size": file_size,
        "extension": ext,
        "errors": errors,
        "warnings": warnings,
        "release_notes": release_notes,
    })


@router.post("/deploy/create")
@require_admin
async def deploy_create(request: Request,
                        device_type: str = Form(...),
                        version: str = Form(...),
                        target_mode: str = Form("all"),
                        cohort_id: str = Form(""),
                        percent: str = Form("10"),
                        emails: str = Form(""),
                        deploy_type: str = Form("progressive"),
                        stage_hours: str = Form("24"),
                        name: str = Form(""),
                        rollback_artifact_id: str = Form(""),
                        binary: UploadFile = File(...)):
    # 1. Upload artifact
    error = artifacts_svc.validate_upload(binary.filename or "", binary.size or 0)
    if error:
        raise HTTPException(400, error)

    data = await binary.read()
    if len(data) > artifacts_svc.MAX_UPLOAD_SIZE:
        raise HTTPException(400, "Fichier trop volumineux (>100 Mo)")

    # Extract dm-config.json and dm-manifest.json from the package before stripping
    deploy_config_template = None
    deploy_dm_manifest = None
    try:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                basename = name.rsplit("/", 1)[-1].lower()
                if basename in ("dm-config.json", "dm_config.json"):
                    raw = zf.read(name).decode("utf-8", errors="replace")
                    deploy_config_template = json.loads(raw)
                    deploy_config_template = _apply_platform_defaults(deploy_config_template)
                elif basename in ("dm-manifest.json", "dm_manifest.json"):
                    raw = zf.read(name).decode("utf-8", errors="replace")
                    deploy_dm_manifest = json.loads(raw)
    except Exception:
        pass

    # Strip dm-config.json from the binary before storage (users shouldn't see placeholders)
    data = _strip_dm_metadata_from_zip(data)

    checksum = artifacts_svc.compute_checksum(data)
    binaries_dir = os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")
    rel_path = f"{device_type}/{version}_{binary.filename}"
    os.makedirs(f"{binaries_dir}/{device_type}", exist_ok=True)
    local_path = f"{binaries_dir}/{rel_path}"
    with open(local_path, "wb") as f:
        f.write(data)


    conn = get_db_connection()
    try:
        actor = getattr(request.state, "admin_session", {})
        with conn.cursor() as cur:
            artifact_id = artifacts_svc.create_artifact(
                cur, device_type=device_type, platform_variant="",
                version=version, s3_path=local_path, checksum=checksum,
            )

            # 2. Cohort
            target_cohort_id = None
            if target_mode == "existing" and cohort_id:
                target_cohort_id = int(cohort_id)
            elif target_mode == "percent":
                pct = max(1, min(100, int(percent)))
                cid = cohorts_svc.create_cohort(
                    cur, name=f"auto-{device_type}-{version}-{pct}pct",
                    description=f"Auto-created: {pct}% rollout", type="percentage",
                )
                target_cohort_id = cid
            elif target_mode == "emails":
                email_list = [e.strip() for e in emails.strip().splitlines() if e.strip()]
                if email_list:
                    cid = cohorts_svc.create_cohort(
                        cur, name=f"auto-{device_type}-{version}-manual",
                        description=f"Auto-created: {len(email_list)} emails",
                        type="manual",
                    )
                    cohorts_svc.add_members(cur, cid, [("email", e) for e in email_list])
                    target_cohort_id = cid

            # 3. Campaign
            rollout_config = None
            urgency = "normal"
            if deploy_type == "progressive":
                hours = max(1, int(stage_hours))
                rollout_config = {
                    "stages": [
                        {"percent": 5, "duration_hours": hours, "label": "Canary (5%)"},
                        {"percent": 25, "duration_hours": hours, "label": "Early adopters (25%)"},
                        {"percent": 50, "duration_hours": hours, "label": "Moitie (50%)"},
                        {"percent": 100, "duration_hours": 0, "label": "Deploiement complet"},
                    ]
                }
            else:
                urgency = "normal"

            campaign_name = name.strip() or f"MaJ {device_type} {version}"
            # Resolve plugin_id from device_type for auto-completion of older campaigns
            _deploy_plugin_id = None
            cur.execute("SELECT id FROM plugins WHERE device_type = %s AND status = 'active' LIMIT 1", (device_type,))
            _prow = cur.fetchone()
            if _prow:
                _deploy_plugin_id = _prow[0]
            campaign_id = campaigns_svc.create_campaign(
                cur, name=campaign_name, type="plugin_update",
                artifact_id=artifact_id,
                rollback_artifact_id=int(rollback_artifact_id) if rollback_artifact_id else None,
                target_cohort_id=target_cohort_id,
                urgency=urgency, status="active",
                rollout_config=rollout_config,
                created_by=actor.get("email"),
                plugin_id=_deploy_plugin_id,
            )

            # Store dm-config.json template in the plugin record if extracted
            if deploy_config_template:
                try:
                    cur.execute(
                        "UPDATE plugins SET config_template = %s WHERE device_type = %s",
                        (json.dumps(deploy_config_template), device_type),
                    )
                except Exception as ct_err:
                    logger.warning("deploy: config_template store failed: %s", ct_err)

            # Store changelog from dm-manifest.json if present
            if deploy_dm_manifest:
                manifest_changelog = deploy_dm_manifest.get("changelog", [])
                if isinstance(manifest_changelog, list) and manifest_changelog:
                    try:
                        cur.execute(
                            "UPDATE plugins SET changelog = %s WHERE device_type = %s",
                            (json.dumps(manifest_changelog), device_type),
                        )
                    except Exception as cl_err:
                        logger.warning("deploy: changelog update failed: %s", cl_err)

            audit_log(cur, actor=actor, action="deploy.create",
                      resource_type="campaign", resource_id=str(campaign_id),
                      payload={"name": campaign_name, "device_type": device_type,
                               "version": version, "deploy_type": deploy_type,
                               "has_config_template": bool(deploy_config_template)},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/deploy/{campaign_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        logger.error("deploy create failed: %s", e)
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/deploy/{campaign_id}", response_class=HTMLResponse)
@require_admin
async def deploy_tracking(request: Request, campaign_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            campaign = campaigns_svc.get_campaign(cur, campaign_id)
            if not campaign:
                raise HTTPException(404, "Deploiement non trouve")
            stats = campaigns_svc.get_campaign_stats(cur, campaign_id)
            events = campaigns_svc.get_campaign_events(cur, campaign_id, limit=10)
            # Resolve plugin info for icon display
            plugin = None
            pid = campaign.get("plugin_id")
            if pid:
                plugin = catalog_svc.get_plugin(cur, pid)
            if not plugin:
                # Fallback: find plugin by device_type
                dt = campaign.get("device_type", "")
                if dt:
                    cur.execute("SELECT * FROM plugins WHERE device_type = %s AND status = 'active' LIMIT 1", (dt,))
                    row = cur.fetchone()
                    if row:
                        cols = [d[0] for d in cur.description]
                        plugin = dict(zip(cols, row))
        return templates.TemplateResponse("deploy_wizard.html", {
            "request": request,
            "mode": "tracking",
            "campaign": campaign,
            "stats": stats,
            "events": events,
            "plugin": plugin,
        })
    finally:
        conn.close()


@router.get("/api/deploy/{campaign_id}/progress")
@require_admin
async def api_deploy_progress(request: Request, campaign_id: int):
    """JSON endpoint for real-time progress chart."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            campaign = campaigns_svc.get_campaign(cur, campaign_id)
            if not campaign:
                return JSONResponse({"error": "not found"}, status_code=404)
            stats = campaigns_svc.get_campaign_stats(cur, campaign_id)
            events = campaigns_svc.get_campaign_events(cur, campaign_id, limit=10)

        rollout_config = campaign.get("rollout_config")
        if isinstance(rollout_config, str):
            rollout_config = json.loads(rollout_config)
        stages = (rollout_config or {}).get("stages", [])

        created_at = campaign.get("created_at")
        created_iso = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)

        event_list = []
        for e in events:
            ts = e.get("updated_at")
            event_list.append({
                "email": e.get("email", ""),
                "status": e.get("status", ""),
                "version_before": e.get("version_before", ""),
                "version_after": e.get("version_after", ""),
                "updated_at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            })

        return JSONResponse({
            "campaign_id": campaign_id,
            "status": campaign.get("status"),
            "created_at": created_iso,
            "stages": stages,
            "stats": stats,
            "events": event_list,
        })
    finally:
        conn.close()


@router.post("/deploy/{campaign_id}/pause")
@require_admin
async def deploy_pause(request: Request, campaign_id: int):
    return _campaign_action(campaign_id, "paused", "pause", request, redirect_prefix="deploy")


@router.post("/deploy/{campaign_id}/resume")
@require_admin
async def deploy_resume(request: Request, campaign_id: int):
    return _campaign_action(campaign_id, "active", "resume", request, redirect_prefix="deploy")


@router.post("/deploy/{campaign_id}/abort")
@require_admin
async def deploy_abort(request: Request, campaign_id: int):
    return _campaign_action(campaign_id, "rolled_back", "rollback", request, redirect_prefix="deploy")


@router.post("/deploy/{campaign_id}/complete")
@require_admin
async def deploy_complete(request: Request, campaign_id: int):
    return _campaign_action(campaign_id, "completed", "complete", request, redirect_prefix="deploy")


# ─── Catalog ─────────────────────────────────────────────────────────────

PLUGIN_CATEGORIES = ["productivity", "security", "communication", "tools", "other"]


@router.get("/api/catalog/check-slug")
@require_admin
async def api_check_slug(request: Request, slug: str = ""):
    """Check if a slug is available and suggest alternatives if not."""
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')
    if not slug:
        return JSONResponse({"available": False, "slug": "", "alternatives": []})
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM plugins WHERE slug = %s", (slug,))
            taken = cur.fetchone()[0] > 0
            alternatives = []
            if taken:
                for suffix in ["-2", "-v2", "-new", "-ext", "-pro"]:
                    candidate = slug + suffix
                    cur.execute("SELECT COUNT(*) FROM plugins WHERE slug = %s", (candidate,))
                    if cur.fetchone()[0] == 0:
                        alternatives.append(candidate)
                        if len(alternatives) >= 3:
                            break
        return JSONResponse({"available": not taken, "slug": slug, "alternatives": alternatives})
    finally:
        conn.close()

LLM_SUGGEST_PROMPT = """Tu es un assistant qui analyse un README ou le contenu d'un plugin pour pre-remplir une fiche de catalogue.

A partir du texte fourni, extrais les informations suivantes au format JSON strict (pas de markdown, pas de commentaires) :
{
  "name": "Nom du plugin (court, lisible)",
  "slug": "nom-en-kebab-case",
  "intent": "Proposition de valeur en 1-2 phrases",
  "description": "Description detaillee (2-5 phrases)",
  "key_features": ["Tag court 1", "Tag court 2", ...],  // tags metier courts (2-3 mots max) ex: "Redaction IA", "Mode hors-ligne", "SSO Keycloak"
  "category": "productivity|security|communication|tools|other",
  "device_type": "libreoffice|firefox|chrome|edge|matisse",
  "changelog": "Changelog extrait si present (format markdown), sinon vide"
}

Si tu ne trouves pas une info, mets une chaine vide ou une liste vide. Reponds uniquement avec le JSON."""


@router.post("/api/catalog/suggest")
@require_admin
async def api_catalog_suggest(request: Request):
    """Use LLM to suggest catalog fields from README and/or plugin content."""
    import urllib.request as urlreq
    import zipfile
    import io

    form = await request.form()
    texts = []

    # 1. README from file upload
    readme_file = form.get("readme_file")
    if readme_file and getattr(readme_file, "filename", None):
        data = await readme_file.read()
        texts.append(f"=== README ({readme_file.filename}) ===\n" + data.decode("utf-8", errors="replace")[:15000])

    # 2. README from URL
    readme_url = str(form.get("readme_url", "")).strip()
    if readme_url:
        try:
            with urlreq.urlopen(readme_url, timeout=10) as r:
                content = r.read().decode("utf-8", errors="replace")[:15000]
            texts.append(f"=== README (URL) ===\n" + content)
        except Exception as e:
            texts.append(f"=== README URL error: {e} ===")

    # 3. Plugin file — extract manifest/description from ZIP
    has_readme = False
    has_manifest = False
    dm_manifest = None
    oxt_version = ""
    oxt_identifier = ""
    icon_data = None
    icon_filename = None
    plugin_file = form.get("plugin_file")
    if plugin_file and getattr(plugin_file, "filename", None):
        pdata = await plugin_file.read()
        extracted = []
        interesting_files = {
            "manifest.json", "description.xml", "package.json",
            "readme.md", "readme.txt", "readme", "readme.rst",
            "notice-utilisateur.md", "notice-utilisateur.txt",
            "notice_utilisateur.md", "notice_utilisateur.txt",
            "changelog.md", "changelog.txt", "changes.md", "history.md",
            "dm-config.json", "dm_config.json",
            "dm-manifest.json", "dm_manifest.json",
        }
        icon_basenames = {"logo.png", "icon128.png", "icon48.png"}
        config_template = None
        has_config_template = False
        try:
            with zipfile.ZipFile(io.BytesIO(pdata)) as zf:
                for name in zf.namelist():
                    basename = name.rsplit("/", 1)[-1].lower()
                    # Extract icon from assets/ directory
                    if basename in icon_basenames and "assets/" in name.lower():
                        icon_data = zf.read(name)
                        icon_filename = basename
                        continue
                    if basename in interesting_files:
                        raw = zf.read(name).decode("utf-8", errors="replace")
                        if basename in ("dm-config.json", "dm_config.json"):
                            try:
                                config_template = json.loads(raw)
                                has_config_template = True
                            except json.JSONDecodeError:
                                pass
                        elif basename in ("dm-manifest.json", "dm_manifest.json"):
                            try:
                                dm_manifest = json.loads(raw)
                                has_manifest = True
                            except json.JSONDecodeError:
                                pass
                        elif basename == "description.xml":
                            try:
                                import xml.etree.ElementTree as ET
                                root = ET.fromstring(raw)
                                ns = {"d": "http://openoffice.org/extensions/description/2006"}
                                ver_el = root.find(".//d:version", ns)
                                if ver_el is not None:
                                    oxt_version = ver_el.get("value", "")
                                ident_el = root.find(".//d:identifier", ns)
                                if ident_el is not None:
                                    oxt_identifier = ident_el.get("value", "")
                            except Exception:
                                pass
                            extracted.append(f"--- {name} ---\n{raw[:8000]}")
                        else:
                            extracted.append(f"--- {name} ---\n{raw[:8000]}")
                            if basename.startswith(("readme", "notice")):
                                has_readme = True
        except zipfile.BadZipFile:
            pass
        if extracted:
            texts.append(f"=== Plugin ({plugin_file.filename}) ===\n" + "\n\n".join(extracted))

    # If dm-manifest.json found, use it directly as suggestion (skip LLM)
    if has_manifest and dm_manifest:
        suggestion = {
            "slug": dm_manifest.get("slug", ""),
            "name": dm_manifest.get("name", ""),
            "description": dm_manifest.get("description", ""),
            "intent": dm_manifest.get("intent", ""),
            "device_type": dm_manifest.get("device_type", "libreoffice"),
            "category": dm_manifest.get("category", "productivity"),
            "publisher": dm_manifest.get("publisher", ""),
            "visibility": dm_manifest.get("visibility", "public"),
            "homepage_url": dm_manifest.get("homepage_url", ""),
            "support_email": dm_manifest.get("support_email", ""),
            "icon_url": dm_manifest.get("icon_url", ""),
            "doc_url": dm_manifest.get("doc_url", ""),
            "license": dm_manifest.get("license", ""),
            "key_features": dm_manifest.get("key_features", []),
            "changelog": dm_manifest.get("changelog", []),
            "_has_readme": has_readme,
            "_has_manifest": True,
            "_has_config_template": has_config_template,
            "_source": "dm-manifest.json",
        }
        if oxt_version:
            suggestion["oxt_version"] = oxt_version
        if oxt_identifier:
            suggestion["oxt_identifier"] = oxt_identifier
        if config_template:
            suggestion["config_template"] = config_template
        # Store icon as base64 data URL (no shared filesystem needed)
        if icon_data and icon_filename:
            import base64 as _b64
            ext = icon_filename.rsplit(".", 1)[-1].lower()
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "svg": "image/svg+xml", "webp": "image/webp"}.get(ext, "image/png")
            data_url = f"data:{mime};base64,{_b64.b64encode(icon_data).decode()}"
            suggestion["icon_url"] = data_url
            suggestion["icon_data_url"] = data_url
        return JSONResponse(suggestion)

    if not texts:
        return JSONResponse({"error": "Aucune source fournie"}, status_code=400)

    combined = "\n\n".join(texts)[:20000]

    # Call LLM (OpenAI-compatible)
    llm_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    llm_token = os.getenv("LLM_API_TOKEN", "")
    llm_model = os.getenv("DEFAULT_MODEL_NAME", "gpt-oss-120b")

    if not llm_url or not llm_token:
        return JSONResponse({"error": "LLM non configure (LLM_BASE_URL / LLM_API_TOKEN)"}, status_code=503)

    payload = json.dumps({
        "model": llm_model,
        "messages": [
            {"role": "system", "content": LLM_SUGGEST_PROMPT},
            {"role": "user", "content": combined},
        ],
        "temperature": 0.2,
        "max_tokens": 2000,
    }).encode()

    req = urlreq.Request(
        f"{llm_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm_token}",
        },
    )
    try:
        with urlreq.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
        content = result["choices"][0]["message"]["content"]
        # Extract JSON from response (handle markdown code blocks)
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        suggestion = json.loads(content.strip())
        suggestion["_has_readme"] = has_readme
        suggestion["_has_config_template"] = has_config_template
        if oxt_version:
            suggestion["oxt_version"] = oxt_version
        if oxt_identifier:
            suggestion["oxt_identifier"] = oxt_identifier
        if config_template:
            suggestion["config_template"] = config_template
        # Store icon as base64 data URL (even for LLM path)
        if icon_data and icon_filename:
            import base64 as _b64
            ext = icon_filename.rsplit(".", 1)[-1].lower()
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "svg": "image/svg+xml", "webp": "image/webp"}.get(ext, "image/png")
            data_url = f"data:{mime};base64,{_b64.b64encode(icon_data).decode()}"
            suggestion["icon_url"] = data_url
            suggestion["icon_data_url"] = data_url
        return JSONResponse(suggestion)
    except Exception as e:
        logger.error("LLM suggest failed: %s", e)
        return JSONResponse({"error": f"Erreur LLM: {e}"}, status_code=502)


@router.post("/catalog/purge-removed")
@require_admin
async def catalog_purge_removed(request: Request):
    """Permanently delete all plugins in 'removed' status."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            count = catalog_svc.purge_removed(cur)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="plugin.purge_removed",
                      resource_type="plugin", resource_id="*",
                      payload={"deleted_count": count},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog?purged={count}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/catalog", response_class=HTMLResponse)
@require_admin
async def catalog_list(request: Request, status: str = "", device_type: str = "",
                       category: str = ""):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            plugins = catalog_svc.list_plugins(
                cur, status=status or None, device_type=device_type or None,
                category=category or None,
            )
        return templates.TemplateResponse("catalog.html", {
            "request": request, "plugins": plugins,
            "categories": PLUGIN_CATEGORIES,
            "filters": {"status": status, "device_type": device_type, "category": category},
        })
    finally:
        conn.close()


@router.get("/catalog/new", response_class=HTMLResponse)
@require_admin
async def catalog_new(request: Request):
    # Provide real env var values for config preview substitutions
    secret_vars = {"LLM_API_TOKEN", "TELEMETRY_SALT", "TELEMETRY_KEY"}
    sub_vars = [
        "LLM_BASE_URL", "DEFAULT_MODEL_NAME", "LLM_API_TOKEN",
        "KEYCLOAK_ISSUER_URL", "KEYCLOAK_REALM", "KEYCLOAK_CLIENT_ID",
        "KEYCLOAK_REDIRECT_URI", "KEYCLOAK_ALLOWED_REDIRECT_URI",
        "PUBLIC_BASE_URL", "TELEMETRY_SALT",
    ]
    substitution_values = {}
    for var in sub_vars:
        val = os.getenv(var, "")
        if var in secret_vars and val:
            substitution_values[var] = val[:4] + "***"
        else:
            substitution_values[var] = val
    return templates.TemplateResponse("catalog_plugin_new.html", {
        "request": request,
        "device_types": DEVICE_TYPES,
        "categories": PLUGIN_CATEGORIES,
        "substitution_values": substitution_values,
    })


@router.post("/catalog")
@require_admin
async def catalog_create(request: Request,
                         slug: str = Form(...), name: str = Form(...),
                         description: str = Form(""), intent: str = Form(""),
                         key_features: str = Form(""),
                         changelog: str = Form(""),
                         device_type: str = Form("libreoffice"),
                         category: str = Form("productivity"),
                         icon_url: str = Form(""),
                         homepage_url: str = Form(""),
                         doc_url: str = Form(""),
                         support_email: str = Form(""),
                         publisher: str = Form("DNUM"),
                         visibility: str = Form("public"),
                         license: str = Form(""),
                         config_template: str = Form(""),
                         alias: str = Form(""),
                         initial_version: str = Form(""),
                         initial_release_notes: str = Form(""),
                         binary: UploadFile | None = File(None)):
    features = [f.strip() for f in key_features.split(",") if f.strip()] if key_features else []
    # Parse changelog: accept JSON array or markdown text
    parsed_changelog = None
    if changelog and changelog.strip():
        try:
            parsed_changelog = json.loads(changelog)
        except (json.JSONDecodeError, ValueError):
            # Store markdown as a single-entry changelog
            parsed_changelog = [{"version": "0.0.0", "changes": [changelog.strip()]}]
    # Parse config_template JSON if provided (from dm-config.json extraction)
    parsed_template = None
    if config_template and config_template.strip():
        try:
            parsed_template = json.loads(config_template)
            parsed_template = _apply_platform_defaults(parsed_template)
        except json.JSONDecodeError:
            logger.warning("Invalid config_template JSON in create form, ignoring")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            plugin_id = catalog_svc.create_plugin(
                cur, slug=slug, name=name, description=description,
                intent=intent, key_features=features, changelog=json.dumps(parsed_changelog or []),
                device_type=device_type, category=category, icon_url=icon_url,
                homepage_url=homepage_url, doc_url=doc_url, support_email=support_email,
                license=license, publisher=publisher, visibility=visibility,
                config_template=parsed_template,
            )
            actor = getattr(request.state, "admin_session", {})
            # Create alias if provided
            if alias and alias.strip():
                try:
                    cur.execute(
                        "INSERT INTO plugin_aliases (alias, plugin_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (alias.strip().lower(), plugin_id),
                    )
                except Exception:
                    logger.warning("alias creation failed for %s", alias)
            # Auto-create initial version + artifact if binary provided
            version_created = ""
            if initial_version and binary and binary.filename:
                try:
                    bin_data = await binary.read()
                    if bin_data:
                        bin_data = _strip_dm_metadata_from_zip(bin_data)
                        checksum = artifacts_svc.compute_checksum(bin_data)
                        binaries_dir = os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")
                        rel_path = f"{device_type}/{initial_version}_{binary.filename}"
                        os.makedirs(f"{binaries_dir}/{device_type}", exist_ok=True)
                        local_path = f"{binaries_dir}/{rel_path}"
                        with open(local_path, "wb") as f:
                            f.write(bin_data)
                        # Binary is pulled on-demand by API pods via _pull_binary_from_admin()
                        artifact_id = artifacts_svc.create_artifact(
                            cur, device_type=device_type, platform_variant="",
                            version=initial_version, s3_path=local_path, checksum=checksum,
                        )
                        catalog_svc.create_version(
                            cur, plugin_id=plugin_id, version=initial_version,
                            artifact_id=artifact_id,
                            release_notes=initial_release_notes,
                            download_url="", distribution_mode="managed",
                            min_host_version="", max_host_version="",
                            status="published",
                        )
                        version_created = initial_version
                except Exception as ver_err:
                    logger.warning("auto-version creation failed: %s", ver_err)

            audit_log(cur, actor=actor, action="plugin.create",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"slug": slug, "name": name, "alias": alias,
                               "initial_version": version_created},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?created=1", status_code=303)
    except Exception as e:
        conn.rollback()
        logger.error("plugin create failed: %s", e)
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/catalog/{plugin_id}", response_class=HTMLResponse)
@require_admin
async def catalog_plugin_detail(request: Request, plugin_id: int, tab: str = "versions"):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            plugin = catalog_svc.get_plugin(cur, plugin_id)
            if not plugin:
                raise HTTPException(404, "Plugin non trouve")
            versions = catalog_svc.list_versions(cur, plugin_id)
            stats = catalog_svc.get_plugin_stats(cur, plugin_id)
            installations = catalog_svc.list_installations(cur, plugin_id, limit=20)
            artifact_list = artifacts_svc.list_artifacts(cur)
            # Env overrides
            cur.execute("""
                SELECT * FROM plugin_env_overrides WHERE plugin_id = %s
                ORDER BY environment, key
            """, (plugin_id,))
            env_cols = [d[0] for d in cur.description]
            env_overrides = [dict(zip(env_cols, r)) for r in cur.fetchall()]
            # Keycloak clients
            kc_clients = keycloak_svc.get_plugin_clients(cur, plugin_id)
            all_kc_clients = keycloak_svc.list_clients(cur)
            kc_defaults = keycloak_svc.get_defaults()
            # Waitlist
            cur.execute("""
                SELECT * FROM plugin_waitlist WHERE plugin_id = %s
                ORDER BY created_at DESC LIMIT 50
            """, (plugin_id,))
            wl_cols = [d[0] for d in cur.description]
            waitlist = [dict(zip(wl_cols, r)) for r in cur.fetchall()]
            # Aliases
            cur.execute("SELECT alias FROM plugin_aliases WHERE plugin_id = %s ORDER BY alias", (plugin_id,))
            aliases = [r[0] for r in cur.fetchall()]
        features = plugin.get("key_features") or []
        if isinstance(features, str):
            features = json.loads(features)
        # Prepare config_template for the Configuration tab
        ct = plugin.get("config_template")
        if ct and isinstance(ct, str):
            try:
                ct = json.loads(ct)
            except (json.JSONDecodeError, TypeError):
                ct = None
        config_template_json = json.dumps(ct, indent=2, ensure_ascii=False) if ct else ""
        # Format changelog: if it's a JSON array, render as readable text
        raw_changelog = plugin.get("changelog")
        if raw_changelog:
            if isinstance(raw_changelog, str):
                try:
                    raw_changelog = json.loads(raw_changelog)
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(raw_changelog, list):
                lines = []
                for entry in raw_changelog:
                    if isinstance(entry, dict):
                        header = f"v{entry.get('version', '?')}"
                        if entry.get("date"):
                            header += f"  ({entry['date']})"
                        lines.append(header)
                        for c in entry.get("changes", []):
                            lines.append(f"  - {c}")
                        lines.append("")
                plugin["changelog"] = "\n".join(lines).strip()
        # Deploy environments
        deploy_environments = _get_deploy_environments()
        # Cohorts for targeting
        with conn.cursor() as cur2:
            cohort_list = cohorts_svc.list_cohorts(cur2)
            # Deployments for this plugin
            cur2.execute("""
                SELECT c.*, pv.version
                FROM campaigns c
                LEFT JOIN plugin_versions pv ON pv.id = c.version_id
                WHERE c.plugin_id = %s
                ORDER BY c.created_at DESC LIMIT 50
            """, (plugin_id,))
            dep_cols = [d[0] for d in cur2.description]
            deployments = [dict(zip(dep_cols, r)) for r in cur2.fetchall()]
            # Add progress_pct
            for d in deployments:
                try:
                    s = campaigns_svc.get_campaign_stats(cur2, d["id"])
                    d["progress_pct"] = s.get("progress_pct", 0)
                except Exception:
                    d["progress_pct"] = 0
        return templates.TemplateResponse("catalog_plugin.html", {
            "request": request, "plugin": plugin, "versions": versions,
            "stats": stats, "installations": installations,
            "artifacts": artifact_list, "features": features,
            "env_overrides": env_overrides, "kc_clients": kc_clients,
            "all_kc_clients": all_kc_clients, "kc_defaults": kc_defaults,
            "waitlist": waitlist, "aliases": aliases,
            "config_template_json": config_template_json,
            "config_from_package": bool(ct),
            "deploy_environments": deploy_environments,
            "cohorts": cohort_list,
            "deployments": deployments,
            "tab": tab, "timeago": timeago,
        })
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/edit")
@require_admin
async def catalog_plugin_edit(request: Request, plugin_id: int,
                              name: str = Form(...), description: str = Form(""),
                              intent: str = Form(""), key_features: str = Form(""),
                              changelog: str = Form(""),
                              category: str = Form("productivity"),
                              homepage_url: str = Form(""),
                              support_email: str = Form(""),
                              publisher: str = Form("DNUM"),
                              visibility: str = Form("public")):
    features = [f.strip() for f in key_features.split(",") if f.strip()] if key_features else []
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            catalog_svc.update_plugin(cur, plugin_id,
                                      name=name, description=description, intent=intent,
                                      key_features=features, changelog=changelog,
                                      category=category, homepage_url=homepage_url,
                                      support_email=support_email, publisher=publisher,
                                      visibility=visibility)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="plugin.update",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"name": name},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/status")
@require_admin
async def catalog_plugin_status(request: Request, plugin_id: int,
                                status: str = Form(...)):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            catalog_svc.update_plugin(cur, plugin_id, status=status)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action=f"plugin.{status}",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"status": status},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/duplicate")
@require_admin
async def catalog_plugin_duplicate(request: Request, plugin_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            src = catalog_svc.get_plugin(cur, plugin_id)
            if not src:
                raise HTTPException(404, "Plugin introuvable")
            # Find unique slug
            base_slug = src["slug"] + "-copy"
            slug = base_slug
            n = 1
            while catalog_svc.get_plugin_by_slug(cur, slug):
                n += 1
                slug = f"{base_slug}-{n}"
            config_tpl = src.get("config_template")
            if isinstance(config_tpl, str):
                import json as _json
                config_tpl = _json.loads(config_tpl)
            features = src.get("key_features") or []
            if isinstance(features, str):
                import json as _json
                features = _json.loads(features)
            new_id = catalog_svc.create_plugin(
                cur, slug=slug, name=f"{src['name']} (copie)",
                description=src.get("description", ""),
                intent=src.get("intent", ""),
                key_features=features,
                changelog=src.get("changelog", ""),
                device_type=src.get("device_type", "libreoffice"),
                category=src.get("category", "productivity"),
                homepage_url=src.get("homepage_url", ""),
                support_email=src.get("support_email", ""),
                publisher=src.get("publisher", "DNUM"),
                visibility=src.get("visibility", "public"),
                config_template=config_tpl,
            )
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="plugin.duplicate",
                      resource_type="plugin", resource_id=str(new_id),
                      payload={"source_plugin_id": plugin_id, "slug": slug},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{new_id}?tab=edit", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/versions")
@require_admin
async def catalog_version_create(request: Request, plugin_id: int,
                                 version: str = Form(...),
                                 release_notes: str = Form(""),
                                 artifact_id: str = Form(""),
                                 download_url: str = Form(""),
                                 distribution_mode: str = Form("managed"),
                                 min_host_version: str = Form(""),
                                 max_host_version: str = Form(""),
                                 status: str = Form("draft")):
    if not artifact_id and not download_url.strip():
        raise HTTPException(400, "Un artifact ou une URL de téléchargement est requis.")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            vid = catalog_svc.create_version(
                cur, plugin_id=plugin_id, version=version,
                artifact_id=int(artifact_id) if artifact_id else None,
                release_notes=release_notes, download_url=download_url,
                distribution_mode=distribution_mode,
                min_host_version=min_host_version,
                max_host_version=max_host_version, status=status,
            )
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="version.create",
                      resource_type="plugin_version", resource_id=str(vid),
                      payload={"plugin_id": plugin_id, "version": version, "status": status},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=versions", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/versions/{version_id}/status")
@require_admin
async def catalog_version_status(request: Request, plugin_id: int,
                                 version_id: int, status: str = Form(...)):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            catalog_svc.update_version_status(cur, version_id, status)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action=f"version.{status}",
                      resource_type="plugin_version", resource_id=str(version_id),
                      payload={"plugin_id": plugin_id, "status": status},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=versions", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/versions/purge")
@require_admin
async def catalog_versions_purge(request: Request, plugin_id: int):
    """Delete all deprecated and yanked versions for a plugin."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Detach campaigns referencing old versions
            cur.execute("""
                UPDATE campaigns SET version_id = NULL
                WHERE version_id IN (
                    SELECT id FROM plugin_versions
                    WHERE plugin_id = %s AND status IN ('deprecated', 'yanked')
                )
            """, (plugin_id,))
            # Delete old versions
            cur.execute("""
                DELETE FROM plugin_versions
                WHERE plugin_id = %s AND status IN ('deprecated', 'yanked')
            """, (plugin_id,))
            deleted = cur.rowcount
            # Clean orphan artifacts
            cur.execute("""
                DELETE FROM artifacts
                WHERE id NOT IN (SELECT artifact_id FROM plugin_versions WHERE artifact_id IS NOT NULL)
                  AND id NOT IN (SELECT artifact_id FROM campaigns WHERE artifact_id IS NOT NULL)
                  AND id NOT IN (SELECT rollback_artifact_id FROM campaigns WHERE rollback_artifact_id IS NOT NULL)
            """)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="versions.purge",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"deleted_versions": deleted},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=versions", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/deployments/purge")
@require_admin
async def catalog_deployments_purge(request: Request, plugin_id: int):
    """Delete all completed/rolled_back/draft campaigns for a plugin, keep active+paused."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Delete campaign_device_status for old campaigns first (FK)
            cur.execute("""
                DELETE FROM campaign_device_status
                WHERE campaign_id IN (
                    SELECT id FROM campaigns
                    WHERE plugin_id = %s AND status IN ('completed', 'rolled_back', 'draft')
                )
            """, (plugin_id,))
            # Delete old campaigns
            cur.execute("""
                DELETE FROM campaigns
                WHERE plugin_id = %s AND status IN ('completed', 'rolled_back', 'draft')
            """, (plugin_id,))
            deleted = cur.rowcount
            # Also clean campaigns without plugin_id that are completed
            cur.execute("""
                DELETE FROM campaign_device_status
                WHERE campaign_id IN (
                    SELECT id FROM campaigns
                    WHERE plugin_id IS NULL AND status IN ('completed', 'rolled_back')
                )
            """)
            cur.execute("""
                DELETE FROM campaigns
                WHERE plugin_id IS NULL AND status IN ('completed', 'rolled_back')
            """)
            deleted += cur.rowcount
            # Clean orphan artifacts
            cur.execute("""
                DELETE FROM artifacts
                WHERE id NOT IN (SELECT artifact_id FROM plugin_versions WHERE artifact_id IS NOT NULL)
                  AND id NOT IN (SELECT artifact_id FROM campaigns WHERE artifact_id IS NOT NULL)
                  AND id NOT IN (SELECT rollback_artifact_id FROM campaigns WHERE rollback_artifact_id IS NOT NULL)
            """)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="deployments.purge",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"deleted_campaigns": deleted},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=deployments", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/versions/upload")
@require_admin
async def catalog_version_upload(request: Request, plugin_id: int,
                                 version: str = Form(...),
                                 release_notes: str = Form(""),
                                 distribution_mode: str = Form("managed"),
                                 min_host_version: str = Form(""),
                                 max_host_version: str = Form(""),
                                 environment: str = Form("dev"),
                                 deploy_strategy: str = Form("patch_all"),
                                 stage_hours: str = Form("24"),
                                 target_mode: str = Form("all"),
                                 cohort_id: str = Form(""),
                                 percent: str = Form("10"),
                                 emails: str = Form(""),
                                 action: str = Form("deploy"),
                                 binary: UploadFile = File(...)):
    """Upload binary → create artifact + version → optionally deploy."""
    # 1. Validate & read binary
    error = artifacts_svc.validate_upload(binary.filename or "", binary.size or 0)
    if error:
        raise HTTPException(400, error)
    data = await binary.read()
    if len(data) > artifacts_svc.MAX_UPLOAD_SIZE:
        raise HTTPException(400, "Fichier trop volumineux (>100 Mo)")

    # 2. Extract dm-config.json and dm-manifest.json before stripping
    deploy_config_template = None
    dm_manifest = None
    try:
        import zipfile as _zf
        with _zf.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                basename = name.rsplit("/", 1)[-1].lower()
                if basename in ("dm-config.json", "dm_config.json"):
                    raw = zf.read(name).decode("utf-8", errors="replace")
                    deploy_config_template = json.loads(raw)
                    deploy_config_template = _apply_platform_defaults(deploy_config_template)
                elif basename in ("dm-manifest.json", "dm_manifest.json"):
                    raw = zf.read(name).decode("utf-8", errors="replace")
                    dm_manifest = json.loads(raw)
    except Exception:
        pass

    # 3. Strip dm-config.json from binary, store artifact
    data = _strip_dm_metadata_from_zip(data)
    checksum = artifacts_svc.compute_checksum(data)

    conn = get_db_connection()
    try:
        plugin = None
        with conn.cursor() as cur:
            plugin = catalog_svc.get_plugin(cur, plugin_id)
            if not plugin:
                raise HTTPException(404, "Plugin introuvable")
            device_type = plugin.get("device_type", "libreoffice")

            binaries_dir = os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")
            rel_path = f"{device_type}/{version}_{binary.filename}"
            os.makedirs(f"{binaries_dir}/{device_type}", exist_ok=True)
            local_path = f"{binaries_dir}/{rel_path}"
            with open(local_path, "wb") as f:
                f.write(data)
        

            # 4. Create artifact
            artifact_id = artifacts_svc.create_artifact(
                cur, device_type=device_type, platform_variant="",
                version=version, s3_path=local_path, checksum=checksum,
            )

            # 5. Create version (published immediately)
            vid = catalog_svc.create_version(
                cur, plugin_id=plugin_id, version=version,
                artifact_id=artifact_id,
                release_notes=release_notes, download_url="",
                distribution_mode=distribution_mode,
                min_host_version=min_host_version,
                max_host_version=max_host_version,
                status="published",
            )

            # 6. Store config_template if extracted
            if deploy_config_template:
                try:
                    catalog_svc.update_plugin(cur, plugin_id, config_template=deploy_config_template)
                except Exception as ct_err:
                    logger.warning("version upload: config_template store failed: %s", ct_err)

            # 6b. Extract release_notes + update plugin changelog from dm-manifest.json
            if dm_manifest:
                manifest_changelog = dm_manifest.get("changelog", [])
                if isinstance(manifest_changelog, list) and manifest_changelog:
                    # Find release notes for this specific version
                    if not release_notes:
                        for entry in manifest_changelog:
                            if entry.get("version") == version:
                                changes = entry.get("changes", [])
                                release_notes = "\n".join(f"- {c}" for c in changes)
                                # Update the version record with extracted notes
                                try:
                                    cur.execute(
                                        "UPDATE plugin_versions SET release_notes = %s WHERE id = %s",
                                        (release_notes, vid),
                                    )
                                except Exception:
                                    pass
                                break
                    # Update plugin changelog with the full manifest changelog
                    try:
                        catalog_svc.update_plugin(cur, plugin_id,
                                                  changelog=json.dumps(manifest_changelog))
                    except Exception as cl_err:
                        logger.warning("version upload: changelog update failed: %s", cl_err)

            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="version.upload",
                      resource_type="plugin_version", resource_id=str(vid),
                      payload={"plugin_id": plugin_id, "version": version,
                               "has_config_template": bool(deploy_config_template)},
                      ip=request.client.host if request.client else None)

            # 7. Deploy if requested
            campaign_id = None
            if action == "deploy":
                # Cohort
                target_cohort_id = None
                if target_mode == "existing" and cohort_id:
                    target_cohort_id = int(cohort_id)
                elif target_mode == "percent":
                    pct = max(1, min(100, int(percent)))
                    cid = cohorts_svc.create_cohort(
                        cur, name=f"auto-{device_type}-{version}-{environment}-{pct}pct",
                        description=f"Auto: {pct}% rollout on {environment}",
                        type="percentage",
                    )
                    target_cohort_id = cid
                elif target_mode == "emails":
                    email_list = [e.strip() for e in emails.strip().splitlines() if e.strip()]
                    if email_list:
                        cid = cohorts_svc.create_cohort(
                            cur, name=f"auto-{device_type}-{version}-{environment}-manual",
                            description=f"Auto: {len(email_list)} emails on {environment}",
                            type="manual",
                        )
                        cohorts_svc.add_members(cur, cid, [("email", e) for e in email_list])
                        target_cohort_id = cid

                # Rollout config
                rollout_config = None
                urgency = "normal"
                if deploy_strategy == "progressive":
                    hours = max(1, int(stage_hours))
                    rollout_config = {
                        "stages": [
                            {"percent": 5, "duration_hours": hours, "label": "Canary (5%)"},
                            {"percent": 25, "duration_hours": hours, "label": "Early adopters (25%)"},
                            {"percent": 50, "duration_hours": hours, "label": "Moitie (50%)"},
                            {"percent": 100, "duration_hours": 0, "label": "Deploiement complet"},
                        ]
                    }
                else:
                    urgency = "normal"

                campaign_name = f"MaJ {plugin['name']} {version} [{environment}]"
                campaign_id = campaigns_svc.create_campaign(
                    cur, name=campaign_name, type="plugin_update",
                    artifact_id=artifact_id,
                    target_cohort_id=target_cohort_id,
                    urgency=urgency, status="active",
                    rollout_config=rollout_config,
                    created_by=actor.get("email"),
                    plugin_id=plugin_id,
                )
                # Set environment and plugin_id on campaign
                cur.execute(
                    "UPDATE campaigns SET environment = %s, plugin_id = %s, version_id = %s WHERE id = %s",
                    (environment, plugin_id, vid, campaign_id),
                )
                audit_log(cur, actor=actor, action="deploy.create",
                          resource_type="campaign", resource_id=str(campaign_id),
                          payload={"plugin_id": plugin_id, "version": version,
                                   "environment": environment, "strategy": deploy_strategy},
                          ip=request.client.host if request.client else None)

            conn.commit()

        if campaign_id:
            return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=deployments", status_code=303)
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=versions", status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error("catalog version upload failed: %s", e)
        raise HTTPException(400, str(e))
    finally:
        conn.close()


# ─── Deployment actions (from catalog) ──────────────────────────────

def _catalog_deploy_action(plugin_id: int, campaign_id: int, new_status: str,
                           action_name: str, request: Request):
    """Campaign lifecycle action with redirect back to catalog deployments tab."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            campaigns_svc.update_campaign_status(cur, campaign_id, new_status)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action=f"campaign.{action_name}",
                      resource_type="campaign", resource_id=str(campaign_id),
                      payload={"new_status": new_status, "plugin_id": plugin_id},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=deployments", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/deployments/{campaign_id}/pause")
@require_admin
async def catalog_deploy_pause(request: Request, plugin_id: int, campaign_id: int):
    return _catalog_deploy_action(plugin_id, campaign_id, "paused", "pause", request)


@router.post("/catalog/{plugin_id}/deployments/{campaign_id}/resume")
@require_admin
async def catalog_deploy_resume(request: Request, plugin_id: int, campaign_id: int):
    return _catalog_deploy_action(plugin_id, campaign_id, "active", "resume", request)


@router.post("/catalog/{plugin_id}/deployments/{campaign_id}/abort")
@require_admin
async def catalog_deploy_abort(request: Request, plugin_id: int, campaign_id: int):
    return _catalog_deploy_action(plugin_id, campaign_id, "rolled_back", "rollback", request)


# ─── Communications ──────────────────────────────────────────────────

@router.get("/communications", response_class=HTMLResponse)
@require_admin
async def communications_list(request: Request, type: str = "", status: str = ""):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            comm_list = comms_svc.list_communications(
                cur, type=type or None, status=status or None,
            )
            plugin_list = catalog_svc.list_plugins(cur)
        return templates.TemplateResponse("communications.html", {
            "request": request, "communications": comm_list,
            "plugins": plugin_list,
            "filters": {"type": type, "status": status},
        })
    finally:
        conn.close()


@router.get("/communications/new", response_class=HTMLResponse)
@require_admin
async def communication_new(request: Request, type: str = "announcement"):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            plugin_list = catalog_svc.list_plugins(cur)
            cohort_list = cohorts_svc.list_cohorts(cur)
        return templates.TemplateResponse("communication_new.html", {
            "request": request, "comm_type": type,
            "plugins": plugin_list, "cohorts": cohort_list,
        })
    finally:
        conn.close()


@router.post("/communications")
@require_admin
async def communication_create(request: Request,
                                type: str = Form(...),
                                title: str = Form(...),
                                body: str = Form(...),
                                priority: str = Form("normal"),
                                target_plugin_id: str = Form(""),
                                target_cohort_id: str = Form(""),
                                starts_at: str = Form(""),
                                expires_at: str = Form(""),
                                survey_question: str = Form(""),
                                survey_choices: str = Form(""),
                                survey_allow_multiple: str = Form(""),
                                survey_allow_comment: str = Form(""),
                                start_status: str = Form("draft")):
    choices = [c.strip() for c in survey_choices.strip().splitlines() if c.strip()] if survey_choices else None
    conn = get_db_connection()
    try:
        actor = getattr(request.state, "admin_session", {})
        with conn.cursor() as cur:
            comm_id = comms_svc.create_communication(
                cur, type=type, title=title, body=body, priority=priority,
                target_plugin_id=int(target_plugin_id) if target_plugin_id else None,
                target_cohort_id=int(target_cohort_id) if target_cohort_id else None,
                starts_at=starts_at or None, expires_at=expires_at or None,
                survey_question=survey_question or None,
                survey_choices=choices,
                survey_allow_multiple=survey_allow_multiple == "on",
                survey_allow_comment=survey_allow_comment == "on",
                status=start_status,
                created_by=actor.get("email"),
            )
            audit_log(cur, actor=actor, action="communication.create",
                      resource_type="communication", resource_id=str(comm_id),
                      payload={"type": type, "title": title},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/communications/{comm_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/communications/{comm_id}", response_class=HTMLResponse)
@require_admin
async def communication_detail(request: Request, comm_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            comm = comms_svc.get_communication(cur, comm_id)
            if not comm:
                raise HTTPException(404, "Communication non trouvee")
            stats = comms_svc.get_communication_stats(cur, comm_id)
            survey_results = None
            if comm.get("type") == "survey":
                survey_results = comms_svc.get_survey_results(cur, comm_id)
        return templates.TemplateResponse("communication_detail.html", {
            "request": request, "comm": comm, "stats": stats,
            "survey_results": survey_results,
        })
    finally:
        conn.close()


@router.post("/communications/{comm_id}/status")
@require_admin
async def communication_status(request: Request, comm_id: int,
                                status: str = Form(...)):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            comms_svc.update_communication_status(cur, comm_id, status)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action=f"communication.{status}",
                      resource_type="communication", resource_id=str(comm_id),
                      payload={"status": status},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/communications/{comm_id}", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


# ─── Catalog Preview (static HTML) ────────────────────────────────────────

@router.get("/catalog-preview", response_class=HTMLResponse)
@require_admin
async def catalog_preview(request: Request):
    return templates.TemplateResponse("catalog_preview.html", {"request": request})


# ─── Env Overrides ───────────────────────────────────────────────────────

@router.post("/catalog/{plugin_id}/env")
@require_admin
async def catalog_env_upsert(request: Request, plugin_id: int,
                              environment: str = Form(...),
                              key: str = Form(...),
                              value: str = Form(...),
                              is_secret: str = Form("")):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO plugin_env_overrides (plugin_id, environment, key, value, is_secret)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (plugin_id, environment, key)
                DO UPDATE SET value = %s, is_secret = %s, updated_at = NOW()
            """, (plugin_id, environment, key, value, is_secret == "on",
                  value, is_secret == "on"))
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="env.override.upsert",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"env": environment, "key": key},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=env", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/catalog/{plugin_id}/env/{override_id}")
@require_admin
async def catalog_env_delete(request: Request, plugin_id: int, override_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM plugin_env_overrides WHERE id = %s AND plugin_id = %s",
                        (override_id, plugin_id))
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="env.override.delete",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"override_id": override_id},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=env", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/api/catalog/migrate-config-templates")
@require_admin
async def catalog_migrate_config_templates(request: Request):
    """One-shot migration: read config templates from filesystem, store in DB."""
    from app.main import _load_config_template
    conn = get_db_connection()
    migrated = []
    errors = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, slug, device_type, config_template FROM plugins")
            cols = [d[0] for d in cur.description]
            plugins = [dict(zip(cols, r)) for r in cur.fetchall()]
            for p in plugins:
                if p["config_template"]:
                    migrated.append({"slug": p["slug"], "status": "skipped (already has template)"})
                    continue
                try:
                    # Try loading from filesystem for each standard profile to build a dm-config.json
                    template = {"configVersion": 1, "default": {}}
                    for profile in ("dev", "int", "prod"):
                        try:
                            cfg = _load_config_template(profile, device=p["device_type"],
                                                        device_name=p["slug"])
                            config_obj = cfg.get("config", cfg)
                            if profile == "dev":
                                template["default"] = config_obj
                            else:
                                # Only keep keys that differ from default
                                diff = {}
                                for k, v in config_obj.items():
                                    if template["default"].get(k) != v:
                                        diff[k] = v
                                if diff:
                                    template[profile] = diff
                        except FileNotFoundError:
                            pass
                    template = _apply_platform_defaults(template)
                    cur.execute("UPDATE plugins SET config_template = %s WHERE id = %s",
                                (json.dumps(template), p["id"]))
                    migrated.append({"slug": p["slug"], "status": "migrated"})
                except Exception as e:
                    errors.append({"slug": p["slug"], "error": str(e)})
            conn.commit()
        actor = getattr(request.state, "admin_session", {})
        return JSONResponse({"migrated": migrated, "errors": errors})
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


@router.post("/api/catalog/{plugin_id}/config-template")
@require_admin
async def catalog_save_config_template(request: Request, plugin_id: int):
    """Save or update the dm-config.json template for a plugin."""
    body = await request.json()
    template = body.get("config_template")
    if not template or not isinstance(template, dict):
        return JSONResponse({"error": "config_template manquant ou invalide"}, status_code=400)
    # Apply platform defaults to server profiles
    template = _apply_platform_defaults(template)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            ok = catalog_svc.update_plugin(cur, plugin_id, config_template=json.dumps(template))
            if not ok:
                return JSONResponse({"error": "Plugin non trouve"}, status_code=404)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="plugin.config_template.update",
                      resource_type="plugin", resource_id=str(plugin_id),
                      ip=request.client.host if request.client else None)
            conn.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


@router.get("/api/catalog/{plugin_id}/preview")
@require_admin
async def catalog_preview_config(request: Request, plugin_id: int, profile: str = "dev"):
    """Preview the final config JSON as the plugin would receive it."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            plugin = catalog_svc.get_plugin(cur, plugin_id)
            if not plugin:
                return JSONResponse({"error": "Plugin non trouve"}, status_code=404)
            # Simulate config loading
            from app.main import _load_config_template, _substitute_env, _apply_overrides, _apply_catalog_overrides
            cfg = _load_config_template(profile, device=plugin["device_type"],
                                        device_name=plugin["slug"], cur=cur)
            cfg = _substitute_env(cfg)
            cfg = _apply_overrides(cfg, profile=profile, device=plugin["device_type"])
            cfg = _apply_catalog_overrides(cfg, plugin_id=plugin_id, profile=profile, cur=cur)
            config_obj = cfg.get("config")
            if isinstance(config_obj, dict):
                config_obj["device_name"] = plugin["slug"]
                config_obj["config_path"] = f"/config/{plugin['slug']}/config.json"
        return JSONResponse(cfg)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


# ─── Keycloak Clients ────────────────────────────────────────────────────

@router.get("/api/keycloak/defaults")
@require_admin
async def api_keycloak_defaults(request: Request):
    return JSONResponse(keycloak_svc.get_defaults())


@router.get("/api/keycloak/clients")
@require_admin
async def api_keycloak_clients(request: Request):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            clients = keycloak_svc.list_clients(cur)
        return JSONResponse(clients)
    finally:
        conn.close()


@router.post("/api/keycloak/clients")
@require_admin
async def api_keycloak_create_client(request: Request,
                                      client_id: str = Form(...),
                                      realm: str = Form(...),
                                      description: str = Form(""),
                                      client_type: str = Form("public"),
                                      redirect_uris: str = Form(""),
                                      pkce_enabled: str = Form("on")):
    uris = [u.strip() for u in redirect_uris.strip().splitlines() if u.strip()]
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            kc_id = keycloak_svc.create_client(
                cur, client_id=client_id, realm=realm, description=description,
                client_type=client_type, redirect_uris=uris,
                pkce_enabled=pkce_enabled == "on",
            )
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="keycloak.client.create",
                      resource_type="keycloak_client", resource_id=str(kc_id),
                      payload={"client_id": client_id, "realm": realm},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return JSONResponse({"ok": True, "id": kc_id, "client_id": client_id})
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()


@router.get("/api/keycloak/clients/{client_db_id}/export")
@require_admin
async def api_keycloak_export(request: Request, client_db_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            client = keycloak_svc.get_client(cur, client_db_id)
            if not client:
                raise HTTPException(404, "Client non trouve")
        export = keycloak_svc.export_keycloak_json(client)
        return JSONResponse(
            export,
            headers={"Content-Disposition": f'attachment; filename="keycloak-client-{client["client_id"]}.json"'}
        )
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/keycloak/link")
@require_admin
async def catalog_keycloak_link(request: Request, plugin_id: int,
                                 keycloak_client_id: int = Form(...),
                                 environment: str = Form("prod")):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            keycloak_svc.link_plugin_client(cur, plugin_id, keycloak_client_id, environment)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="keycloak.link",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"kc_client_id": keycloak_client_id, "env": environment},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=keycloak", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


# ─── Access (maturity + waitlist) ────────────────────────────────────────

@router.post("/catalog/{plugin_id}/access")
@require_admin
async def catalog_access_update(request: Request, plugin_id: int,
                                 maturity: str = Form("release"),
                                 access_mode: str = Form("open"),
                                 required_group: str = Form("")):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            catalog_svc.update_plugin(cur, plugin_id,
                                      maturity=maturity, access_mode=access_mode,
                                      required_group=required_group or None)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="plugin.access.update",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"maturity": maturity, "access_mode": access_mode},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=access", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/waitlist/{wl_id}/approve")
@require_admin
async def catalog_waitlist_approve(request: Request, plugin_id: int, wl_id: int):
    conn = get_db_connection()
    try:
        actor = getattr(request.state, "admin_session", {})
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE plugin_waitlist SET status = 'approved', reviewed_by = %s, reviewed_at = NOW()
                WHERE id = %s AND plugin_id = %s
            """, (actor.get("email"), wl_id, plugin_id))
            audit_log(cur, actor=actor, action="waitlist.approve",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"waitlist_id": wl_id},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=access", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/catalog/{plugin_id}/waitlist/{wl_id}/reject")
@require_admin
async def catalog_waitlist_reject(request: Request, plugin_id: int, wl_id: int):
    conn = get_db_connection()
    try:
        actor = getattr(request.state, "admin_session", {})
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE plugin_waitlist SET status = 'rejected', reviewed_by = %s, reviewed_at = NOW()
                WHERE id = %s AND plugin_id = %s
            """, (actor.get("email"), wl_id, plugin_id))
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=access", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


# ─── Alias ───────────────────────────────────────────────────────────────

@router.post("/catalog/{plugin_id}/alias")
@require_admin
async def catalog_alias_add(request: Request, plugin_id: int,
                             alias: str = Form(...)):
    alias = alias.strip().lower()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO plugin_aliases (alias, plugin_id) VALUES (%s, %s)",
                        (alias, plugin_id))
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="alias.add",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"alias": alias},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=alias", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/catalog/{plugin_id}/alias/{alias}")
@require_admin
async def catalog_alias_delete(request: Request, plugin_id: int, alias: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM plugin_aliases WHERE alias = %s AND plugin_id = %s",
                        (alias, plugin_id))
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="alias.delete",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"alias": alias},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=alias", status_code=303)
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/api/catalog/{plugin_id}/alias-stats")
@require_admin
async def catalog_alias_stats(request: Request, plugin_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT alias, COUNT(*) AS total_calls,
                       COUNT(DISTINCT client_uuid) FILTER (WHERE client_uuid IS NOT NULL) AS unique_devices
                FROM alias_access_log
                WHERE plugin_id = %s AND accessed_at > NOW() - INTERVAL '7 days'
                GROUP BY alias ORDER BY total_calls DESC
            """, (plugin_id,))
            cols = [d[0] for d in cur.description]
            stats = [dict(zip(cols, row)) for row in cur.fetchall()]
        return JSONResponse(stats)
    finally:
        conn.close()


# ─── Plugin Logo ─────────────────────────────────────────────────────────

@router.post("/catalog/{plugin_id}/logo")
@require_admin
async def catalog_upload_logo(request: Request, plugin_id: int,
                              logo: UploadFile = File(...)):
    """Upload plugin logo/mascot."""
    allowed_ext = {".png", ".jpg", ".jpeg", ".svg", ".webp"}
    filename = logo.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed_ext:
        return HTMLResponse(f'<div class="dm-flash dm-flash--error">Format non supporte ({ext}). Utiliser PNG, JPG, SVG ou WebP.</div>')

    data = await logo.read()
    if len(data) > 2 * 1024 * 1024:
        return HTMLResponse('<div class="dm-flash dm-flash--error">Fichier trop volumineux (max 2 Mo).</div>')

    import base64 as _b64
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml", ".webp": "image/webp"}.get(ext, "image/png")
    data_url = f"data:{mime};base64,{_b64.b64encode(data).decode()}"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            plugin = catalog_svc.get_plugin(cur, plugin_id)
            if not plugin:
                raise HTTPException(404, "Plugin non trouve")

            catalog_svc.update_plugin(cur, plugin_id, icon_url=data_url)
            actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=actor, action="plugin.logo.upload",
                      resource_type="plugin", resource_id=str(plugin_id),
                      payload={"filename": icon_filename, "size": len(data)},
                      ip=request.client.host if request.client else None)
            conn.commit()
        return RedirectResponse(f"/admin/catalog/{plugin_id}?tab=edit", status_code=303)
    except Exception as e:
        conn.rollback()
        return HTMLResponse(f'<div class="dm-flash dm-flash--error">Erreur: {e}</div>')
    finally:
        conn.close()


# ─── Files API (internal, token-secured — allows DM API pods to pull binaries) ───

def _files_token_check(request: Request) -> bool:
    expected = os.getenv("DM_QUEUE_ADMIN_TOKEN", "").strip()
    if not expected:
        return False
    import hmac
    return hmac.compare_digest(
        (request.headers.get("x-admin-token") or "").strip(), expected
    )


@router.put("/api/files/upload/{path:path}")
async def admin_files_upload(request: Request, path: str, file: UploadFile = File(...)):
    """Store a binary file on the admin persistent volume (token-secured)."""
    if not _files_token_check(request):
        raise HTTPException(403, "Invalid token")
    base = os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")
    full = os.path.normpath(os.path.join(base, path))
    if not full.startswith(os.path.normpath(base)):
        raise HTTPException(400, "Invalid path")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    data = await file.read()
    with open(full, "wb") as f:
        f.write(data)
    return JSONResponse({"ok": True, "path": path, "size": len(data)})


@router.get("/api/files/{path:path}")
async def admin_files_get(request: Request, path: str):
    """Serve a local binary file (token-secured, used by API pods for pull-on-miss)."""
    if not _files_token_check(request):
        raise HTTPException(403, "Invalid token")
    base = os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")
    full = os.path.normpath(os.path.join(base, path))
    if not full.startswith(os.path.normpath(base)):
        raise HTTPException(400, "Invalid path")
    if not os.path.isfile(full):
        raise HTTPException(404, "File not found")
    return FileResponse(full)


@router.get("/api/files")
async def admin_files_list(request: Request, prefix: str = ""):
    """List binary files (token-secured)."""
    if not _files_token_check(request):
        raise HTTPException(403, "Invalid token")
    base = os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")
    target = os.path.normpath(os.path.join(base, prefix)) if prefix else base
    if not target.startswith(os.path.normpath(base)):
        raise HTTPException(400, "Invalid path")
    if not os.path.isdir(target):
        return JSONResponse({"files": []})
    result = []
    for root, _dirs, files in os.walk(target):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base)
            result.append({"path": rel, "size": os.path.getsize(full)})
    return JSONResponse({"files": sorted(result, key=lambda x: x["path"]), "total": len(result)})


# ─── Dashboard APIs ──────────────────────────────────────────────────────

@router.get("/api/debug/status", response_class=HTMLResponse)
@require_admin
async def api_debug_status(request: Request):
    """HTMX fragment: service availability banner for dashboard."""
    import urllib.request as urlreq
    import time as _time

    checks = {}
    # DB
    t0 = _time.monotonic()
    try:
        conn = get_db_connection()
        conn.cursor().execute("SELECT 1")
        conn.close()
        checks["db"] = {"ok": True, "ms": round((_time.monotonic()-t0)*1000)}
    except Exception:
        checks["db"] = {"ok": False, "ms": round((_time.monotonic()-t0)*1000)}

    # Keycloak — use JWKS endpoint (cluster-internal, no auth required)
    import urllib.error as _urllib_error
    jwks_url = os.getenv("DM_AUTH_JWKS_URL", "")
    if jwks_url:
        t0 = _time.monotonic()
        try:
            urlreq.urlopen(jwks_url, timeout=5)
            checks["keycloak"] = {"ok": True, "ms": round((_time.monotonic()-t0)*1000)}
        except Exception:
            checks["keycloak"] = {"ok": False, "ms": round((_time.monotonic()-t0)*1000)}

    # LLM — tolerate 401/403 (relay auth required, but service is reachable)
    llm_url = os.getenv("LLM_BASE_URL", "")
    if llm_url:
        t0 = _time.monotonic()
        try:
            token = os.getenv("LLM_API_TOKEN", "")
            req = urlreq.Request(f"{llm_url.rstrip('/')}/models",
                                 headers={"Authorization": f"Bearer {token}"})
            urlreq.urlopen(req, timeout=5)
            checks["llm"] = {"ok": True, "ms": round((_time.monotonic()-t0)*1000)}
        except _urllib_error.HTTPError as e:
            checks["llm"] = {"ok": e.code in (401, 403), "ms": round((_time.monotonic()-t0)*1000)}
        except Exception:
            checks["llm"] = {"ok": False, "ms": round((_time.monotonic()-t0)*1000)}

    # Relay
    relay_url = os.getenv("DM_RELAY_ASSISTANT_URL", "http://relay-assistant")
    t0 = _time.monotonic()
    try:
        urlreq.urlopen(f"{relay_url.rstrip('/')}/healthz", timeout=3)
        checks["relay"] = {"ok": True, "ms": round((_time.monotonic()-t0)*1000)}
    except Exception:
        checks["relay"] = {"ok": False, "ms": round((_time.monotonic()-t0)*1000)}

    all_ok = all(c["ok"] for c in checks.values())
    down = [k for k, v in checks.items() if not v["ok"]]

    if all_ok:
        banner = '<div class="dm-flash dm-flash--success" style="margin-bottom:0;display:flex;justify-content:space-between;align-items:center;">'
        banner += '<span>&#9679; Tous les services sont operationnels</span>'
        detail = " | ".join(f'{k} {v["ms"]}ms' for k, v in checks.items())
        banner += f'<span style="font-size:0.8rem;color:#666;">{detail} &mdash; <a href="/admin/debug">Details</a></span>'
        banner += '</div>'
    else:
        banner = '<div class="dm-flash dm-flash--warning" style="margin-bottom:0;display:flex;justify-content:space-between;align-items:center;">'
        banner += f'<span>&#9684; Service degrade &mdash; {", ".join(down)} indisponible(s)</span>'
        banner += '<a href="/admin/debug" style="font-size:0.8rem;">Details</a>'
        banner += '</div>'

    return HTMLResponse(banner)


@router.get("/api/adoption")
@require_admin
async def api_adoption(request: Request, period: str = "1M"):
    """Adoption metrics for dashboard chart."""
    intervals = {"1J": "1 day", "1S": "7 days", "1M": "30 days", "3M": "90 days", "6M": "180 days"}
    interval = intervals.get(period, "30 days")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT client_uuid) FROM provisioning WHERE status = 'ENROLLED'")
            total = cur.fetchone()[0]
            cur.execute(f"""
                SELECT COUNT(DISTINCT client_uuid) FROM provisioning
                WHERE status = 'ENROLLED' AND created_at > NOW() - INTERVAL '{interval}'
            """)
            new_period = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(DISTINCT client_uuid) FROM device_connections
                WHERE created_at > NOW() - INTERVAL '7 days'
            """)
            active_7d = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM plugins WHERE status = 'active'")
            plugins_count = cur.fetchone()[0]
            # Timeseries
            cur.execute(f"""
                SELECT d::date AS date, COUNT(DISTINCT p.client_uuid) AS enrolled
                FROM generate_series(NOW() - INTERVAL '{interval}', NOW(), '1 day') d
                LEFT JOIN provisioning p ON p.status = 'ENROLLED' AND p.created_at <= d
                GROUP BY d::date ORDER BY d::date
            """)
            timeseries = [{"date": str(r[0]), "enrolled": r[1]} for r in cur.fetchall()]
        return JSONResponse({
            "period": period,
            "summary": {
                "total": total, "new_period": new_period,
                "active_pct": round(active_7d / total * 100) if total else 0,
                "plugins": plugins_count,
            },
            "timeseries": timeseries,
        })
    finally:
        conn.close()


# ─── Debug Page ──────────────────────────────────────────────────────────

@router.get("/debug", response_class=HTMLResponse)
@require_admin
async def debug_page(request: Request):
    """Full debug page with all service health checks."""
    import urllib.request as urlreq
    import urllib.error as urllib_error
    import time as _time
    import socket
    import concurrent.futures

    def _check(name, fn):
        t0 = _time.monotonic()
        try:
            detail = fn()
            return name, {"status": "ok", "latency_ms": round((_time.monotonic()-t0)*1000), "detail": detail}
        except Exception as e:
            return name, {"status": "error", "latency_ms": round((_time.monotonic()-t0)*1000), "detail": str(e)[:120]}

    def check_db():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'")
        n = cur.fetchone()[0]
        conn.close()
        return f"{n} tables"

    def check_keycloak():
        # Use JWKS URL (cluster-internal, no auth required) as primary check
        jwks_url = os.getenv("DM_AUTH_JWKS_URL", "")
        if jwks_url:
            with urlreq.urlopen(jwks_url, timeout=5) as r:
                data = json.loads(r.read())
            n_keys = len(data.get("keys", []))
            return f"{os.getenv('KEYCLOAK_REALM','?')}, {n_keys} keys"
        # Fallback: openid-configuration via issuer
        issuer = os.getenv("ADMIN_OIDC_ISSUER_URL") or os.getenv("KEYCLOAK_ISSUER_URL", "")
        if not issuer: return "non configure"
        with urlreq.urlopen(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=5) as r:
            data = json.loads(r.read())
        return f"{os.getenv('KEYCLOAK_REALM','?')}, {len([k for k in data if 'endpoint' in k])} endpoints"

    def check_llm():
        llm_url = os.getenv("LLM_BASE_URL", "")
        if not llm_url: return "non configure"
        token = os.getenv("LLM_API_TOKEN", "")
        model = os.getenv("DEFAULT_MODEL_NAME", "?")
        req = urlreq.Request(f"{llm_url.rstrip('/')}/models",
                             headers={"Authorization": f"Bearer {token}"})
        try:
            with urlreq.urlopen(req, timeout=5) as r:
                pass
            return f"{model}"
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                return f"{model} (accessible, auth relay requise)"
            raise
        except OSError:
            return f"{model} (upstream injoignable)"

    def check_relay():
        relay_url = os.getenv("DM_RELAY_ASSISTANT_URL", "http://relay-assistant")
        with urlreq.urlopen(f"{relay_url.rstrip('/')}/healthz", timeout=3) as r:
            pass
        return "nginx OK"

    def check_telemetry():
        url = os.getenv("DM_TELEMETRY_UPSTREAM_ENDPOINT", "")
        if not url: return "non configure"
        payload = b'{"resourceSpans":[]}'
        req = urlreq.Request(url, data=payload, method="POST",
                             headers={"Content-Type": "application/json"})
        with urlreq.urlopen(req, timeout=5) as r:
            pass
        return "accessible"

    # Run checks in parallel
    checks = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(_check, "PostgreSQL", check_db),
            pool.submit(_check, "Keycloak OIDC", check_keycloak),
            pool.submit(_check, "LLM", check_llm),
            pool.submit(_check, "Relay-assistant", check_relay),
            pool.submit(_check, "Telemetrie upstream", check_telemetry),
        ]
        for f in concurrent.futures.as_completed(futures, timeout=15):
            name, result = f.result()
            checks[name] = result

    # Config vars (safe)
    secret_keys = {"LLM_API_TOKEN", "AWS_SECRET_ACCESS_KEY", "DATABASE_URL",
                   "ADMIN_OIDC_CLIENT_SECRET", "ADMIN_SESSION_SECRET",
                   "DM_RELAY_PROXY_SHARED_TOKEN", "DM_TELEMETRY_TOKEN_SIGNING_KEY"}
    config_vars = []
    for key in ["PUBLIC_BASE_URL", "DM_APP_ENV", "DM_CONFIG_PROFILE", "DM_PORT",
                "DM_RELAY_ENABLED", "DM_TELEMETRY_ENABLED",
                "KEYCLOAK_ISSUER_URL", "KEYCLOAK_REALM", "KEYCLOAK_CLIENT_ID",
                "LLM_BASE_URL", "LLM_API_TOKEN", "DEFAULT_MODEL_NAME",
                "DM_S3_BUCKET", "DATABASE_URL"]:
        val = os.getenv(key, "")
        if key in secret_keys and val:
            val = val[:4] + "***" + val[-4:] if len(val) > 8 else "***"
        config_vars.append({"key": key, "value": val or "(vide)"})

    # DB stats
    db_stats = []
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        for (tbl,) in cur.fetchall():
            try:
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                db_stats.append({"table": tbl, "rows": cur.fetchone()[0]})
            except Exception:
                db_stats.append({"table": tbl, "rows": "?"})
        conn.close()
    except Exception:
        pass

    # System
    system_info = {
        "hostname": socket.gethostname(),
        "python": os.popen("python3 --version 2>&1").read().strip(),
        "uptime": "N/A",
    }

    # Telemetry info
    from app.main import _resolve_public_telemetry_endpoint
    telemetry_info = {
        "enabled": os.getenv("DM_TELEMETRY_ENABLED", "true"),
        "public_endpoint": _resolve_public_telemetry_endpoint(),
        "upstream_endpoint": os.getenv("DM_TELEMETRY_UPSTREAM_ENDPOINT", "(vide)"),
        "upstream_auth_type": os.getenv("DM_TELEMETRY_UPSTREAM_AUTH_TYPE", ""),
        "token_ttl": os.getenv("DM_TELEMETRY_TOKEN_TTL_SECONDS", "300"),
        "require_token": os.getenv("DM_TELEMETRY_REQUIRE_TOKEN", "true"),
        "max_body_size_mb": os.getenv("DM_TELEMETRY_MAX_BODY_SIZE_MB", "2"),
        "grafana_url": os.getenv("DM_TELEMETRY_GRAFANA_URL", ""),
    }

    return templates.TemplateResponse("debug.html", {
        "request": request, "checks": checks, "config_vars": config_vars,
        "db_stats": db_stats, "system_info": system_info,
        "telemetry_info": telemetry_info,
    })


# ─── Audit Log ────────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
@require_admin
async def audit_list(request: Request, actor: str = "", action: str = "",
                     resource_type: str = "", page: int = 0):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            entries = audit_svc.list_audit_entries(
                cur, actor=actor or None, action=action or None,
                resource_type=resource_type or None,
                limit=100, offset=page * 100,
            )
        return templates.TemplateResponse("audit_log.html", {
            "request": request, "entries": entries, "page": page,
            "filters": {"actor": actor, "action": action, "resource_type": resource_type},
        })
    finally:
        conn.close()


@router.get("/audit/export")
@require_admin
async def audit_export(request: Request, actor: str = "", action: str = "",
                       resource_type: str = ""):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            entries = audit_svc.list_audit_entries(
                cur, actor=actor or None, action=action or None,
                resource_type=resource_type or None, limit=10000,
            )
            # Audit the export itself
            admin_actor = getattr(request.state, "admin_session", {})
            audit_log(cur, actor=admin_actor, action="audit.export",
                      resource_type="audit", payload={"count": len(entries)},
                      ip=request.client.host if request.client else None)
            conn.commit()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["horodatage", "acteur", "action", "type_ressource", "id_ressource", "details"])
        for e in entries:
            writer.writerow([
                e["created_at"], e["actor_email"], e["action"],
                e["resource_type"], e.get("resource_id", ""),
                json.dumps(e.get("payload")) if e.get("payload") else "",
            ])
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_export.csv"},
        )
    finally:
        conn.close()


# ─── File browser ───────────────────────────────────────────────────────

_DATA_ROOT = Path(os.getenv("DM_DATA_ROOT", "/data"))

_FILE_ROOTS = {
    "enroll": lambda: Path(os.getenv("DM_ENROLL_DIR", "/data/enroll")),
    "binaries": lambda: Path(os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")),
    "config": lambda: Path(os.getenv("DM_CONFIG_DIR", "/data/content/config")),
}


def _human_size(size_bytes: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.0f} {unit}" if unit == "o" else f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} To"


def _safe_resolve(root: Path, subpath: str) -> Path:
    """Resolve subpath under root, preventing directory traversal."""
    resolved = (root / subpath).resolve()
    if not str(resolved).startswith(str(root.resolve())):
        raise HTTPException(403, "Acces interdit")
    return resolved


def _list_dir(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    entries = []
    for item in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        stat = item.stat()
        entries.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "size": stat.st_size if item.is_file() else 0,
            "size_human": _human_size(stat.st_size) if item.is_file() else "-",
            "mtime": stat.st_mtime,
        })
    return entries


@router.get("/files", response_class=HTMLResponse)
@require_admin
async def files_index(request: Request):
    """File browser — shows root directories."""
    roots = []
    for name, path_fn in _FILE_ROOTS.items():
        p = path_fn()
        count = len(list(p.iterdir())) if p.is_dir() else 0
        roots.append({"name": name, "path": name, "exists": p.is_dir(), "count": count})
    return templates.TemplateResponse("files.html", {
        "request": request,
        "mode": "roots",
        "roots": roots,
        "entries": [],
        "current_path": "",
        "breadcrumbs": [],
    })


@router.get("/files/{path:path}", response_class=HTMLResponse)
@require_admin
async def files_browse(request: Request, path: str):
    """File browser — browse a subdirectory or show file info."""
    parts = PurePosixPath(path).parts
    if not parts:
        return RedirectResponse("/admin/files", status_code=303)

    root_name = parts[0]
    if root_name not in _FILE_ROOTS:
        raise HTTPException(404, f"Repertoire inconnu: {root_name}")

    root = _FILE_ROOTS[root_name]()
    subpath = str(PurePosixPath(*parts[1:])) if len(parts) > 1 else ""
    resolved = _safe_resolve(root, subpath)

    if not resolved.exists():
        raise HTTPException(404, "Fichier ou repertoire introuvable")

    # Build breadcrumbs
    breadcrumbs = [{"name": root_name, "path": root_name}]
    accumulated = root_name
    for part in parts[1:]:
        accumulated = f"{accumulated}/{part}"
        breadcrumbs.append({"name": part, "path": accumulated})

    if resolved.is_dir():
        entries = _list_dir(resolved)
        return templates.TemplateResponse("files.html", {
            "request": request,
            "mode": "list",
            "roots": [],
            "entries": entries,
            "current_path": path,
            "breadcrumbs": breadcrumbs,
        })

    # File detail
    stat = resolved.stat()
    file_info = {
        "name": resolved.name,
        "size": stat.st_size,
        "size_human": _human_size(stat.st_size),
        "mtime": stat.st_mtime,
        "suffix": resolved.suffix,
        "preview": None,
    }
    if resolved.suffix in (".json", ".txt", ".log", ".yaml", ".yml", ".conf", ".md", ".csv"):
        try:
            file_info["preview"] = resolved.read_text(errors="replace")[:10_000]
        except Exception:
            pass

    return templates.TemplateResponse("files.html", {
        "request": request,
        "mode": "detail",
        "roots": [],
        "entries": [],
        "file": file_info,
        "current_path": path,
        "breadcrumbs": breadcrumbs,
        "download_url": f"/admin/files-dl/{path}",
    })


@router.get("/files-dl/{path:path}")
@require_admin
async def files_download(request: Request, path: str):
    """Download a file."""
    parts = PurePosixPath(path).parts
    if not parts or parts[0] not in _FILE_ROOTS:
        raise HTTPException(404)
    root = _FILE_ROOTS[parts[0]]()
    subpath = str(PurePosixPath(*parts[1:])) if len(parts) > 1 else ""
    resolved = _safe_resolve(root, subpath)
    if not resolved.is_file():
        raise HTTPException(404, "Fichier introuvable")
    return FileResponse(resolved, filename=resolved.name)
