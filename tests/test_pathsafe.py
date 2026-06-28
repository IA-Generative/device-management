"""Tests de la jointure de chemin unifiée (app/pathsafe.py).

Couvre le vecteur path traversal relevé en audit : ``../``, encodages déjà
décodés par le routeur, chemin absolu, symlink sortant, et le bug de frontière
``/base`` vs ``/base-evil`` que les helpers historiques laissaient passer.
"""

import os

import pytest
from fastapi import HTTPException

from app.pathsafe import safe_path_join, safe_segment


# ---------------------------------------------------------------------------
# safe_path_join
# ---------------------------------------------------------------------------

def test_join_nominal(tmp_path):
    base = str(tmp_path)
    result = safe_path_join(base, "libreoffice/1.2.3_app.oxt")
    assert result == os.path.realpath(os.path.join(base, "libreoffice/1.2.3_app.oxt"))
    assert result.startswith(os.path.realpath(base) + os.sep)


def test_join_empty_relative_returns_base(tmp_path):
    base = str(tmp_path)
    assert safe_path_join(base, "") == os.path.realpath(base)


@pytest.mark.parametrize("evil", [
    "../etc/passwd",
    "../../../../etc/passwd",
    "a/../../b",
    "/etc/passwd",            # chemin absolu
    "..%2f..%2fetc",          # déjà décodé côté routeur ; ici littéral, mais doit rester sous base
])
def test_join_blocks_traversal(tmp_path, evil):
    base = str(tmp_path / "binaries")
    os.makedirs(base, exist_ok=True)
    # Soit le chemin sort de base → 400, soit il reste sous base (cas du
    # littéral encodé non décodé) → jamais hors de base.
    try:
        result = safe_path_join(base, evil)
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        assert result == base or result.startswith(os.path.realpath(base) + os.sep)


def test_join_blocks_absolute_escape(tmp_path):
    base = str(tmp_path / "binaries")
    os.makedirs(base, exist_ok=True)
    with pytest.raises(HTTPException) as exc:
        safe_path_join(base, "../secret.txt")
    assert exc.value.status_code == 400


def test_join_blocks_symlink_escape(tmp_path):
    """Cas que les anciens helpers (abspath / startswith) laissaient passer."""
    base = tmp_path / "binaries"
    base.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "passwd").write_text("root:x:0:0")
    # Un symlink dans base pointant hors de base.
    os.symlink(str(secret), str(base / "link"))
    with pytest.raises(HTTPException) as exc:
        safe_path_join(str(base), "link/passwd")
    assert exc.value.status_code == 400


def test_join_boundary_prefix_not_confused(tmp_path):
    """/base ne doit pas autoriser un voisin /base-evil (bug _safe_resolve)."""
    base = tmp_path / "binaries"
    base.mkdir()
    sibling = tmp_path / "binaries-evil"
    sibling.mkdir()
    (sibling / "x").write_text("data")
    with pytest.raises(HTTPException) as exc:
        safe_path_join(str(base), "../binaries-evil/x")
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# safe_segment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "1.2.3_app.oxt",
    "libreoffice",
    "v1.2.3-beta+build.1",
    "chromium-x86_64",
])
def test_segment_accepts_clean(value):
    assert safe_segment(value, "field") == value


@pytest.mark.parametrize("value,expected", [
    ("../x", "x"),
    ("a/b", "b"),
    ("/etc/passwd", "passwd"),
    ("../../etc/passwd", "passwd"),
])
def test_segment_reduces_path_to_safe_basename(value, expected):
    """Un chemin est réduit à son basename (traversée neutralisée), pas conservé."""
    out = safe_segment(value, "field")
    assert out == expected
    assert "/" not in out and ".." not in out


@pytest.mark.parametrize("value", [
    "..",
    "",
    ".hidden",          # ne commence pas par alphanum
    "a\x00b",
    "/",                # basename vide
])
def test_segment_rejects_invalid(value):
    with pytest.raises(HTTPException) as exc:
        safe_segment(value, "field")
    assert exc.value.status_code == 400
