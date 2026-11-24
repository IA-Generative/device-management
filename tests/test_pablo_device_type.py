"""Le plugin Thunderbird 31 `pablo` est un type de plugin connu du DM (issue #62)."""

from app import main as m
from app.admin.router import DEVICE_TYPES


class _DeadCursor:
    """Curseur d'une base injoignable : chaque requête échoue."""

    def execute(self, *_args, **_kwargs):
        raise RuntimeError("database unreachable")


def test_pablo_is_offered_as_a_device_type():
    types = {d["id"]: d for d in DEVICE_TYPES}
    assert types["pablo"]["ext"] == ".xpi"
    assert "matisse" in types  # TB60 reste un plugin distinct


def test_pablo_downloads_as_xpi_not_bin():
    # Le nom de fichier servi par le catalogue en dépend : un .bin n'est pas
    # proposé par le sélecteur « installer depuis un fichier » de Thunderbird.
    assert m._DEVICE_TYPE_EXT.get("pablo", "bin") == "xpi"


def test_pablo_resolves_without_database():
    assert m._resolve_device("pablo", _DeadCursor()) == ("pablo", "pablo", None, "fallback")


def test_unknown_device_still_unknown_without_database():
    assert m._resolve_device("toaster", _DeadCursor()) == (None, None, None, "unknown")
