"""Relevé des releases GitHub d'un plugin (issue #108) — service, sans base ni réseau.

GitHub est simulé par un transport httpx factice : pagination, composant, motif,
canal, drapeau pré-version discordant, artefact ou signature manquants, 401/403/404,
délai dépassé, proxy, espacement des essais, et le jeton qui ne sort jamais.
"""
from __future__ import annotations

import httpx
import pytest

from app.services import github_releases as gh

API = "https://api.github.test"
TOKEN = "jeton-" + "factice"
REPO = "IA-Generative/iassistant-matisse"
SOURCE = {"repository": REPO, "component": "IAssistant-tb60",
          "asset_pattern": "mirai-matisse-*.xpi", "channel": gh.CHANNEL_GENERAL}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DM_GITHUB_API_URL", API)
    monkeypatch.delenv("DM_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DM_GITHUB_POLL_INTERVAL", raising=False)
    monkeypatch.delenv("DM_PACKAGE_SIGNATURE_POLICY", raising=False)


def _asset(name: str, asset_id: int = 1) -> dict:
    return {"id": asset_id, "name": name, "url": f"{API}/repos/{REPO}/releases/assets/{asset_id}"}


def _release(version: str, *, component: str = "IAssistant-tb60", prerelease: bool | None = None,
             assets: list | None = None, signed: bool = True, **extra) -> dict:
    tag = f"{component}-v{version}" if component else f"v{version}"
    if assets is None:
        name = f"mirai-matisse-{version}.xpi"
        assets = [_asset(name, 10)] + ([_asset(name + ".sig.json", 11)] if signed else [])
    return {"tag_name": tag, "draft": False, "body": f"Notes {version}",
            "prerelease": ("-" in version) if prerelease is None else prerelease,
            "published_at": "2026-11-01T10:00:00Z", "html_url": f"https://github.test/{REPO}/releases/tag/{tag}",
            "assets": assets, **extra}


def _client(handler) -> httpx.Client:
    return gh._client(transport=httpx.MockTransport(handler))


def _by_version(rows: list[dict]) -> dict:
    return {r["version"]: r for r in rows}


# ── Source ───────────────────────────────────────────────────────────────────

def test_source_is_normalized_and_a_repository_url_is_reduced():
    source = gh.normalize_source({"repository": " https://github.com/IA-Generative/iassistant-matisse.git ",
                                  "component": "IAssistant-tb60", "asset_pattern": "mirai-matisse-*.xpi"})
    assert source == {**SOURCE, "channel": "general"}


def test_empty_repository_means_no_source():
    assert gh.normalize_source(None) is None
    assert gh.normalize_source({"repository": "", "asset_pattern": "x-*.xpi"}) is None


@pytest.mark.parametrize("raw", [
    {"repository": "sans-barre", "asset_pattern": "a-*.xpi"},
    {"repository": "o/r", "asset_pattern": ""},
    {"repository": "o/r", "asset_pattern": "../a.xpi"},
    {"repository": "o/r", "asset_pattern": "a-*.xpi", "channel": "nightly"},
    {"repository": "o/r", "asset_pattern": "a-*.xpi", "component": "avec espace"},
    {"repository": "o/r/../../x", "asset_pattern": "a-*.xpi"},
])
def test_malformed_source_is_refused_with_a_message(raw):
    with pytest.raises(gh.SourceError) as refused:
        gh.normalize_source(raw)
    assert str(refused.value)


# ── Tags ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tag,component,expected", [
    ("IAssistant-tb60-v0.24.0", "IAssistant-tb60", "0.24.0"),
    ("IAssistant-tb60-v0.24.0-rc.80", "IAssistant-tb60", "0.24.0-rc.80"),
    ("IAssistant-lo-v1.0.0", "IAssistant-tb60", None),
    ("IAssistant-tb60-extra-v1.0.0", "IAssistant-tb60", None),
    ("v1.2.3", "", "1.2.3"),
    ("1.2.3", "", "1.2.3"),
    ("IAssistant-tb60-v0.24.0", "", None),
    ("vnext", "", None),
    ("IAssistant-tb60-v", "IAssistant-tb60", None),
])
def test_version_is_read_from_the_component_tag(tag, component, expected):
    assert gh.tag_version(tag, component) == expected


# ── Classement ───────────────────────────────────────────────────────────────

def test_releases_of_the_component_are_kept_and_ordered_like_the_dm():
    rows = gh.classify([
        _release("0.9.0"), _release("0.10.0"), _release("0.10.0-rc.2"), _release("0.10.0-rc.10"),
        _release("3.0.0", component="IAssistant-lo"),
        _release("5.0.0", draft=True),
    ], {**SOURCE, "channel": gh.CHANNEL_ALL})
    assert [r["version"] for r in rows] == ["0.10.0", "0.10.0-rc.10", "0.10.0-rc.2", "0.9.0"]
    assert all(r["state"] == gh.STATE_AVAILABLE and r["reason"] is None for r in rows)
    top = rows[0]
    assert top["tag"] == "IAssistant-tb60-v0.10.0" and top["nature"] == "general"
    assert top["asset_name"] == "mirai-matisse-0.10.0.xpi"
    assert top["asset_api_url"].endswith("/assets/10") and top["signature_api_url"].endswith("/assets/11")
    assert top["release_notes"] == "Notes 0.10.0" and top["published_at"].startswith("2026-11-01T10:00:00")


@pytest.mark.parametrize("channel,available", [
    (gh.CHANNEL_GENERAL, {"1.0.0"}),
    (gh.CHANNEL_RC, {"1.0.0", "1.1.0-rc.1"}),
    (gh.CHANNEL_ALL, {"1.0.0", "1.1.0-rc.1", "1.1.0-dev.4"}),
])
def test_channel_decides_what_is_offered_and_says_why_the_rest_is_ignored(channel, available):
    rows = _by_version(gh.classify(
        [_release("1.0.0"), _release("1.1.0-rc.1"), _release("1.1.0-dev.4")], {**SOURCE, "channel": channel}))
    assert {v for v, r in rows.items() if r["state"] == gh.STATE_AVAILABLE} == available
    for version, row in rows.items():
        if version not in available:
            assert row["state"] == gh.STATE_IGNORED
            assert "Hors du canal suivi" in row["reason"]


def test_prerelease_flag_disagreeing_with_the_number_is_said_and_the_number_wins():
    rows = _by_version(gh.classify(
        [_release("2.0.0", prerelease=True), _release("2.1.0-rc.1", prerelease=False)],
        {**SOURCE, "channel": gh.CHANNEL_RC}))
    general, candidate = rows["2.0.0"], rows["2.1.0-rc.1"]
    assert general["state"] == gh.STATE_AVAILABLE and general["nature"] == "general"
    assert general["prerelease"] is True and "marque cette release comme pré-version" in general["reason"]
    assert candidate["state"] == gh.STATE_AVAILABLE and candidate["nature"] == "rc"
    assert candidate["prerelease"] is False and "ne marque pas" in candidate["reason"]


def test_release_without_a_matching_asset_is_ignored_with_the_pattern():
    [row] = gh.classify([_release("1.0.0", assets=[_asset("autre-chose.zip")])], SOURCE)
    assert row["state"] == gh.STATE_IGNORED and row["asset_name"] is None
    assert "mirai-matisse-*.xpi" in row["reason"]


def test_signature_file_alone_never_counts_as_the_package():
    [row] = gh.classify([_release("1.0.0", assets=[_asset("mirai-matisse-1.0.0.xpi.sig.json")])],
                        {**SOURCE, "asset_pattern": "mirai-matisse-*"})
    assert row["state"] == gh.STATE_IGNORED


def test_several_matching_assets_the_conventional_name_wins_else_ambiguous():
    assets = [_asset("mirai-matisse-1.0.0.xpi", 1), _asset("mirai-matisse-1.0.0-int.xpi", 2)]
    [row] = gh.classify([_release("1.0.0", assets=assets)], SOURCE)
    assert row["state"] == gh.STATE_AVAILABLE and row["asset_name"] == "mirai-matisse-1.0.0.xpi"

    assets = [_asset("mirai-matisse-int.xpi", 1), _asset("mirai-matisse-prod.xpi", 2)]
    [row] = gh.classify([_release("1.0.0", assets=assets)], SOURCE)
    assert row["state"] == gh.STATE_IGNORED and "Plusieurs fichiers" in row["reason"]


def test_missing_signature_is_a_remark_and_a_warning_when_the_instance_requires_it(monkeypatch):
    [row] = gh.classify([_release("1.0.0", signed=False)], SOURCE)
    assert row["state"] == gh.STATE_AVAILABLE and row["signature_api_url"] is None
    assert "mirai-matisse-1.0.0.xpi.sig.json" in row["reason"] and "refusé" not in row["reason"]

    monkeypatch.setenv("DM_PACKAGE_SIGNATURE_POLICY", "require")
    [row] = gh.classify([_release("1.0.0", signed=False)], SOURCE)
    assert row["state"] == gh.STATE_AVAILABLE and "l'import sera refusé" in row["reason"]


def test_version_already_in_the_catalog_is_marked_imported():
    rows = _by_version(gh.classify([_release("1.0.0"), _release("1.1.0")], SOURCE, known_versions={"1.0.0"}))
    assert rows["1.0.0"]["state"] == gh.STATE_IMPORTED and rows["1.0.0"]["reason"] is None
    assert rows["1.1.0"]["state"] == gh.STATE_AVAILABLE


def test_garbage_from_the_api_does_not_break_the_reading():
    rows = gh.classify([None, "x", {"tag_name": None}, {"tag_name": "IAssistant-tb60-v1.0.0", "assets": "?"},
                        _release("1.2.0", assets=[{"name": 3}, None, _asset("mirai-matisse-1.2.0.xpi")])], SOURCE)
    assert [(r["version"], r["state"]) for r in rows] == [("1.2.0", "available"), ("1.0.0", "ignored")]


# ── Appels à GitHub ──────────────────────────────────────────────────────────

def test_pagination_follows_the_link_header_and_sends_the_token(monkeypatch):
    monkeypatch.setenv("DM_GITHUB_TOKEN", TOKEN)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        headers = {"Link": f'<{API}/next>; rel="next"'} if page == 1 else {}
        return httpx.Response(200, json=[_release(f"1.{page}.0")], headers=headers)

    with _client(handler) as client:
        releases = gh.fetch_releases(REPO, client=client)
    assert [r["tag_name"] for r in releases] == ["IAssistant-tb60-v1.1.0", "IAssistant-tb60-v1.2.0"]
    assert [r.url.path for r in seen] == [f"/repos/{REPO}/releases"] * 2
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in seen)
    assert seen[0].url.params["per_page"] == "100"


def test_pagination_is_bounded_and_anonymous_without_a_token():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[_release("1.0.0")], headers={"Link": f'<{API}/n>; rel="next"'})

    with _client(handler) as client:
        gh.fetch_releases(REPO, client=client)
    assert len(calls) == gh._MAX_PAGES
    assert "Authorization" not in calls[0].headers


def test_full_page_without_link_header_asks_for_the_next_one():
    pages = {1: [_release(f"1.0.{i}") for i in range(gh._PER_PAGE)], 2: [_release("0.9.0")]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[int(request.url.params["page"])])

    with _client(handler) as client:
        assert len(gh.fetch_releases(REPO, client=client)) == gh._PER_PAGE + 1


@pytest.mark.parametrize("status,headers,code", [
    (401, {}, gh.ERR_UNAUTHORIZED),
    (403, {}, gh.ERR_FORBIDDEN),
    (403, {"x-ratelimit-remaining": "0", "retry-after": "120"}, gh.ERR_RATE_LIMITED),
    (429, {}, gh.ERR_RATE_LIMITED),
    (404, {}, gh.ERR_NOT_FOUND),
    (407, {}, gh.ERR_PROXY),
    (500, {}, gh.ERR_HTTP),
])
def test_http_refusals_become_readable_errors_that_never_carry_the_token(monkeypatch, status, headers, code):
    monkeypatch.setenv("DM_GITHUB_TOKEN", TOKEN)
    with _client(lambda request: httpx.Response(status, json={"message": "non"}, headers=headers)) as client:
        with pytest.raises(gh.GithubError) as failed:
            gh.fetch_releases(REPO, client=client)
    assert failed.value.code == code
    assert str(failed.value) and TOKEN not in str(failed.value)
    if "retry-after" in headers:
        assert failed.value.retry_after == 120


@pytest.mark.parametrize("exc,code", [
    (httpx.ConnectTimeout("délai"), gh.ERR_TIMEOUT),
    (httpx.ReadTimeout("délai"), gh.ERR_TIMEOUT),
    (httpx.ProxyError("407 depuis http://user:motdepasse@proxy:3128"), gh.ERR_PROXY),
    (httpx.ConnectError("pas de route vers http://user:motdepasse@proxy:3128"), gh.ERR_NETWORK),
])
def test_network_failures_are_reported_without_the_exception_text(exc, code):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    with _client(handler) as client:
        with pytest.raises(gh.GithubError) as failed:
            gh.fetch_releases(REPO, client=client)
    assert failed.value.code == code
    assert "motdepasse" not in str(failed.value), "l'adresse du proxy peut porter des identifiants"


def test_unreadable_answer_is_an_error_not_an_empty_list():
    for response in (httpx.Response(200, text="<html>portail captif</html>"),
                     httpx.Response(200, json={"message": "pas une liste"})):
        with _client(lambda request, response=response: response) as client:
            with pytest.raises(gh.GithubError) as failed:
                gh.fetch_releases(REPO, client=client)
        assert failed.value.code == gh.ERR_INVALID


# ── Téléchargement ───────────────────────────────────────────────────────────

_ROW = {"asset_api_url": f"{API}/repos/{REPO}/releases/assets/10",
        "signature_api_url": f"{API}/repos/{REPO}/releases/assets/11"}


def test_download_follows_the_redirect_without_carrying_the_token_elsewhere(monkeypatch):
    monkeypatch.setenv("DM_GITHUB_TOKEN", TOKEN)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.github.test":
            return httpx.Response(302, headers={"Location": f"https://objets.test{request.url.path}"})
        body = b"PK-paquet" if request.url.path.endswith("/10") else b'\xef\xbb\xbf{"schema": "x"}'
        return httpx.Response(200, content=body)

    with _client(handler) as client:
        data, signature = gh.download_release(_ROW, client=client)
    assert data == b"PK-paquet", "octets rendus tels que publiés"
    assert signature == '{"schema": "x"}'
    api_calls = [r for r in seen if r.url.host == "api.github.test"]
    elsewhere = [r for r in seen if r.url.host == "objets.test"]
    assert len(api_calls) == 2 and len(elsewhere) == 2
    assert all(r.headers["Accept"] == "application/octet-stream" for r in api_calls)
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in api_calls)
    assert all("Authorization" not in r.headers for r in elsewhere), "le jeton ne suit pas la redirection"


def test_download_without_signature_returns_none_for_it():
    with _client(lambda request: httpx.Response(200, content=b"PK")) as client:
        assert gh.download_release({**_ROW, "signature_api_url": None}, client=client) == (b"PK", None)


def test_download_is_bounded_by_the_upload_limit():
    with _client(lambda request: httpx.Response(200, content=b"x" * 2048)) as client:
        with pytest.raises(gh.GithubError) as failed:
            gh.download_release({**_ROW, "signature_api_url": None}, max_size=1024, client=client)
    assert failed.value.code == gh.ERR_TOO_LARGE


def test_download_refuses_an_address_outside_the_configured_api(monkeypatch):
    monkeypatch.setenv("DM_GITHUB_TOKEN", TOKEN)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"PK")

    with _client(handler) as client:
        with pytest.raises(gh.GithubError) as failed:
            gh.download_release({"asset_api_url": "https://ailleurs.test/assets/1"}, client=client)
    assert failed.value.code == gh.ERR_INVALID and not calls, "aucune requête, donc aucun jeton envoyé"


def test_download_failure_is_reported_like_the_listing():
    with _client(lambda request: httpx.Response(404)) as client:
        with pytest.raises(gh.GithubError) as failed:
            gh.download_release(_ROW, client=client)
    assert failed.value.code == gh.ERR_NOT_FOUND


# ── Espacement des essais et réglages ────────────────────────────────────────

def test_backoff_doubles_then_stays_bounded():
    delays = [gh.backoff_delay(n, 900) for n in range(1, 9)]
    assert delays[:4] == [1800, 3600, 7200, 14400]
    assert delays == sorted(delays) and delays[-1] == 6 * 3600
    assert gh.backoff_delay(50, 900) == 6 * 3600


def test_backoff_honours_the_rate_limit_reset_and_a_long_interval():
    assert gh.backoff_delay(1, 900, retry_after=3000) == 3000
    assert gh.backoff_delay(1, 900, retry_after=10 ** 9) == 6 * 3600
    assert gh.backoff_delay(1, 86400) == 86400, "jamais plus court que l'intervalle choisi"
    assert gh.backoff_delay(1, 0) >= 60


def test_settings_come_from_the_environment(monkeypatch):
    monkeypatch.delenv("DM_GITHUB_API_URL")
    assert gh.api_url() == "https://api.github.com" and gh.poll_interval() == 900
    assert gh.token_configured() is False
    monkeypatch.setenv("DM_GITHUB_API_URL", "http://faux-github:8765/")
    monkeypatch.setenv("DM_GITHUB_POLL_INTERVAL", "0")
    monkeypatch.setenv("DM_GITHUB_TOKEN", TOKEN)
    assert gh.api_url() == "http://faux-github:8765" and gh.poll_interval() == 0
    assert gh.token_configured() is True
    monkeypatch.setenv("DM_GITHUB_POLL_INTERVAL", "n'importe quoi")
    assert gh.poll_interval() == 900


def test_periodic_loop_only_runs_in_the_worker_roles():
    assert gh.runs_here("worker") and gh.runs_here("all")
    for mode in ("api", "admin", "llm", "telemetry-relay"):
        assert not gh.runs_here(mode)
