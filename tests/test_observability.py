"""Tests d'observabilité : l'audit doit aussi sortir sur stdout.

Avant : admin_audit_log n'écrivait qu'en base → invisible dans `kubectl logs`.
Désormais audit_log() émet aussi une ligne structurée sur le logger applicatif,
sans divulguer le payload sensible.
"""

import logging
from unittest.mock import MagicMock

from app.admin.helpers import audit_log


def test_audit_log_emits_stdout_line(caplog):
    cur = MagicMock()
    actor = {"email": "auditeur@example.gov", "sub": "kc-sub-123"}
    with caplog.at_level(logging.INFO, logger="dm-admin"):
        audit_log(cur, actor=actor, action="catalog.suggest",
                  resource_type="catalog", resource_id="llm", ip="203.0.113.7")

    # L'INSERT DB a bien été tenté
    assert cur.execute.called
    # Et une ligne d'audit lisible est émise sur stdout
    lines = [r.getMessage() for r in caplog.records if "audit action=" in r.getMessage()]
    assert lines, "audit_log doit émettre une ligne stdout"
    msg = lines[-1]
    assert "action=catalog.suggest" in msg
    assert "actor=auditeur@example.gov" in msg
    assert "ip=203.0.113.7" in msg


def test_audit_log_does_not_leak_payload(caplog):
    cur = MagicMock()
    actor = {"email": "a@b.c", "sub": "s"}
    secret_payload = {"token": "SUPER-SECRET-VALUE"}
    with caplog.at_level(logging.INFO, logger="dm-admin"):
        audit_log(cur, actor=actor, action="x", resource_type="y",
                  resource_id="z", payload=secret_payload)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "SUPER-SECRET-VALUE" not in joined
