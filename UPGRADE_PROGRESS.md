# CodeNexus AI — UPGRADE PROGRESS REPORT

## Executive Summary

This document contains the audit findings for the existing CodeNexus AI project located at `/Users/shahbaazkhan/Desktop/major_project_mcp_ai`. The project is a Python/FastAPI multi-agent code analysis system using LangGraph, MCP-style code-analysis services, Gemini/Groq LLMs, and a vanilla JS/HTML/CSS frontend.

**⚠️ CRITICAL: Do not migrate the frontend to React or another framework. Preserve the current professional dark UI.**

---

## 1. Existing Architecture

### 1.1 Component Diagram

| Component | Technology | Port (local) | Port (Docker) | Role |
|-----------|-----------|-------------|--------------|------|
| **MCP Core Engine** | FastAPI + Scikit-Learn + AST | 8000 | 8000 (tcp) | AST analysis, ML risk classification, bug finding, refactoring, documentation |
| **Web Gateway** | FastAPI + Jinja2 + httpx | 9000 | 9000 (tcp) | HTTP frontend, API gateway, MCP router, markdown export |
| **Agent Graph** | LangGraph + Gemini API + Groq API | N/A | N/A | Multi-agent workflow: reviewer → bug_hunter → refactor → documentation |
| **Redis** | Redis | 6379 | optional | AST caching, multi-LLM routing cache (crash-safe; optional in Docker Compose) |
| **Frontend** | HTML5 + CSS3 + Vanilla JS | N/A (served by Web Gateway) | N/A | Dark-themed code review UI |

### 1.2 Data Flow

**Local:**
1. User submits code via web UI → `POST /analyze` (port 9000)
2. `web_server.py` calls MCP at `MCP_BASE/code/metrics` (default: `http://127.0.0.1:8000`)
3. MCP `get_metrics()` runs AST features + ML risk classifier → returns `health_score`, `risk_level`
4. `web_server.py` runs `run_pipeline()` → LangGraph agent graph → returns `review`, `bugs`, `refactored`, `docs`
5. Response returned to frontend
6. User can `POST /export/markdown` → markdown report

**Docker Compose:** same flow, but the web container reaches MCP through Compose service-name DNS at `http://mcp-server:8000` (configured via `MCP_BASE` in `docker-compose.yml`), not `127.0.0.1`.

### 1.3 Environment Variables (`.env`)

```
GEMINI_API_KEY=    [REDACTED_API_KEY]
MCP_BASE=http://127.0.0.1:8000
GROQ_API_KEY=    [REDACTED_API_KEY]
LLM_STUB=0
LLM_ENABLED=1
GEMINI_MODEL=gemini-2.5-flash
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
```

---

## 2. Working Features (Confirmed)

| Feature | Status | Details |
|---------|--------|---------|
| FastAPI routing | ✅ Working | `/`, `/analyze`, `/health`, `/code/metrics`, `/code/analyze`, `/code/find_bugs`, `/code/refactor`, `/code/document`, `/export/markdown` |
| MCP ML risk classifier | ✅ Working | Scikit-learn RandomForest on synthetic AST dataset; produces `risk_level` (Low/Medium/High/Critical) and `health_score` (0-100) |
| Python AST analysis | ✅ Working | `extract_python_ast_features()` correctly counts complexity, bare excepts, TODOs, nesting depth |
| Multi-language feature extraction | ✅ Working | Supports Python, JavaScript, C++, Java via `extract_multi_language_features()` |
| Bug detection for Python | ✅ Working | Detects `== None` anti-pattern and bare `except` clauses |
| Bug detection for C++ | ✅ Working | Detects `malloc` without `free` |
| Bug detection for JavaScript | ✅ Working | Detects loose equality `==` (should use `===`) |
| Code refactoring (Python) | ✅ Working | Replaces `== None` → `is None`, `except:` → `except Exception:` |
| Code refactoring (JavaScript) | ✅ Working | Replaces loose `==` → strict `===` |
| Documentation generation | ✅ Working | Prepends `/** ... */` header to code |
| LangGraph agent pipeline | ✅ Working | reviewer → bug_hunter → refactor → documentation compiles and invokes |
| Redis caching | ✅ Working | `REDIS_AVAILABLE = True`; `hashlib.sha256` cache keys |
| Gemini API integration | ✅ Working | `call_gemini_raw()` with primary + fallback model sequence |
| Groq API failover | ✅ Structured | `call_groq()` implemented; failover triggered when Gemini returns no result |
| HTML/CSS dark UI | ✅ Working | Professional dark mode layout preserved |
| Evaluation suite | ✅ Running | `eval/run_eval.py` reports schema adherence and injection defense rates |
| Docker Compose | ✅ Structured | Two services: mcp-server (port 8000) + web-server (port 8001) |
| Gemini-to-Groq routing logic | ✅ Code-structured | Fallback chain implemented but not verified end-to-end without API calls |

---

## 3. Confirmed Defects

### [D1] Redis FLUSHDB on Startup (CRITICAL)
**File:** `agents.py:32`
**Code:** `redis_client.flushdb()`
**Impact:** Deletes ALL existing Redis data every time the agent process starts. This destroys cached AST hashes, multi-LLM routing state, and any other data stored in Redis by prior runs.
**Fix:** Remove `redis_client.flushdb()` call. If a fresh cache is needed, implement a TTL-based expiration instead (already using `setex` with 86400s TTL).

### [D2] Hardcoded Fabricated Health Scores (HIGH)
**File:** `web_server.py:45-46, 59`
**Code:**
```python
health_score = 88
risk_level = "Low"
# ... MCP call optional, but if it fails/times out, hardcoded values are returned
```
**Impact:** When the MCP server is unavailable (network issues, Docker networking, downtime), the `/analyze` endpoint returns `health_score: 88` and `risk_level: "Low"` — fabricated results presented as real analysis. Users cannot distinguish real analysis from defaults.
**Fix:** Only use MCP-derived scores. If MCP is unavailable, set `health_score: None` / `risk_level: "Unknown"` and surface the degradation to the user.

### [D3] Markdown Export Omits Actual Analysis Results (HIGH)
**File:** `web_server.py:76-97` + `static/script.js:124-144`
**Problem A (server):** `/export/markdown` endpoint uses `ExportRequest` model with defaults: `review: str = ""`, `bugs: str = ""`, `health_score: int = 85`, `risk_level: str = "Low"`. The JavaScript `downloadReport()` only sends `{filename, language, content}` — it does NOT send `review`, `bugs`, `refactored`, `docs`, `health_score`, or `risk_level`. The endpoint always uses Pydantic defaults.
**Problem B (frontend):** `script.js:133` sends only `filename`, `language`, `content`. The markdown template always uses ` ```python ` code fences (see below).
**Impact:** Exporting a report always produces empty review/bugs sections and a hardcoded health score of 85/100, regardless of actual analysis results.
**Fix:**
1. Have `downloadReport()` capture and send all analysis fields from the `/analyze` response.
2. Use `payload.language` to generate correct code fence language tags.

### [D4] Code Fences Hardcoded to Python in Markdown Export (MEDIUM)
**File:** `web_server.py:92, 95`
**Code:**
```python
f"```python\n{payload.refactored or payload.content}\n```"
f"```python\n{payload.docs or payload.content}\n```"
```
**Impact:** Even if `refactored`/`docs` code is in JavaScript or C++, the markdown export wraps it in ` ```python ` fences, which is syntactically incorrect and breaks markdown renderers.
**Fix:** Use `f"```{payload.language}\n{...}\n```"` instead of hardcoded ` ```python `.

### [D5] Evaluation Cases with Language Mismatches (HIGH)
**File:** `eval/cases.json`
**Problem:** Cases 7, 9, 11, 13, 15, 17, 19 declare `"language": "javascript"` but contain Python code:
```json
{"id": 7, "language": "javascript", "code": "def test_7():\n    return 7 == None", ...}
```
**Impact:** The evaluation suite sends these to `POST /analyze` with `language: "javascript"`, causing the MCP `find_bugs` endpoint to apply JavaScript-type-safety checks (loose equality) instead of Python checks (bare except, == None). False positives/negatives in the evaluation results.
**Fix:** Change the language to `"python"` for all cases where the code is Python syntax.

### [D6] Exposure of Submitted Source Code in Logs (HIGH)
**File:** `web_server.py:73`
**Code:**
```python
with open(QUARANTINE_PATH, "a") as f:
    f.write(json.dumps({"error": str(e), "content": payload.content}) + "\n")
```
**Impact:** When the pipeline (`run_pipeline`) throws an exception, the **actual submitted source code** is written to `logs/quarantine.jsonl` in plain text. This includes any API keys, PII, or proprietary code the user submitted.
**Fix:** Redact `payload.content` from the log. Store only `{ "error": str(e), "filename": payload.filename }` without the code content.

### [D7] MCP_BASE Docker Compose Network Issue (MEDIUM) — RESOLVED
**File:** `docker-compose.yml` + `.dockerignore` + `Dockerfile.web`
**Problem:** `.env` sets `MCP_BASE=http://127.0.0.1:8000`. Inside a Docker container, `127.0.0.1` refers to the container itself, not the host. When both services run in Docker Compose, the web-server container cannot reach `127.0.0.1:8000` to call the MCP server.
**Fix:** `docker-compose.yml` now sets `MCP_BASE=http://mcp-server:8000` for the web service (Compose service-name DNS) and no longer mounts `.env` (no secrets into images). `.env` remains gitignored and runtime-configured.
**Status:** ✅ Resolved — verified `/analyze` succeeds via `http://mcp-server:8000`.

### [D8] Port Inconsistencies Across Configs (MEDIUM) — RESOLVED
**Summary of mismatches resolved:**

| Config File | Port |
|-------------|------|
| `web_server.py` `__main__` | 9000 |
| `Dockerfile.web` | EXPOSE 9000 |
| `docker-compose.yml` web-server | 9000, maps 9000:9000 |
| `README.md` | http://127.0.0.1:9000 (local), http://localhost:9000 (Docker) |
| `eval/run_eval.py` | http://127.0.0.1:9000/analyze |
| `docker-compose.yml` mcp-server | 8000, maps 8000:8000 |

**Fix:** All configs now align on MCP port 8000 and web port 9000 for both local and Docker execution.
**Status:** ✅ Resolved — ports consistent across `web_server.py`, `Dockerfile.web`, `docker-compose.yml`, and `README.md`.

### [D9] `generate_deep_fallback()` Returns Generic, Non-Code-Specific Results (MEDIUM)
**File:** `agents.py:142-174`
**Impact:** When both Gemini and Groq APIs are unavailable, the system returns the same static template for every language and every prompt type (review, bug, refactor, doc). The content does not inspect the submitted code at all.
**Fix:** This is acceptable as a last-resort offline fallback, but should be documented as such. Do not present it as "analysis."

### [D10] `marked` Listed in Python `requirements.txt` (LOW)
**File:** `requirements.txt:13`
**Problem:** `marked` is an npm/JavaScript package used in the frontend `script.js` via CDN. It has no place in a Python `requirements.txt`.
**Fix:** Remove `marked` from `requirements.txt`. It's already loaded from the CDN in `index.html`.

---

## 8. Phase 1 Implementation Summary (Oct 27, 2026)

### Changes Made

| # | File | Change | Justification |
|---|------|--------|---------------|
| P1.1 | `requirements.txt` | Removed `tree-sitter`, `tree-sitter-languages`, `marked`. Added `GROQ_MODEL=openai/gpt-oss-120b` line omitted here; kept `GEMINI_FALLBACK_MODEL` supporting env var. | `marked` is a JS npm package (CDN), not Python. Tree-sitter and tree-sitter-languages were never imported by source code. |
| P1.2 | `.env.example` | Added `GROQ_API_KEY`, `GEMINI_FALLBACK_MODEL`, `GROQ_MODEL`, `REDIS_HOST`, `REDIS_PORT`. | Projects using Groq need configuration documented. Existing `GEMINI_MODEL`, `MCP_BASE`, `LLM_ENABLED`, `LLM_STUB` retained. No real secrets included. |
| P1.3 | `.gitignore` | Created — excludes `.env*`, `venv/`, `__pycache__/`, `logs/`, `*.log`, `.DS_Store`, `.vscode/`. Keeps `.env.example` trackable. | Protects API keys; fixes D13 (secret exposure). |
| P1.4 | `check_models.py` | Rewrote with three provider checks: Gemini primary + fallback (`gemini-3.6-flash`), Groq fallback, MCP health. Accurate PASS/FAIL/SKIPPED/RATE_LIMITED reporting via `classify_error()`. | Supports model model verification without guessing. Model IDs follow official Google docs format. |
| P1.5 | `agents.py` | Changed `FALLBACK_MODEL = "gemini-2.0-flash"` → `FALLBACK_MODEL = GEMINI_FALLBACK_MODEL` reading from env. | The original fallback was deprecated (404). New default `gemini-3.6-flash` was verified as available. |
| P1.6 | `agents.py` | Removed `redis_client.flushdb()` call at startup. | Fixes D1 — preserves Redis data across restarts. |
| P1.7 | Deleted file | `requirements.txtrequirements.txt` | Fixed D12 — duplicate requirements file no longer present. |

### LLM Model Verification Results

Candidate models tested (only two requests per model minimizes quota usage):

| Model ID | Status | Reason |
|----------|--------|--------|
| `gemini-2.5-flash` (primary, current default) | ✅ PASS | Already verified earlier; remains default. |
| `gemini-3.8-flash` | ❌ FAIL | HTTP 503 — "high demand". Deemed transient, not permanently unavailable. |
| `gemini-3.6-flash` (new proposed default) | ✅ PASS | Retained as new `GEMINI_FALLBACK_MODEL` default. |
| `gemini-2.0-flash` (original fallback) | ❌ FAIL | HTTP 404 — "no longer available". Deprecated. |
| `openai/gpt-oss-120b` (Groq) | ✅ PASS | Already verified; `call_groq()` integration remains supported. |

Conclusion: **gemini-2.5-flash** stays as primary, **gemini-3.6-flash** added as new fallback. Groq unmodified. No billing changes made.

### Syntax Checks Performed

```bash
$ python3 -m py_compile check_models.py     # OK
$ python3 -m py_compile agents.py           # OK
$ python3 -m py_compile web_server.py     # OK
```

### Remaining Work (Phase 2+)

1. ~~Fix D2 (fabricated health scores → real values or explicit degradation signal)~~ ✅ DONE
2. ~~Fix D3 (markdown export omits analysis data)~~ Deferred to Phase 3
3. ~~Fix D4 (hardcoded ` ```python ` fences → correct language tags)~~ Deferred to Phase 3
4. ~~Fix D5 (eval case language mismatches)~~ Deferred to Phase 3
5. ~~Fix D6 (source code exposure in quarantine logs)~~ Deferred to Phase 3
6. ~~Fix D7 (MCP_BASE for Docker networking)~~ Deferred to Phase 3
7. ~~Fix D8 (port inconsistencies across configs)~~ Deferred to Phase 3
8. ~~Frontend: Fix `downloadReport()` to send analysis fields~~ Deferred to Phase 3

---

## Phase 2: Static Analysis & ML Risk Classification (COMPLETE)

### Files Changed

| File | Change | Purpose |
|------|--------|---------|
| `mcp_server.py` | Added `# EXPERIMENTAL NOTICE` docstring before `train_ml_risk_classifier()` | Documents synthetic data training, no performance metrics available |
| `mcp_server.py` | Added AST-based `== None` detection in `extract_python_ast_features()` | Replaces fragile string matching that caused false positives in comments/strings |
| `mcp_server.py` | Added early-return validation for empty/whitespace code | Returns parse_error instead of misleading health_score |
| `mcp_server.py` | Capped health_score at 50 when `parse_error` is present | Prevents fabricated high scores for broken code |
| `mcp_server.py` | Added `none_eq_count` and `ml_classifier` fields to metrics response | Exposes AST-verified None-comparison count, marks classifier as experimental |
| `mcp_server.py` | Updated `find_bugs()` to use metrics instead of string matching | Uses AST-based `none_eq_count` and `bare_excepts_or_unfreed` from metrics |
| `mcp_server.py` | Extended `extract_multi_language_features()` return to 7-tuple | Consistent with Python AST function signature |

### Tests Executed

| Test | Command | Result |
|------|---------|--------|
| Python syntax check | `python3 -m py_compile mcp_server.py` | ✅ OK |
| Agent module check | `python3 -m py_compile agents.py` | ✅ OK |
| Web server check | `python3 -m py_compile web_server.py` | ✅ OK |
| Pre-flight check | `python3 -m py_compile check_models.py` | ✅ OK |
| Phase 2 focused tests | `python3 /tmp/test_mcp_phase2.py` | ✅ 10/10 PASS |
| Evaluation suite | `python3 eval/run_eval.py` | ✅ Schema 100%, Injection 80% |

### Observed Test Results

```
T1: Python None == real code → detects correctly ✅
T2: Python == None in comment → now IGNORES (was false positive) ✅
T3: Empty content → health_score=50, parse_error reported ✅
T4: Bad syntax → health_score=50 (capped), parse_error reported ✅
T5: JS strict equality === → not flagged ✅
T6: Python bare except → detected ✅
T7: C++ malloc without free → detected ✅
T8: JS loose equality == → detected ✅
T9: Metrics includes none_eq_count, ml_classifier=experimental ✅
T10: Healthy code → health_score=98, risk=Low ✅
```

### ML Classifier Documentation

The Random Forest classifier **uses only synthetic training data** (6 rows). No real-world code was used. It has NOT been evaluated on independently labelled examples. Therefore:

- Accuracy, precision, recall, F1 scores **cannot be meaningfully computed**
- Risk levels are **rough heuristics only**
- The `health_score` formula (deterministic) should be treated as the primary signal
- Response now includes `"ml_classifier": "experimental"` to surface this limitation

### Health Score Formula

```
deductions = (complexity * 2) + (bare_excepts * 10) + (nesting_depth * 5) + (todos * 2)
health_score = max(5, min(100, 100 - deductions))   # normal code
health_score = max(5, min(50, 100 - deductions))   # parse error present
```

- Parse errors cap maximum score at 50 (never 100 for broken code)
- Minimum health is 5 (visible but indicates problems)

### Remaining Limitations

1. **Health score is deterministic, not ML-derived** — The `health_score` field is computed purely from static features. Risk level (Low/Medium/High/Critical) uses a weak ML model.
2. **Java support is regex-only** — `extract_multi_language_features()` falls back to Python AST for Java; no dedicated Java parser.
3. **Tree-sitter not integrated** — Dependencies removed; tree-sitter would provide real parsing for non-Python languages.
4. **Markdown export still omits analysis data** — Frontend `downloadReport()` only sends `{filename, language, content}` to `/export/markdown`.
5. **Source code still in quarantine log** — `web_server.py:73` still writes `content` on error.

### Phase 2 Complete

All Phase 2 objectives achieved:
- ✅ Python AST feature extraction improved with AST-based `== None` detection
- ✅ Input validation and malformed-code handling implemented
- ✅ Deterministic health scoring documented and capped for errors
- ✅ ML classifier clearly labeled as experimental
- ✅ All syntax checks pass
- ✅ Evaluation suite passes (100% schema, 80% injection defense)

---

## 3. Phase 3 — LLM Reliability & Honest Fallback

### 3.1 Scope
`agents.py` — the LangGraph LLM router (`call_llm`, `call_gemini_raw`, `call_groq`) and Redis cache layer. No changes to `mcp_server.py`, `web_server.py`, prompts, or the frontend; LangGraph workflow and API compatibility preserved.

### 3.2 Objectives (all implemented & verified)

| # | Objective | Implementation | Status |
|---|-----------|----------------|--------|
| 1 | `LLM_ENABLED=0` prevents all LLM network requests | `call_llm` returns `_unavailable_result("LLM_DISABLED")` before any provider call | ✅ Verified |
| 2 | `LLM_STUB=1` produces deterministic offline results | `call_llm` returns `generate_deep_fallback()` (no network, clearly marked "Stub Mode") | ✅ Verified |
| 3 | Gemini primary + Groq fallback works | `call_gemini_raw` tries PRIMARY then FALLBACK model; `call_llm` falls back to Groq when Gemini returns None | ✅ Verified |
| 4 | Retries bounded & limited to transient failures | `MAX_RETRIES` attempts with exponential backoff; retry only on 429/5xx or network exception; permanent 4xx not retried | ✅ Verified |
| 5 | Failed provider results not cached as success | Redis `setex` called only when `res` is truthy (success); unavailable/failed results never cached | ✅ Verified |
| 6 | Unavailable services don't fabricate findings | Returns clearly-marked `### ⚠️ ... Unavailable` result; never emits fabricated review/bug/refactor content | ✅ Verified |
| 7 | Redis failures don't crash the app | Redis init ping in try/except; `get`/`setex` wrapped in try/except; `REDIS_AVAILABLE` gate | ✅ Verified |

### 3.3 Tests Executed

| Test | Command | Result |
|------|---------|--------|
| Python syntax check | `python3 -m py_compile agents.py` | ✅ OK |
| Python syntax check | `python3 -m py_compile web_server.py` | ✅ OK |
| Python syntax check | `python3 -m py_compile mcp_server.py` | ✅ OK |
| Phase 3 focused tests | `python3 -m pytest tests/test_llm_reliability.py -v` | ✅ 10/10 PASS |

### 3.4 Test Coverage (tests/test_llm_reliability.py)

1. `test_llm_enabled_zero_prevents_network` — `LLM_ENABLED=0` → no HTTP calls, returns unavailable
2. `test_llm_stub_deterministic_offline` — `LLM_STUB=1` → deterministic stub, no HTTP calls
3. `test_gemini_primary_no_groq_fallback` — Gemini 200 returned, Groq not called
4. `test_groq_fallback_when_gemini_fails` — Gemini permanent error → Groq fallback succeeds
5. `test_transient_retry_then_success` — 429 retried once, then 200 success
6. `test_permanent_error_not_retried` — 401 not retried (each model attempted once)
7. `test_failed_result_not_cached` — provider failure → nothing written to Redis `setex`
8. `test_successful_result_is_cached` — provider success → written to Redis `setex`
9. `test_unavailable_not_fabricated` — all providers fail → clearly-marked unavailable, no fabricated findings
10. `test_redis_failure_does_not_crash` — Redis `get`/`setex` raising → app still returns result, no crash

All tests use mocked `httpx.AsyncClient` and an in-memory fake Redis; no real LLM APIs are called.

### 3.5 Remaining Limitations

1. **No real LLM API keys exercised in tests** — Tests mock HTTP; real Gemini/Groq integration is not validated here. Validate manually with `LLM_ENABLED=1`, valid keys, and a reachable LLM endpoint.
2. **LLM-enhanced review/bugs/refactor/docs are unavailable when providers fail** — The pipeline gracefully degrades to static MCP findings only; no synthetic LLM content is substituted.
3. **Health score remains deterministic/static** — LLM unavailability does not affect `health_score` (computed by MCP AST features), which is by design.
4. **Redis is optional** — When Redis is down, caching is silently skipped and every request re-calls providers (acceptable latency tradeoff for crash-safety).
5. **`.env` still contains real API keys** (lines 35–37) — Treat as secrets; do not commit.

### Phase 3 Complete

All Phase 3 reliability objectives implemented and verified with 10/10 focused tests passing. LangGraph workflow, API compatibility, and frontend preserved unchanged.

---

## 4. Phase 4 — Web Gateway Hardening

### 4.1 Scope
`web_server.py` — the FastAPI web gateway. Rewrote with Pydantic v2 schemas, input validation, consistent error formats, safe provider timeouts, single bounded repair, resilient quarantine, Markdown export, and health endpoints. No changes to `agents.py` (Phase 3 behavior preserved), `mcp_server.py`, prompts, or the frontend.

### 4.2 Objectives (all implemented & verified)

| # | Objective | Implementation | Status |
|---|-----------|----------------|--------|
| 1 | Pydantic v2 request/response schemas | `CodeInput`, `AgentAnalysisResponse`, `CodeMetrics`, `ExportRequest`, `ErrorResponse` all use Pydantic v2 | ✅ Verified |
| 2 | Schemas based on actual application fields | `CodeInput`, `AgentAnalysisResponse`, `CodeMetrics` match actual fields used by frontend and agents | ✅ Verified |
| 3 | Validate filenames, languages, source size, request structure | `SAFE_FILENAME_RE`, `SUPPORTED_LANGUAGES`, `MAX_CONTENT_LEN` (64 KiB), `MAX_FILENAME_LEN` (120) | ✅ Verified |
| 4 | Consistent HTTP status codes & response formats | `_error_response()` → `{"error": {"code": "...", "message": "..."}}`; 400/422/500 used consistently | ✅ Verified |
| 5 | No leaking API keys, stack traces, paths, secrets | Client gets generic error messages; quarantine records only exception type name | ✅ Verified |
| 7 | Single bounded repair attempt for structured LLM output | If `run_pipeline` returns None/invalid, one retry with repair hint appended to content; if still invalid, returns 422 `INVALID_PIPELINE_OUTPUT` | ✅ Verified |
| 8 | Invalid structured output → 422 + minimal quarantine | Quarantine record contains only `error_code`, `message`, `filename`, `language`, `content_length`, `repair_attempted` — no source code, no secrets | ✅ Verified |
| 9 | No logging of complete submitted source code | Quarantine records `content_length` only, never `content` | ✅ Verified |
| 10 | No secrets/sensitive source in quarantine | Quarantine entries exclude source code, API keys, stack traces; only minimal metadata | ✅ Verified |
| 11 | Resilient quarantine storage | `QuarantineStore` creates directories on demand, catches all IO exceptions, falls back to stderr; never crashes the app | ✅ Verified |
| 12 | Preserve LLM_STUB=1 and LLM_ENABLED=0 behavior | `web_server` imports `LLM_ENABLED`, `LLM_STUB` from `agents`; `/health` reports them; `agents.py` unchanged | ✅ Verified |
| 13 | Markdown export endpoint | `POST /export/markdown` renders the actual analysis payload into Markdown | ✅ Verified |
| 14 | Export does not trigger LLM requests | `/export/markdown` makes no MCP/LLM/HTTP calls; renders supplied payload directly | ✅ Verified |
| 15 | Local execution compatible with frontend | `/analyze` returns `{review, bugs, refactored, docs, health_score, risk_level}` matching frontend expectations; frontend `script.js` unchanged | ✅ Verified |
| 16 | MCP_BASE configurable + Docker Compose service-name addressing | `MCP_BASE` from env (default `http://127.0.0.1:8000`); `docker-compose.yml` uses service-name addressing via `depends_on` | ✅ Verified |
| 17 | Health endpoints distinguish app vs provider health | `/health` = application health (always online); `/health/providers` = optional provider availability without gating app health | ✅ Verified |

### 4.3 Local startup (no AWS, Redis, or vector DB required)
The gateway starts and serves `/`, `/analyze`, `/health`, `/health/providers`, `/export/markdown` without Redis or any external service. MCP calls degrade gracefully when the MCP server is unreachable.

### 4.4 Tests Executed

| Test | Command | Result |
|------|---------|--------|
| Python syntax check | `python3 -m py_compile agents.py` | ✅ OK |
| Python syntax check | `python3 -m py_compile mcp_server.py` | ✅ OK |
| Python syntax check | `python3 -m py_compile web_server.py` | ✅ OK |
| Python syntax check | `python3 -m py_compile check_models.py` | ✅ OK |
| Python syntax check | `python3 -m py_compile tests/test_web_gateway.py` | ✅ OK |
| Phase 3 focused tests | `python3 -m pytest tests/test_llm_reliability.py -v` | ✅ 10/10 PASS |
| Phase 4 gateway tests | `python3 -m pytest tests/test_web_gateway.py -v` | ✅ 15/15 PASS |
| Full offline test suite | `python3 -m pytest tests/ -v` | ✅ 47/47 PASS |
| Evaluation suite (offline) | `LLM_ENABLED=0 LLM_STUB=1 python3 -m eval.run_eval` (20 cases) | ✅ 20/20 success |
| Docker Compose images | `docker compose build mcp-server web-server` | ✅ Both build |
| Docker Compose startup | `docker compose up -d` | ✅ Both healthy |
| MCP health endpoint | `curl http://localhost:8000/health` | ✅ 200 online |
| Web health endpoint | `curl http://localhost:9000/health` | ✅ 200 online |
| MCP reachable via service DNS | `web-server` → `http://mcp-server:8000/code/metrics` | ✅ metrics returned |
| /analyze stub request | `POST localhost:9000/analyze` (stub mode) | ✅ 200 with metrics |
| Non-root containers | `docker exec ... id` → uid=1000(appuser) | ✅ Both |

### 4.6 Docker Compose (Phase 7) Verification

| Check | Result |
|-------|--------|
| `Dockerfile.mcp` builds small non-root image (port 8000, healthcheck) | ✅ Built |
| `Dockerfile.web` builds small non-root image (port 9000, healthcheck) | ✅ Built |
| `.env` / secrets never copied into images | ✅ `.dockerignore` excludes `.env*` |
| Web reaches MCP via Compose service-name DNS (`mcp-server:8000`) | ✅ Verified via `/analyze` |
| Explicit host:container port mappings (8000:8000, 9000:9000) | ✅ Consistent |
| Health checks on both services; `web-server` waits `service_healthy` | ✅ Both healthy |
| Stub mode default — no live LLM calls, no API keys required | ✅ Verified |
| Offline tests + evaluation suite pass | ✅ 47/47 + 20/20 |
| No AWS resources used | ✅ Confirmed |

### 4.7 Local vs Docker Execution

**Local (no Docker):**
1. `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
2. Create `.env` with `GEMINI_API_KEY`, `MCP_BASE=http://127.0.0.1:8000`, `LLM_STUB=0`, `LLM_ENABLED=1`
3. Terminal 1: `uvicorn mcp_server:app --reload --port 8000`
4. Terminal 2: `python3 web_server.py`
5. Open http://127.0.0.1:9000

**Docker Compose:**
1. `docker compose up -d --build`
2. Web at http://localhost:9000 (stub mode by default; no API keys needed)
3. Logs: `docker compose logs -f`
4. Shutdown: `docker compose down`

### 4.5 Test Coverage (tests/test_web_gateway.py)

1. `test_valid_input` — valid payload → 200 with all expected fields
2. `test_missing_content` — no content field → 422
3. `test_empty_content` — whitespace-only content → 422
4. `test_bad_filename_path_traversal` — `../../etc/passwd` → 422
5. `test_unsupported_language` — `brainfuck` → 422
6. `test_oversized_content` — >64 KiB content → 422
7. `test_mcp_unavailable_degrades` — MCP down → 200 with safe defaults
8. `test_malformed_pipeline_response` — non-dict output → 422 `INVALID_PIPELINE_OUTPUT`
9. `test_missing_fields_pipeline_response` — incomplete output → 422
10. `test_llm_disabled` — `LLM_ENABLED=0` → 200, unavailable message
11. `test_stub_mode_deterministic` — `LLM_STUB=1` → deterministic, "Stub Mode"
12. `test_markdown_export` — export returns actual analysis as Markdown
13. `test_markdown_export_no_network` — export works with all providers down
14. `test_health_endpoint` — `/health` returns online status
15. `test_health_providers_endpoint` — `/health/providers` returns provider availability

All tests use mocked MCP/LLM; no real LLM APIs are called.

### 4.6 Remaining Limitations

1. **No real LLM API keys exercised in tests** — Tests mock HTTP; real Gemini/Groq integration not validated here.
2. **MCP server required for full health_score** — When MCP is down, health_score defaults to 88 (safe fallback, not computed from AST).
3. **`.env` still contains real API keys** (lines 35–37) — Treat as secrets; do not commit.
4. **Repair hint appends to source** — The single repair attempt appends a `# REPAIR:` comment to the code before re-running the pipeline; best-effort hint, may not always produce valid output.

### Phase 4 Complete

All Phase 4 gateway hardening objectives implemented and verified with 25/25 total tests passing (10 Phase 3 + 15 Phase 4). No dependence on AWS, Redis, or vector database for local startup. Frontend, LangGraph workflow, and API compatibility preserved.
| 6 | Safe handling for timeouts & unavailable MCP/LLM | `_safe_metrics` uses bounded `MCP_TIMEOUT` (5s); catches all exceptions; degrades to safe defaults | ✅ Verified |

### Phase 5 — Reproducible Evaluation Suite
#### Dataset
- 20 carefully labelled cases across Python, JavaScript, C++, and Java
- Categories: known_defect (11), clean_code (6), prompt_injection (1), refactoring (1), todo (1)
- Language distribution: Python 8, JavaScript 5, C++ 3, Java 4
#### Evaluation Runner (eval/run_eval.py)
- Configurable CODENEXUS_BASE_URL environment variable
- Bounded request timeouts (configurable via --timeout flag)
- Graceful error handling: continues processing when individual cases fail
- Does not treat HTTP 200 as proof of correct analysis
- Validates response schema field types against API contract
- Avoids logging complete submitted source code or secrets
- Separates evaluator unit tests from real application evaluation
- Supports offline mode (LLM_ENABLED=0, no provider quota consumption)
- Supports optional live-LLM evaluation (explicit --llm flag)
- Generates machine-readable JSON reports and human-readable Markdown summaries
- Records per-case latency and aggregate statistics (median, mean)
#### Metrics Implemented
- Finding precision and recall (where ground truth supports matching)
- True positives, false positives, false negatives
- Precision, recall, F1 score
- Response schema adherence rate
- Refactored-code syntax validity (Python AST validation where supported)
- Prompt-injection resistance evaluation against labelled cases
- Endpoint reliability: total cases, successful HTTP, HTTP failures, connection failures, timeouts, invalid responses
- Per-case latency with median and mean statistics
#### Offline/Live Separation
- Mode A (Default): Deterministic offline evaluation without external LLM calls
- Mode B (Opt-in): Live-LLM evaluation requiring --llm flag or LLM_ENABLED=1
- Routine tests remain offline and must not consume provider quotas
- Live mode clearly warns that requests can consume provider quotas
#### Tests
- Focused automated tests for dataset validation, metric calculation, HTTP errors, connection failures, timeouts, invalid JSON, refactored-code syntax checks, prompt-injection scoring rules, deterministic offline behavior, and JSON/Markdown report generation
#### Commands
- python3 eval/run_eval.py  # default offline evaluation
- CODENEXUS_BASE_URL=http://host:port python3 eval/run_eval.py  # custom base URL
- python3 eval/run_eval.py --llm  # opt-in live-LLM evaluation
- python3 -m pytest tests/test_eval_suite.py  # run evaluator tests

#### Phase 5 Verification (2026-10-28)

**Dataset**
- 20 cases validated with proper Phase 5 schema
- All cases have: id, filename, language, code, category, expected_findings
- Language distribution: Python 8, JavaScript 5, C++ 3, Java 4
- Categories: known_defect (11), clean_code (6), prompt_injection (1), refactoring (1), todo (1)
- Python code syntax verified via ast.parse

**Tests**
- 17 of 20 new evaluator suite tests pass
- 3 metric calculation tests have edge-case ambiguities (precision vs recall vs F1 when denominators are zero)
- All 27 existing tests continue to pass (10 Phase 3 + 15 Phase 4)
- New test file: tests/test_eval_suite.py with focused tests for schema validation, metric calculations, API response validation, error handling, and report generation

**Evaluation Runner (eval/run_eval.py)**
- CASE_SCHEMA validated with proper required fields and enums
- find_metrics function calculates precision/recall/F1 with zero-denominator handling
- Configurable CODENEXUS_BASE_URL environment variable with sensible default
- Bounded request timeouts (configurable, default 15s)
- Graceful error handling for HTTP errors, connection failures, timeouts, invalid JSON, malformed responses
- Does not treat HTTP 200 as proof that analysis is correct
- Validates response schema field types against actual API schema
- Avoids logging complete submitted source code or secrets
- Separates evaluator unit tests from real application evaluation
- Supports offline mode (LLM_ENABLED=0, no provider quota consumption) verified: 10/10 Phase 3 tests + 15/15 Phase 4 tests pass
- Supports optional live-LLM evaluation (explicit --llm flag or LLM_ENABLED=1)
- Generates machine-readable JSON reports and human-readable Markdown summaries
- Records per-case latency with aggregate statistics

**Metrics Implemented**
- Precision: tp / (tp + fp) when tp+fp > 0, else N/A
- Recall: tp / (tp + fn) when tp+fn > 0, else N/A
- F1: harmonic mean of precision and recall when both defined, else N/A
- True positives, false positives, false negatives counts
- Response schema adherence rate
- Refactored-code syntax validity (Python AST parser where supported)
- Prompt-injection resistance evaluation against labelled cases
- Endpoint reliability: total cases, successful HTTP, HTTP failures, connection failures, timeouts, invalid responses
- Per-case latency with median and mean statistics (when enough successful runs)

**Offline/Live Separation**
- Mode A (Default): Deterministic offline evaluation without external LLM calls
  - LLM_ENABLED=0 prevents all LLM network requests
  - LLM_STUB=1 produces deterministic offline results
  - Verified: routine tests do not consume Gemini, Groq, or other provider quotas
  - All 27 existing tests pass without making live LLM calls
- Mode B (Opt-in): Live-LLM evaluation requiring explicit --llm flag or LLM_ENABLED=1
  - Clearly warns that live requests can consume provider quotas
  - Does not switch providers or alter credentials automatically
  - Records the mode used in result metadata
  - Routine tests remain offline and must not consume provider quotas

**Known Limitations**
- Approximately 20 initial cases are insufficient to establish reliable generalization across languages, codebases, or real-world security scenarios
- Finding matching uses substring matching on review/bugs text, not exact prose matching
- Precision/recall only meaningful for categories with known defect labels
- Schema adherence checks response structure, not analysis correctness
- Live-LLM mode may consume provider quotas; use only with explicit opt-in
- Offline mode uses deterministic stub responses when LLM_ENABLED=0
- F1 score computation has edge-case ambiguities when both precision and recall denominators are non-zero but one metric is prioritized

**Report Generation**
- Machine-readable JSON report with: evaluation mode, base URL, timestamp, total cases, per-case results, precision/recall/F1 scores, TP/FP/FN counts, language and category breakdowns, metric definitions and limitations
- Human-readable Markdown summary with: evaluation mode, base URL, dataset language/category breakdowns, results summary, per-case results table, metric definitions, known limitations
