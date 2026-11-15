"""Clés d'API personnelles — parties pures (forme de la clé, portées, saisie)."""
from __future__ import annotations

import pytest

from app.services import api_keys


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def test_token_is_read_from_bearer_or_from_the_service_header():
    key = "dmk_0123456789ab_" + "A" * 43
    assert api_keys.token_from_headers(_Headers({"authorization": f"Bearer {key}"})) == key
    assert api_keys.token_from_headers(_Headers({"x-admin-token": key})) == key
    # Le jeton de service et les jetons d'un autre système ne sont pas pris pour une clé.
    assert api_keys.token_from_headers(_Headers({"x-admin-token": "jeton-de-service"})) == ""
    assert api_keys.token_from_headers(_Headers({"authorization": "Bearer eyJhbGciOi"})) == ""
    assert api_keys.token_from_headers(_Headers({})) == ""


def test_scopes_write_implies_read_and_unknown_is_refused():
    assert api_keys.normalize_scopes(["write"]) == ["read", "write"]
    assert api_keys.normalize_scopes("read") == ["read"]
    for bad in ([], "", "admin", ["read", "tout"]):
        with pytest.raises(api_keys.ApiKeyInputError):
            api_keys.normalize_scopes(bad)


def test_plugins_are_a_clean_list_of_catalogue_names():
    assert api_keys.normalize_plugins(" mirai-matisse, mirai-libreoffice\nmirai-matisse ") == \
        ["mirai-libreoffice", "mirai-matisse"]
    assert api_keys.normalize_plugins("") == []
    with pytest.raises(api_keys.ApiKeyInputError):
        api_keys.normalize_plugins("../etc")


class _NoDb:
    """Aucune requête ne doit partir pour une clé mal formée."""
    def execute(self, *a, **k):
        raise AssertionError("pas de requête attendue")


@pytest.mark.parametrize("token", ["", "dmk_", "dmk_xyz_secret", "dmk_0123456789ab_tropcourt",
                                   "autre_0123456789ab_" + "A" * 43, "dmk_0123456789AB_" + "A" * 43])
def test_a_malformed_key_is_refused_without_touching_the_database(token):
    assert api_keys.authenticate(_NoDb(), token) == (None, api_keys.INVALID)


@pytest.mark.parametrize("bad", ["", "abc", "0", "-3", str(api_keys.MAX_TTL_DAYS + 1), None])
def test_an_expiry_is_always_between_one_day_and_the_maximum(bad):
    with pytest.raises(api_keys.ApiKeyInputError):
        api_keys.expiry_in(bad)


def test_expiry_counts_from_now():
    import datetime
    now = datetime.datetime.now(datetime.UTC)
    delta = api_keys.expiry_in("30") - now
    assert datetime.timedelta(days=29, hours=23) < delta <= datetime.timedelta(days=30, seconds=5)
