"""
Tests for the AI Usage Tracking & Token Cost Guard (usage_meter.py).

Covers:
  1. Gemini input/output token extraction from provider usageMetadata.
  2. Groq input/output token extraction from provider usage block.
  3. Estimated cost calculation (input/output/total, per-million pricing).
  4. Request limit enforcement (max requests per session).
  5. Estimated cost limit enforcement (max cost per session).
  6. Token limit enforcement (max tokens per request).
  7. No provider fallback bypass when limits are reached (call_llm).
  8. Missing usage metadata handling (values recorded as 0, not invented).
  9. Existing LLM reliability tests still pass (see tests/test_llm_reliability.py).
 10. Existing stub mode still works (see tests/test_llm_reliability.py,
     tests/test_web_gateway.py).

No real AI API calls are made. All HTTP calls are mocked.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

# Force deterministic env BEFORE importing agents/usage_meter so module
# globals are test-safe and no real network is attempted.
os.environ.setdefault("LLM_ENABLED", "1")
os.environ.setdefault("LLM_STUB", "0")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6379")

import agents  # noqa: E402
from usage_meter import usage_meter  # noqa: E402


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


def _make_gemini_response(usage_metadata=None, text="gemini response"):
    body = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    if usage_metadata is not None:
        body["usageMetadata"] = usage_metadata
    return _FakeResponse(200, body)


def _make_groq_response(usage=None, text="groq response"):
    body = {"choices": [{"message": {"content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return _FakeResponse(200, body)


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
def _reset_usage_meter():
    """Reset the singleton usage meter before each test."""
    usage_meter.reset()
    # Reset the shared circuit breaker so tests don't leak OPEN state.
    agents.gemini_circuit_breaker.state = agents.gemini_circuit_breaker.CLOSED
    agents.gemini_circuit_breaker._consecutive_failures = 0
    agents.gemini_circuit_breaker._open_since = 0.0
    yield
    usage_meter.reset()


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch):
    monkeypatch.setattr(agents, "REDIS_AVAILABLE", False)
    monkeypatch.setattr(agents, "redis_client", None)


# ── 1. Gemini input/output token extraction ──


def test_gemini_usage_extraction_from_usage_metadata(monkeypatch, fake_client):
    """Gemini usageMetadata inputTokenCount/outputTokenCount are captured."""
    _set_live(monkeypatch)
    fake_client.responses = [
        _make_gemini_response({"inputTokenCount": 120, "outputTokenCount": 45})
    ]
    result = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result == "gemini response"
    summary = usage_meter.get_summary()
    assert summary.total_requests == 1
    assert summary.input_tokens == 120, summary
    assert summary.output_tokens == 45, summary
    assert summary.total_tokens == 165, summary


# ── 2. Groq input/output token extraction ──


def test_groq_usage_extraction_from_usage_block(monkeypatch, fake_client):
    """Groq usage prompt_tokens/completion_tokens are captured on fallback."""
    _set_live(monkeypatch)
    # Gemini fails on all retry attempts (max_retries + 1 = 4), then Groq fallback.
    gemini_failures = [_FakeResponse(503) for _ in range(agents.GEMINI_MAX_RETRIES + 1)]
    fake_client.responses = gemini_failures + [
        _make_groq_response({"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280}),
    ]
    result = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result == "groq response"
    summary = usage_meter.get_summary()
    assert summary.total_requests == 1
    assert summary.input_tokens == 200, summary
    assert summary.output_tokens == 80, summary
    assert summary.total_tokens == 280, summary


# ── 3. Estimated cost calculation ──


def test_estimated_cost_calculation_input_and_output_separate():
    """Cost is calculated per-million-token, input and output separately."""
    usage_meter.reset()
    # GEMINI_3_6_FLASH: input $0.075/1M, output $0.30/1M
    input_cost, output_cost, total_cost = usage_meter.estimate_cost(
        "gemini-3.6-flash", input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert input_cost == 0.075
    assert output_cost == 0.30
    assert total_cost == 0.375

    # Partial tokens scale linearly.
    input_cost, output_cost, total_cost = usage_meter.estimate_cost(
        "gemini-3.6-flash", input_tokens=500_000, output_tokens=250_000
    )
    assert input_cost == 0.0375
    assert output_cost == 0.075
    assert total_cost == pytest.approx(0.1125)


def test_estimated_cost_missing_pricing_yields_zero_labelled_estimate():
    """Unknown model -> zero cost (estimate, never presented as actual billing)."""
    usage_meter.reset()
    input_cost, output_cost, total_cost = usage_meter.estimate_cost(
        "unknown-model", input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert input_cost == 0.0
    assert output_cost == 0.0
    assert total_cost == 0.0


# ── 4. Request limit enforcement ──


def test_request_limit_enforcement():
    """When max requests per session is reached, further calls are rejected."""
    usage_meter.reset()
    usage_meter.max_requests_per_session = 2
    usage_meter.enabled = True

    assert usage_meter.check_limits(estimated_input_tokens=10) is None
    usage_meter.record_usage("gemini", "gemini-3.6-flash", 10, 10)
    assert usage_meter.check_limits(estimated_input_tokens=10) is None
    usage_meter.record_usage("gemini", "gemini-3.6-flash", 10, 10)
    # Limit reached (2 requests used).
    reason = usage_meter.check_limits(estimated_input_tokens=10)
    assert reason == "max_requests_per_session"
    summary = usage_meter.get_summary()
    assert summary.limit_reached is True
    assert summary.limit_reason == "max_requests_per_session"


# ── 5. Estimated cost limit enforcement ──


def test_estimated_cost_limit_enforcement():
    """When max estimated cost per session is reached, calls are rejected."""
    usage_meter.reset()
    usage_meter.max_estimated_cost_per_session = 0.001  # very small
    usage_meter.enabled = True

    # Record usage whose estimated cost exceeds the limit.
    usage_meter.record_usage("gemini", "gemini-3.6-flash", 1_000_000, 1_000_000)
    # input $0.075 + output $0.30 = $0.375 > $0.001 -> limit reached.
    reason = usage_meter.check_limits(estimated_input_tokens=0, estimated_output_tokens=0)
    assert reason == "max_estimated_cost_per_session"
    summary = usage_meter.get_summary()
    assert summary.limit_reached is True
    assert summary.limit_reason == "max_estimated_cost_per_session"


# ── 6. Token limit enforcement ──


def test_token_limit_enforcement_per_request():
    """When max tokens per request is set, oversized requests are rejected."""
    usage_meter.reset()
    usage_meter.max_tokens_per_request = 100
    usage_meter.enabled = True

    assert usage_meter.check_limits(estimated_input_tokens=40, estimated_output_tokens=40) is None
    reason = usage_meter.check_limits(estimated_input_tokens=60, estimated_output_tokens=60)
    assert reason == "max_tokens_per_request"


# ── 7. No provider fallback bypass when limits reached ──


def test_no_fallback_bypass_when_limits_reached(monkeypatch, fake_client):
    """call_llm rejects before contacting ANY provider when a limit is reached;
    Groq fallback is NOT used to bypass the guard."""
    _set_live(monkeypatch)
    usage_meter.reset()
    usage_meter.max_requests_per_session = 1
    usage_meter.enabled = True

    # Consume the single allowed request via Gemini.
    fake_client.responses = [_make_gemini_response({"inputTokenCount": 10, "outputTokenCount": 5})]
    r1 = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert r1 == "gemini response"

    # Second request must be rejected outright — no Gemini attempt, no Groq fallback.
    fake_client.responses = []  # nothing left; if called, test should fail via no response
    r2 = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert "usage limit" in r2.lower() or "unavailable" in r2.lower(), r2
    # Exactly one provider call was made total (the first successful Gemini call).
    # The rejected second request made zero additional calls.
    assert len(fake_client.calls) == 1, f"Provider called despite limit: {fake_client.calls}"
    summary = usage_meter.get_summary()
    assert summary.limit_reached is True


# ── 8. Missing usage metadata handling ──


def test_gemini_missing_usage_metadata_recorded_as_zero(monkeypatch, fake_client):
    """Gemini response without usageMetadata -> input/output recorded as 0, not invented."""
    _set_live(monkeypatch)
    fake_client.responses = [_make_gemini_response(usage_metadata=None)]
    result = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result == "gemini response"
    summary = usage_meter.get_summary()
    assert summary.total_requests == 1
    assert summary.input_tokens == 0, summary
    assert summary.output_tokens == 0, summary
    assert summary.total_tokens == 0, summary


def test_groq_missing_usage_block_recorded_as_zero(monkeypatch, fake_client):
    """Groq response without usage block -> input/output recorded as 0, not invented."""
    _set_live(monkeypatch)
    # Gemini fails on all retry attempts, then Groq succeeds with no usage block.
    gemini_failures = [_FakeResponse(503) for _ in range(agents.GEMINI_MAX_RETRIES + 1)]
    fake_client.responses = gemini_failures + [
        _make_groq_response(usage=None),  # Groq succeeds, no usage
    ]
    result = asyncio.run(agents.call_llm("review this code", "python", agent="reviewer"))
    assert result == "groq response"
    summary = usage_meter.get_summary()
    assert summary.total_requests == 1
    assert summary.input_tokens == 0, summary
    assert summary.output_tokens == 0, summary


# ── 9. Reservation commit/release lifecycle ──


def test_reserve_commit_release_lifecycle():
    """reserve() takes a slot, commit() consumes it on success, release() frees it on failure."""
    usage_meter.reset()
    usage_meter.max_requests_per_session = 10
    usage_meter.enabled = True

    # Initially no reservations.
    assert usage_meter._reserved_requests == 0

    # Reserve a slot.
    ok = asyncio.run(usage_meter.reserve(estimated_input_tokens=10, estimated_output_tokens=0))
    assert ok is True
    assert usage_meter._reserved_requests == 1

    # Commit consumes the reserved slot (successful provider call).
    asyncio.run(usage_meter.commit())
    assert usage_meter._reserved_requests == 0


def test_release_frees_reservation_without_recording_usage():
    """release() frees a reserved slot without recording usage (failed provider call)."""
    usage_meter.reset()
    usage_meter.max_requests_per_session = 10
    usage_meter.enabled = True

    ok = asyncio.run(usage_meter.reserve(estimated_input_tokens=10, estimated_output_tokens=0))
    assert ok is True
    assert usage_meter._reserved_requests == 1

    # Release without commit: slot freed, usage not recorded.
    asyncio.run(usage_meter.release())
    assert usage_meter._reserved_requests == 0
    summary = usage_meter.get_summary()
    assert summary.total_requests == 0


def test_concurrent_reservations_respect_request_limit():
    """Concurrent reservations + completed requests cannot exceed max_requests_per_session."""
    usage_meter.reset()
    usage_meter.max_requests_per_session = 2
    usage_meter.enabled = True

    # Two concurrent reservations consume both slots.
    ok1 = asyncio.run(usage_meter.reserve(estimated_input_tokens=10, estimated_output_tokens=0))
    ok2 = asyncio.run(usage_meter.reserve(estimated_input_tokens=10, estimated_output_tokens=0))
    assert ok1 is True
    assert ok2 is True
    assert usage_meter._reserved_requests == 2

    # A third reservation must be rejected.
    ok3 = asyncio.run(usage_meter.reserve(estimated_input_tokens=10, estimated_output_tokens=0))
    assert ok3 is False
    assert usage_meter._reserved_requests == 2

    # Releasing one slot frees capacity.
    asyncio.run(usage_meter.release())
    ok4 = asyncio.run(usage_meter.reserve(estimated_input_tokens=10, estimated_output_tokens=0))
    assert ok4 is True


def test_reset_simulates_restart_clears_all_counters():
    """reset() (e.g. on process/container restart) clears all usage and reservation state."""
    usage_meter.reset()
    usage_meter.max_requests_per_session = 10
    usage_meter.enabled = True

    # Record usage and a reservation.
    usage_meter.record_usage("gemini", "gemini-3.6-flash", 100, 50)
    asyncio.run(usage_meter.reserve(estimated_input_tokens=10, estimated_output_tokens=0))

    summary_before = usage_meter.get_summary()
    # total_requests counts completed requests (1) plus reserved requests (1).
    assert summary_before.total_requests == 2
    assert summary_before.input_tokens == 100
    assert usage_meter._reserved_requests == 1

    # Simulate restart (new container/task).
    usage_meter.reset()

    summary_after = usage_meter.get_summary()
    assert summary_after.total_requests == 0
    assert summary_after.input_tokens == 0
    assert summary_after.output_tokens == 0
    assert summary_after.total_tokens == 0
    assert summary_after.estimated_total_cost_usd == 0.0
    assert usage_meter._reserved_requests == 0
    assert usage_meter._last_recorded_provider is None


# ── Usage-limit master switch tests ──


def test_usage_meter_enabled_by_default():
    """AI_USAGE_LIMIT_ENABLED defaults to '1' (enabled) for per-task protections."""
    from usage_meter import UsageMeter
    u = UsageMeter()
    assert u.enabled is True


def test_usage_meter_disabled_via_env():
    """AI_USAGE_LIMIT_ENABLED=0 disables the usage guard entirely."""
    from usage_meter import UsageMeter
    u = UsageMeter()
    u.enabled = False
    u.max_requests_per_session = 2
    u.max_estimated_cost_per_session = 1.00

    # With guard disabled, reservations beyond the nominal limit still succeed.
    ok1 = asyncio.run(u.reserve(estimated_input_tokens=10000000, estimated_output_tokens=0))
    ok2 = asyncio.run(u.reserve(estimated_input_tokens=10000000, estimated_output_tokens=0))
    assert ok1 is True
    assert ok2 is True
    u.release()
    u.release()


def test_usage_meter_enabled_blocks_excess_tokens():
    """AI_USAGE_LIMIT_ENABLED=1 preserves per-request token enforcement."""
    from usage_meter import UsageMeter
    u = UsageMeter()
    u.enabled = True
    u.max_requests_per_session = 1000
    u.max_estimated_cost_per_session = 999999.0
    u.max_tokens_per_request = 4000

    reason = u.check_limits(estimated_input_tokens=5000, estimated_output_tokens=0)
    assert reason == "max_tokens_per_request"


# ── Helpers ──


def _set_live(monkeypatch):
    monkeypatch.setattr(agents, "LLM_ENABLED", True)
    monkeypatch.setattr(agents, "LLM_STUB", False)
    monkeypatch.setattr(agents, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(agents, "GROQ_API_KEY", "test-groq-key")