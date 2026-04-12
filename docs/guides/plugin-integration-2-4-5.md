# Plugin Integration 2-4-5 (Bootstrap, Telemetry pre-login, SSO PKCE)

This document focuses only on:
- `2)` fetch minimal config
- `4)` telemetry pre-login
- `5)` SSO login with Keycloak PKCE

Scope:
- LibreOffice plugin client implementation
- current Device Management endpoints already available

## 2) Fetch minimal config (`/bootstrap/config/libreoffice/config.json`)

### Endpoint
```
GET /bootstrap/config/libreoffice/config.json?profile=prod
```

### Goal
- Bootstrap the plugin with minimum runtime settings.
- Get telemetry settings (`telemetryEnabled`, `telemetryEndpoint`, `telemetryAuthorizationType`, `telemetryKey`).

### Client behavior
1. Call config on startup.
2. If response is valid JSON:
   - cache it in memory
   - keep `telemetryKey` if present and not expired
3. If request fails:
   - fallback to last cached config (if any)
   - disable telemetry send until next successful refresh

### Minimal pseudo-code
```text
cfg = GET /bootstrap/config/libreoffice/config.json?profile=prod
if ok:
  state.config = cfg
  state.telemetry.endpoint = cfg.config.telemetryEndpoint
  state.telemetry.auth_type = cfg.config.telemetryAuthorizationType
  state.telemetry.token = cfg.config.telemetryKey
else:
  use cached cfg if available
```

## 4) Telemetry pre-login

### Principle
- Before SSO login, send only technical telemetry (no user content).
- Use short-lived Bearer token from Device Management.

### Token refresh endpoint
```
GET /bootstrap/telemetry/token?device=libreoffice&profile=prod
```

Expected response shape:
```json
{
  "telemetryEnabled": true,
  "telemetryEndpoint": "https://<host>/telemetry/v1/traces",
  "telemetryAuthorizationType": "Bearer",
  "telemetryKey": "<token>",
  "telemetryKeyExpiresAt": 1769961300,
  "telemetryKeyTtlSeconds": 300
}
```

### Send telemetry
```
POST https://<host>/telemetry/v1/traces
Authorization: Bearer <telemetryKey>
Content-Type: application/json or application/x-protobuf
```

### Rotation strategy (recommended)
1. If token missing or expires in less than 30s, call `/bootstrap/telemetry/token`.
2. On `401/403` from telemetry relay:
   - refresh token once
   - retry once
3. If still failing:
   - queue locally
   - retry with exponential backoff

### Pre-login event policy
- Allowed examples:
  - plugin start/stop
  - config fetch status
  - telemetry transport errors
- Not allowed pre-login:
  - email content
  - document text
  - full user identifiers in clear

## 5) SSO login with Keycloak PKCE

### Principle
- Public client, Authorization Code + PKCE.
- No client secret in plugin.

### Flow
1. Generate `code_verifier`.
2. Derive `code_challenge` (`S256`).
3. Open browser to Keycloak authorize endpoint.
4. Receive `code` on localhost callback.
5. Exchange `code + code_verifier` for tokens.
6. Store refresh token in OS secure storage.
7. Keep access token in memory only.

### Minimal pseudo-code
```text
pkce = generate_pkce()
open_browser(authorize_url(pkce.challenge))
code = wait_local_callback()
tokens = exchange_code_for_tokens(code, pkce.verifier)
store_refresh_token_securely(tokens.refresh_token)
state.sso.access_token = tokens.access_token
```

### After login
- Keep telemetry flow identical, but user is now authenticated in plugin.
- If your backend later exposes `/bootstrap/identity/bind`, call it after login to bind `plugin_uuid <-> keycloak sub`.

## Copy/Paste prompt for plugin dev (2/4/5 only)

```text
Implement only steps 2, 4 and 5 for the LibreOffice plugin:

2) Bootstrap config:
- GET /bootstrap/config/libreoffice/config.json?profile=prod
- read telemetryEnabled, telemetryEndpoint, telemetryAuthorizationType, telemetryKey
- fallback to cached config on transient failures

4) Telemetry pre-login:
- send only technical events
- if telemetry token missing/near expiry, GET /bootstrap/telemetry/token
- POST traces to /telemetry/v1/traces with Bearer token
- on 401/403: refresh once and retry once, then queue and backoff

5) SSO PKCE:
- implement Authorization Code + PKCE
- no client secret in plugin
- store refresh token in OS secure storage
- keep access token in memory

Security:
- never log tokens
- handle clock skew
- sanitize payloads before send
```
