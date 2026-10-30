import asyncio
import os
import hashlib
import time
import random
import httpx
import redis
from dotenv import load_dotenv
from pathlib import Path
from typing import Optional, TypedDict, Dict, Any, Tuple
from langgraph.graph import StateGraph, END

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

MCP_BASE = os.getenv("MCP_BASE", "http://127.0.0.1:8000")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip(" '\"\t\r\n")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip(" '\"\t\r\n")

# ── LLM_ENABLED: when "0", skip all LLM network requests ───────
LLM_ENABLED = os.getenv("LLM_ENABLED", "1").strip() == "1"

# Preserve deterministic stub mode (no network calls)
LLM_STUB = os.getenv("LLM_STUB", "0").lower() == "1"

# Model names configurable through environment variables
PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Bounded retry configuration with exponential backoff
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BASE_BACKOFF = float(os.getenv("BASE_BACKOFF", "0.5"))

# ─── Safe latency controls (interactive requests) ───────────────
# Interactive retry budget: small, bounded. Only transient errors
# (HTTP 429/5xx) are retried; auth/invalid-model/malformed requests
# are never retried.
MAX_INTERACTIVE_RETRIES = int(os.getenv("MAX_INTERACTIVE_RETRIES", "2"))
MAX_INTERACTIVE_RETRIES = max(0, MAX_INTERACTIVE_RETRIES)
MAX_NONINTERACTIVE_RETRIES = int(os.getenv("MAX_NONINTERACTIVE_RETRIES", "6"))

# Short bounded backoff with jitter (seconds). Cap removes pathological
# tail latency from exponential growth.
BASE_BACKOFF = float(os.getenv("BASE_BACKOFF", "0.5"))
MAX_BACKOFF = float(os.getenv("MAX_BACKOFF", "3.0"))

# Gemini circuit breaker
GEMINI_CB_ENABLED = os.getenv("GEMINI_CB_ENABLED", "1").strip().lower() in ("1", "true", "yes")
GEMINI_CB_THRESHOLD = int(os.getenv("GEMINI_CB_THRESHOLD", "3"))  # consecutive transient failures to open
GEMINI_CB_COOLDOWN = float(os.getenv("GEMINI_CB_COOLDOWN", "30.0"))  # seconds before recovery probe

# Max concurrent provider requests (shared across all four agents).
MAX_CONCURRENT_PROVIDER_REQUESTS = int(os.getenv("MAX_CONCURRENT_PROVIDER_REQUESTS", "2"))

# Per-provider timeout (seconds). Prevents any single call from blocking
# the process for a long time.
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "8.0"))
GROQ_TIMEOUT = float(os.getenv("GROQ_TIMEOUT", "8.0"))

# Output token limit per LLM response (0 = no limit).
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "2048"))
GROQ_MAX_OUTPUT_TOKENS = int(os.getenv("GROQ_MAX_OUTPUT_TOKENS", "2048"))

def _is_retryable_http_status(code: int) -> bool:
    """Return True only for transient HTTP errors: 429 and 5xx."""
    return code == 429 or 500 <= code < 600

# Reliability: Identify transient network exceptions that are safe to retry.
def _is_retryable_exception(exc: BaseException) -> bool:
    """Identify transient network exceptions that are safe to retry."""
    return isinstance(
        exc,
        (
            TimeoutError,
            asyncio.TimeoutError,
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.NetworkError,
            httpx.ProxyError,
        ),
    )

# ─── Shared Gemini quota control ──────────────────────────────────────
# Single in-process limiter shared by all four LangGraph agents so that
# concurrent agent executions cannot burst past the free-tier budget.
# NOTE: This coordinates requests within ONE running application instance.
# API quotas are enforced per Google Cloud project (shared across all API
# keys and all application instances in the same project), so multiple
# simultaneous instances are NOT globally coordinated by this limiter.
GEMINI_MAX_CONCURRENT_REQUESTS = int(os.getenv("GEMINI_MAX_CONCURRENT_REQUESTS", "2"))
GEMINI_MAX_REQUESTS_PER_MINUTE = int(os.getenv("GEMINI_MAX_REQUESTS_PER_MINUTE", "15"))
GEMINI_MAX_INPUT_TOKENS_PER_MINUTE = int(os.getenv("GEMINI_MAX_INPUT_TOKENS_PER_MINUTE", "0"))
GEMINI_SAFETY_MARGIN = float(os.getenv("GEMINI_SAFETY_MARGIN", "0.8"))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "2048"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", str(MAX_RETRIES)))

# Separate Groq request/retry controls (fallback provider).
GROQ_MAX_CONCURRENT_REQUESTS = int(os.getenv("GROQ_MAX_CONCURRENT_REQUESTS", "2"))
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", str(MAX_RETRIES)))

def estimate_tokens(text: str) -> int:
    """Rough input-token estimate (chars / 4). Treated as an estimate only."""
    if not text:
        return 0
    return max(1, len(text) // 4)

class GeminiQuotaLimiter:
    """Shared in-process quota controller for Gemini requests."""

    def __init__(self) -> None:
        self.max_concurrent = max(1, GEMINI_MAX_CONCURRENT_REQUESTS)
        self.max_rpm = max(0, GEMINI_MAX_REQUESTS_PER_MINUTE)
        self.max_tpm = max(0, GEMINI_MAX_INPUT_TOKENS_PER_MINUTE)
        self.safety_margin = min(1.0, max(0.0, GEMINI_SAFETY_MARGIN))
        self.max_output_tokens = max(0, GEMINI_MAX_OUTPUT_TOKENS)
        self.max_retries = max(0, GEMINI_MAX_RETRIES)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._request_times = []
        self._token_window_tokens = 0
        self._token_window_start = time.monotonic()
        self.stats = {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "retries": 0,
            "provider_gemini": 0,
            "provider_groq": 0,
            "budget_rejected": 0,
        }

    def _expire_window(self, now):
        cutoff = now - 60.0
        self._request_times = [t for t in self._request_times if t > cutoff]
        if now - self._token_window_start >= 60.0:
            self._token_window_tokens = 0
            self._token_window_start = now

    def _within_budget(self, est_input_tokens):
        now = time.monotonic()
        self._expire_window(now)
        if self.max_rpm and len(self._request_times) >= int(self.max_rpm * self.safety_margin):
            return False, "rpm"
        if self.max_tpm and (self._token_window_tokens + est_input_tokens) > int(self.max_tpm * self.safety_margin):
            return False, "tpm"
        return True, ""

    async def acquire(self, est_input_tokens=0):
        await self._semaphore.acquire()
        try:
            ok, reason = self._within_budget(est_input_tokens)
            if not ok:
                self.stats["budget_rejected"] += 1
                self.release()
                return reason
            self.stats["requests"] += 1
            self.stats["input_tokens"] += est_input_tokens
            now = time.monotonic()
            self._expire_window(now)
            self._request_times.append(now)
            self._token_window_tokens += est_input_tokens
            return "ok"
        except Exception:
            self.release()
            raise

    def release(self):
        try:
            self._semaphore.release()
        except ValueError:
            pass

    def record_output_tokens(self, tokens):
        if tokens:
            self.stats["output_tokens"] += tokens

quota_limiter = GeminiQuotaLimiter()
_groq_semaphore = asyncio.Semaphore(max(1, GROQ_MAX_CONCURRENT_REQUESTS))

def parse_retry_after(headers, default=0.0):
    try:
        raw = headers.get("retry-after") if hasattr(headers, "get") else None
        if raw is None:
            return default
        return float(raw)
    except Exception:
        return default

# ─── Structured timing instrumentation ──────────────────────────
class _Timing:
    """Records per-call, per-agent timing for observability.

    Attributes:
        agent: identifier of the calling agent (e.g. 'reviewer').
        start: monotonic start timestamp.
        gemini_durations: list of (http_status_or_None, duration_s).
        groq_durations: list of (http_status_or_None, duration_s).
        cache_hit: whether a Redis cache hit served the response.
    """

    def __init__(self, agent: str = "<root>") -> None:
        self.agent = agent
        self.start = time.perf_counter()
        self.gemini_durations = []  # (status, duration_s)
        self.groq_durations = []    # (status, duration_s)
        self.cache_hit = False

    def gemini_attempt(self, status: Optional[int], duration: float) -> None:
        self.gemini_durations.append((status, duration))

    def groq_attempt(self, status: Optional[int], duration: float) -> None:
        self.groq_durations.append((status, duration))

    def duration(self) -> float:
        return time.perf_counter() - self.start

# ─── Gemini circuit breaker ─────────────────────────────────────
class GeminiCircuitBreaker:
    """Process-local circuit breaker for the primary Gemini model.

    State:
      * CLOSED  – normal operation; Gemini is attempted.
      * OPEN    – transient failures exceeded the threshold; requests are
                  routed directly to Groq without contacting Gemini.
      * HALF    – recovery probe window; a limited number of probes are
                  allowed to test whether Gemini has recovered.

    Thread/atomicity:
      This class is *process-local*. It uses `asyncio.Lock` to coordinate
      state transitions inside a single running application instance's
      event loop. It is **not** distributed: four agents running inside
      one process share one breaker, but separate application instances
      (e.g. separate Docker containers / processes) each keep their own
      independent state. A stateless probe endpoint cannot read or write
      this state.

    Verdict semantics:
      * trip(agent)  – called after consecutive transient failures;
                         returns True when the breaker should move to
                         OPEN (i.e. threshold reached).
      * probe()      – called at the start of a Gemini request when
                         OPEN. Returns True if the cooldown has elapsed
                         and the breaker should attempt a recovery probe;
                         returns False to skip Gemini entirely.
      * reset()      – called only on a successful Gemini response; moves
                       the breaker back to CLOSED.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF = "half"

    def __init__(
        self,
        enabled: bool = True,
        threshold: int = GEMINI_CB_THRESHOLD,
        cooldown: float = GEMINI_CB_COOLDOWN,
    ) -> None:
        self.enabled = enabled
        self.threshold = max(1, threshold)
        self.cooldown = max(0.0, cooldown)
        self.state = self.CLOSED
        self._consecutive_failures = 0
        self._open_since = 0.0
        self._lock = asyncio.Lock()
        self.stats = {
            "trips": 0,
            "resets": 0,
            "probes_attempted": 0,
            "probes_skipped": 0,
        }

    async def trip(self, agent: str, permanent: bool = False) -> bool:
        """Record a Gemini failure.

        Args:
            agent: Agent identifier for logging.
            permanent: If True, treat as permanent error (400, 401) and
                immediately open the circuit. If False, treat as transient
                failure and use threshold-based opening.

        Returns True when the circuit should move to OPEN.
        """
        async with self._lock:
            if not self.enabled:
                return False
            if self.state == self.OPEN:
                # Keep a rolling count of consecutive failures while open;
                # the breaker stays open until a success resets it.
                self._consecutive_failures += 1
                return False
            self._consecutive_failures += 1
            if permanent or self._consecutive_failures >= self.threshold:
                self.state = self.OPEN
                self._open_since = time.monotonic()
                self.stats["trips"] += 1
                if permanent:
                    print(
                        f"🚨 [Gemini CircuitBreaker] {agent}: "
                        f"permanent error -> OPEN"
                    )
                else:
                    print(
                        f"🚨 [Gemini CircuitBreaker] {agent}: "
                        f"{self._consecutive_failures} consecutive transient "
                        f"failures -> OPEN"
                    )
                return True
            return False

    async def probe(self) -> bool:
        """Decide whether to attempt a recovery probe (HALF state)."""
        async with self._lock:
            if not self.enabled:
                return False
            if self.state != self.OPEN:
                return False
            if time.monotonic() - self._open_since < self.cooldown:
                self.stats["probes_skipped"] += 1
                return False
            # Move to HALF so we allow a limited number of probes.
            self.state = self.HALF
            self.stats["probes_attempted"] += 1
            print(
                f"🔎 [Gemini CircuitBreaker] Recovery probe scheduled "
                f"(HALF, since {self._open_since:.1f})"
            )
            return True

    async def reset(self) -> None:
        """Reset the circuit to CLOSED after a successful Gemini request."""
        async with self._lock:
            if not self.enabled:
                return
            if self.state == self.CLOSED:
                return
            self.state = self.CLOSED
            self._consecutive_failures = 0
            self.stats["resets"] += 1
            print("✅ [Gemini CircuitBreaker] Reset to CLOSED")

# Shared circuit breaker instance (process-local, asyncio.Lock).
gemini_circuit_breaker = GeminiCircuitBreaker(
    enabled=GEMINI_CB_ENABLED,
    threshold=GEMINI_CB_THRESHOLD,
    cooldown=GEMINI_CB_COOLDOWN,
)
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    redis_client.ping()
    REDIS_AVAILABLE = True
    print("⚡ CodeNexus AI: Redis Active (AST Caching + Multi-LLM Router Enabled)")
except Exception:
    REDIS_AVAILABLE = False
    redis_client = None
    print("ℹ️ CodeNexus AI: Redis Offline — Direct LLM Fallback Active")

class AgentState(TypedDict):
    filename: str
    content: str
    language: str
    review: str
    bugs: str
    refactored: str
    docs: str

async def call_groq(prompt: str, agent: str = "agent") -> Optional[str]:
    if not GROQ_API_KEY:
        return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "You are a Senior Staff Software Engineer and Security Auditor. Provide thorough, detailed code analysis with deep explanations and clear markdown formatting."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 2048
    }

    for attempt in range(GROQ_MAX_RETRIES):
        backoff = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** attempt))
        try:
            async with httpx.AsyncClient(timeout=GROQ_TIMEOUT) as client:
                r = await client.post(url, headers=headers, json=payload)
                if r.status_code == 200:
                    print(f"⚡ [Groq Success] GPT OSS 120B response generated!")
                    return r.json()["choices"][0]["message"]["content"]
                elif _is_retryable_http_status(r.status_code):
                    if attempt < GROQ_MAX_RETRIES - 1:
                        print(f"⚠️ [Groq Retry] HTTP {r.status_code} on attempt {attempt + 1}/{GROQ_MAX_RETRIES}, retrying in {backoff:.1f}s...")
                        await asyncio.sleep(backoff)
                        continue
                else:
                    print(f"⚠️ [Groq Warning] HTTP {r.status_code} (not retrying - permanent error)")
                    return None
        except Exception as e:
            if _is_retryable_exception(e):
                if attempt < GROQ_MAX_RETRIES - 1:
                    print(f"⚠️ [Groq Retry] {e}")
                    await asyncio.sleep(backoff)
                    continue
            print(f"⚠️ [Groq Warning]: {e}")
            return None
    print(f"❌ [Groq Failed] All {GROQ_MAX_RETRIES} attempts exhausted")
    return None

# ─── Gemini API Call ─────────────────────────────────────────
async def call_gemini_raw(prompt: str, agent: str = "agent") -> Optional[str]:
    """Call the configured PRIMARY Gemini model with quota control + bounded retries.

    Only the primary model is attempted here; provider failure is escalated to
    call_llm, which falls back to Groq. This avoids burning quota on a second
    Gemini model before trying the configured fallback provider.
    """
    if not GEMINI_API_KEY:
        return None

    est_input = estimate_tokens(prompt)
    verdict = await quota_limiter.acquire(est_input)
    if verdict != "ok":
        print(f"⚠️ [Gemini Budget] {agent}: request rejected ({verdict}), falling back.")
        return None

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if quota_limiter.max_output_tokens:
        payload["generationConfig"] = {"maxOutputTokens": quota_limiter.max_output_tokens}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{PRIMARY_MODEL}:generateContent"
    last_status = None
    acquired = True
    timing = _Timing(agent=agent)
    try:
        for attempt in range(quota_limiter.max_retries + 1):
            backoff = BASE_BACKOFF * (2 ** attempt)
            try:
                async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT) as client:
                    r = await client.post(url, headers=headers, json=payload)
                    last_status = r.status_code
                    duration = time.perf_counter() - timing.start
                    timing.gemini_attempt(last_status, duration)
                    if r.status_code == 200:
                        data = r.json()
                        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                        res = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                        usage = data.get("usageMetadata", {})
                        out_tok = usage.get("outputTokenCount") or estimate_tokens(res)
                        quota_limiter.record_output_tokens(out_tok)
                        if res.strip():
                            await gemini_circuit_breaker.reset()
                            quota_limiter.stats["provider_gemini"] += 1
                            print(f"✅ [Gemini Success] {PRIMARY_MODEL} ({agent})")
                            return res
                    elif _is_retryable_http_status(r.status_code):
                        # On each transient error, trip the circuit breaker.
                        # If the threshold is reached, the circuit opens and we
                        # stop retrying. Otherwise we may retry if budget remains.
                        retry_after = parse_retry_after(getattr(r, "headers", {}))
                        delay = retry_after if retry_after > 0 else backoff
                        await gemini_circuit_breaker.trip(agent)
                        if gemini_circuit_breaker.state == gemini_circuit_breaker.OPEN:
                            print(f"⚠️ [Gemini Retry] {agent} HTTP {r.status_code} (circuit opened, not retrying)")
                            break
                        # Circuit stayed CLOSED; retry if budget permits
                        if attempt < quota_limiter.max_retries:
                            if attempt == 0:
                                print(f"⚠️ [Gemini Retry] {agent} HTTP {r.status_code}, retrying in {delay:.1f}s...")
                            else:
                                print(f"⚠️ [Gemini Retry] {agent} HTTP {r.status_code} (attempt {attempt + 1}), retrying in {delay:.1f}s...")
                            quota_limiter.stats["retries"] += 1
                            await asyncio.sleep(delay)
                            continue
                        else:
                            print(f"⚠️ [Gemini Retry] {agent} HTTP {r.status_code} (max retries exhausted)")
                            break
                    else:
                        # Permanent error (auth, invalid model, malformed): do NOT
                        # retry and do NOT route to Groq — trip the circuit so we
                        # don't waste quota on a misconfigured provider.
                        await gemini_circuit_breaker.trip(agent, permanent=True)
                        print(f"⚠️ [Gemini] {agent} HTTP {r.status_code} (permanent, not retrying)")
                        break
            except Exception as e:
                duration = time.perf_counter() - timing.start
                timing.gemini_attempt(None, duration)
                if _is_retryable_exception(e):
                    await gemini_circuit_breaker.trip(agent)
                    if gemini_circuit_breaker.state == gemini_circuit_breaker.OPEN:
                        print(f"⚠️ [Gemini Retry] {agent} {e} (circuit opened, not retrying)")
                        break
                    # Circuit stayed CLOSED; retry if budget permits
                    if attempt < quota_limiter.max_retries:
                        if attempt == 0:
                            print(f"⚠️ [Gemini Retry] {agent}: {e}")
                        else:
                            print(f"⚠️ [Gemini Retry] {agent} attempt {attempt + 1}, retrying in {backoff:.1f}s...")
                        quota_limiter.stats["retries"] += 1
                        await asyncio.sleep(backoff)
                        continue
                    else:
                        print(f"⚠️ [Gemini Retry] {agent} {e} (max retries exhausted)")
                        break
                else:
                    # Auth/invalid model / malformed - do NOT retry, trip circuit.
                    await gemini_circuit_breaker.trip(agent, permanent=True)
                    print(f"⚠️ [Gemini] {agent}: permanent error {e} - no retry")
                    break
        # Circuit is OPEN - return None so call_llm can handle Groq failover.
        if gemini_circuit_breaker.state == gemini_circuit_breaker.OPEN:
            print(f"🔁 [Gemini CircuitBreaker] OPEN -> returning None for failover (agent={agent})")
        print(f"❌ [Gemini Failed] {agent} all attempts exhausted (last HTTP {last_status})")
        return None
    finally:
        if acquired:
            quota_limiter.release()

# ─── Universal Multi-LLM Router ─────────────────────────────
async def call_llm(prompt: str, language: str = "", agent: str = "agent") -> str:
    # Honor deterministic stub mode (no network calls)
    if LLM_STUB:
        return generate_deep_fallback(prompt, language)

    # LLM_DISABLED: skip all network requests, return clearly marked unavailable
    if not LLM_ENABLED:
        return _unavailable_result(prompt, language, "LLM_DISABLED")

    # 1. Check Redis Cache
    content_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    cache_key = f"codenexus:cache:{agent}:{PRIMARY_MODEL}:{content_hash}"

    if REDIS_AVAILABLE:
        try:
            cached_val = redis_client.get(cache_key)
            if cached_val:
                print(f"⚡ [Redis Hit] Hash {content_hash[:8]}... served instantly.")
                return cached_val
        except Exception:
            pass

    provider_used = None

    # 2. Try Gemini Primary with circuit breaker logic
    skip_gemini = False
    if gemini_circuit_breaker.state == gemini_circuit_breaker.OPEN:
        probe_ok = await gemini_circuit_breaker.probe()
        if not probe_ok:
            skip_gemini = True

    if not skip_gemini:
        res = await call_gemini_raw(prompt, agent=agent)
        if res:
            provider_used = "gemini"
        else:
            # 3. Fallback to Groq API if Gemini fails
            print("🔄 [Failover] Gemini unavailable! Calling Groq...")
            res = await call_groq(prompt, agent=agent)
            if res:
                provider_used = "groq"
    else:
        # Circuit is open and cooldown not elapsed; go directly to Groq
        print("🔄 [Failover] Gemini circuit OPEN and cooldown not elapsed, skipping Gemini and calling Groq...")
        res = await call_groq(prompt, agent=agent)
        if res:
            provider_used = "groq"

    # 4. If all providers fail or LLM disabled, return honest unavailable result
    if not res:
        print("⚠️ [Offline] All LLM providers unavailable.")
        return _unavailable_result(prompt, language, "PROVIDERS_FAILED")

    # Cache only successful provider responses
    if REDIS_AVAILABLE:
        try:
            redis_client.setex(cache_key, 86400, res)
        except Exception:
            pass

    return res

def _unavailable_result(prompt: str, language: str, reason: str) -> str:
    """Return a clearly marked unavailable result that downstream code can detect."""
    p_lower = prompt.lower()
    lang = language.capitalize() or "Python"

    if "review" in p_lower:
        section = "Review"
    elif "bug" in p_lower or "vulnerability" in p_lower:
        section = "Bug Analysis"
    elif "refactor" in p_lower:
        section = "Refactoring"
    else:
        section = "Documentation"

    reason_map = {
        "LLM_DISABLED": "LLM analysis disabled via LLM_ENABLED=0",
        "PROVIDERS_FAILED": "All LLM providers failed or returned errors",
    }
    reason_text = reason_map.get(reason, reason)

    return (
        f"### ⚠️ {section} Unavailable ({lang})\n\n"
        f"**Reason**: {reason_text}\n\n"
        f"**Static analysis findings** (from local scanners) are still available in the MCP response.\n"
        f"LLM-enhanced {section.lower()} could not be generated.\n\n"
        f"---"
    )

# ─── MCP tool call helper ───────────────────────────────────────
async def call_mcp(endpoint: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{MCP_BASE}/code/{endpoint}", json=payload)
            return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

def merge_artifacts(*dicts: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a sequence of agent result dicts, preserving the existing
    merge semantics (later dicts overwrite earlier ones on conflicts)."""
    merged: Dict[str, Any] = {}
    for d in dicts:
        merged.update(d or {})
    return merged

# ─── Deep Structured Fallback Generator ────────────────────
def generate_deep_fallback(prompt: str, language: str) -> str:
    """Deterministic stub response when LLM_STUB=1 (no network calls).

    Returns clearly marked stub output so downstream code can distinguish
    it from actual LLM-generated analysis.
    """
    p_lower = prompt.lower()
    lang = language.capitalize() or "Python"

    if "review" in p_lower:
        return (
            f"### 🛡️ {lang} Review — Stub Mode (No LLM)\n\n"
            f"*LLM analysis disabled. Run with LLM_ENABLED=1 and valid API keys for real analysis.*\\n\\n"
            f"- Enable LLM connectivity for architectural recommendations.\n"
        )
    elif "bug" in p_lower or "vulnerability" in p_lower:
        return (
            f"### ✳ {lang} Bug Inspection — Stub Mode (No LLM)\n\n"
            f"*LLM analysis disabled. Run with LLM_ENABLED=1 and valid API keys for real analysis.*\n\n"
            f"- Static analysis from scanners is available via MCP.\n"
            f"- Enable LLM connectivity for detailed vulnerability reasoning.\n"
        )
    elif "refactor" in p_lower:
        return (
            f"// {lang} Refactored Code — Stub Mode (No LLM)\n"
            f"// *LLM refactoring disabled. Enable LLM_ENABLED=1 for real analysis.*\n"
            f"// Production refactoring will populate when API connectivity is restored.\n"
        )
    else:
        return (
            f"/** {lang} Documentation — Stub Mode (No LLM)\n"
            f" * *LLM documentation disabled. Enable LLM_ENABLED=1 for real analysis.*\n"
            f" * Detailed contract descriptions and inline comments will populate when API connectivity is restored.\n"
            f" */"
        )

# ─── MCP Tool Call Helper ────────────────────────────────────
async def call_mcp(endpoint: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{MCP_BASE}/code/{endpoint}", json=payload)
            return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

# ─── LangGraph Agent Nodes ─────────────────────────────────
async def reviewer_node(state: AgentState) -> dict:
    mcp_res = await call_mcp("analyze", {"filename": state["filename"], "content": state["content"], "language": state["language"]})
    raw_issues = mcp_res.get("issues", ["No static syntax issues found."])
    issues_str = "\n- ".join(raw_issues)

    prompt = (
        f"You are a Senior Software Architect conducting an in-depth code review.\n"
        f"Language: {state['language']}\n"
        f"Source Code:\n```{state['language']}\n{state['content']}\n```\n\n"
        f"Static Analysis Findings:\n- {issues_str}\n\n"
        f"Provide a comprehensive, detailed architectural review. Cover:\n"
        f"1. Code Health & Design Patterns\n"
        f"2. Performance & Algorithmic Complexity\n"
        f"3. Actionable Engineering Improvements."
    )
    review = await call_llm(prompt, state["language"], agent="reviewer")
    return {"review": review}

async def bug_hunter_node(state: AgentState) -> dict:
    mcp_res = await call_mcp("find_bugs", {"filename": state["filename"], "content": state["content"], "language": state["language"]})
    raw_bugs = mcp_res.get("bugs", ["No static bugs found."])
    bugs_str = "\n- ".join(raw_bugs)

    prompt = (
        f"You are a Lead Security Engineer & Bug Hunter.\n"
        f"Language: {state['language']}\n"
        f"Source Code:\n```{state['language']}\n{state['content']}\n```\n\n"
        f"Static Scanner Hints:\n- {bugs_str}\n\n"
        f"Perform a thorough logical bug inspection. For each issue:\n"
        f"- Explain WHY the bug occurs\n"
        f"- Point out the exact code flaw\n"
        f"- Detail how to fix it step by step."
    )
    res_bugs = await call_llm(prompt, state["language"], agent="bug_hunter")
    return {"bugs": res_bugs}

async def refactor_node(state: AgentState) -> dict:
    mcp_res = await call_mcp("refactor", {"filename": state["filename"], "content": state["content"], "language": state["language"]})
    ref_base = mcp_res.get("refactored_code", state["content"])
    prompt = (
        f"Refactor this {state['language']} code to production standards.\n"
        f"Return ONLY valid refactored {state['language']} code inside code blocks:\n"
        f"```{state['language']}\n{ref_base}\n```"
    )
    refactored = await call_llm(prompt, state["language"], agent="refactor")
    return {"refactored": refactored if len(refactored) > 10 else ref_base}

async def docs_node(state: AgentState) -> dict:
    mcp_res = await call_mcp("document", {"filename": state["filename"], "content": state["content"], "language": state["language"]})
    doc_base = mcp_res.get("documented_code", state["content"])
    prompt = (
        f"Add production docstrings and inline commentary to this {state['language']} code:\n"
        f"```{state['language']}\n{doc_base}\n```"
    )
    docs = await call_llm(prompt, state["language"], agent="documentation")
    return {"docs": docs if len(docs) > 10 else doc_base}

# ─── LangGraph Graph Setup ──────────────────────────────────
workflow = StateGraph(AgentState)
workflow.add_node("reviewer", reviewer_node)
workflow.add_node("bug_hunter", bug_hunter_node)
workflow.add_node("refactor", refactor_node)
workflow.add_node("documentation", docs_node)

workflow.set_entry_point("reviewer")
workflow.add_edge("reviewer", "bug_hunter")
workflow.add_edge("bug_hunter", "refactor")
workflow.add_edge("refactor", "documentation")
workflow.add_edge("documentation", END)

graph_engine = workflow.compile()

async def run_pipeline(filename: str, language: str, content: str) -> dict:
    initial_state = {
        "filename": filename,
        "content": content,
        "language": language,
        "review": "",
        "bugs": "",
        "refactored": "",
        "docs": ""
    }
    return await graph_engine.ainvoke(initial_state)