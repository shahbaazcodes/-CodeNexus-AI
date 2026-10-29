"""
Focused tests for Phase 3 LLM reliability and honest-fallback changes.

Mocks HTTP and Redis interactions. Does not make real LLM API calls.
"""
import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

# Force deterministic, offline-friendly env BEFORE importing agents so module
# globals (LLM_ENABLED, LLM_STUB, API keys, MAX_RETRIES, ...) are test-safe.
os.environ.setdefault("LLM_ENABLED", "1")
os.environ.setdefault("LLM_STUB", "0")
os.environ.setdefault("MAX_RETRIES", "2")
os.environ.setdefault("BASE_BACKOFF", "0.01")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6379")

import agents  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json
class _FakeClient:
    def __init__(self, responses=None, side_effect=None):
        self.responses = responses or []
        self.side_effect = side_effect
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, headers, json, timeout))
        if self.side_effect:
            raise self.side_effect
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse(200, {"choices": [{"message": {"content": "mocked"}}]})


def _make_gemini_response(text="gemini response"):
    return _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def _make_groq_response(text="groq response"):
    return _FakeResponse(200, {"choices": [{"message": {"content": text}}]})


def _is_gemini_url(url):
    return "generativelanguage.googleapis.com" in url


def _is_groq_url(url):
    return "api.groq.com" in url


@pytest.fixture
def fake_client(monkeypatch):
    """Install a fake httpx.AsyncClient into the agents module."""
    client = _FakeClient()
    monkeypatch.setattr(agents.httpx, "AsyncClient", lambda *a, **k: client)
    return client


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch):
    """Disable the real Redis client for every test so tests are hermetic.

    Tests that need Redis opt in via the `fake_redis` fixture, which
    re-enables it with an in-memory fake.
    """
    monkeypatch.setattr(agents, "REDIS_AVAILABLE", False)
    monkeypatch.setattr(agents, "redis_client", None)


@pytest.fixture
def fake_redis(monkeypatch):
    """Install a fake redis client that records get/setex calls."""
    store = {}
    calls = {"get": [], "setex": []}

    class FakeRedis:
        def get(self, key):
            calls["get"].append(key)
            return store.get(key)

        def setex(self, key, ttl, value):
            calls["setex"].append((key, ttl, value))
            store[key] = value
            return True

    fake = FakeRedis()
    monkeypatch.setattr(agents, "REDIS_AVAILABLE", True)
    monkeypatch.setattr(agents, "redis_client", fake)
    return fake, calls


def _set_llm_mode(monkeypatch, enabled=True, stub=False, gemini_key="test-gemini-key",
                  groq_key="test-groq-key"):
    monkeypatch.setattr(agents, "LLM_ENABLED", enabled)
    monkeypatch.setattr(agents, "LLM_STUB", stub)
    monkeypatch.setattr(agents, "GEMINI_API_KEY", gemini_key)
    monkeypatch.setattr(agents, "GROQ_API_KEY", groq_key)


# ── Requirement 1: LLM_ENABLED=0 prevents all LLM network requests ──
def test_llm_enabled_zero_prevents_network(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=False, stub=False)
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result
    assert "disabled via LLM_ENABLED=0" in result
    assert fake_client.calls == [], "no HTTP calls should be made when LLM_ENABLED=0"


# ── Requirement 2: LLM_STUB=1 produces deterministic offline results ──
def test_llm_stub_deterministic_offline(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=True)
    r1 = asyncio.run(agents.call_llm("review this code", "python"))
    r2 = asyncio.run(agents.call_llm("review this code", "python"))
    assert r1 == r2
    assert "Stub Mode" in r1
    assert fake_client.calls == [], "stub mode must not call the network"


# ── Requirement 3: Gemini primary works, no Groq fallback ──
def test_gemini_primary_no_groq_fallback(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake_client.responses = [_make_gemini_response("gemini says hello")]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert result == "gemini says hello"
    assert any(_is_gemini_url(c[0]) for c in fake_client.calls)
    assert not any(_is_groq_url(c[0]) for c in fake_client.calls), \
        "groq must not be called when gemini succeeds"


# ── Requirement 3: Groq fallback when Gemini fails ──
def test_groq_fallback_when_gemini_fails(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    # Both gemini models return a permanent (non-retryable) error, then groq succeeds.
    fake_client.responses = [_FakeResponse(401), _FakeResponse(401)]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert result == "mocked"  # groq default response
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    groq_calls = [c for c in fake_client.calls if _is_groq_url(c[0])]
    assert len(gemini_calls) == 2, "both gemini models attempted once"
    assert len(groq_calls) == 1, "groq fallback invoked after gemini failure"


# ── Requirement 4: Retries bounded & limited to transient failures ──
def test_transient_retry_then_success(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    # 429 (transient) then 200 success within the same model.
    fake_client.responses = [_FakeResponse(429), _make_gemini_response("after retries")]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert result == "after retries"
    assert len(fake_client.calls) == 2, "one retry then success"


def test_permanent_error_not_retried(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False, groq_key="")
    # 401 is permanent: each gemini model attempted exactly once, no retry.
    fake_client.responses = [_FakeResponse(401), _FakeResponse(401)]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    assert len(gemini_calls) == 2
    # Each model URL appears exactly once -> no retries on permanent errors.
    urls = [c[0] for c in gemini_calls]
    assert urls.count(urls[0]) == 1 and urls.count(urls[1]) == 1


# ── Requirement 5: Failed provider results not cached as success ──
def test_failed_result_not_cached(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False, gemini_key="", groq_key="")
    fake, calls = fake_redis
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result
    assert calls["setex"] == [], "failed provider results must not be cached as success"


def test_successful_result_is_cached(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis
    fake_client.responses = [_make_gemini_response("cache me")]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert result == "cache me"
    assert len(calls["setex"]) == 1, "successful results must be cached"


# ── Requirement 6: Unavailable services do not fabricate findings ──
def test_unavailable_not_fabricated(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False, gemini_key="", groq_key="")
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result
    assert "All LLM providers failed" in result or "disabled" in result
    # Must not look like a real code review / fabricated findings.
    assert "Cyclomatic" not in result
    assert "health_score" not in result


# ── Requirement 7: Redis failures do not crash the app ──
def test_redis_failure_does_not_crash(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False, gemini_key="", groq_key="")
    monkeypatch.setattr(agents, "REDIS_AVAILABLE", True)

    class BadRedis:
        def get(self, key):
            raise RuntimeError("redis down")

        def setex(self, key, ttl, value):
            raise RuntimeError("redis down")

    monkeypatch.setattr(agents, "redis_client", BadRedis())
    # Should not raise; falls through to providers, then unavailable result.
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result