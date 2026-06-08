#!/usr/bin/env python3
"""Teste des clés API Scaleway (LLM, endpoint OpenAI-compatible) contre l'API réelle.

Pour chaque candidat de secret :
  1. GET  {base}/models           -> auth : 200 = clé VALIDE, 401/403 = INVALIDE
  2. POST {base}/chat/completions -> inférence réelle (optionnel, flag --chat)

Les secrets ne sont JAMAIS passés en argument de ligne de commande
(ils seraient visibles dans `ps` et l'historique shell). Sources acceptées :
  - --file <chemin> : un token par ligne (lignes vides / commençant par # ignorées)
  - pipe stdin      : un token par ligne
  - sinon           : prompt interactif masqué (getpass), un par ligne, fin = ligne vide

La base URL et le modèle sont lus depuis deploy/docker/.env par défaut, ou via
--base-url / --model, ou les variables d'env LLM_BASE_URL / DEFAULT_MODEL_NAME.

Exemples :
  scripts/test-scaleway-key.py                      # prompt masqué, test auth
  scripts/test-scaleway-key.py --chat               # + test d'inférence
  printf '%s\n' "$CANDIDAT" | scripts/test-scaleway-key.py --chat
  scripts/test-scaleway-key.py --file candidats.txt

Codes de sortie : 0 si au moins une clé valide, 1 sinon, 2 si erreur d'usage.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.scaleway.ai/a9158aac-8404-46ea-8bf5-1ca048cd6ab4/v1"
DEFAULT_MODEL = "gpt-oss-120b"


def _load_env_file(path: str) -> dict:
    """Lecture minimale d'un fichier .env (KEY=VALUE), sans dépendance."""
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _mask(token: str) -> str:
    """Masque un secret pour l'affichage : 6 premiers + 2 derniers caractères."""
    token = token.strip()
    if len(token) <= 10:
        return (token[:2] + "…") if token else "(vide)"
    return f"{token[:6]}…{token[-2:]} (len={len(token)})"


def _request(method: str, url: str, token: str, timeout: float, body: bytes | None = None):
    """Retourne (status:int, payload:str). status=0 si erreur réseau (payload=raison)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode("utf-8", "replace")
        except Exception:
            payload = ""
        return e.code, payload
    except urllib.error.URLError as e:
        return 0, f"URLError: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def check_models(base: str, token: str, timeout: float):
    """GET /models. Retourne (verdict:str, ok:bool, detail:str)."""
    status, payload = _request("GET", base.rstrip("/") + "/models", token, timeout)
    if status == 200:
        n = "?"
        try:
            data = json.loads(payload)
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                n = len(data["data"])
        except Exception:
            pass
        return "VALIDE", True, f"HTTP 200, {n} modèle(s)"
    if status in (401, 403):
        return "INVALIDE", False, f"HTTP {status} (auth refusée)"
    if status == 0:
        return "ERREUR RÉSEAU", False, payload
    return f"INATTENDU", False, f"HTTP {status} : {payload[:200]}"


def check_chat(base: str, token: str, model: str, timeout: float):
    """POST /chat/completions minimal. Retourne (ok:bool, detail:str)."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }).encode("utf-8")
    status, payload = _request(
        "POST", base.rstrip("/") + "/chat/completions", token, timeout, body
    )
    if status == 200:
        return True, "HTTP 200 (inférence OK)"
    if status in (401, 403):
        return False, f"HTTP {status} (auth refusée)"
    if status == 404:
        return False, f"HTTP 404 (modèle '{model}' introuvable ?)"
    if status == 0:
        return False, payload
    return False, f"HTTP {status} : {payload[:200]}"


def collect_tokens(file_path: str | None) -> list[str]:
    """Lit les tokens depuis --file, sinon stdin (pipe), sinon prompt masqué."""
    def _parse(lines):
        out = []
        for ln in lines:
            ln = ln.strip()
            if ln and not ln.startswith("#") and ln not in out:
                out.append(ln)
        return out

    if file_path:
        try:
            with open(file_path, encoding="utf-8") as f:
                return _parse(f.readlines())
        except FileNotFoundError:
            print(f"Erreur : fichier introuvable : {file_path}", file=sys.stderr)
            print("Astuce : crée-le (une clé par ligne) ou utilise le prompt "
                  "interactif (sans --file) ou un pipe stdin.", file=sys.stderr)
            sys.exit(2)
        except OSError as e:
            print(f"Erreur de lecture de {file_path} : {e}", file=sys.stderr)
            sys.exit(2)

    if not sys.stdin.isatty():
        return _parse(sys.stdin.readlines())

    print("Entre les clés à tester (saisie masquée), une par ligne. Ligne vide pour terminer.",
          file=sys.stderr)
    tokens: list[str] = []
    while True:
        try:
            t = getpass.getpass(f"  clé #{len(tokens) + 1} > ")
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break
        if not t.strip():
            break
        if t.strip() not in tokens:
            tokens.append(t.strip())
    return tokens


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    env = _load_env_file(os.path.join(here, "..", "deploy", "docker", ".env"))

    ap = argparse.ArgumentParser(description="Teste des clés API Scaleway (LLM).")
    ap.add_argument("--base-url",
                    default=os.getenv("LLM_BASE_URL") or env.get("LLM_BASE_URL") or DEFAULT_BASE_URL,
                    help="Base URL de l'API (défaut: .env LLM_BASE_URL)")
    ap.add_argument("--model",
                    default=os.getenv("DEFAULT_MODEL_NAME") or env.get("DEFAULT_MODEL_NAME") or DEFAULT_MODEL,
                    help="Modèle pour le test --chat (défaut: .env DEFAULT_MODEL_NAME)")
    ap.add_argument("--file", help="Fichier de tokens (un par ligne)")
    ap.add_argument("--chat", action="store_true",
                    help="Teste aussi une vraie inférence (POST /chat/completions)")
    ap.add_argument("--timeout", type=float, default=15.0, help="Timeout réseau (s)")
    args = ap.parse_args()

    tokens = collect_tokens(args.file)
    if not tokens:
        print("Aucune clé fournie.", file=sys.stderr)
        return 2

    print(f"\nEndpoint : {args.base_url}")
    if args.chat:
        print(f"Modèle   : {args.model}")
    print(f"Clés     : {len(tokens)}\n")

    valid = 0
    for i, tok in enumerate(tokens, 1):
        verdict, ok, detail = check_models(args.base_url, tok, args.timeout)
        mark = "✅" if ok else "❌"
        print(f"[{i}/{len(tokens)}] {mark} {verdict:14s} {_mask(tok)} — /models: {detail}")
        if ok:
            valid += 1
            if args.chat:
                chat_ok, chat_detail = check_chat(args.base_url, tok, args.model, args.timeout)
                cmark = "✅" if chat_ok else "⚠️ "
                print(f"            {cmark} /chat/completions: {chat_detail}")

    print(f"\nRésultat : {valid}/{len(tokens)} clé(s) valide(s).")
    return 0 if valid > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
