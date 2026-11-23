import asyncio
import hashlib
import json
import os
import sys
import time

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

# Force deterministic, offline-friendly env BEFORE importing agents
# so module globals (LLM_ENABLED, LLM_STUB, API keys, MAX_RETRIES, ...) are test-safe.
os.environ.setdefault("LLM_ENABLED", "1")
os.environ.setdefault("LLM_STUB", "0")
os.environ.setdefault("MAX_INTERACTIVE_RETRIES", "2")
os.environ.setdefault("MAX_NONINTERACTIVE_RETRIES", "6")
os.environ.setdefault("BASE_BACKOFF", "0.01")
os.environ.setdefault("MAX_BACKOFF", "0.1")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6379")

import agents  # noqa: E402
from usage_meter import usage_meter  # noqa: E402  (usage metering & cost guard)


class _FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self.headers = {}
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


# --- AUTOUSE: Reset circuit-breaker and quota state between tests ---
import pytest
import asyncio

@pytest.fixture(autouse=True)
def _reset_global_llm_state(monkeypatch):
    """Keep circuit-breaker and quota state isolated between tests."""
    breaker = agents.gemini_circuit_breaker
    limiter = agents.quota_limiter

    breaker_attrs = {
        "state": breaker.CLOSED,
        "_consecutive_failures": 0,
        "_open_since": 0.0,
        "threshold": breaker.threshold,
        "cooldown": breaker.cooldown,
    }
    breaker_stats = dict(breaker.stats)
    limiter_attrs = {
        "max_retries": agents.GEMINI_MAX_RETRIES,
        "max_rpm": agents.GEMINI_MAX_REQUESTS_PER_MINUTE,
        "max_tpm": agents.GEMINI_MAX_INPUT_TOKENS_PER_MINUTE,
        "_semaphore": asyncio.Semaphore(
            agents.GEMINI_MAX_CONCURRENT_REQUESTS
        ),
        "_request_times": [],
        "_token_window_tokens": 0,
        "_token_window_start": time.monotonic(),
    }
    limiter_stats = dict(limiter.stats)

    for name, value in breaker_attrs.items():
        monkeypatch.setattr(breaker, name, value)
    for name, value in limiter_attrs.items():
        monkeypatch.setattr(limiter, name, value)
    monkeypatch.setattr(breaker, "stats", dict(breaker_stats))
    monkeypatch.setattr(limiter, "stats", dict(limiter_stats))
    usage_meter.reset()
    # Reset usage-meter limits to permissive defaults so they cannot leak
    # between tests and block requests in tests that expect live LLM calls.
    usage_meter.max_requests_per_session = None
    usage_meter.max_estimated_cost_per_session = None
    usage_meter.max_tokens_per_request = None
    usage_meter.enabled = True
    yield



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


# ── Requirement 3: Groq fallback when Gemini fails (permanent 401) ──
def test_groq_fallback_when_gemini_fails(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    # Permanent 401 -> primary Gemini attempted once, no retry, then Groq.
    fake_client.responses = [_FakeResponse(401)]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert result == "mocked"  # groq default response
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    groq_calls = [c for c in fake_client.calls if _is_groq_url(c[0])]
    assert len(gemini_calls) == 1, "only primary gemini attempted (no wasted quota on fallback model)"
    assert len(groq_calls) == 1, "groq fallback invoked after gemini failure"


# ── Requirement 4: Transient 429 retried to bounded success, then cached ──
def test_transient_retry_then_success(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis
    # 429 (transient) then 200 success within the same model.
    fake_client.responses = [_FakeResponse(429), _make_gemini_response("after retries")]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert result == "after retries"
    assert len(fake_client.calls) == 2, "one retry then success"
    # Cache only successful provider responses.
    assert len(calls["setex"]) == 1
    assert "codenexus:cache:" in calls["setex"][0][0]


# ── Requirement 5: 503 treated as transient, retried then Groq fallback on budget exhaustion ──
def test_503_then_groq_fallback_on_budget_exhaustion(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    agents.quota_limiter.max_retries = 1
    # 503, 503 (2 retries), then Groq succeeds.
    fake_client.responses = [_FakeResponse(503), _FakeResponse(503), _make_groq_response("groq ok")]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "groq ok" in result
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    groq_calls = [c for c in fake_client.calls if _is_groq_url(c[0])]
    # 2 Gemini attempts (1 retry) + 1 Groq call.
    assert len(gemini_calls) == 2
    assert len(groq_calls) == 1


# ── Requirement 6: Permanent 401 not retried, no Groq (no key) ──
def test_permanent_error_not_retried(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False, groq_key="")
    fake_client.responses = [_FakeResponse(401)]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    assert len(gemini_calls) == 1, "primary gemini attempted exactly once, no retry on permanent error"


# ── Requirement 7: Invalid model 400 not retried, circuit trips ──
def test_invalid_model_not_retried(monkeypatch, fake_client, monkeypatch_cb):
    _set_llm_mode(monkeypatch, enabled=True, stub=False, gemini_key="test-gemini-key", groq_key="")
    # A real Gemini request receives a permanent 400 and must not retry.
    fake_client.responses = [_FakeResponse(400)]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    assert len(gemini_calls) == 1
    assert agents.gemini_circuit_breaker.stats["trips"] >= 1
    

# ── Requirement 8: Circuit opens after threshold of 503s, routed to Groq ──
def test_circuit_opens_after_threshold_then_routes_to_groq(monkeypatch, fake_client, monkeypatch_cb):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    agents.quota_limiter.max_retries = 1
    agents.gemini_circuit_breaker.threshold = 2
    # First request: 2 Gemini 503s, circuit opens, falls back to Groq.
    fake_client.responses = [_FakeResponse(503), _FakeResponse(503)]
    r1 = asyncio.run(agents.call_llm("review this code", "python"))
    gemini_calls_1 = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    groq_calls_1 = [c for c in fake_client.calls if _is_groq_url(c[0])]
    assert len(gemini_calls_1) == 2, "two gemini attempts to reach threshold"
    assert len(groq_calls_1) == 1, "circuit opened -> routed to Groq"
    # Second request: circuit open -> skip Gemini, Groq directly.
    fake_client.calls = []
    fake_client.responses = [_make_groq_response("groq again")]
    r2 = asyncio.run(agents.call_llm("review this code2", "python"))
    gemini_calls_2 = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    groq_calls_2 = [c for c in fake_client.calls if _is_groq_url(c[0])]
    assert len(gemini_calls_2) == 0, "circuit open: Gemini skipped"
    assert len(groq_calls_2) == 1, "circuit open: Groq attempted directly"
    assert "groq again" in r2


# ── Requirement 9: Recovery probe after cooldown opens HALF state ──
def test_recovery_probe_after_cooldown(monkeypatch, fake_client, monkeypatch_cb):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    agents.quota_limiter.max_retries = 1
    agents.gemini_circuit_breaker.threshold = 2
    agents.gemini_circuit_breaker.cooldown = 0.0  # immediate probe
    # Open circuit with 2 failures.
    fake_client.responses = [_FakeResponse(503), _FakeResponse(503)]
    asyncio.run(agents.call_llm("review 1", "python"))
    assert agents.gemini_circuit_breaker.state == agents.gemini_circuit_breaker.OPEN
    # First probe attempt should be ALLOWED (HALF) and trigger a Gemini call.
    agents.quota_limiter.max_retries = 0
    fake_client.calls = []
    fake_client.responses = [_make_gemini_response("probe ok")]
    r = asyncio.run(agents.call_llm("review 2", "python"))
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    assert len(gemini_calls) == 1, "recovery probe: one Gemini attempt in HALF state"
    assert "probe ok" in r, "probe succeeded -> circuit resets"
    assert agents.gemini_circuit_breaker.state == agents.gemini_circuit_breaker.CLOSED


# ── Requirement 10: Cooldown respected; probe skipped until time passes ──
def test_cooldown_respected(monkeypatch, fake_client, monkeypatch_cb):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    agents.gemini_circuit_breaker.threshold = 2
    agents.gemini_circuit_breaker.cooldown = 100.0
    agents.quota_limiter.max_retries = 1
    fake_client.responses = [_FakeResponse(503), _FakeResponse(503)]
    asyncio.run(agents.call_llm("review 1", "python"))
    assert agents.gemini_circuit_breaker.state == agents.gemini_circuit_breaker.OPEN
    # Before cooldown: probe skipped, Gemini never contacted.
    fake_client.calls = []
    fake_client.responses = [_make_groq_response("groq during cooldown")]
    r = asyncio.run(agents.call_llm("review 2", "python"))
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    groq_calls = [c for c in fake_client.calls if _is_groq_url(c[0])]
    assert len(gemini_calls) == 0, "cooldown active: Gemini skipped"
    assert len(groq_calls) == 1, "cooldown active: routed to GroQ"
    assert "groq during cooldown" in r


# ── Requirement 11: Auth/invalid model errors trip circuit without retrying ──
def test_auth_error_trips_circuit_without_retry(monkeypatch, fake_client, monkeypatch_cb):
    _set_llm_mode(monkeypatch, enabled=True, stub=False, gemini_key="test-gemini-key", groq_key="")
    fake_client.responses = [_FakeResponse(401)]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    assert len(gemini_calls) == 1, "auth error: no retry"
    assert agents.gemini_circuit_breaker.stats["trips"] >= 1


# ── Requirement 12: Bounded retry budget respected (no unbounded loops) ──
def test_bounded_retry_budget_respected(monkeypatch, fake_client, monkeypatch_cb):
    _set_llm_mode(monkeypatch, enabled=True, stub=False, groq_key="")
    agents.quota_limiter.max_retries = 1
    # 429 then 401. Retry count bounded to 1 retry for the 429; 401 stops.
    fake_client.responses = [_FakeResponse(429), _FakeResponse(401)]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    # 429 retried once (2 total), then 401 stops.
    assert len(gemini_calls) == 2
    assert "Unavailable" in result


# ── Requirement 13: No duplicate SDK/application retries (single retry in call_gemini_raw) ──
def test_no_duplicate_retries(monkeypatch, fake_client, monkeypatch_cb):
    """Ensure only one retry loop exists per call (no SDK + app double retry)."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False, groq_key="")
    agents.quota_limiter.max_retries = 1
    fake_client.responses = [_FakeResponse(503), _make_gemini_response("recovered")]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    groq_calls = [c for c in fake_client.calls if _is_groq_url(c[0])]
    assert result == "recovered"
    assert len(gemini_calls) == 2
    assert len(groq_calls) == 0

# ── Requirement 14: Shared quota limiting across concurrent agent calls ──
def test_shared_quota_limits_concurrency(monkeypatch, fake_client):
    """Concurrent agents must share the semaphore; no more than N in flight."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    monkeypatch.setattr(agents, "MAX_CONCURRENT_PROVIDER_REQUESTS", 1)
    agents.quota_limiter._semaphore = asyncio.Semaphore(1)
    agents.quota_limiter.max_rpm = 0
    agents.quota_limiter.max_tpm = 0

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def slow_post(url, headers=None, json=None, timeout=None):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    fake_client.calls = []
    orig_post = fake_client.post
    fake_client.post = slow_post

    async def _run():
        return await asyncio.gather(*[
            agents.call_llm("review code", "python", agent=f"agent{i}") for i in range(4)
        ])
    results = asyncio.run(_run())
    fake_client.post = orig_post

    # Results were produced; concurrency was bounded (max 1 in flight).
    assert max_in_flight == 1, f"expected serialized concurrency, got {max_in_flight}"
    assert all("ok" in r for r in results)


# ── Requirement 14b: HTTP 429 with short Retry-After retries then succeeds ──
def test_429_short_retry_after_retries_then_success(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    agents.quota_limiter.max_retries = 2
    agents.gemini_circuit_breaker.cooldown = 0.0
    # First attempt: 429 with short Retry-After; second: success.
    resp = _FakeResponse(429)
    resp.headers["retry-after"] = "0.01"
    fake_client.responses = [resp, _make_gemini_response("recovered")]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    groq_calls = [c for c in fake_client.calls if _is_groq_url(c[0])]
    assert result == "recovered", f"expected recovery, got {result!r}"
    assert len(gemini_calls) == 2, "one retry then success"
    assert len(groq_calls) == 0, "no Groq fallback when Gemini recovers"


# ── Requirement 14c: HTTP 429 without Retry-After retries with backoff ──
def test_429_no_retry_after_retries_with_backoff(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    agents.quota_limiter.max_retries = 1
    agents.gemini_circuit_breaker.cooldown = 0.0
    fake_client.responses = [_FakeResponse(429), _make_gemini_response("recovered")]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    assert result == "recovered"
    assert len(gemini_calls) == 2, "429 without Retry-After still retries with backoff"


# ── Requirement 14d: HTTP 429 with long Retry-After falls back to Groq immediately ──
def test_429_long_retry_after_immediate_groq_fallback(monkeypatch, fake_client, monkeypatch_cb):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    agents.quota_limiter.max_retries = 2
    agents.gemini_circuit_breaker.cooldown = 0.0
    agents.GEMINI_MAX_RETRY_AFTER = 1.0  # short bound for test
    # 429 with a Retry-After far exceeding the bound -> immediate Groq fallback.
    resp = _FakeResponse(429)
    resp.headers["retry-after"] = "60"
    fake_client.responses = [resp, _make_groq_response("groq answer")]
    result = asyncio.run(agents.call_llm("review this code", "python"))
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    groq_calls = [c for c in fake_client.calls if _is_groq_url(c[0])]
    assert result == "groq answer", f"expected Groq fallback, got {result!r}"
    assert len(gemini_calls) == 1, "only one Gemini attempt (no retry on long Retry-After)"
    assert len(groq_calls) == 1, "Groq fallback invoked"


# ── Requirement 14e: Exception variable stays in scope (regression for indentation bug) ──
def test_exception_variable_in_scope(monkeypatch, fake_client):
    """The exception handler body must reference `e` inside `except` (not outside)."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False, groq_key="")
    agents.quota_limiter.max_retries = 0
    agents.gemini_circuit_breaker.cooldown = 0.0

    async def raise_timeout(url, headers=None, json=None, timeout=None):
        fake_client.calls.append((url, headers, json, timeout))
        raise httpx.TimeoutException("read timed out")

    orig = fake_client.post
    fake_client.post = raise_timeout
    try:
        result = asyncio.run(agents.call_llm("review this code", "python"))
    finally:
        fake_client.post = orig

    assert "Unavailable" in result, f"expected unavailable result, got {result!r}"
    gemini_calls = [c for c in fake_client.calls if _is_gemini_url(c[0])]
    assert len(gemini_calls) == 1, "single Gemini attempt on timeout"


# ── Requirement 14f: Concurrent agents do not exceed configured quota limit ──
def test_concurrent_agents_respect_quota_semaphore(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    agents.quota_limiter.max_rpm = 0
    agents.quota_limiter.max_tpm = 0
    agents.quota_limiter._semaphore = asyncio.Semaphore(agents.GEMINI_MAX_CONCURRENT_REQUESTS)

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def slow_post(url, headers=None, json=None, timeout=None):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    fake_client.calls = []
    orig_post = fake_client.post
    fake_client.post = slow_post

    async def _run():
        return await asyncio.gather(*[
            agents.call_llm("review code", "python", agent=f"agent{i}")
            for i in range(agents.GEMINI_MAX_CONCURRENT_REQUESTS * 2)
        ])

    results = asyncio.run(_run())
    fake_client.post = orig_post

    assert max_in_flight <= agents.GEMINI_MAX_CONCURRENT_REQUESTS, \
        f"exceeded quota: {max_in_flight} in flight, limit {agents.GEMINI_MAX_CONCURRENT_REQUESTS}"
    assert all("ok" in r for r in results)


# ── Requirement 15: Timeout respected ──
def test_timeout_respected(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    # Simulate a caller that never resolves -> asyncio.TimeoutError short-circuits.
    async def hang_post(*a, **k):
        await asyncio.sleep(10)
        return _FakeResponse(200, {"choices": [{"message": {"content": "x"}}]})
    orig = fake_client.post
    fake_client.post = hang_post
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(agents.call_groq("p", "agent"), timeout=0.05))
    fake_client.post = orig


# ── Requirement 16: Token budget rejection triggers fallback ──
def test_token_budget_rejection_triggers_fallback(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    agents.quota_limiter.max_rpm = 0
    agents.quota_limiter.max_tpm = 5  # tiny budget
    agents.quota_limiter.stats["budget_rejected"] = 0

    # Gemini rejected by budget; Groq succeeds.
    fake_client.responses = [_FakeResponse(200, {"choices": [{"message": {"content": "groq answer"}}]})]
    result = asyncio.run(agents.call_llm("a very long prompt that exceeds token budget " * 50, "python"))
    assert "groq answer" in result
    assert agents.quota_limiter.stats["budget_rejected"] >= 1


# ── Requirement 17: Groq failure without fabricated results ──
def test_groq_failure_no_fabrication(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=False, gemini_key="", groq_key="")
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result
    assert "All LLM providers failed" in result
    assert "Cyclomatic" not in result
    assert "health_score" not in result


# ── Requirement 18: Structured timing instrumentation present ──
def test_timing_instrumentation_callable():
    timing = agents._Timing(agent="reviewer")
    assert hasattr(timing, "gemini_durations")
    assert hasattr(timing, "groq_durations")
    assert hasattr(timing, "cache_hit")
    assert timing.duration() >= 0


# ── Requirement 19: Merge helper preserves agent artifacts ──
def test_merge_artifacts_preserves_order():
    a = {"review": "r1"}
    b = {"bugs": "b1", "review": "r2"}
    merged = agents.merge_artifacts(a, b)
    assert merged["review"] == "r2"
    assert merged["bugs"] == "b1"


# ── Requirement 20: Stub mode makes no external API calls ──
def test_stub_mode_no_external_calls(monkeypatch, fake_client):
    _set_llm_mode(monkeypatch, enabled=True, stub=True)
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Stub Mode" in result
    assert fake_client.calls == [], "stub mode must not call the network"


# ── Cache-key helpers ──
def _expected_cache_key(agent: str, language: str, prompt: str) -> str:
    """Replicate the cache-key construction in agents.call_llm."""
    routing_fingerprint = agents._routing_config_fingerprint()
    content_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return f"codenexus:cache:{agent}:{language}:{routing_fingerprint}:{content_hash}"


# ── Redis cache correctness tests ──
def test_identical_inputs_produce_same_cache_key():
    key1 = _expected_cache_key("reviewer", "python", "review this code")
    key2 = _expected_cache_key("reviewer", "python", "review this code")
    assert key1 == key2


def test_different_source_produces_different_cache_key():
    key1 = _expected_cache_key("reviewer", "python", "review this code")
    key2 = _expected_cache_key("reviewer", "python", "review different code")
    assert key1 != key2


def test_different_language_produces_different_cache_key():
    key1 = _expected_cache_key("reviewer", "python", "review this code")
    key2 = _expected_cache_key("reviewer", "javascript", "review this code")
    assert key1 != key2


def test_different_agent_produces_different_cache_key():
    key1 = _expected_cache_key("reviewer", "python", "review this code")
    key2 = _expected_cache_key("bug_hunter", "python", "review this code")
    assert key1 != key2


def test_different_routing_config_produces_different_cache_key(monkeypatch):
    key_default = _expected_cache_key("reviewer", "python", "review this code")
    monkeypatch.setattr(agents, "PRIMARY_MODEL", "gemini-2.0-flash")
    key_changed = _expected_cache_key("reviewer", "python", "review this code")
    assert key_default != key_changed


def test_successful_response_cached_with_ttl(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis
    fake_client.responses = [_make_gemini_response("cached gemini response")]
    result = asyncio.run(
        agents.call_llm("review this code", "python", agent="reviewer")
    )
    assert result == "cached gemini response"
    assert len(calls["setex"]) == 1
    key, ttl, value = calls["setex"][0]
    assert ttl == 86400, "existing 24-hour TTL must be preserved"
    assert key == _expected_cache_key("reviewer", "python", "review this code")
    payload = json.loads(value) if isinstance(value, str) else value
    assert payload["response"] == "cached gemini response"
    assert payload["provider_provenance"]["provider"] == "gemini"


def test_failed_response_not_cached(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(
        monkeypatch, enabled=True, stub=False, gemini_key="", groq_key=""
    )
    fake, calls = fake_redis
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert "Unavailable" in result
    assert calls["setex"] == [], "failed/unavailable results must not be cached"


def test_redis_get_failure_does_not_crash(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake_client.responses = [_make_gemini_response("after redis get failure")]

    def boom_get(key):
        raise RuntimeError("redis get down")

    fake_redis[0].get = boom_get
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert result == "after redis get failure"


def test_redis_set_failure_does_not_crash(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake_client.responses = [_make_gemini_response("after redis set failure")]

    def boom_setex(key, ttl, value):
        raise RuntimeError("redis set down")

    fake_redis[0].setex = boom_setex
    result = asyncio.run(agents.call_llm("review this code", "python"))
    assert result == "after redis set failure"


def test_cache_hit_returns_without_llm_call(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis
    key = _expected_cache_key("reviewer", "python", "review this code")
    payload = json.dumps(
        {
            "response": "cached answer",
            "provider_provenance": {"provider": "gemini", "model": "gemini-3.6-flash"},
        }
    )
    fake.setex(key, 86400, payload)
    result = asyncio.run(
        agents.call_llm("review this code", "python", agent="reviewer")
    )
    assert result == "cached answer"
    assert fake_client.calls == [], "LLM must not be called on a cache hit"
    assert calls["get"], "cache get must be attempted on lookup"


def test_prompt_change_produces_cache_miss(monkeypatch, fake_client, fake_redis):
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis
    key_a = _expected_cache_key("reviewer", "python", "prompt A")
    fake.setex(key_a, 86400, json.dumps({"response": "cached A", "provider_provenance": {}}))

    # Prompt A is a cache hit.
    result_a = asyncio.run(agents.call_llm("prompt A", "python", agent="reviewer"))
    assert result_a == "cached A"

    # Prompt B is a different rendered prompt -> cache miss -> LLM called.
    fake_client.responses = [_make_gemini_response("fresh B")]
    result_b = asyncio.run(agents.call_llm("prompt B", "python", agent="reviewer"))
    assert result_b == "fresh B"
    assert len(fake_client.calls) == 1, "LLM must be called on a cache miss"


# ── Production cache-key correctness via agents.call_llm() ──
def test_same_prompt_agent_different_language_does_not_share(monkeypatch, fake_client, fake_redis):
    """Python vs JavaScript with same prompt/agent must not share a cache entry."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis

    fake_client.responses = [_make_gemini_response("py result")]
    result_py = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result_py == "py result"

    fake_client.responses = [_make_gemini_response("js result")]
    result_js = asyncio.run(agents.call_llm("review this code", "javascript", agent="reviewer"))
    assert result_js == "js result"

    assert len(calls["get"]) == 2, "each language must perform its own cache lookup"
    assert len(calls["setex"]) == 2, "each language must store its own cache entry"
    py_keys = [k for k, _, _ in calls["setex"] if ":python:" in k]
    js_keys = [k for k, _, _ in calls["setex"] if ":javascript:" in k]
    assert py_keys and js_keys, "expected keys for both languages"
    assert set(py_keys).isdisjoint(js_keys), "python and javascript must not share a cache entry"


def test_different_agents_do_not_share_cache_entry(monkeypatch, fake_client, fake_redis):
    """Same prompt/language but different agents must not share a cache entry."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis

    fake_client.responses = [_make_gemini_response("reviewer result")]
    result_r = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result_r == "reviewer result"

    fake_client.responses = [_make_gemini_response("bug_hunter result")]
    result_b = asyncio.run(agents.call_llm("review this code", "python", agent="bug_hunter"))
    assert result_b == "bug_hunter result"

    reviewer_keys = [k for k, _, _ in calls["setex"] if ":reviewer:" in k]
    bug_hunter_keys = [k for k, _, _ in calls["setex"] if ":bug_hunter:" in k]
    assert reviewer_keys and bug_hunter_keys
    assert set(reviewer_keys).isdisjoint(bug_hunter_keys), "agents must not share cache entries"


def test_changing_primary_model_invalidates_cache_entry(monkeypatch, fake_client, fake_redis):
    """Changing PRIMARY_MODEL must invalidate the old cache entry."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis

    key_before = _expected_cache_key("reviewer", "python", "review this code")
    fake.setex(key_before, 86400, json.dumps({"response": "old model result", "provider_provenance": {}}))

    # Old model -> cache hit, no LLM call.
    fake_client.responses = []
    result_old = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result_old == "old model result"
    assert fake_client.calls == [], "no LLM call on cache hit with old model"

    # Switch primary model -> old entry must not match; LLM called.
    monkeypatch.setattr(agents, "PRIMARY_MODEL", "gemini-2.0-flash")
    fake_client.responses = [_make_gemini_response("new model result")]
    result_new = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result_new == "new model result"
    assert len(fake_client.calls) == 1, "LLM must be called after primary model change"


def test_changing_groq_model_invalidates_cache_entry(monkeypatch, fake_client, fake_redis):
    """Changing GROQ_MODEL must invalidate the old cache entry."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis

    key_before = _expected_cache_key("reviewer", "python", "review this code")
    fake.setex(key_before, 86400, json.dumps({"response": "old groq result", "provider_provenance": {}}))

    fake_client.responses = []
    result_old = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result_old == "old groq result"
    assert fake_client.calls == [], "no LLM call on cache hit with old groq model"

    monkeypatch.setattr(agents, "GROQ_MODEL", "openai/gpt-oss-20b")
    fake_client.responses = [_make_gemini_response("new groq result")]
    result_new = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result_new == "new groq result"
    assert len(fake_client.calls) == 1, "LLM must be called after groq model change"


def test_changing_prompt_content_invalidates_cache_entry(monkeypatch, fake_client, fake_redis):
    """Changing prompt content must invalidate the old cache entry."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis

    key_a = _expected_cache_key("reviewer", "python", "prompt A")
    fake.setex(key_a, 86400, json.dumps({"response": "cached A", "provider_provenance": {}}))

    result_a = asyncio.run(agents.call_llm("prompt A", "python", agent="reviewer"))
    assert result_a == "cached A"
    assert fake_client.calls == [], "no LLM call on cache hit"

    # Different prompt content -> cache miss -> LLM called.
    fake_client.responses = [_make_gemini_response("fresh B")]
    result_b = asyncio.run(agents.call_llm("prompt B", "python", agent="reviewer"))
    assert result_b == "fresh B"
    assert len(fake_client.calls) == 1, "LLM must be called when prompt content changes"


# ── Safe cache-miss handling for malformed / invalid payloads ──
def test_malformed_cache_payload_causes_safe_miss(monkeypatch, fake_client, fake_redis):
    """Malformed JSON in cache must not crash; treated as a cache miss."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis
    key = _expected_cache_key("reviewer", "python", "review this code")
    fake.setex(key, 86400, "{not valid json")

    fake_client.responses = [_make_gemini_response("fresh result")]
    result = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result == "fresh result"
    assert len(fake_client.calls) == 1, "LLM must be called on malformed cache payload"


def test_cache_payload_without_response_causes_safe_miss(monkeypatch, fake_client, fake_redis):
    """A cache payload lacking a valid 'response' string must not be returned as a hit."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis
    key = _expected_cache_key("reviewer", "python", "review this code")
    fake.setex(key, 86400, json.dumps({"provider_provenance": {}}))

    fake_client.responses = [_make_gemini_response("fresh result")]
    result = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result == "fresh result"
    assert len(fake_client.calls) == 1, "LLM must be called when response is missing"


def test_cache_payload_with_empty_response_causes_safe_miss(monkeypatch, fake_client, fake_redis):
    """A cache payload with an empty response string must not be returned as a hit."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis
    key = _expected_cache_key("reviewer", "python", "review this code")
    fake.setex(key, 86400, json.dumps({"response": "", "provider_provenance": {}}))

    fake_client.responses = [_make_gemini_response("fresh result")]
    result = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result == "fresh result"
    assert len(fake_client.calls) == 1, "LLM must be called when response is empty"


def test_non_dict_cache_payload_causes_safe_miss(monkeypatch, fake_client, fake_redis):
    """A non-dict cache payload (e.g. a bare string) must not be returned as a hit."""
    _set_llm_mode(monkeypatch, enabled=True, stub=False)
    fake, calls = fake_redis
    key = _expected_cache_key("reviewer", "python", "review this code")
    fake.setex(key, 86400, "just a string")

    fake_client.responses = [_make_gemini_response("fresh result")]
    result = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result == "fresh result"
    assert len(fake_client.calls) == 1, "LLM must be called on non-dict cache payload"


@pytest.fixture
def monkeypatch_cb(monkeypatch):
    return monkeypatch
