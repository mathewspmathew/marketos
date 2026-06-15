import json

from fastapi.testclient import TestClient

import services.chatbot_svc.app as app_module
from services.chatbot_svc.app import app


def test_eval_endpoint_404_when_no_report(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "EVAL_REPORTS_DIR", tmp_path)
    client = TestClient(app)
    resp = client.get("/eval/chatbot")
    assert resp.status_code == 404
    assert "no eval run yet" in resp.json()["detail"]


def test_eval_endpoint_serves_latest_report(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "EVAL_REPORTS_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({"cases_total": 5}))
    client = TestClient(app)
    resp = client.get("/eval/chatbot")
    assert resp.status_code == 200
    assert resp.json() == {"cases_total": 5}
