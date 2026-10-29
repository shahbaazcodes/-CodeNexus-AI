"""
Phase 4 — FastAPI Web Gateway tests.

Covers: valid input, invalid input, oversized requests, unavailable services,
malformed responses, disabled LLMs, and Markdown export.

All HTTP/MCP calls are mocked; no real LLM APIs are called.
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

# Force offline-friendly env before importing web_server so agents' module
# globals are test-safe and no real network is attempted.
os.environ.setdefault("LLM_ENABLED", "1")
os.environ.setdefault("LLM_STUB", "1")
os.environ.setdefault("MAX_RETRIES", "1")
os.environ.setdefault("BASE_BACKOFF", "0.01")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("MCP_BASE", "http://127.0.0.1:8000")
os.environ.setdefault("QUARANTINE_PATH", "logs/test_quarantine.jsonl")

import web_server  # noqa: E402


@pytest.fixture
def client():
    return TestClient(web_server.app)


VALID_PAYLOAD = {
    "filename": "main.py",
    "language": "python",
    "content": "def hello():\n    print(\"hello\")\n",
}


# ── Valid input ──
def test_valid_input(client):
    with patch.object(web_server, "_safe_metrics", return_value=(88, "Low", None)), \
         patch.object(web_server, "run_pipeline", return_value={
             "review": "Looks good.",
             "bugs": "No bugs.",
             "refactored": "def hello():\n    print(\"hello\")\n",
             "docs": "def hello():\n    print(\"hello\")\n",
             "health_score": 88,
             "risk_level": "Low",
         }):
        resp = client.post("/analyze", json=VALID_PAYLOAD)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "review" in data
        assert "bugs" in data
        assert "refactored" in data
        assert "docs" in data
        assert "health_score" in data
        assert "risk_level" in data
        assert data["health_score"] == 88
        assert data["risk_level"] == "Low"


# ── Invalid input: missing content ──
def test_missing_content(client):
    resp = client.post("/analyze", json={"filename": "main.py", "language": "python"})
    assert resp.status_code == 422, resp.text


# ── Invalid input: empty content ──
def test_empty_content(client):
    resp = client.post("/analyze", json={
        "filename": "main.py", "language": "python", "content": "   \n  \n"
    })
    assert resp.status_code == 422, resp.text


# ── Invalid input: bad filename (path traversal) ──
def test_bad_filename_path_traversal(client):
    resp = client.post("/analyze", json={
        "filename": "../../etc/passwd",
        "language": "python",
        "content": "x = 1\n",
    })
    assert resp.status_code == 422, resp.text


# ── Invalid input: unsupported language ──
def test_unsupported_language(client):
    resp = client.post("/analyze", json={
        "filename": "main.py",
        "language": "brainfuck",
        "content": "x = 1\n",
    })
    assert resp.status_code == 422, resp.text


# ── Oversized request ──
def test_oversized_content(client):
    big = "x = 1\n" * (70 * 1024)
    resp = client.post("/analyze", json={
        "filename": "main.py",
        "language": "python",
        "content": big,
    })
    assert resp.status_code == 422, resp.text


# ── Unavailable services: MCP down → degrade gracefully ──
def test_mcp_unavailable_degrades(client):
    with patch.object(web_server, "_safe_metrics", return_value=(88, "Low", None)), \
         patch.object(web_server, "run_pipeline", return_value={
             "review": "Review.",
             "bugs": "Bugs.",
             "refactored": "code",
             "docs": "docs",
             "health_score": 88,
             "risk_level": "Low",
         }):
        resp = client.post("/analyze", json=VALID_PAYLOAD)
        assert resp.status_code == 200, resp.text


# ── Malformed pipeline response → 422 ──
def test_malformed_pipeline_response(client):
    with patch.object(web_server, "_safe_metrics", return_value=(88, "Low", None)), \
         patch.object(web_server, "run_pipeline", return_value="not a dict"):
        resp = client.post("/analyze", json=VALID_PAYLOAD)
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert "detail" in body
        assert body["detail"]["error"]["code"] == "INVALID_PIPELINE_OUTPUT"


# ── Missing fields in pipeline response → 422 ──
def test_missing_fields_pipeline_response(client):
    with patch.object(web_server, "_safe_metrics", return_value=(88, "Low", None)), \
         patch.object(web_server, "run_pipeline", return_value={"review": "only review"}):
        resp = client.post("/analyze", json=VALID_PAYLOAD)
        assert resp.status_code == 422, resp.text


# ── Disabled LLM (LLM_ENABLED=0) ──
def test_llm_disabled(client):
    with patch.object(web_server, "LLM_ENABLED", False), \
         patch.object(web_server, "_safe_metrics", return_value=(88, "Low", None)), \
         patch.object(web_server, "run_pipeline", return_value={
             "review": "### ⚠️ Review Unavailable (Python)\n\n**Reason**: LLM analysis disabled via LLM_ENABLED=0\n",
             "bugs": "### ⚠️ Bug Analysis Unavailable (Python)\n\n**Reason**: LLM analysis disabled via LLM_ENABLED=0\n",
             "refactored": "// Python Refactored Code — Stub Mode (No LLM)\n",
             "docs": "/** Python Documentation — Stub Mode (No LLM)\n */",
             "health_score": 88,
             "risk_level": "Low",
         }):
        resp = client.post("/analyze", json=VALID_PAYLOAD)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "disabled via LLM_ENABLED=0" in data["review"] or "Unavailable" in data["review"]


# ── Stub mode (LLM_STUB=1) produces deterministic offline results ──
def test_stub_mode_deterministic(client):
    with patch.object(web_server, "_safe_metrics", return_value=(88, "Low", None)), \
         patch.object(web_server, "run_pipeline", return_value={
             "review": "### 🛡️ Python Review — Stub Mode (No LLM)\n",
             "bugs": "### ✳ Python Bug Inspection — Stub Mode (No LLM)\n",
             "refactored": "// Python Refactored Code — Stub Mode (No LLM)\n",
             "docs": "/** Python Documentation — Stub Mode (No LLM)\n */",
             "health_score": 88,
             "risk_level": "Low",
         }):
        r1 = client.post("/analyze", json=VALID_PAYLOAD).json()
        r2 = client.post("/analyze", json=VALID_PAYLOAD).json()
        assert r1 == r2
        assert "Stub Mode" in r1["review"]


# ── Markdown export ──
def test_markdown_export(client):
    payload = {
        "filename": "main.py",
        "language": "python",
        "content": "def hello():\n    pass\n",
        "review": "Looks good.",
        "bugs": "No bugs.",
        "refactored": "def hello():\n    pass\n",
        "docs": "def hello():\n    pass\n",
        "health_score": 90,
        "risk_level": "Low",
    }
    resp = client.post("/export/markdown", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "content" in data
    assert "filename" in data
    md = data["content"]
    assert "# 🛡️ CodeNexus AI — Code Audit Report" in md
    assert "main.py" in md
    assert "Python" in md
    assert "90/100" in md
    assert "Looks good." in md
    assert "No bugs." in md
    assert "def hello():" in md


# ── Markdown export does not trigger LLM/MCP (no network) ──
def test_markdown_export_no_network(client):
    with patch.object(web_server, "_safe_metrics", side_effect=Exception("boom")), \
         patch.object(web_server, "run_pipeline", side_effect=Exception("boom")):
        resp = client.post("/export/markdown", json={
            "filename": "main.py",
            "language": "python",
            "content": "x = 1\n",
            "review": "r",
            "bugs": "b",
            "refactored": "code",
            "docs": "docs",
            "health_score": 50,
            "risk_level": "Medium",
        })
        assert resp.status_code == 200, resp.text
        md = resp.json()["content"]
        assert "50/100" in md
        assert "Medium" in md


# ── Health endpoints ──
def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "online"
    assert "llm_enabled" in data
    assert "llm_stub" in data


def test_health_providers_endpoint(client):
    resp = client.get("/health/providers")
    assert resp.status_code == 200
    data = resp.json()
    assert "providers" in data
    assert "mcp" in data["providers"]
    assert "gemini" in data["providers"]
    assert "groq" in data["providers"]
    assert "redis" in data["providers"]


# ── Pipeline exception → graceful degradation, no retry ──
def test_pipeline_exception_graceful_degradation(client):
    """When run_pipeline raises, the gateway returns 200 with unavailable
    results — never a 422, never a retry."""
    call_count = {"n": 0}

    def _boom(*a, **k):
        call_count["n"] += 1
        raise RuntimeError("pipeline exploded")

    with patch.object(web_server, "_safe_metrics", return_value=(88, "Low", None)), \
         patch.object(web_server, "run_pipeline", side_effect=_boom):
        resp = client.post("/analyze", json=VALID_PAYLOAD)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "Unavailable" in data["review"]
        assert "Unavailable" in data["bugs"]
        # Pipeline called exactly once — never retried on exception.
        assert call_count["n"] == 1


# ── Malformed (non-dict) pipeline output → one repair attempt, then 422 ──
def test_malformed_output_single_repair_then_422(client):
    """A non-dict pipeline output gets at most one repair attempt; if the
    repair also returns non-dict, a 422 is returned."""
    call_count = {"n": 0}

    def _malformed(*a, **k):
        call_count["n"] += 1
        return "not a dict"

    with patch.object(web_server, "_safe_metrics", return_value=(88, "Low", None)), \
         patch.object(web_server, "run_pipeline", side_effect=_malformed):
        resp = client.post("/analyze", json=VALID_PAYLOAD)
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["detail"]["error"]["code"] == "INVALID_PIPELINE_OUTPUT"
        # Original call + one repair attempt = 2 calls max.
        assert call_count["n"] == 2