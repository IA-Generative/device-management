"""Cloud-native readiness — request-id de corrélation (X-Request-ID) et
formateur de logs JSON (app/observability.py)."""

import json
import logging

from fastapi.testclient import TestClient

from app.main import app
from app.observability import JsonLogFormatter, RequestIdLogFilter, current_request_id


def test_response_gets_generated_request_id():
    client = TestClient(app)
    res = client.get("/livez")
    assert res.status_code == 200
    assert res.headers.get("X-Request-ID")


def test_response_honors_incoming_request_id():
    client = TestClient(app)
    res = client.get("/livez", headers={"X-Request-ID": "caller-supplied-id"})
    assert res.headers.get("X-Request-ID") == "caller-supplied-id"


def test_request_id_not_leaked_across_requests():
    client = TestClient(app)
    first = client.get("/livez", headers={"X-Request-ID": "first-id"}).headers["X-Request-ID"]
    second = client.get("/livez").headers["X-Request-ID"]
    assert first == "first-id"
    assert second != "first-id"
    # Context var must be reset after each request — no residual state.
    assert current_request_id() == ""


def test_request_id_log_filter_injects_current_id():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    RequestIdLogFilter().filter(record)
    assert record.request_id == "-"  # no request in flight in this unit test


def test_json_log_formatter_produces_valid_json():
    record = logging.LogRecord("device-management", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.request_id = "abc123"
    line = JsonLogFormatter().format(record)
    parsed = json.loads(line)
    assert parsed["message"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["request_id"] == "abc123"


def test_configure_logging_installs_json_formatter_when_dm_log_format_json(monkeypatch):
    import app.main as m

    root = logging.getLogger()
    stdout_handler = next((h for h in root.handlers if getattr(h, "_dm_stdout", False)), None)
    assert stdout_handler is not None, "the app's stdout handler should already be installed at import time"
    root.removeHandler(stdout_handler)

    monkeypatch.setenv("DM_LOG_FORMAT", "json")
    try:
        m._configure_logging()
        installed = next(h for h in root.handlers if getattr(h, "_dm_stdout", False))
        assert isinstance(installed.formatter, JsonLogFormatter)
    finally:
        root.removeHandler(installed)
        root.addHandler(stdout_handler)
