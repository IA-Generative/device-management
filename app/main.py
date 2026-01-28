from __future__ import annotations

import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

import uvicorn

from .settings import settings
from .s3 import s3_client

app = FastAPI(title="Device Management API", version="0.1.0")

# ---- CORS
origins = [o.strip() for o in settings.allow_origins.split(",")] if settings.allow_origins else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)

MAX_BODY_BYTES = settings.max_body_size_mb * 1024 * 1024


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


_TEMPLATE_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _repo_root() -> str:
    # app/ is a package folder; repo root is one level above
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_config_template(profile: str) -> dict:
    """Load a config template JSON from `exemple/`.

    Resolution order:
    - config/config.<profile>.json
    - config/config.json
    """
    base = os.path.join(_repo_root(), "config")
    candidates = [
        os.path.join(base, f"config.{profile}.json"),
        os.path.join(base, "config.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError("No config template found in ./config (expected config.json)")


def _substitute_env_in_str(value: str) -> str:
    """Replace ${VARNAME} with os.environ['VARNAME'] if set, else keep empty string."""

    def repl(m: re.Match[str]) -> str:
        var = m.group(1)
        return os.getenv(var, "")

    return _TEMPLATE_VAR_RE.sub(repl, value)


def _substitute_env(obj):
    """Recursively substitute env vars in any string values."""
    if isinstance(obj, dict):
        return {k: _substitute_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_env(v) for v in obj]
    if isinstance(obj, str):
        return _substitute_env_in_str(obj)
    return obj


def _apply_overrides(cfg: dict) -> dict:
    """Apply targeted overrides from env (e.g., telemetrySel)."""
    # Keep existing structure but allow overriding config.telemetrySel from env.
    telemetry_sel = os.getenv("DM_TELEMETRY_SEL") or os.getenv("TELEMETRY_SEL")
    if telemetry_sel:
        cfg.setdefault("config", {})
        if isinstance(cfg["config"], dict):
            cfg["config"]["telemetrySel"] = telemetry_sel
    return cfg


@app.get("/healthz")
def healthz():
    errors: list[str] = []

    if settings.store_enroll_locally:
        try:
            _ensure_dir(settings.enroll_dir)
            test_path = os.path.join(settings.enroll_dir, ".write_test")
            with open(test_path, "wb") as f:
                f.write(b"ok")
            os.remove(test_path)
        except Exception as e:
            errors.append(f"Local enroll_dir not writable: {e!r}")

    if settings.store_enroll_s3 and not settings.s3_bucket:
        errors.append("S3 bucket is not configured (DM_S3_BUCKET missing).")

    if errors:
        return JSONResponse(status_code=412, content={"ok": False, "errors": errors})

    return {"ok": True}



@app.get("/config/config.json")
def get_config(profile: str | None = None):
    """Return remote-config JSON.

    The response is loaded from a static template file under `exemple/` and supports
    placeholder substitution with environment variables using the syntax: ${VARNAME}.

    Profile selection:
    - Request: /config/config.json?profile=dev|prod
    - Default: DM_CONFIG_PROFILE (defaults to "prod")
    """
    prof = (profile or os.getenv("DM_CONFIG_PROFILE", "prod")).strip().lower()
    if prof not in ("dev", "prod"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "profile must be 'dev' or 'prod'"})

    try:
        cfg = _load_config_template(prof)
    except FileNotFoundError as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    # 1) generic substitution ${VARNAME}
    cfg = _substitute_env(cfg)

    # 2) targeted overrides (e.g. telemetrySel)
    cfg = _apply_overrides(cfg)

    # 3) keep top-level enable switch from service settings if you still want a global kill-switch
    cfg["enabled"] = bool(settings.config_enabled)

    return JSONResponse(content=cfg, headers={"Cache-Control": "no-store"})


@app.api_route("/enroll", methods=["POST", "PUT", "OPTIONS"])
async def enroll(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204)

    body = await request.body()
    if len(body) == 0:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Empty body"})
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"ok": False, "error": "Body too large"})

    try:
        json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Body is not valid JSON"})

    epoch_ms = int(time.time() * 1000)
    rid = uuid.uuid4().hex
    fname = f"{epoch_ms}-{rid}.json"

    stored = {}

    if settings.store_enroll_locally:
        _ensure_dir(settings.enroll_dir)
        path = os.path.join(settings.enroll_dir, fname)
        try:
            with open(path, "wb") as f:
                f.write(body)
            stored["local"] = path
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot write local file: {e!r}")

    if settings.store_enroll_s3:
        if not settings.s3_bucket:
            raise HTTPException(status_code=500, detail="S3 bucket not configured (DM_S3_BUCKET).")
        key = f"{settings.s3_prefix_enroll.rstrip('/')}/{fname}"
        try:
            s3 = s3_client()
            s3.put_object(
                Bucket=settings.s3_bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
            stored["s3"] = f"s3://{settings.s3_bucket}/{key}"
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot write to S3: {e!r}")

    return JSONResponse(status_code=201, content={"ok": True, "stored": stored})


@app.get("/binaries/{path:path}")
def get_binary(path: str):
    if not settings.s3_bucket:
        raise HTTPException(status_code=500, detail="S3 bucket not configured (DM_S3_BUCKET).")

    key = f"{settings.s3_prefix_binaries.rstrip('/')}/{path.lstrip('/')}"
    s3 = s3_client()

    if settings.binaries_mode == "presign":
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.s3_bucket, "Key": key},
                ExpiresIn=settings.presign_ttl_seconds,
            )
            return RedirectResponse(url=url, status_code=302)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Binary not found or cannot presign: {e!r}")

    if settings.binaries_mode == "proxy":
        try:
            obj = s3.get_object(Bucket=settings.s3_bucket, Key=key)
            body_stream = obj["Body"]
            content_type = obj.get("ContentType") or "application/octet-stream"

            def iterfile():
                for chunk in iter(lambda: body_stream.read(1024 * 1024), b""):
                    yield chunk

            return StreamingResponse(iterfile(), media_type=content_type)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Binary not found: {e!r}")

    raise HTTPException(status_code=500, detail="Invalid DM_BINARIES_MODE (must be presign or proxy).")

# ---- Local entrypoint (VS Code friendly)
# Allows debugging by running this file directly (e.g. VS Code: "Python: Current File").
# In production, prefer: uvicorn app.main:app --host 0.0.0.0 --port 8088

def _get_port() -> int:
    try:
        return int(os.getenv("PORT", "8088"))
    except ValueError:
        return 8088


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=_get_port(),
        reload=os.getenv("RELOAD", "false").lower() in ("1", "true", "yes"),
        log_level=os.getenv("LOG_LEVEL", "info"),
    )
