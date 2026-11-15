#!/usr/bin/env python3
"""Faux GitHub : sert les releases d'un dépôt et leurs fichiers depuis un dossier.

Pour essayer le tirage des releases (issue #108) sans accès à GitHub : pointer le
DM dessus avec `DM_GITHUB_API_URL=http://<hôte>:<port>`. Bibliothèque standard
seulement.

    python3 scripts/faux_github.py --dir ./releases --port 8765

Arborescence servie (un dossier par tag, les fichiers du dossier sont ses artefacts) ::

    releases/
      IA-Generative/iassistant-matisse/
        IAssistant-tb60-v0.24.0/
          mirai-matisse-0.24.0.xpi
          mirai-matisse-0.24.0.xpi.sig.json
          release.json                 (facultatif)
        IAssistant-tb60-v0.25.0-rc.1/
          mirai-matisse-0.25.0-rc.1.xpi

`release.json` (facultatif) précise la release : `{"prerelease": true,
"draft": false, "body": "notes…", "published_at": "2026-11-01T10:00:00Z"}`.
Sans lui : `prerelease` vaut vrai si la version du tag porte un suffixe
(« -rc.1 »), la date est celle du dossier, les notes sont vides.

Routes (celles dont le DM se sert) :

- `GET /repos/{org}/{depot}/releases?per_page=&page=` : liste paginée (en-tête Link) ;
- `GET /repos/{org}/{depot}/releases/assets/{id}` : métadonnées, ou — avec
  `Accept: application/octet-stream` — redirection 302 vers le fichier, comme GitHub ;
- `GET /fichiers/{org}/{depot}/{tag}/{nom}` : les octets.

Options de panne, pour voir le DM se dégrader : `--jeton X` exige
`Authorization: Bearer X` (401 sinon) ; `--panne 403` répond ce code à tout ;
`--lenteur 30` attend avant de répondre (délai dépassé côté DM).
"""
from __future__ import annotations

import argparse
import json
import re
import time
import zlib
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_SUFFIXED = re.compile(r"v?\d+(?:\.\d+)*-")


def _asset_id(repo: str, tag: str, name: str) -> int:
    """Identifiant stable d'un fichier (le même d'un démarrage à l'autre)."""
    return zlib.crc32(f"{repo}/{tag}/{name}".encode()) or 1


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_releases(root: Path, repo: str, base_url: str) -> list[dict]:
    """Releases du dépôt `org/depot`, la plus récemment publiée d'abord."""
    repo_dir = root / repo
    releases = []
    if not repo_dir.is_dir():
        return releases
    for tag_dir in repo_dir.iterdir():
        if not tag_dir.is_dir() or not _SEGMENT.fullmatch(tag_dir.name):
            continue
        tag = tag_dir.name
        meta = {}
        meta_path = tag_dir / "release.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        version_part = tag.rsplit("-v", 1)[-1] if "-v" in tag else tag
        published = meta.get("published_at") or _iso(tag_dir.stat().st_mtime)
        assets = []
        for path in sorted(tag_dir.iterdir()):
            if not path.is_file() or path.name == "release.json" or path.name.startswith("."):
                continue
            asset_id = _asset_id(repo, tag, path.name)
            assets.append({
                "id": asset_id, "name": path.name, "size": path.stat().st_size, "state": "uploaded",
                "content_type": "application/octet-stream",
                "url": f"{base_url}/repos/{repo}/releases/assets/{asset_id}",
                "browser_download_url": f"{base_url}/fichiers/{repo}/{quote(tag)}/{quote(path.name)}",
            })
        releases.append({
            "id": _asset_id(repo, tag, ""), "tag_name": tag, "name": meta.get("name") or tag,
            "draft": bool(meta.get("draft", False)),
            "prerelease": bool(meta.get("prerelease", bool(_SUFFIXED.match(version_part)))),
            "body": meta.get("body") or "",
            "created_at": published, "published_at": published,
            "html_url": f"{base_url}/{repo}/releases/tag/{quote(tag)}",
            "assets": assets,
        })
    releases.sort(key=lambda r: r["published_at"], reverse=True)
    return releases


def make_handler(root: Path, *, token: str = "", failure: int = 0, delay: float = 0.0,
                 public_url: str = ""):
    class Handler(BaseHTTPRequestHandler):
        server_version = "faux-github/1"

        def log_message(self, fmt, *args):  # journal sobre, sans en-têtes
            print(f"[faux-github] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

        def _base(self) -> str:
            return public_url.rstrip("/") or f"http://{self.headers.get('Host', 'localhost')}"

        def _send(self, status: int, body: bytes, content_type: str = "application/json",
                  headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload, headers: dict | None = None) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), headers=headers)

        def do_GET(self):  # noqa: N802 (nom imposé par http.server)
            if delay:
                time.sleep(delay)
            if failure:
                return self._json(failure, {"message": f"panne simulée ({failure})"})
            url = urlsplit(self.path)
            parts = [unquote(p) for p in url.path.split("/") if p]
            if any(not _SEGMENT.fullmatch(p) for p in parts):
                return self._json(404, {"message": "Not Found"})

            # Les octets : pas de jeton exigé, comme l'adresse signée où GitHub redirige.
            if len(parts) == 5 and parts[0] == "fichiers":
                path = root / parts[1] / parts[2] / parts[3] / parts[4]
                if not path.is_file():
                    return self._json(404, {"message": "Not Found"})
                return self._send(200, path.read_bytes(), "application/octet-stream")

            if token and self.headers.get("Authorization") != f"Bearer {token}":
                return self._json(401, {"message": "Bad credentials"})
            if len(parts) < 4 or parts[0] != "repos" or parts[3] != "releases":
                return self._json(404, {"message": "Not Found"})
            repo = f"{parts[1]}/{parts[2]}"
            if not (root / repo).is_dir():
                return self._json(404, {"message": "Not Found"})
            releases = list_releases(root, repo, self._base())

            if len(parts) == 4:
                query = parse_qs(url.query)
                try:
                    per_page = max(1, min(100, int(query.get("per_page", ["30"])[0])))
                    page = max(1, int(query.get("page", ["1"])[0]))
                except ValueError:
                    return self._json(422, {"message": "Validation Failed"})
                start = (page - 1) * per_page
                headers = {}
                if start + per_page < len(releases):
                    headers["Link"] = (f'<{self._base()}/repos/{repo}/releases'
                                       f'?per_page={per_page}&page={page + 1}>; rel="next"')
                return self._json(200, releases[start:start + per_page], headers)

            if len(parts) == 6 and parts[4] == "assets" and parts[5].isdigit():
                for release in releases:
                    for asset in release["assets"]:
                        if asset["id"] == int(parts[5]):
                            if "application/octet-stream" in self.headers.get("Accept", ""):
                                return self._send(302, b"", headers={"Location": asset["browser_download_url"]})
                            return self._json(200, asset)
            return self._json(404, {"message": "Not Found"})

    return Handler


def serve(directory: str, *, host: str = "127.0.0.1", port: int = 0, **options) -> ThreadingHTTPServer:
    """Serveur prêt à tourner (`serve_forever`) ; `port=0` laisse le système en choisir un."""
    return ThreadingHTTPServer((host, port), make_handler(Path(directory).resolve(), **options))


def main() -> None:
    parser = argparse.ArgumentParser(description="Faux GitHub pour le tirage des releases (issue #108).")
    parser.add_argument("--dir", required=True, help="dossier racine : <org>/<depot>/<tag>/<fichiers>")
    parser.add_argument("--host", default="127.0.0.1", help="adresse d'écoute (0.0.0.0 dans un conteneur)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--jeton", default="", help="exige « Authorization: Bearer <jeton> » (401 sinon)")
    parser.add_argument("--panne", type=int, default=0, help="répond ce code HTTP à toute requête")
    parser.add_argument("--lenteur", type=float, default=0.0, help="secondes d'attente avant de répondre")
    parser.add_argument("--url-publique", default="",
                        help="adresse sous laquelle le DM joint ce serveur (défaut : en-tête Host)")
    args = parser.parse_args()
    server = serve(args.dir, host=args.host, port=args.port, token=args.jeton, failure=args.panne,
                   delay=args.lenteur, public_url=args.url_publique)
    print(f"[faux-github] {args.dir} servi sur http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
