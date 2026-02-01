#!/usr/bin/env sh
set -eu

AUTH_URL="https://openwebui-sso.fake-domain.name/realms/openwebui/protocol/openid-connect/auth"
TOKEN_URL="https://openwebui-sso.fake-domain.name/realms/openwebui/protocol/openid-connect/token"
CLIENT_ID="boostrap-iasssistant"
REDIRECT_URI="${REDIRECT_URI:-http://localhost:28443/callback}"

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

export CODE_VERIFIER

echo "Open this URL in your browser and login:"
echo "${AUTH_URL}?response_type=code&client_id=${CLIENT_ID}&redirect_uri=$(python - <<PY
import urllib.parse
print(urllib.parse.quote('${REDIRECT_URI}', safe=''))
PY
)&scope=openid%20email&code_challenge_method=S256&code_challenge=${CODE_CHALLENGE}"

echo ""
printf "Paste the code here: "
read CODE

echo ""
echo "Exchanging code for tokens..."
curl -sS -X POST "${TOKEN_URL}" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "client_id=${CLIENT_ID}" \
  -d "redirect_uri=${REDIRECT_URI}" \
  -d "code=${CODE}" \
  -d "code_verifier=${CODE_VERIFIER}"
echo ""
