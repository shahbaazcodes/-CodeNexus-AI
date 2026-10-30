import asyncio
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
@pytest.fixture
def monkeypatch_cb(monkeypatch):
    return monkeypatch
