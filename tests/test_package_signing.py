"""Signature des paquets (ADR-0006) — format, vérification, délégation, configuration."""
from __future__ import annotations

import base64
import datetime
import json

import pytest

from app.services import package_signing as ps
from tests.package_trust_helpers import (
    build_envelope,
    delegation,
    new_key,
    private_pem,
    public_pem,
    statement_of,
)

DATA = b"octets du paquet"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("DM_PACKAGE_SIGNATURE_POLICY", "DM_PACKAGE_PUBLISHER_KEYS", "DM_PACKAGE_ALLOWED_REFS",
                 "DM_PACKAGE_ALLOWED_REPOSITORIES", "DM_PACKAGE_SIGNING_KEY", "DM_PACKAGE_SIGNING_KEY_FILE",
                 "DM_PACKAGE_SIGNING_DELEGATION", "DM_PACKAGE_TRUST_ROOTS", "DM_PACKAGE_SIGNING_REQUIRED"):
        monkeypatch.delenv(name, raising=False)


# ─── Format ─────────────────────────────────────────────────────────────────

def test_signature_is_raw_r_s_over_the_exact_payload_bytes():
    key = new_key()
    envelope = ps.make_envelope({"schema": ps.STATEMENT_SCHEMA, "kind": "build"}, key)
    entry = envelope["signatures"][0]
    assert envelope["schema"] == ps.ENVELOPE_SCHEMA and entry["alg"] == "ES256"
    assert len(base64.b64decode(entry["sig"])) == 64, "r||s brut, pas du DER : c'est ce que lit WebCrypto"
    assert entry["keyid"] == ps.key_id(key.public_key()) and len(entry["keyid"]) == 16
    statement, signer = ps.verify_envelope(envelope, ps.load_public_keys(public_pem(key)))
    assert statement["kind"] == "build" and signer == entry["keyid"]


def test_a_changed_payload_is_refused():
    key = new_key()
    envelope = ps.make_envelope({"schema": ps.STATEMENT_SCHEMA, "kind": "build", "version": "1.0.0"}, key)
    forged = json.loads(base64.b64decode(envelope["payload"]))
    forged["version"] = "9.9.9"
    envelope["payload"] = base64.b64encode(json.dumps(forged).encode()).decode()
    with pytest.raises(ps.SignatureError) as e:
        ps.verify_envelope(envelope, ps.load_public_keys(public_pem(key)))
    assert e.value.code == ps.BAD_SIGNATURE


def test_unknown_key_and_malformed_envelopes_are_told_apart():
    key, other = new_key(), new_key()
    envelope = ps.make_envelope({"schema": ps.STATEMENT_SCHEMA}, key)
    with pytest.raises(ps.SignatureError) as e:
        ps.verify_envelope(envelope, ps.load_public_keys(public_pem(other)))
    assert e.value.code == ps.UNKNOWN_KEY
    for bad in (b"pas du json", {"schema": "autre"}, {"schema": ps.ENVELOPE_SCHEMA},
                {"schema": ps.ENVELOPE_SCHEMA, "payload": "%%%", "signatures": [
                    {"keyid": ps.key_id(key.public_key()), "alg": "ES256", "sig": "AAAA"}]}):
        with pytest.raises(ps.SignatureError) as e:
            ps.verify_envelope(bad, ps.load_public_keys(public_pem(key)))
        assert e.value.code == ps.MALFORMED


def test_only_p256_keys_are_accepted():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    p384 = ec.generate_private_key(ec.SECP384R1())
    pem = p384.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    with pytest.raises(ps.SigningConfigError):
        ps.load_public_keys(pem)


# ─── Signature d'origine ────────────────────────────────────────────────────

def test_verify_build_checks_digest_slug_version_and_kind(monkeypatch):
    key = new_key()
    monkeypatch.setenv("DM_PACKAGE_PUBLISHER_KEYS", public_pem(key))
    envelope = build_envelope(key, DATA)
    result = ps.verify_build(json.dumps(envelope).encode(), DATA, slug="plugin", version="1.0.0")
    assert result["source"]["ref"] == "refs/heads/main" and result["keyid"] == ps.key_id(key.public_key())

    cases = [
        (dict(data=DATA + b"!"), ps.DIGEST_MISMATCH),
        (dict(slug="autre"), ps.SLUG_MISMATCH),
        (dict(version="2.0.0"), ps.VERSION_MISMATCH),
    ]
    for override, code in cases:
        kwargs = {"data": DATA, "slug": "plugin", "version": "1.0.0", **override}
        with pytest.raises(ps.SignatureError) as e:
            ps.verify_build(envelope, kwargs.pop("data"), **kwargs)
        assert e.value.code == code
    with pytest.raises(ps.SignatureError) as e:
        ps.verify_build(build_envelope(key, DATA, kind="served"), DATA)
    assert e.value.code == ps.WRONG_KIND


def test_verify_build_accepts_a_catalogue_alias_as_the_signed_name(monkeypatch):
    key = new_key()
    monkeypatch.setenv("DM_PACKAGE_PUBLISHER_KEYS", public_pem(key))
    envelope = build_envelope(key, DATA, slug="nom-du-depot")
    with pytest.raises(ps.SignatureError):
        ps.verify_build(envelope, DATA, slug="nom-au-catalogue")
    assert ps.verify_build(envelope, DATA, slug="nom-au-catalogue", slug_aliases=["nom-du-depot"])


def test_verify_build_without_envelope_or_without_keys(monkeypatch):
    with pytest.raises(ps.SignatureError) as e:
        ps.verify_build(None, DATA)
    assert e.value.code == ps.NO_ENVELOPE
    with pytest.raises(ps.SignatureError) as e:
        ps.verify_build(build_envelope(new_key(), DATA), DATA)
    assert e.value.code == ps.NOT_CONFIGURED


def test_ref_allowed_follows_both_lists(monkeypatch):
    source = {"ref": "refs/heads/exp/essai", "repository": "org/depot"}
    assert ps.ref_allowed(source), "aucune liste configurée : pas de restriction"
    monkeypatch.setenv("DM_PACKAGE_ALLOWED_REFS", "refs/heads/main, refs/tags/*")
    assert not ps.ref_allowed(source)
    assert ps.ref_allowed({"ref": "refs/tags/v1.0.0", "repository": "org/depot"})
    monkeypatch.setenv("DM_PACKAGE_ALLOWED_REPOSITORIES", "autre/*")
    assert not ps.ref_allowed({"ref": "refs/heads/main", "repository": "org/depot"})


def test_policy_defaults_to_off_and_ignores_unknown_values(monkeypatch):
    assert ps.policy() == ps.POLICY_OFF
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "REQUIRE")
    assert ps.policy() == ps.POLICY_REQUIRE
    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "n'importe quoi")
    assert ps.policy() == ps.POLICY_OFF


# ─── Signature du DM ────────────────────────────────────────────────────────

def test_without_a_key_the_dm_does_not_sign():
    assert ps.sign_served(DATA, slug="p", version="1.0.0", filename="p-1.0.0.xpi") is None
    assert ps.trust_document() is None and not ps.signing_enabled()


def test_simple_mode_the_dm_key_is_its_own_anchor(monkeypatch):
    dm = new_key()
    monkeypatch.setenv("DM_PACKAGE_SIGNING_KEY", private_pem(dm))
    envelope = ps.sign_served(DATA, slug="p", version="1.0.0", filename="p-1.0.0.xpi", dm="https://dm.test",
                              upstream={"sha256": "x", "verified": True}, changed_files=["b", "a"],
                              extension_id="plugin@example.invalid")
    assert "delegation" not in envelope
    trust = ps.trust_document()
    assert [r["keyid"] for r in trust["roots"]] == [ps.key_id(dm.public_key())]
    roots = {r["keyid"]: ps.public_key_from_b64(r["public_key"]) for r in trust["roots"]}
    statement, _signer = ps.verify_envelope(envelope, roots)
    assert statement["kind"] == "served" and statement["changed_files"] == ["a", "b"]
    assert statement["extension_id"] == "plugin@example.invalid" and statement["size"] == len(DATA)


def test_delegated_mode_the_anchor_is_the_root_and_the_envelope_carries_the_delegation(monkeypatch):
    root, dm = new_key(), new_key()
    monkeypatch.setenv("DM_PACKAGE_SIGNING_KEY", private_pem(dm))
    monkeypatch.setenv("DM_PACKAGE_TRUST_ROOTS", public_pem(root))
    monkeypatch.setenv("DM_PACKAGE_SIGNING_DELEGATION", json.dumps(delegation(root, dm)))
    envelope = ps.sign_served(DATA, slug="p", version="1.0.0", filename="p-1.0.0.xpi")
    trust = ps.trust_document()
    assert [r["keyid"] for r in trust["roots"]] == [ps.key_id(root.public_key())]
    roots = ps.load_public_keys(public_pem(root))
    _statement, signer = ps.verify_envelope(envelope, roots)
    assert signer == ps.key_id(dm.public_key())
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=400)
    with pytest.raises(ps.SignatureError) as e:
        ps.verify_envelope(envelope, roots, now=later)
    assert e.value.code == ps.DELEGATION_INVALID


@pytest.mark.parametrize("setup", ["expired", "other_key", "no_root", "root_without_delegation", "bad_key"])
def test_an_inconsistent_signing_configuration_refuses_to_sign(monkeypatch, setup):
    root, dm, other = new_key(), new_key(), new_key()
    monkeypatch.setenv("DM_PACKAGE_SIGNING_KEY", private_pem(dm))
    if setup == "expired":
        monkeypatch.setenv("DM_PACKAGE_TRUST_ROOTS", public_pem(root))
        monkeypatch.setenv("DM_PACKAGE_SIGNING_DELEGATION",
                           json.dumps(delegation(root, dm, days_before=30, days_after=-1)))
    elif setup == "other_key":
        monkeypatch.setenv("DM_PACKAGE_TRUST_ROOTS", public_pem(root))
        monkeypatch.setenv("DM_PACKAGE_SIGNING_DELEGATION", json.dumps(delegation(root, other)))
    elif setup == "no_root":
        monkeypatch.setenv("DM_PACKAGE_SIGNING_DELEGATION", json.dumps(delegation(root, dm)))
    elif setup == "root_without_delegation":
        monkeypatch.setenv("DM_PACKAGE_TRUST_ROOTS", public_pem(root))
    else:
        monkeypatch.setenv("DM_PACKAGE_SIGNING_KEY", "-----BEGIN PRIVATE KEY-----\nnon\n-----END PRIVATE KEY-----")
    with pytest.raises(ps.SigningConfigError):
        ps.sign_served(DATA, slug="p", version="1.0.0", filename="p-1.0.0.xpi")


def test_the_key_can_come_from_a_mounted_file(monkeypatch, tmp_path):
    dm = new_key()
    path = tmp_path / "cle.pem"
    path.write_text(private_pem(dm))
    monkeypatch.setenv("DM_PACKAGE_SIGNING_KEY_FILE", str(path))
    assert ps.signing_enabled()
    assert statement_of(ps.sign_served(DATA, slug="p", version="1", filename="f"))["kind"] == "served"
