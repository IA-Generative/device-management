"""PoolConn n'emprunte une connexion qu'à l'entrée du `with` et la rend toujours.

Régression : l'emprunt à la construction retenait une connexion chaque fois qu'un
appelant créait le contexte sans l'ouvrir (get_config sans device) — pool épuisé,
/config et /catalog en 500.
"""
from app.services.db import PoolConn


class _Conn:
    autocommit = False


class _Pool:
    def __init__(self):
        self.out = 0

    def getconn(self):
        self.out += 1
        return _Conn()

    def putconn(self, _conn):
        self.out -= 1


def test_construction_seule_n_emprunte_rien():
    pool = _Pool()
    for _ in range(50):
        PoolConn(pool)
    assert pool.out == 0


def test_with_emprunte_et_rend():
    pool = _Pool()
    with PoolConn(pool) as conn:
        assert pool.out == 1
        assert conn.autocommit is True
    assert pool.out == 0


def test_rend_meme_sur_exception():
    pool = _Pool()
    try:
        with PoolConn(pool):
            raise ValueError("boom")
    except ValueError:
        pass
    assert pool.out == 0


def test_enter_exit_explicites():
    pool = _Pool()
    ctx = PoolConn(pool)
    ctx.__enter__()
    assert pool.out == 1
    ctx.__exit__(None, None, None)
    ctx.__exit__(None, None, None)
    assert pool.out == 0


def test_exit_sans_enter_ne_rend_rien():
    pool = _Pool()
    PoolConn(pool).__exit__(None, None, None)
    assert pool.out == 0
