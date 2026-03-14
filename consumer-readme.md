# Client Integration README

This document is for developers integrating with the Device Management API.

Dedicated guide for steps 2/4/5 (bootstrap config, telemetry pre-login, SSO PKCE):
- `plugin-integration-2-4-5.md`

## High-Level Flow (Ideal Sequence)
1) Fetch configuration parameters from the API.
2) Enroll the user in Keycloak.
3) Obtain an access token.
4) Validate the token’s email and store the token in the plugin preferences.
5) Enroll the plugin instance in the Device Management backend with:
   - plugin identifier
   - plugin UUID
   - user email

## Endpoints

### 1) Fetch configuration
```
GET /config/config.json
GET /config/config.json?profile=dev|prod|int
GET /config/<device>/config.json
GET /config/<device>/config.json?profile=dev|prod|int
```
Response contains client settings such as:
- `updateUrl`
- `config.owuiEndpoint`
- `config.owuiModel`
- `config.tokenOWUI` (if configured)
- `config.keycloakIssuerUrl`
- `config.keycloakRealm`
- `config.keycloakClientId`

### 2) Enroll the plugin (backend)
```
POST /enroll
Content-Type: application/json
```
Example payload:
```json
{
  "plugin_id": "matisse",
  "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
  "email": "user@example.com"
}
```
Expected response:
```json
{ "ok": true, "stored": { "local": "...", "s3": "s3://..." } }
```

## Proposed Secure Flow (V2, Recommended)

This section proposes a robust flow that keeps pre-login telemetry while preventing abuse.

### Lifecycle
1) Plugin bootstraps with minimal config (`/config/...`), no long-lived secret.
2) Plugin performs anonymous enroll with `plugin_uuid` + public key.
3) Backend returns a challenge.
4) Plugin signs challenge with local private key and confirms enroll.
5) Backend issues a short-lived telemetry token (`preauth`, low scope).
6) User logs in with Keycloak PKCE.
7) Plugin binds user identity (`sub`) to the enrolled device.
8) Backend issues normal telemetry token (`user` scope), rotated periodically.

### Proposed Endpoints and Contracts

#### A) Start anonymous enroll
```
POST /enroll
Content-Type: application/json
```
Request:
```json
{
  "device_name": "libreoffice",
  "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
  "public_key": "<base64-ed25519-public-key>",
  "plugin_version": "0.3.0"
}
```
Response:
```json
{
  "ok": true,
  "enroll_id": "a0f9c6c4...",
  "challenge": "2f8f7d4c..."
}
```

#### B) Confirm enroll (proof of key possession)
```
POST /enroll/confirm
Content-Type: application/json
```
Request:
```json
{
  "enroll_id": "a0f9c6c4...",
  "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
  "signature": "<base64-signature-of-challenge>"
}
```
Response:
```json
{
  "ok": true,
  "telemetry": {
    "authorizationType": "Bearer",
    "endpoint": "https://<host>/telemetry/v1/traces",
    "token": "<short-lived-preauth-token>",
    "expiresAt": 1769961000,
    "ttlSeconds": 120
  }
}
```

#### C) Bind authenticated user to enrolled device
```
POST /identity/bind
Authorization: Bearer <Keycloak access token>
Content-Type: application/json
```
Request:
```json
{
  "plugin_uuid": "b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a",
  "nonce": "7c8a2...",
  "signature": "<base64-signature-of-nonce>"
}
```
Response:
```json
{
  "ok": true,
  "subject": "<keycloak-sub>",
  "telemetry": {
    "authorizationType": "Bearer",
    "endpoint": "https://<host>/telemetry/v1/traces",
    "token": "<short-lived-user-token>",
    "expiresAt": 1769961300,
    "ttlSeconds": 300
  }
}
```

#### D) Rotate telemetry token
```
GET /telemetry/token?device=libreoffice&profile=prod
```
Behavior:
- Before bind: returns `preauth` token (restricted scope).
- After bind: returns `user` token (normal scope).
- All tokens must be short-lived and non-cacheable.

### Validation Rules (Server Side)
- Reject enroll without `plugin_uuid` and valid `public_key`.
- Verify signatures for `/enroll/confirm` and `/identity/bind`.
- For `/identity/bind`, validate Keycloak JWT (`iss`, `aud`, `exp`, `sub`).
- Store `sub` as canonical user identifier (email optional, secondary field).
- Rate-limit by `plugin_uuid + sub + source_ip`.

### Minimal DB fields
- `plugin_uuid` (unique)
- `public_key`
- `status`: `PENDING|ANON_ENROLLED|USER_BOUND|REVOKED`
- `subject` (Keycloak sub)
- `email_hmac` (optional, privacy-safe lookup)
- `last_seen_at`, `created_at`, `updated_at`

## Prompt Template For LibreOffice Plugin

Use this prompt with the developer implementing the plugin:

```text
Implement a secure bootstrap/enroll/telemetry flow in the LibreOffice plugin.

Requirements:
1) On first run, generate and persist:
   - plugin_uuid (stable)
   - ed25519 keypair (private key in OS secure storage)
2) Fetch minimal config from /bootstrap/config/libreoffice/config.json.
3) Anonymous enroll:
   - POST /bootstrap/enroll (plugin_uuid, device_name, public_key)
   - receive enroll_id + challenge
   - sign challenge with private key
   - POST /bootstrap/enroll/confirm
   - store telemetry preauth token (short TTL)
4) Telemetry pre-login:
   - send only technical events
   - rotate token via /bootstrap/telemetry/token
5) SSO login with Keycloak PKCE.
6) Identity binding:
   - POST /bootstrap/identity/bind with access_token + signed nonce
   - receive normal telemetry token (user scope)
7) Send telemetry to /telemetry/v1/traces with Bearer token.
8) Token refresh strategy:
   - proactive refresh when expires in <30s
   - on 401/403, refresh once; if still failing, re-run bind/login path
9) Security:
   - never log secrets/tokens
   - handle clock skew
   - retry with exponential backoff + local queue for transient network failures

Deliverables:
- clear module boundaries (auth, enroll, telemetry, storage)
- unit tests for state transitions and token refresh behavior
- robust error handling and user-safe messages
```

## Keycloak Flow (Client Side)
These steps are performed by the client/plugin.

1) Enroll the user in Keycloak.
2) Obtain an access token.
3) Validate that the token email matches the user’s email.
4) Store the token in plugin preferences for subsequent API calls.

> Note: Use your Keycloak realm/client configuration (issuer URL, client id, scopes).

## Recommended Keycloak Setup (Security / Simplicity)

Best compromise for a desktop plugin: **Authorization Code Flow + PKCE** (public client).

### Client Settings
- Client ID: `device-management-plugin`
- Access type: `public`
- Standard Flow: `ON`
- Implicit Flow: `OFF`
- Direct Access Grants (ROPC): `OFF`
- PKCE: `required`
- Redirect URI: `http://localhost:41100/callback` (example)
- Web Origins: `http://localhost:41100` (or `*` if required)

### Token Settings
- Access token lifespan: 10–15 minutes
- Refresh token lifespan: 7–30 days
- Refresh token rotation: **ON**
- Reuse refresh tokens: **OFF**

### Why this is the best compromise
- No client secret stored in the plugin (public client).
- Standard OIDC flow with PKCE (safe against code interception).
- Silent refresh via refresh token.

## Silent Authentication (Refresh Token)

### Pseudo‑flow
```
User opens browser login (Auth Code + PKCE)
-> plugin receives code on redirect URI
-> exchange code for access_token + refresh_token + id_token
-> validate id_token (issuer/audience/signature)
-> verify email + email_verified
-> store refresh_token securely

Later (silent):
-> refresh_token grant to get new access_token
-> if refresh fails, force re-login
```

### Secure Storage (per OS)
- Windows: Credential Manager
- macOS: Keychain
- Linux: Secret Service (libsecret)

Avoid storing tokens in plain text.

## OpenWebUI Integration (Silent)
Use the refreshed access token when calling OpenWebUI.  
If refresh fails, prompt user to re-authenticate.

## Suggested Client Sequence (Pseudo)
```text
GET /config/config.json
-> read endpoints + settings

Keycloak: login/enroll user
-> get access token
-> verify token email
-> store token in plugin preferences

POST /enroll
-> body: plugin_id, plugin_uuid, email
```

## cURL Examples

### Config (fetch parameters)
```
curl -sS https://bootstrap.fake-domain.name/config/config.json | python -c 'import json,sys; print(json.load(sys.stdin).get("updateUrl"))'
```

### Enroll (test pass)
```
curl -sS -o /dev/null -w "%{http_code}\n" \
  -H "Content-Type: application/json" \
  -d '{"device_name":"matisse","plugin_uuid":"b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a","email":"user@example.com"}' \
  https://bootstrap.fake-domain.name/enroll
```

### Enroll (test fail)
```
curl -sS -o /dev/null -w "%{http_code}\n" \
  -H "Content-Type: application/json" \
  -d '{"device_name":"","plugin_uuid":"","email":""}' \
  https://bootstrap.fake-domain.name/enroll
```

### Binary (presign or proxy)
Assuming `binaries/ok/ok.png` exists at the S3 bucket root:
```
curl -sS -o /dev/null -D - \
  https://bootstrap.fake-domain.name/binaries/ok/ok.png
```

## Troubleshooting
- `400 Body is not valid JSON`: ensure valid JSON in `/enroll`.
- `500 S3 bucket not configured`: check server env `DM_S3_BUCKET`.
- `401/403`: check Keycloak token and email verification logic.

## Keycloak Client Import (PKCE)

### Import JSON (client public + PKCE)
Create a file named `bootstrap-iassistant-client.json` and import it in the Keycloak console.

```json
{
  "clientId": "bootstrap-iassistant",
  "name": "bootstrap-iassistant",
  "enabled": true,
  "publicClient": true,
  "standardFlowEnabled": true,
  "implicitFlowEnabled": false,
  "directAccessGrantsEnabled": false,
  "serviceAccountsEnabled": false,
  "redirectUris": [
    "http://localhost:28443/callback",
    "http://localhost:28444/callback",
    "http://localhost:28445/callback",
    "http://localhost:28446/callback",
    "http://localhost:28447/callback",
    "http://localhost:28448/callback",
    "http://localhost:28449/callback",
    "http://localhost:28450/callback",
    "http://localhost:28451/callback",
    "http://localhost:28452/callback"
  ],
  "webOrigins": [
    "http://localhost:28443",
    "http://localhost:28444",
    "http://localhost:28445",
    "http://localhost:28446",
    "http://localhost:28447",
    "http://localhost:28448",
    "http://localhost:28449",
    "http://localhost:28450",
    "http://localhost:28451",
    "http://localhost:28452"
  ],
  "attributes": {
    "pkce.code.challenge.method": "S256",
    "pkce.code.challenge.required": "true",
    "post.logout.redirect.uris": "http://localhost:28443/*"
  }
}
```

Realm URL:
`https://openwebui-sso.fake-domain.name/realms/openwebui/`

### Simple PKCE Test (Manual)

1) Generate PKCE values:
```bash
CODE_VERIFIER=$(python - <<'PY'
import os,base64
v = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
print(v)
PY
)

CODE_CHALLENGE=$(python - <<'PY'
import hashlib,base64,os
v = os.environ["CODE_VERIFIER"].encode()
h = hashlib.sha256(v).digest()
print(base64.urlsafe_b64encode(h).decode().rstrip("="))
PY
)
```

2) Open in a browser:
```
https://openwebui-sso.fake-domain.name/realms/openwebui/protocol/openid-connect/auth?response_type=code&client_id=bootstrap-iassistant&redirect_uri=http%3A%2F%2Flocalhost%3A28443%2Fcallback&scope=openid%20email&code_challenge_method=S256&code_challenge=${CODE_CHALLENGE}
```

3) Exchange the code for tokens:
```
curl -sS -X POST \
  https://openwebui-sso.fake-domain.name/realms/openwebui/protocol/openid-connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "client_id=bootstrap-iassistant" \
  -d "redirect_uri=http://localhost:28443/callback" \
  -d "code=${CODE}" \
  -d "code_verifier=${CODE_VERIFIER}"
```

### PKCE Test Script
Use the helper script:
```
keycloak/pkce-test.sh
```
