"""Cloud-native readiness — les binaires ne doivent plus dépendre d'un disque
local (PVC) quand DM_BINARIES_MODE est "presign" ou "proxy" : upload direct
vers S3, et service direct depuis S3 (pas de cache disque, pas de pull-on-miss
vers un pod admin)."""

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.main as m


@pytest.fixture(autouse=True)
def _restore_binaries_mode():
    original = m.settings.binaries_mode
    yield
    m.settings.binaries_mode = original


def test_pull_binary_from_admin_noop_when_not_local(monkeypatch):
    """No PVC-backed admin pod to pull from outside local mode: must not touch
    the network nor the disk, and must return False."""
    m.settings.binaries_mode = "presign"
    called = {"get": False}

    def _fake_get(*a, **kw):
        called["get"] = True
        raise AssertionError("httpx.get must not be called outside local mode")

    monkeypatch.setattr(m.httpx, "get", _fake_get)
    assert m._pull_binary_from_admin("/data/content/binaries/libreoffice/1.0.0_x.oxt") is False
    assert called["get"] is False


def test_serve_binary_path_presign_redirects_without_touching_disk(monkeypatch, tmp_path):
    m.settings.binaries_mode = "presign"
    m.settings.s3_bucket = "my-bucket"

    fake_client = MagicMock()
    fake_client.generate_presigned_url.return_value = "https://s3.example.com/signed-url"
    monkeypatch.setattr(m, "s3_client", lambda: fake_client)

    # Path deliberately doesn't exist on disk — presign mode must not care.
    missing_path = str(tmp_path / "does" / "not" / "exist.oxt")
    response = m._serve_binary_path(missing_path, "plugin-1.0.0.oxt")

    assert response is not None
    assert response.status_code == 302
    fake_client.generate_presigned_url.assert_called_once()
    _, kwargs = fake_client.generate_presigned_url.call_args
    assert kwargs["Params"]["Key"] == missing_path
    assert kwargs["Params"]["Bucket"] == "my-bucket"


def test_serve_binary_path_local_mode_unchanged(tmp_path):
    m.settings.binaries_mode = "local"
    local_file = tmp_path / "plugin-1.0.0.oxt"
    local_file.write_bytes(b"binary-content")

    response = m._serve_binary_path(str(local_file), "plugin-1.0.0.oxt")
    assert response is not None
    assert response.status_code == 200


def test_persist_plugin_binary_uploads_to_s3_without_local_write(monkeypatch, tmp_path):
    m.settings.binaries_mode = "presign"
    m.settings.s3_bucket = "my-bucket"
    monkeypatch.setattr(m.settings, "local_binaries_dir", str(tmp_path / "unused"))

    fake_client = MagicMock()
    monkeypatch.setattr(m, "s3_client", lambda: fake_client)

    ref = m._persist_plugin_binary(b"payload", "libreoffice/1.0.0_x.oxt", "x.oxt", "libreoffice")

    assert ref == f"{m.S3_BINARIES_PREFIX.rstrip('/')}/libreoffice/1.0.0_x.oxt"
    fake_client.put_object.assert_called_once()
    _, kwargs = fake_client.put_object.call_args
    assert kwargs["Bucket"] == "my-bucket"
    assert kwargs["Key"] == ref
    # No local cache directory was created — no PVC dependency.
    assert not (tmp_path / "unused").exists()


def test_persist_plugin_binary_requires_bucket_when_not_local(monkeypatch):
    m.settings.binaries_mode = "proxy"
    monkeypatch.setattr(m.settings, "s3_bucket", None)
    with pytest.raises(RuntimeError, match="DM_S3_BUCKET"):
        m._persist_plugin_binary(b"payload", "libreoffice/1.0.0_x.oxt", "x.oxt", "libreoffice")


def test_healthz_skips_write_test_when_store_enroll_locally_false(monkeypatch):
    """No PVC-backed enroll_dir to write to: /healthz must not touch the disk
    and must report the local_storage check as "skipped", not failed."""
    monkeypatch.setattr(m.settings, "store_enroll_locally", False)
    # Deliberately unwritable/non-existent — proves the write_test was skipped.
    monkeypatch.setattr(m.settings, "enroll_dir", "/nonexistent/enroll/dir")

    res = TestClient(m.app).get("/healthz")

    assert res.status_code == 200
    checks = res.json()["checks"]
    assert checks["local_storage"] == {"status": "skipped"}


# ─── issue #5 — ré-upload d'une même version : le cache disque doit être
# vérifié au service, sinon le pod sert un binaire périmé et le client boucle
# sur « checksum mismatch ». Le chemin s3_path est indexé par NUMÉRO DE VERSION :
# republier le même numéro change la métadonnée en base sans toucher au blob.

def _sha256(data: bytes) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_serve_binary_path_repulls_when_cache_diverges(monkeypatch, tmp_path):
    """Le cas de l'issue : cache = A, base = checksum(B) → re-pull, et ce sont
    les octets de B qui sont servis."""
    m.settings.binaries_mode = "local"
    cached = tmp_path / "plugin-1.0.0.oxt"
    cached.write_bytes(b"binaire-A-perime")
    neuf = b"binaire-B-republie-sous-le-meme-numero"

    pulls = []

    def _fake_pull(path):
        pulls.append(path)
        with open(path, "wb") as f:
            f.write(neuf)
        return True

    monkeypatch.setattr(m, "_pull_binary_from_admin", _fake_pull)

    response = m._serve_binary_path(str(cached), "plugin-1.0.0.oxt", _sha256(neuf))

    assert response is not None
    assert response.status_code == 200
    assert pulls == [str(cached)]
    assert cached.read_bytes() == neuf


def test_serve_binary_path_serves_conforming_cache_without_pull(monkeypatch, tmp_path):
    """Cas nominal : cache conforme → servi tel quel, AUCUN aller-retour vers
    l'admin (sinon le correctif transforme chaque téléchargement en pull)."""
    m.settings.binaries_mode = "local"
    data = b"binaire-conforme"
    cached = tmp_path / "plugin-1.0.0.oxt"
    cached.write_bytes(data)

    def _no_pull(path):
        raise AssertionError("aucun pull ne doit avoir lieu quand le cache est conforme")

    monkeypatch.setattr(m, "_pull_binary_from_admin", _no_pull)

    response = m._serve_binary_path(str(cached), "plugin-1.0.0.oxt", _sha256(data))

    assert response is not None
    assert response.status_code == 200


def test_serve_binary_path_serves_when_checksum_unknown(monkeypatch, tmp_path):
    """`artifacts.checksum` est nullable : sans checksum connu, on sert comme
    avant — on ne casse pas un téléchargement qui marche."""
    m.settings.binaries_mode = "local"
    cached = tmp_path / "plugin-1.0.0.oxt"
    cached.write_bytes(b"vieil-artefact-sans-checksum")

    def _no_pull(path):
        raise AssertionError("aucun pull sans checksum connu")

    monkeypatch.setattr(m, "_pull_binary_from_admin", _no_pull)

    response = m._serve_binary_path(str(cached), "plugin-1.0.0.oxt", None)

    assert response is not None
    assert response.status_code == 200


def test_serve_binary_path_refuses_to_serve_when_repull_does_not_fix(monkeypatch, tmp_path):
    """Le re-pull n'a pas corrigé la divergence : 404 franc plutôt qu'un binaire
    dont on SAIT qu'il est faux (c'est la boucle décrite par l'issue)."""
    m.settings.binaries_mode = "local"
    cached = tmp_path / "plugin-1.0.0.oxt"
    cached.write_bytes(b"binaire-A-perime")

    def _pull_toujours_faux(path):
        with open(path, "wb") as f:
            f.write(b"toujours-le-mauvais-contenu")
        return True

    monkeypatch.setattr(m, "_pull_binary_from_admin", _pull_toujours_faux)

    assert m._serve_binary_path(str(cached), "plugin-1.0.0.oxt", _sha256(b"attendu")) is None


def test_serve_binary_path_verifies_after_pull_on_miss(monkeypatch, tmp_path):
    """Cache absent : le binaire ramené par le pull est vérifié lui aussi."""
    m.settings.binaries_mode = "local"
    absent = tmp_path / "plugin-1.0.0.oxt"

    def _pull_faux(path):
        with open(path, "wb") as f:
            f.write(b"contenu-non-conforme")
        return True

    monkeypatch.setattr(m, "_pull_binary_from_admin", _pull_faux)

    assert m._serve_binary_path(str(absent), "plugin-1.0.0.oxt", _sha256(b"attendu")) is None


def test_file_checksum_memoise_par_chemin_mtime_taille(tmp_path):
    """Le hash est mémoïsé par (chemin, mtime, taille) : tant que le fichier ne
    bouge pas on ne le relit pas, et dès qu'il bouge la mémoïsation est caduque."""
    f = tmp_path / "plugin-1.0.0.oxt"
    f.write_bytes(b"contenu-initial")

    premier = m._file_checksum(str(f))
    assert premier == _sha256(b"contenu-initial")

    # Preuve que l'entrée mémoïsée est bien réutilisée : on l'empoisonne.
    st = os.stat(f)
    m._BINARY_HASH_CACHE[(str(f), st.st_mtime_ns, st.st_size)] = "sha256:temoin"
    assert m._file_checksum(str(f)) == "sha256:temoin"

    # Le fichier change (taille et mtime) → nouvelle clé, hash recalculé.
    f.write_bytes(b"contenu-remplace-plus-long")
    assert m._file_checksum(str(f)) == _sha256(b"contenu-remplace-plus-long")


def test_file_checksum_none_si_fichier_absent(tmp_path):
    assert m._file_checksum(str(tmp_path / "absent.oxt")) is None
