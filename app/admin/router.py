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

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
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
)

logger = logging.getLogger("dm-admin-router")

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")

# Register custom Jinja2 filters
templates.env.globals["timeago"] = timeago
templates.env.globals["span_label"] = span_label


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
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
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

    # Store locally
    binaries_dir = os.getenv("DM_LOCAL_BINARIES_DIR", "/data/content/binaries")
    os.makedirs(f"{binaries_dir}/{device_type}", exist_ok=True)
    local_path = f"{binaries_dir}/{device_type}/{version}_{binary.filename}"
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
                     request: Request):
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
        return RedirectResponse(f"/admin/campaigns/{campaign_id}", status_code=303)
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
